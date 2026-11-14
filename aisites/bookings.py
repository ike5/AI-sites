"""Bookings: a visiting agent holds limited capacity, confirms the hold, or releases it.

A structure opts in with the `booking` setting, which names one of its `capacity` fields. `reserve` takes
places off that field at once and gives a hold that expires by itself; `book` confirms it with the
details the owner asked for; `release` gives the places back. Bookings live in their own private table:
they never enter the public history, the manifest or record reads. The only public trace is the capacity
value going down and up, which is a live value and so only moves the change feed.

If the owner removes a record, its open bookings are released (reason `record_removed`): nothing is left
to hold. If the owner sets the capacity field, that is the new number of free places; places released
later are added to it.
"""

from __future__ import annotations

import json
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from starlette.requests import Request

from . import hooks, ops, schema, timeview
from .actions import PaymentHandoff
from .errors import SiteError, not_found
from .schema import StructureDef

SETTING = "booking"
DEFAULT_HOLD, MAX_HOLD, DEFAULT_ACTIVE = 15, 30, 3
ACTIVE = ("held", "booked")
STATUSES = ("held", "booked", "released", "expired")
_KEYS = {"field", "open", "hold_minutes", "max_active_per_caller", "details", "payment"}
_PAY_KEYS = {"url", "amount_field", "currency_field"}


# ---------------------------------------------------------------- the setting

def _whole(value: Any, name: str, low: int, high: Optional[int] = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < low or (high is not None and value > high):
        span = f"from {low} to {high}" if high is not None else f"{low} or more"
        raise SiteError("invalid", f"booking.{name} must be a whole number {span}; got {value!r}.")
    return value


def check_setting(site: Any, sdef: StructureDef, value: Any) -> dict:
    """Check and normalise a `booking` setting so every later call can trust its shape."""
    if sdef.type != "collection":
        raise SiteError("invalid", f"'{sdef.name}' is a {sdef.type}; only a collection can take bookings.",
                        "Put bookable things in a collection with a capacity field, e.g. kind Slot.")
    if not isinstance(value, dict):
        raise SiteError("invalid", "The booking setting must be an object.",
                        'Example: {"field": "remaining", "hold_minutes": 15}')
    unknown = sorted(set(value) - _KEYS)
    if unknown:
        raise SiteError("invalid", f"The booking setting does not take: {', '.join(unknown)}.",
                        "It takes: " + ", ".join(sorted(_KEYS)) + ".")
    capacity = sdef.names("capacity")
    field = value.get("field")
    if field is None:
        if len(capacity) != 1:
            raise SiteError("invalid", f"booking.field is needed: '{sdef.name}' has "
                            f"{'no' if not capacity else len(capacity)} capacity fields.",
                            "Name one with policy capacity" + (f": {', '.join(capacity)}." if capacity else
                                                               ", after adding it, e.g. 'seats:integer:capacity'."))
        field = capacity[0]
    if field not in capacity:
        raise SiteError("invalid", f"booking.field '{field}' is not a capacity field of '{sdef.name}'.",
                        ("Capacity fields: " + ", ".join(capacity) + ".") if capacity else
                        "Add one with alter, e.g. 'seats:integer:capacity'.")
    for row in site.store.q("SELECT set_json FROM schedules WHERE collection=?", (sdef.name,)):
        if field in json.loads(row["set_json"]):
            raise SiteError("invalid", f"'{field}' has a scheduled change, so bookings cannot draw on it.",
                            "Cancel that scheduled change with `unschedule` first.", status=409)
    out: dict = {
        "field": field,
        "open": value.get("open", False),
        "hold_minutes": _whole(value.get("hold_minutes", DEFAULT_HOLD), "hold_minutes", 1, MAX_HOLD),
        "max_active_per_caller": _whole(value.get("max_active_per_caller", DEFAULT_ACTIVE),
                                        "max_active_per_caller", 1),
    }
    if not isinstance(out["open"], bool):
        raise SiteError("invalid", "booking.open must be true or false.")
    details = value.get("details", [])
    if not isinstance(details, list):
        raise SiteError("invalid", "booking.details must be a list of field definitions.",
                        'Example: ["name!:string", "party_size!:integer"]')
    specs = []
    for spec in details:
        f = schema.parse_field(spec)
        if f.choices or f.policy == "capacity":
            raise SiteError("invalid", f"Booking detail '{f.name}' cannot be a choice or a capacity field.",
                            "Use plain types such as string, integer or date.")
        if f.name in {s.name for s in specs}:
            raise SiteError("invalid", f"Booking detail '{f.name}' is listed twice.")
        specs.append(f)
    out["details"] = [f.model_dump(mode="json", exclude_none=True) for f in specs]
    pay = value.get("payment")
    if pay is not None:
        out["payment"] = _check_payment(sdef, pay)
    return out


def _check_payment(sdef: StructureDef, pay: Any) -> dict:
    if not isinstance(pay, dict):
        raise SiteError("invalid", "booking.payment must be an object.",
                        'Example: {"url": "https://pay.example/{booking}", "amount_field": "price"}')
    unknown = sorted(set(pay) - _PAY_KEYS)
    if unknown:
        raise SiteError("invalid", f"booking.payment does not take: {', '.join(unknown)}.",
                        "It takes: " + ", ".join(sorted(_PAY_KEYS)) + ".")
    url = pay.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise SiteError("invalid", "booking.payment.url must be an http(s) address.",
                        "Write {booking} where the booking id goes, e.g. https://pay.example/{booking}.")
    amount = sdef.field(pay.get("amount_field") or "")
    if amount is None or amount.type not in ("decimal", "number", "integer") or amount.many:
        raise SiteError("invalid", f"booking.payment.amount_field must name a single number field of "
                        f"'{sdef.name}'; got {pay.get('amount_field')!r}.",
                        "Number fields: " + (", ".join(f.name for f in sdef.fields if f.type in
                                                       ("decimal", "number", "integer") and not f.many) or "none")
                        + ".")
    out = {"url": url, "amount_field": amount.name}
    if pay.get("currency_field") is not None:
        cur = sdef.field(pay["currency_field"])
        if cur is None or cur.type != "string":
            raise SiteError("invalid", f"booking.payment.currency_field must name a text field of "
                            f"'{sdef.name}'; got {pay['currency_field']!r}.",
                            "Omit it to use the site's currency.")
        out["currency_field"] = cur.name
    return out


def details_schema(setting: dict) -> dict:
    """What `book` needs, as JSON Schema, so a visiting agent can fill it in without guessing."""
    props, required = {}, []
    for raw in setting.get("details", []):
        f = schema.FieldSpec(**raw)
        p = dict(schema._JSON_TYPES[f.type])
        if f.many:
            p = {"type": "array", "items": p}
        if f.description:
            p["description"] = f.description
        props[f.name] = p
        if f.required and f.default is None:
            required.append(f.name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def bookable(setting: dict, target: str, prefix: str = "") -> dict:
    """The public description of how booking works on a structure. Holds no booking data."""
    return {"target": target, "capacity_field": setting["field"], "needs_key": not setting["open"],
            "hold_minutes": setting["hold_minutes"], "max_active_per_caller": setting["max_active_per_caller"],
            "details": details_schema(setting), "payment": "payment" in setting,
            "actions": {op: f"{prefix}/actions/{target}/{op}" for op in ("reserve", "book", "release", "booking")},
            "steps": [f"reserve {{key, quantity}} places a hold for {setting['hold_minutes']} minutes.",
                      "book {booking, details} confirms it before then; an unconfirmed hold releases itself.",
                      "release {booking} gives the places back. booking {booking} shows its status.",
                      "Only the caller that made a booking can see or change it."]}


# ---------------------------------------------------------------- the mixin

class BookingsMixin:
    def _booking_setting(self, target: str) -> tuple:
        sdef = self._sdef(target)
        setting = sdef.settings.get(SETTING)
        if not setting:
            raise SiteError("not_bookable", f"'{target}' does not take bookings.",
                            "GET /actions lists what visitors can do here.", status=404)
        if sdef.field(setting["field"]) is None:
            raise SiteError("not_bookable", f"'{target}' takes bookings on '{setting['field']}', which is no longer "
                            "one of its fields.", "The site owner must fix the booking setting.", status=409)
        return sdef, setting

    def _settle_bookings(self) -> None:
        """Bring every open booking up to date: expire overdue holds, release those whose record is gone.

        Runs at the start of every booking call and from the expiry timer, so a site nobody has read for
        days is still correct, however many holds became overdue meanwhile.
        """
        now = self.now_z()
        with self.store.tx():
            # A record removed (or removed and added again since) leaves nothing for the booking to hold.
            self.store.x(
                "UPDATE bookings SET status='released', released=?, reason='record_removed', expires=NULL "
                "WHERE status IN ('held','booked') AND NOT EXISTS (SELECT 1 FROM records r WHERE "
                "r.collection=bookings.collection AND r.key=bookings.key AND r.created <= bookings.created)",
                (now,))
            for b in self.store.q("SELECT * FROM bookings WHERE status='held' AND expires <= ? ORDER BY expires",
                                  (now,)):
                self._give_back(b, "expired", "expired")

    def _give_back(self, b: Any, status: str, reason: str) -> None:
        """End an open booking and return its places to the record's capacity."""
        self.store.x("UPDATE bookings SET status=?, released=?, reason=? WHERE id=?",
                     (status, self.now_z(), reason, b["id"]))
        sdef = self._defs().get(b["collection"])
        setting = sdef.settings.get(SETTING) if sdef else None
        row = self._row(b["collection"], b["key"])
        if not setting or row is None or sdef.field(setting["field"]) is None:
            return
        current = self._capacity(sdef, setting, row)
        self.change(sdef.name, b["key"], {setting["field"]: current + b["quantity"]})

    def _capacity(self, sdef: StructureDef, setting: dict, row: Any) -> int:
        view = self._view(sdef, row, self._schedules(sdef.name, row["key"]).get(row["key"], []), self.now_z(), True)
        value = view["data"].get(setting["field"])
        return value if isinstance(value, int) else 0

    def _own_booking(self, caller: ops.Principal, target: str, booking_id: Any):
        """The booking if this caller made it (the owner may see any). Everyone else gets 'not found'."""
        row = self.store.one("SELECT * FROM bookings WHERE id=? AND collection=?", (str(booking_id), target))
        if row is None or (caller.kind != "owner" and row["caller"] != caller.id):
            raise not_found(f"Booking '{booking_id}' in '{target}'", "Use the booking id that `reserve` returned.")
        return row

    @staticmethod
    def _booking_view(b: Any, owner: bool = False) -> dict:
        out = {"booking": b["id"], "target": b["collection"], "key": b["key"], "status": b["status"],
               "quantity": b["quantity"], "created": b["created"]}
        for name in ("expires", "confirmed", "released", "reason"):
            if b[name]:
                out[name] = b[name]
        details = json.loads(b["details"])
        if details:
            out["details"] = details
        if owner:
            out["caller"] = b["caller"]
        return out

    # ------------------------------------------------------------ visitor operations

    def _active_bookings(self, sdef: StructureDef, caller: ops.Principal) -> int:
        """Holds, plus confirmed bookings for things still to come.

        A booking whose record has ended or expired no longer counts, or a regular customer would use up
        the limit for good.
        """
        now, active = self.now_z(), 0
        for b in self.store.q("SELECT key, status FROM bookings WHERE caller=? AND collection=? "
                              "AND status IN ('held','booked')", (caller.id, sdef.name)):
            if b["status"] == "booked":
                row = self._row(sdef.name, b["key"])
                view = self._view(sdef, row, self._schedules(sdef.name, b["key"]).get(b["key"], []), now,
                                  False) if row else None
                if view is None:
                    continue
                times = view.get("instants", {})
                over = times.get("end") or times.get("start")
                if over and over <= now:
                    continue
            active += 1
        return active

    def reserve(self, target: str, key: str, quantity: int = 1, caller: ops.Principal = ops.OWNER) -> dict:
        """Hold `quantity` places on a record. The hold expires unless confirmed with `book`."""
        _whole_arg(quantity, "quantity")
        with self.store.tx():
            self.tick()
            self._settle_bookings()
            sdef, setting = self._booking_setting(target)
            owner = caller.kind == "owner"
            row = self._row(target, key)
            view = self._view(sdef, row, self._schedules(target, str(key)).get(str(key), []), self.now_z(),
                              owner) if row else None
            if view is None:  # an invisible record reads exactly like a missing one
                raise not_found(f"Record '{key}' in '{target}'",
                                f"List what can be booked with GET /collections/{target}.")
            if not owner:
                active = self._active_bookings(sdef, caller)
                limit = setting["max_active_per_caller"]
                if active >= limit:
                    raise SiteError("too_many_bookings", f"This caller already has {active} active holds or "
                                    f"bookings in '{target}'; the limit is {limit}.",
                                    "Book or release one of them first.", status=409)
            left = self._capacity(sdef, setting, row)
            if quantity > left:
                raise SiteError("no_capacity", f"Only {left} left for '{key}' in '{target}'; asked for {quantity}.",
                                f"Reserve {left} or fewer, or choose another record." if left else
                                "Choose another record, or check again later.", status=409)
            now = self.now()
            expires = timeview.to_z(now + timedelta(minutes=setting["hold_minutes"]))
            bid = "bk_" + secrets.token_hex(8)
            self.store.x("INSERT INTO bookings (id, collection, key, quantity, status, caller, created, expires) "
                         "VALUES (?,?,?,?,'held',?,?,?)",
                         (bid, target, row["key"], quantity, caller.id, timeview.to_z(now), expires))
            self.change(target, row["key"], {setting["field"]: left - quantity})
            self._timer(expires, f"bookings/{bid}", "hold expired", f"{SETTING}:{bid}")
            out = self._booking_view(self.store.one("SELECT * FROM bookings WHERE id=?", (bid,)))
            out["remaining"] = left - quantity
            out["book"] = {"href": f"/actions/{target}/book", "before": expires, "details": details_schema(setting)}
            return out

    def book(self, target: str, booking: str, details: Optional[dict] = None,
             caller: ops.Principal = ops.OWNER) -> dict:
        """Confirm the caller's own hold with the details the owner asked for."""
        with self.store.tx():
            self.tick()
            self._settle_bookings()
            sdef, setting = self._booking_setting(target)
            b = self._own_booking(caller, target, booking)
            if b["status"] != "held":
                raise _not_open(b)
            clean = _check_details(setting, details or {}, self.tz)
            now = self.now_z()
            self.store.x("UPDATE bookings SET status='booked', details=?, confirmed=?, expires=NULL WHERE id=?",
                         (json.dumps(clean), now, b["id"]))
            self.store.x("DELETE FROM timers WHERE ref=?", (f"{SETTING}:{b['id']}",))
            out = self._booking_view(self.store.one("SELECT * FROM bookings WHERE id=?", (b["id"],)))
            payment = self._payment(sdef, setting, b)
            if payment is not None:
                out["payment"] = payment
            return out

    def _payment(self, sdef: StructureDef, setting: dict, b: Any) -> Optional[dict]:
        pay = setting.get("payment")
        row = self._row(sdef.name, b["key"])
        if not pay or row is None:
            return None
        data = self._view(sdef, row, self._schedules(sdef.name, b["key"]).get(b["key"], []), self.now_z(), True)["data"]
        try:
            amount = Decimal(str(data[pay["amount_field"]])) * b["quantity"]
        except (KeyError, InvalidOperation):
            return None  # nothing to pay for a record without a price
        currency = data.get(pay.get("currency_field") or "") or self.config.get("currency") or ""
        return PaymentHandoff(amount=amount, currency=currency, url=pay["url"].replace("{booking}", b["id"]),
                              note="Complete payment at this address. This site never handles card details."
                              ).model_dump(mode="json")

    def release(self, target: str, booking: str, caller: ops.Principal = ops.OWNER) -> dict:
        """Cancel the caller's own hold or booking; its places return."""
        with self.store.tx():
            self.tick()
            self._settle_bookings()
            self._sdef(target)
            b = self._own_booking(caller, target, booking)
            if b["status"] not in ACTIVE:
                raise _not_open(b, releasing=True)
            self._give_back(b, "released", "released by owner" if caller.kind == "owner" and
                            b["caller"] != caller.id else "released by caller")
            return self._booking_view(self.store.one("SELECT * FROM bookings WHERE id=?", (b["id"],)),
                                      caller.kind == "owner")

    def booking(self, target: str, booking: str, caller: ops.Principal = ops.OWNER) -> dict:
        """The status of the caller's own booking."""
        self.tick()
        self._settle_bookings()
        self._sdef(target)
        b = self._own_booking(caller, target, booking)
        return {**self._booking_view(b, caller.kind == "owner"), "as_of": self.now_z()}

    # ------------------------------------------------------------ owner operations

    def bookings(self, target: str, status: Optional[str] = None, key: Optional[str] = None, limit: int = 100,
                 cursor: Optional[str] = None) -> dict:
        """A structure's bookings with their details, newest first. Owner (or a role granted it) only."""
        self.tick()
        self._settle_bookings()
        self._booking_setting(target)
        if status is not None and status not in STATUSES:
            raise SiteError("invalid", f"'{status}' is not a booking status.", "Statuses: " + ", ".join(STATUSES) + ".")
        try:
            offset, limit = max(0, int(cursor or 0)), max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            raise SiteError("bad_page", "limit and cursor must be whole numbers.")
        sql, params = "FROM bookings WHERE collection=?", [target]
        if status:
            sql, params = sql + " AND status=?", params + [status]
        if key is not None:
            sql, params = sql + " AND key=?", params + [str(key)]
        total = self.store.one("SELECT COUNT(*) AS n " + sql, tuple(params))["n"]
        rows = self.store.q("SELECT * " + sql + " ORDER BY created DESC, id LIMIT ? OFFSET ?",
                            tuple(params + [limit, offset]))
        out = {"target": target, "items": [self._booking_view(r, True) for r in rows], "total": total,
               "as_of": self.now_z()}
        if offset + limit < total:
            out["next"] = str(offset + limit)
        return out


def _whole_arg(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SiteError("invalid", f"`{name}` must be a whole number of 1 or more.")


def _not_open(b: Any, releasing: bool = False) -> SiteError:
    if b["status"] == "booked":
        return SiteError("already_booked", f"Booking '{b['id']}' is already booked.",
                         "To change it, release it and reserve again.", status=409)
    if b["status"] == "expired":
        return SiteError("hold_expired", f"Hold '{b['id']}' expired at {b['released']} and its places were released.",
                         "Nothing to release." if releasing else "Reserve again with `reserve`.", status=409)
    why = " because its record was removed" if b["reason"] == "record_removed" else ""
    return SiteError("already_released", f"Booking '{b['id']}' is already released{why}.",
                     "Nothing to release." if releasing else "Reserve again with `reserve`.", status=409)


def _check_details(setting: dict, details: Any, tz: Any) -> dict:
    if not isinstance(details, dict):
        raise SiteError("invalid", "`details` must be an object.", "GET /bookable/<structure> describes it.")
    sdef = StructureDef(name="details", fields=[schema.FieldSpec(**f) for f in setting.get("details", [])])
    try:
        return schema.validate(sdef, details, tz)
    except SiteError as e:
        raise SiteError("invalid", e.message.replace("'details' record", "Booking details"),
                        ("Details: " + ", ".join(f"{f.name} ({f.type}{', required' if f.required else ''})"
                                                 for f in sdef.fields) + ".") if sdef.fields else
                        "This structure asks for no details; send {}.")


def _expire_hook(site: Any, timer: Any) -> None:
    # One overdue timer settles every overdue hold, so a late tick catches up in one pass.
    site._settle_bookings()


hooks.SETTINGS[SETTING] = check_setting
hooks.TIMERS[SETTING] = _expire_hook

_T = {"type": "string", "description": "Name of the collection that takes bookings."}
_BK = {"type": "string", "description": "Booking id returned by `reserve`."}

ops.register(
    ops.Op("reserve", "Hold places on a record. The hold expires unless confirmed with `book`.",
           lambda site, caller, target, key, quantity=1: site.reserve(target, key, quantity, caller),
           {"target": _T, "key": {"type": "string", "description": "Key of the record to book."},
            "quantity": {"type": "integer", "description": "How many places; default 1."}},
           ("target", "key"), visitor=True, setting=SETTING, positional=("target", "key")),
    ops.Op("book", "Confirm your hold with the details the site asks for (GET /bookable/<structure>).",
           lambda site, caller, target, booking, details=None: site.book(target, booking, details, caller),
           {"target": _T, "booking": _BK, "details": {"type": "object", "description": "What the site asks for, "
                                                                                       "e.g. a name and party size."}},
           ("target", "booking"), visitor=True, setting=SETTING, positional=("target", "booking"), pairs="details"),
    ops.Op("release", "Cancel your hold or booking; its places return. The owner may release any booking.",
           lambda site, caller, target, booking: site.release(target, booking, caller),
           {"target": _T, "booking": _BK}, ("target", "booking"), visitor=True, setting=SETTING,
           positional=("target", "booking")),
    ops.Op("booking", "Show the status of your booking.",
           lambda site, caller, target, booking: site.booking(target, booking, caller),
           {"target": _T, "booking": _BK}, ("target", "booking"), mutates=False, visitor=True, setting=SETTING,
           positional=("target", "booking")),
    ops.Op("bookings", "List a structure's bookings with their details, newest first, filtered by status and "
                       "record key.",
           lambda site, target, status=None, key=None, limit=100, cursor=None:
               site.bookings(target, status, key, limit, cursor),
           {"target": _T, "status": {"type": "string", "enum": list(STATUSES)},
            "key": {"type": "string", "description": "Only bookings of this record."},
            "limit": {"type": "integer"}, "cursor": {"type": "string"}},
           ("target",), mutates=False, grantable=True, positional=("target",)),
)


def routes(app: Any, site: Any, ctx: Any) -> None:
    """GET /bookable/{target}: how to book here, without any booking data."""

    @app.get("/bookable/{target}")
    def bookable_route(request: Request, target: str):
        owner = ctx.is_owner(request)  # also notes a keyed reader for invite-only checks
        sdef = site._defs().get(target)
        setting = sdef.settings.get(SETTING) if sdef is not None else None
        if sdef is None or (sdef.access == "private" and not owner) or not setting:
            raise not_found(f"Bookable structure '{target}'", "GET /actions lists what visitors can do here.")
        if sdef.access == "invite" and not owner and not (site._reader_may(target)
                                                          or site._reader_may(target, "reserve")):
            raise SiteError("needs_key", f"'{target}' is invite-only.",
                            "Send the key you were given as: Authorization: Bearer <key>", status=401)
        return ctx.reply(request, {**bookable(setting, target, ctx.prefix(request)), "as_of": site.now_z()},
                         owner, f"Booking {target}")
