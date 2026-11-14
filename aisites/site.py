"""A site: its structures, records, trees, scheduled changes and the views agents read.

Every method here is the trusted, local form of an operation. The four doors (Python, command line,
admin web API, MCP) all reach these methods through `ops.run`, so they behave identically.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import kinds, schema, timeview
from .errors import SiteError, not_found
from .history import History
from .schema import FieldSpec, StructureDef
from .store import Store
from . import hooks, ops as ops_mod
from .access import AccessMixin, _hash_key
from .actions import ActionsMixin
from .bookings import BookingsMixin
from .checkpoints import CheckpointMixin
from .forms import FormsMixin
from .redaction import RedactionMixin

VERSION = "0.1"
KEY_RE = re.compile(r"^[^\s/?#]+$")
FILTER_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")
CONFIG_FIELDS = ("name", "description", "timezone", "language", "currency")


class Site(RedactionMixin, CheckpointMixin, AccessMixin, ActionsMixin, BookingsMixin, FormsMixin):
    def __init__(self, directory: Any, clock: Optional[Callable[[], datetime]] = None,
                 anchors: Optional[list] = None):
        self.dir = Path(directory)
        db = self.dir / "site.db"
        if not db.exists():
            raise SiteError("no_site", f"There is no site in '{self.dir}'.",
                            "Create one with: aisites site create <directory> --name \"My site\"", status=404)
        self.store = Store(str(db))
        self.clock = clock or (lambda: datetime.now(timeview.UTC))
        self.history = History(self.store, self.now_z)
        self._viewer = threading.local()
        self._actions: dict = {}          # custom actions registered with @site.action
        self._in_flight: dict = {}        # changing visitor calls in progress, per caller
        self._flight_lock = threading.Lock()
        self._anchors = list(anchors or [])

    # ------------------------------------------------------------ creation and config

    @classmethod
    def create(cls, directory: Any, name: str, description: str = "", timezone: str = "UTC",
               language: str = "en", currency: str = "",
               clock: Optional[Callable[[], datetime]] = None) -> tuple:
        """Create a new site folder. Returns (site, owner_key); the key is shown only this once."""
        d = Path(directory)
        if (d / "site.db").exists():
            raise SiteError("exists", f"A site already exists in '{d}'.")
        timeview.zone(timezone)
        d.mkdir(parents=True, exist_ok=True)
        store = Store(str(d / "site.db"))
        signing = Ed25519PrivateKey.generate()
        raw = signing.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
        key_path = d / "site.key"
        key_path.write_text(base64.b64encode(raw).decode())
        os.chmod(key_path, 0o600)
        public = base64.b64encode(signing.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        owner_key = "ais_owner_" + secrets.token_urlsafe(24)
        config = {"name": name, "description": description, "timezone": timezone,
                  "language": language, "currency": currency}
        with store.tx():
            store.set_meta("config", json.dumps(config))
            store.set_meta("owner_hash", _hash_key(owner_key))
            store.set_meta("public_key", public)
            store.set_meta("rev", 0)
        store.close()
        site = cls(d, clock)
        site.history.append("genesis", "site", values=config, detail={"public_key": public, "aisites": VERSION})
        return site, owner_key

    def now(self) -> datetime:
        return self.clock().astimezone(timeview.UTC).replace(microsecond=0)

    def now_z(self) -> str:
        return timeview.to_z(self.now())

    @property
    def config(self) -> dict:
        return json.loads(self.store.meta("config", "{}"))

    @property
    def tz(self) -> ZoneInfo:
        return timeview.zone(self.config.get("timezone") or "UTC")

    @property
    def public_key(self) -> str:
        return self.store.meta("public_key", "")

    def check_owner(self, token: Optional[str]) -> bool:
        return bool(token) and hmac.compare_digest(_hash_key(token), self.store.meta("owner_hash", ""))

    def new_owner_key(self) -> str:
        """Replace the owner key (local use only). The old key stops working."""
        owner_key = "ais_owner_" + secrets.token_urlsafe(24)
        self.store.set_meta("owner_hash", _hash_key(owner_key))
        return owner_key

    def configure(self, **changes: Any) -> dict:
        unknown = set(changes) - set(CONFIG_FIELDS)
        if unknown:
            raise SiteError("invalid", f"Unknown site setting: {', '.join(sorted(unknown))}.",
                            "Settings: " + ", ".join(CONFIG_FIELDS) + ".")
        if changes.get("timezone"):
            timeview.zone(changes["timezone"])
        with self.store.tx():
            config = self.config
            changed = {k: v for k, v in changes.items() if v is not None and config.get(k) != v}
            if changed:
                config.update(changed)
                self.store.set_meta("config", json.dumps(config))
                self.history.append("set", "site", changed)
                self._change(self.now_z(), "site", "changed")
                self._bump()
        return self.config

    # ------------------------------------------------------------ who is looking

    @contextmanager
    def viewing(self, structure: Optional[str], hidden: Any):
        """Within this block, views of `structure` are built for a caller who may not see `hidden` fields.

        Stripping fields from a finished view is not enough: timestamps derived from a hidden field's
        scheduled change would still give its timing away.
        """
        previous = getattr(self._viewer, "limit", None)
        self._viewer.limit = (structure, frozenset(hidden or ()))
        try:
            yield
        finally:
            self._viewer.limit = previous

    def set_reader(self, principal: Any) -> None:
        """Note who is reading on this thread, so invite-only data can be shown to a key that may read it."""
        self._viewer.reader = principal

    def _reader_may(self, target: str, op: str = "read") -> bool:
        reader = getattr(self._viewer, "reader", None)
        if reader is None or reader.kind != "role":
            return False
        return any(g["target"] in (target, "*") and op in g["ops"]
                   for g in self.role_grants(reader.role, reader.id))

    def _reader_keyed(self) -> bool:
        return getattr(getattr(self._viewer, "reader", None), "kind", "") == "role"

    def _hidden_from(self, sdef: StructureDef, owner: bool = True) -> frozenset:
        """Fields the current reader may not see: private ones for visitors, a role's hidden set for staff."""
        if not owner:
            return frozenset(sdef.names("private"))
        limit = getattr(self._viewer, "limit", None)
        return limit[1] if limit and limit[0] == sdef.name else frozenset()

    # ------------------------------------------------------------ bookkeeping

    def _bump(self) -> None:
        self.store.set_meta("rev", int(self.store.meta("rev", 0)) + 1)

    def _change(self, at: str, path: str, what: str) -> None:
        self.store.x("INSERT INTO changes (time, path, what) VALUES (?,?,?)", (at, path, what))

    def _timer(self, at: str, path: str, what: str, ref: str) -> None:
        self.store.x("INSERT INTO timers (at, path, what, ref) VALUES (?,?,?,?)", (at, path, what, ref))

    def tick(self) -> None:
        """Turn time boundaries that have passed (a change starting, a record expiring) into feed entries."""
        now = self.now_z()
        if getattr(self._viewer, "ticking", False):
            return  # a hook below reads and writes records, which would otherwise tick again
        if not self.store.one("SELECT 1 FROM timers WHERE at <= ? LIMIT 1", (now,)):
            return
        self._viewer.ticking = True
        try:
            with self.store.tx():
                for t in self.store.q("SELECT * FROM timers WHERE at <= ? ORDER BY at, id", (now,)):
                    hook = hooks.TIMERS.get(t["ref"].split(":", 1)[0])
                    if hook:  # a feature acts on its own timers, e.g. releasing an expired hold
                        hook(self, t)
                    else:
                        self._change(t["at"], t["path"], t["what"])
                self.store.x("DELETE FROM timers WHERE at <= ?", (now,))
        finally:
            self._viewer.ticking = False

    def etag(self, owner: bool = False, extra: str = "") -> str:
        """A validator for what this reader can see.

        Visitors get one built from the public history head and change feed, so it stays still while
        only private data changes. Keyed readers get one that follows every write.
        """
        self.tick()
        last = self.store.one("SELECT COALESCE(MAX(id), 0) AS m FROM changes")["m"]
        if owner or self._reader_keyed():
            return f'W/"{self.store.meta("rev", 0)}.{last}.o{extra}"'
        return f'W/"p{self.history.head()[0]}.{last}{extra}"'

    # ------------------------------------------------------------ structures

    def _defs(self) -> dict:
        return {r["name"]: StructureDef(**json.loads(r["def"]))
                for r in self.store.q("SELECT name, def FROM structures ORDER BY name")}

    def _sdef(self, name: str, type: Optional[str] = None) -> StructureDef:
        row = self.store.one("SELECT def FROM structures WHERE name=?", (name,))
        if not row:
            existing = ", ".join(self._defs()) or "none yet"
            raise not_found(f"Structure '{name}'", f"Existing structures: {existing}. Create one with `define`.")
        sdef = StructureDef(**json.loads(row["def"]))
        if type and sdef.type != type:
            raise SiteError("wrong_structure", f"'{name}' is a {sdef.type}, not a {type}.")
        return sdef

    @staticmethod
    def _public_def(sdef: StructureDef) -> Optional[dict]:
        """The part of a definition the public history may show: nothing private, not even field names."""
        if not sdef.public:
            return None
        out = sdef.dump()
        out["fields"] = [f for f in out["fields"] if f["policy"] != "private"]
        return out

    def _save_def(self, sdef: StructureDef, before: Optional[dict] = None) -> None:
        """Store a definition. `before` is its public form prior to the change, if it had one."""
        self.store.x("INSERT INTO structures (name, def) VALUES (?, ?) "
                     "ON CONFLICT(name) DO UPDATE SET def=excluded.def",
                     (sdef.name, json.dumps(sdef.dump(), sort_keys=True)))
        path = f"schemas/{sdef.name}"
        now = self.now_z()
        after = self._public_def(sdef)
        if after is not None and after != before:
            self.history.append("set", path, {"definition": after})
            self._change(now, path, "defined")
        elif after is None and before is not None:
            self.history.append("delete", path)
            self._change(now, path, "removed")
        self._bump()

    def _apply_refs(self, fields: list, refs: Optional[dict], defs: dict, own_name: str) -> None:
        by_name = {f.name: f for f in fields}
        for fname, target in (refs or {}).items():
            if fname not in by_name:
                raise SiteError("invalid", f"refs names '{fname}', which is not a field.")
            if target not in defs and target != own_name:
                raise not_found(f"Structure '{target}'", "Define it first, then reference it.")
            ttype = defs[target].type if target in defs else "collection"
            by_name[fname].choices = {ttype: target}
        for f in fields:
            if f.choices:
                ttype, target = next(iter(f.choices.items()))
                if target != own_name and (target not in defs or defs[target].type != ttype):
                    raise not_found(f"The {ttype} '{target}' that field '{f.name}' chooses from",
                                    "Define it first.")

    def define(self, name: str, type: str = "collection", kind: Optional[str] = None, key: Optional[str] = None,
               access: str = "public", description: str = "", fields: Optional[list] = None,
               refs: Optional[dict] = None, settings: Optional[dict] = None) -> dict:
        """Create a structure: a collection (optionally of a built-in kind) or a tree."""
        schema.check_name(name, "structure")
        if type not in ("collection", "tree", "form"):
            raise SiteError("invalid", f"'{type}' is not a structure type.", "Use 'collection', 'tree' or 'form'.")
        if access not in schema.ACCESS:
            raise SiteError("invalid", f"'{access}' is not an access level.", "Use public, invite or private.")
        with self.store.tx():
            defs = self._defs()
            if name in defs:
                raise SiteError("exists", f"Structure '{name}' already exists.", "Use `alter` to change it.",
                                status=409)
            specs: list = []
            semantic = None
            if type == "tree":
                if kind or fields:
                    raise SiteError("invalid", "A tree has no kind or fields; its nodes have an id and a label.")
            elif type == "form":
                # A form's fields describe what a visitor may send; it has no records and so no key field.
                if kind:
                    raise SiteError("invalid", "A form has no kind.")
                specs = [schema.parse_field(spec) for spec in fields or []]
                self._apply_refs(specs, refs, defs, name)
            else:
                if kind:
                    if kind not in kinds.KINDS:
                        raise SiteError("bad_kind", f"'{kind}' is not a built-in kind.",
                                        "Kinds: " + ", ".join(kinds.KINDS) + ". Omit kind for a custom structure.")
                    k = kinds.KINDS[kind]
                    specs = [FieldSpec(**f) for f in k["fields"]]
                    semantic = k["semantic"]
                    key = key or k["key"]
                    description = description or k["description"]
                key = schema.check_name(key or "id", "key field")
                for spec in fields or []:
                    f = schema.parse_field(spec)
                    specs = [s for s in specs if s.name != f.name] + [f]
                key_field = next((s for s in specs if s.name == key), None)
                if key_field is None:
                    specs.insert(0, FieldSpec(name=key, description="Unique key; generated when omitted."))
                elif key_field.type != "string" or key_field.many or key_field.policy != "logged":
                    raise SiteError("invalid", f"Key field '{key}' must be a plain logged string.")
                self._apply_refs(specs, refs, defs, name)
            sdef = StructureDef(name=name, type=type, kind=kind, semantic=semantic, key=key or "id",
                                access=access, description=description, fields=specs)
            self._apply_settings(sdef, settings)
            self._save_def(sdef)
        return self.describe(name)

    def _apply_settings(self, sdef: StructureDef, settings: Optional[dict]) -> None:
        """Set, adjust or remove feature settings; each is checked by the feature that owns it.

        Giving part of a setting changes just that part. null for a whole setting removes it.
        """
        for name, value in (settings or {}).items():
            if value is None:
                sdef.settings.pop(name, None)
                continue
            check = hooks.SETTINGS.get(name)
            if check is None:
                raise SiteError("invalid", f"'{name}' is not a setting.",
                                "Settings: " + (", ".join(hooks.SETTINGS) or "none") + ".")
            if isinstance(value, dict) and isinstance(sdef.settings.get(name), dict):
                # Like `change` on a record: what is given is merged in, and null removes one entry.
                value = {k: v for k, v in {**sdef.settings[name], **value}.items() if v is not None}
            sdef.settings[name] = check(self, sdef, value)

    def alter(self, name: str, add: Optional[list] = None, remove: Optional[list] = None,
              access: Optional[str] = None, description: Optional[str] = None,
              refs: Optional[dict] = None, settings: Optional[dict] = None) -> dict:
        """Change a structure: add or remove fields, change access or description, set references."""
        with self.store.tx():
            sdef = self._sdef(name)
            before = self._public_def(sdef)
            if sdef.type == "tree" and (add or remove or refs):
                raise SiteError("invalid", "A tree has no fields to alter.")
            has_records = bool(self.store.one("SELECT 1 FROM records WHERE collection=? LIMIT 1", (name,)))
            for spec in add or []:
                f = schema.parse_field(spec)
                if sdef.field(f.name):
                    raise SiteError("exists", f"Field '{f.name}' already exists in '{name}'.", status=409)
                if f.required and f.default is None and has_records:
                    raise SiteError("invalid", f"Cannot add required field '{f.name}' to a collection that "
                                    "already has records.", "Add it as optional, or give it a default.")
                sdef.fields.append(f)
            for fname in remove or []:
                if fname == sdef.key:
                    raise SiteError("invalid", f"'{fname}' is the key field and cannot be removed.")
                if not sdef.field(fname):
                    raise not_found(f"Field '{fname}' in '{name}'")
                sdef.fields = [f for f in sdef.fields if f.name != fname]
                self._prune_grants(name, fname)
                for r in self.store.q("SELECT key, data FROM records WHERE collection=?", (name,)):
                    data = json.loads(r["data"])
                    if fname in data:
                        del data[fname]
                        self.store.x("UPDATE records SET data=? WHERE collection=? AND key=?",
                                     (json.dumps(data), name, r["key"]))
            if access is not None:
                if access not in schema.ACCESS:
                    raise SiteError("invalid", f"'{access}' is not an access level.",
                                    "Use public, invite or private.")
                sdef.access = access
            if description is not None:
                sdef.description = description
            if sdef.type != "tree":
                self._apply_refs(sdef.fields, refs, self._defs(), name)
            self._apply_settings(sdef, settings)
            self._save_def(sdef, before)
        return self.describe(name)

    def _referrers(self, target: str) -> list:
        """(structure, field) pairs whose choices point at `target`."""
        return [(d, f) for d in self._defs().values() if d.name != target
                for f in d.fields if f.choices and target in f.choices.values()]

    def drop(self, name: str) -> dict:
        """Delete a structure and everything in it. The public history keeps its past."""
        with self.store.tx():
            sdef = self._sdef(name)
            users = self._referrers(name)
            if users:
                raise SiteError("in_use", f"'{name}' is referenced by " +
                                ", ".join(f"{d.name}.{f.name}" for d, f in users) + ".",
                                "Remove those references with `alter` first.", status=409)
            prefix = f"{'trees' if sdef.type == 'tree' else 'collections'}/{name}/"
            self.store.x("DELETE FROM records WHERE collection=?", (name,))
            self.store.x("DELETE FROM nodes WHERE tree=?", (name,))
            self.store.x("DELETE FROM schedules WHERE collection=?", (name,))
            self.store.x("DELETE FROM messages WHERE form=?", (name,))  # so a later namesake starts empty
            self.store.x("DELETE FROM timers WHERE substr(path, 1, ?) = ?", (len(prefix), prefix))
            self.store.x("DELETE FROM structures WHERE name=?", (name,))
            self._prune_grants(name)
            now = self.now_z()
            if sdef.public:
                self.history.append("delete", f"schemas/{name}")
                self._change(now, f"schemas/{name}", "removed")
            self._bump()
        return {"dropped": name}

    def describe(self, name: Optional[str] = None) -> dict:
        """One structure's definition, or the whole site: settings, structures and available kinds."""
        if name:
            sdef = self._sdef(name)
            out = sdef.dump()
            table, col = ("nodes", "tree") if sdef.type == "tree" else ("records", "collection")
            out["count"] = self.store.one(f"SELECT COUNT(*) AS n FROM {table} WHERE {col}=?", (name,))["n"]
            return out
        return {"site": self.config,
                "structures": [self.describe(n) for n in self._defs()],
                "kinds": kinds.describe()}

    # ------------------------------------------------------------ record helpers

    @staticmethod
    def _no_form(sdef: StructureDef) -> None:
        if sdef.type == "form":
            raise SiteError("wrong_structure", f"'{sdef.name}' is a form; its messages are not ordinary records.",
                            "Use `inbox`, `read_message` and `reply`.")

    def _row(self, name: str, key: str):
        return self.store.one("SELECT * FROM records WHERE collection=? AND key=?", (name, str(key)))

    def _need_row(self, sdef: StructureDef, key: str):
        row = self._row(sdef.name, key)
        if not row:
            raise not_found(f"Record '{key}' in '{sdef.name}'", f"List keys with `list {sdef.name}`.")
        return row

    def _schedules(self, name: str, key: Optional[str] = None) -> dict:
        sql, params = "SELECT * FROM schedules WHERE collection=?", [name]
        if key is not None:
            sql += " AND key=?"
            params.append(str(key))
        out: dict = {}
        for r in self.store.q(sql + " ORDER BY id", tuple(params)):
            out.setdefault(r["key"], []).append({
                "id": r["id"], "set": json.loads(r["set_json"]), "start": r["start"], "end": r["end"],
                "announced": bool(r["announced"]), "seq": r["seq"]})
        return out

    def _check_choices(self, sdef: StructureDef, data: dict) -> None:
        for f in sdef.fields:
            if not f.choices or f.name not in data:
                continue
            ttype, target = next(iter(f.choices.items()))
            values = data[f.name] if f.many else [data[f.name]]
            for v in values:
                if ttype == "tree":
                    ok = self.store.one("SELECT 1 FROM nodes WHERE tree=? AND id=?", (target, str(v)))
                else:
                    ok = self.store.one("SELECT 1 FROM records WHERE collection=? AND key=?", (target, str(v)))
                if not ok:
                    raise SiteError("bad_choice", f"'{v}' is not a valid choice for '{f.name}'.",
                                    f"Valid values are the {'node ids' if ttype == 'tree' else 'keys'} "
                                    f"of the {ttype} '{target}'.")

    def _clean(self, sdef: StructureDef, data: dict) -> dict:
        clean = schema.validate(sdef, data, self.tz, hide=self._hidden_from(sdef))
        check = kinds.VALIDATORS.get(sdef.kind or "")
        if check:
            check(clean)
        self._check_choices(sdef, clean)
        return clean

    def _validity(self, sdef: StructureDef, data: dict) -> tuple:
        """(valid_from, valid_until) as UTC instants, when the structure has those datetime fields."""
        tz = schema.record_zone(sdef, data, self.tz)
        out = []
        for name in ("valid_from", "valid_until"):
            f = sdef.field(name)
            ok = f is not None and f.type == "datetime" and not f.many and name in data
            out.append(timeview.local_to_z(data[name], tz) if ok else None)
        return tuple(out)

    def _publish(self, sdef: StructureDef, key: str, values: dict, now: str, data: dict,
                 what: str = "changed", visible: bool = True) -> None:
        """Log and announce a change to a record. Changes to a not-yet-visible record stay sealed.

        `visible` is False when only private fields changed: the public feed must not move at all.
        """
        self._bump()
        if not sdef.public:
            return
        path = f"collections/{sdef.name}/{key}"
        valid_from, valid_until = self._validity(sdef, data)
        hidden = valid_from is not None and valid_from > now
        if values:
            if hidden:
                self.history.append_sealed(valid_from, {"op": "set", "path": path, "values": values})
            else:
                self.history.append("set", path, values)
        if not hidden and visible:
            self._change(now, path, what)
        self.store.x("DELETE FROM timers WHERE path=? AND ref='validity'", (path,))
        if hidden:
            self._timer(valid_from, path, "became visible", "validity")
        if valid_until is not None and valid_until > now:
            self._timer(valid_until, path, "expired", "validity")

    def _materialize(self, sdef: StructureDef, row) -> tuple:
        """Fold finished scheduled changes into the stored record before a write touches it."""
        now = self.now_z()
        base = json.loads(row["data"])
        updated = row["updated"]
        dirty = False
        for s in self._schedules(sdef.name, row["key"]).get(row["key"], []):
            if s["start"] > now:
                continue
            if s["end"] is None:
                for field, value in s["set"].items():
                    if value is None:
                        base.pop(field, None)
                    else:
                        base[field] = value
                updated = max(updated, s["start"])
            elif s["end"] <= now:
                updated = max(updated, s["end"])
            else:
                continue
            self.store.x("DELETE FROM schedules WHERE id=?", (s["id"],))
            dirty = True
        if dirty:
            self.store.x("UPDATE records SET data=?, updated=? WHERE collection=? AND key=?",
                         (json.dumps(base), updated, sdef.name, row["key"]))
        return base, updated

    def _view(self, sdef: StructureDef, row, schedules: list, now: str, owner: bool) -> Optional[dict]:
        """What a reader sees of one record right now, or None if it is not visible to them."""
        data, active, upcoming, ended = timeview.effective(json.loads(row["data"]), schedules, now)
        valid_from, valid_until = self._validity(sdef, data)
        visible = (valid_from is None or valid_from <= now) and (valid_until is None or now < valid_until)
        if not owner and not visible:
            return None
        hide = self._hidden_from(sdef, owner)
        for name in hide:
            data.pop(name, None)

        def shown(fields: dict) -> dict:
            return {k: v for k, v in fields.items() if k not in hide}

        def concerns(s: dict) -> bool:
            """A scheduled change this reader may know about. One touching only hidden fields is not."""
            return not hide or not s["set"] or bool(shown(s["set"]))

        active, upcoming, ended = ([s for s in group if concerns(s)] for group in (active, upcoming, ended))
        out: dict = {"key": row["key"], "data": data}
        out["updated"] = max([row["updated"]] + [s["start"] for s in active] + [s["end"] for s in ended])
        if row["confirmed"]:
            out["confirmed"] = row["confirmed"]
        if row["live_as_of"] and any(n in data for n in sdef.live_names()):
            out["live_as_of"] = row["live_as_of"]
        inst = schema.instants(sdef, data, self.tz)
        if inst:
            out["instants"] = inst
        if active:
            out["active_changes"] = [{"fields": sorted(shown(s["set"])), "since": s["start"], "until": s["end"]}
                                     for s in active]
        ahead = [s for s in upcoming if owner or s["announced"]]
        if ahead:
            out["upcoming_changes"] = [{"set": shown(s["set"]), "start": s["start"], "end": s["end"]}
                                       for s in ahead]
        nexts = [s["end"] for s in active if s["end"]] + [s["start"] for s in ahead]
        if valid_until and valid_until > now:
            nexts.append(valid_until)
        if owner and valid_from and valid_from > now:
            nexts.append(valid_from)
        if nexts:
            out["next_change"] = min(nexts)
        if owner:
            out["visible"] = visible
            if active or upcoming:
                out["schedules"] = [{"id": s["id"], "set": shown(s["set"]), "start": s["start"],
                                     "end": s["end"], "announced": s["announced"]} for s in active + upcoming]
        return out

    # ------------------------------------------------------------ collection operations

    def add(self, name: str, data: dict, parent: Optional[str] = None, position: Optional[int] = None) -> dict:
        """Add a record to a collection, or a node to a tree. Fails if the key already exists."""
        sdef = self._sdef(name)
        self._no_form(sdef)
        if sdef.type == "tree":
            return self._node_add(sdef, data, parent, position)
        if not isinstance(data, dict):
            raise SiteError("invalid", "data must be an object of field values.")
        with self.store.tx():
            data = dict(data)
            if data.get(sdef.key) in (None, ""):
                data[sdef.key] = secrets.token_hex(4)
            clean = self._clean(sdef, data)
            key = clean[sdef.key]
            if not KEY_RE.match(key):
                raise SiteError("bad_key", f"'{key}' cannot be used as a key.",
                                "Keys may not contain spaces, '/', '?' or '#'.")
            if self._row(name, key):
                raise SiteError("exists", f"Record '{key}' already exists in '{name}'.",
                                "Use `change` to update it.", status=409)
            now = self.now_z()
            live = now if any(n in clean for n in sdef.live_names()) else None
            self.store.x("INSERT INTO records (collection, key, data, version, created, updated, live_as_of) "
                         "VALUES (?,?,?,1,?,?,?)", (name, key, json.dumps(clean), now, now, live))
            self._publish(sdef, key, {n: clean[n] for n in sdef.logged_names() if n in clean}, now, clean, "added")
        return self.get(name, key)

    def get(self, name: str, key: str) -> dict:
        """One record with everything the owner may see, or one tree node with its subtree."""
        self.tick()
        sdef = self._sdef(name)
        self._no_form(sdef)
        if sdef.type == "tree":
            return self._node_view(sdef, str(key))
        row = self._need_row(sdef, key)
        return self._view(sdef, row, self._schedules(name, key).get(row["key"], []), self.now_z(), True)

    def list(self, name: str, where: Optional[dict] = None, sort: Optional[str] = None, limit: int = 100,
             cursor: Optional[str] = None, fields: Optional[list] = None, owner: bool = True) -> dict:
        """Records matching filters such as {"price.lt": 50, "region": "MX"}, sorted and paged."""
        self.tick()
        sdef = self._sdef(name)
        self._no_form(sdef)
        if sdef.type == "tree":
            return self.read_tree(name)
        now_dt, now = self.now(), self.now_z()
        conds = []
        for k, raw in (where or {}).items():
            fname, _, op = k.partition(".")
            op = op or "eq"
            f = sdef.field(fname)
            if f is None or (f.policy == "private" and not owner):
                raise SiteError("bad_filter", f"'{fname}' is not a field of '{name}'.",
                                "Fields: " + ", ".join(self._visible_names(sdef, owner)) + ".")
            if op not in FILTER_OPS:
                raise SiteError("bad_filter", f"'.{op}' is not a filter operator.",
                                "Use field=value or field.gte, .gt, .lte, .lt, .ne.")
            conds.append((f, op, schema.filter_value(f, raw, self.tz, now_dt)))
        if fields:
            for fname in fields:
                if fname not in self._visible_names(sdef, owner):
                    raise SiteError("bad_fields", f"'{fname}' is not a field of '{name}'.",
                                    "Fields: " + ", ".join(self._visible_names(sdef, owner)) + ".")
        sort_field, descending = None, False
        if sort:
            descending = sort.startswith("-")
            sort_field = sdef.field(sort.lstrip("-"))
            if sort_field is None or (sort_field.policy == "private" and not owner):
                raise SiteError("bad_sort", f"'{sort}' is not a field of '{name}'.")
        schedules = self._schedules(name)
        items = []
        for row in self.store.q("SELECT * FROM records WHERE collection=? ORDER BY key", (name,)):
            view = self._view(sdef, row, schedules.get(row["key"], []), now, owner)
            if view is None:
                continue
            tz = schema.record_zone(sdef, view["data"], self.tz)
            if all(self._match(f, op, want, view["data"].get(f.name), tz) for f, op, want in conds):
                items.append((view, tz))
        if sort_field is not None:
            def sort_key(pair):
                v = schema.comparable(sort_field, pair[0]["data"].get(sort_field.name), pair[1])
                return (v is None, v if v is not None else 0)
            present = sorted((p for p in items if p[0]["data"].get(sort_field.name) is not None),
                             key=sort_key, reverse=descending)
            items = present + [p for p in items if p[0]["data"].get(sort_field.name) is None]
        try:
            offset = max(0, int(cursor or 0))
            limit = max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            raise SiteError("bad_page", "limit and cursor must be whole numbers.")
        page = [p[0] for p in items[offset:offset + limit]]
        if fields:
            for view in page:
                view["data"] = {k: v for k, v in view["data"].items() if k in fields}
        out = {"collection": name, "items": page, "total": len(items), "as_of": now}
        if offset + limit < len(items):
            out["next"] = str(offset + limit)
        return out

    def _visible_names(self, sdef: StructureDef, owner: bool) -> list:
        return [f.name for f in sdef.fields if owner or f.policy != "private"]

    @staticmethod
    def _match(f: FieldSpec, op: str, want: Any, value: Any, tz: ZoneInfo) -> bool:
        if value is None:
            return op == "ne"
        if f.many:
            got = [schema.comparable(FieldSpec(name=f.name, type=f.type), v, tz) for v in value]
            return (want in got) if op == "eq" else (want not in got) if op == "ne" else False
        got = schema.comparable(f, value, tz)
        return {"eq": got == want, "ne": got != want, "gt": got > want, "gte": got >= want,
                "lt": got < want, "lte": got <= want}[op]

    def change(self, name: str, key: str, set: dict) -> dict:
        """Change fields of an existing record or tree node. A null value clears a field."""
        sdef = self._sdef(name)
        self._no_form(sdef)
        if sdef.type == "tree":
            return self._node_change(sdef, str(key), set)
        if not isinstance(set, dict) or not set:
            raise SiteError("invalid", "`set` must name at least one field to change.",
                            'Example: {"price": "549.00"}')
        with self.store.tx():
            row = self._need_row(sdef, key)
            base, updated = self._materialize(sdef, row)
            if sdef.key in set and set[sdef.key] != row["key"]:
                raise SiteError("invalid", "A record's key cannot be changed.",
                                "Add a new record and remove the old one.")
            merged = dict(base)
            for field, value in set.items():
                if value is None:
                    merged.pop(field, None)
                else:
                    merged[field] = value
            clean = self._clean(sdef, merged)
            changed = {n: clean.get(n) for n in sdef.names() if clean.get(n) != base.get(n)}
            if changed:
                now = self.now_z()
                live_names, private = sdef.live_names(), sdef.names("private")
                # `updated` is about what visitors can see, so a private-only edit leaves it alone.
                content = any(n not in live_names and n not in private for n in changed)
                live = any(n in live_names for n in changed)
                self.store.x(
                    "UPDATE records SET data=?, version=version+1, updated=?, live_as_of=? "
                    "WHERE collection=? AND key=?",
                    (json.dumps(clean), now if content else updated, now if live else row["live_as_of"],
                     name, row["key"]))
                logged = {n: v for n, v in changed.items() if n in sdef.logged_names()}
                self._publish(sdef, row["key"], logged, now, clean, "changed" if content else "live value changed",
                              visible=content or live)
        return self.get(name, key)

    def remove(self, name: str, key: str, recursive: bool = False) -> dict:
        """Remove a record, or a tree node. Fails if something still refers to it."""
        sdef = self._sdef(name)
        self._no_form(sdef)
        if sdef.type == "tree":
            return self._node_remove(sdef, str(key), recursive)
        with self.store.tx():
            row = self._need_row(sdef, key)
            self._check_unreferenced(name, [row["key"]])
            now = self.now_z()
            path = f"collections/{name}/{row['key']}"
            valid_from, _ = self._validity(sdef, json.loads(row["data"]))
            self.store.x("DELETE FROM records WHERE collection=? AND key=?", (name, row["key"]))
            self.store.x("DELETE FROM schedules WHERE collection=? AND key=?", (name, row["key"]))
            self.store.x("DELETE FROM timers WHERE path=?", (path,))
            if sdef.public:
                self.history.never_reveal_path(path, now)
                if not (valid_from and valid_from > now):
                    self.history.append("delete", path)
                    self._change(now, path, "removed")
            self._bump()
        return {"removed": row["key"]}

    def _check_unreferenced(self, target: str, keys: list) -> None:
        for d, f in self._referrers(target):
            for r in self.store.q("SELECT key, data FROM records WHERE collection=?", (d.name,)):
                value = json.loads(r["data"]).get(f.name)
                values = value if isinstance(value, list) else [value]
                hit = next((k for k in keys if k in values), None)
                if hit is not None:
                    raise SiteError("in_use", f"'{hit}' is still used by {d.name}/{r['key']} ({f.name}).",
                                    "Change or remove that record first.", status=409)

    # ------------------------------------------------------------ time operations

    def schedule(self, name: str, key: str, set: dict, start: Any = None, end: Any = None, on: Any = None,
                 announce: bool = False) -> dict:
        """Plan a change that starts, and optionally ends, by itself.

        `on` is shorthand for one whole day. A change is hidden from visitors until it starts unless
        `announce` is true.
        """
        sdef = self._sdef(name, "collection")
        if not isinstance(set, dict) or not set:
            raise SiteError("invalid", "`set` must name at least one field to change.",
                            'Example: {"price": "47.99"}')
        held = sdef.settings.get("booking", {}).get("field")
        if held in set:
            raise SiteError("invalid", f"'{held}' is the capacity that bookings draw on, so it cannot be scheduled.",
                            "Set it directly with `change`.")
        now_dt, now = self.now(), self.now_z()
        if on is not None:
            if start is not None or end is not None:
                raise SiteError("invalid", "Give either `on`, or `start` with an optional `end`.")
            day = timeview.resolve_date(on, self.tz, now_dt)
            start_dt = datetime.combine(day, time(0, 0), tzinfo=self.tz)
            end_dt = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=self.tz)
            start_z, end_z = timeview.to_z(start_dt), timeview.to_z(end_dt)
        else:
            if start is None:
                raise SiteError("invalid", "A scheduled change needs `start`, or `on` for a single day.")
            start_z = timeview.to_z(timeview.resolve_instant(start, self.tz, now_dt))
            end_z = timeview.to_z(timeview.resolve_instant(end, self.tz, now_dt)) if end is not None else None
        if end_z is not None and end_z <= start_z:
            raise SiteError("invalid", "`end` must be after `start`.")
        if end_z is not None and end_z <= now:
            raise SiteError("invalid", "That period is already over.", "Use `change` for an immediate change.")
        with self.store.tx():
            row = self._need_row(sdef, key)
            if sdef.key in set:
                raise SiteError("invalid", "A record's key cannot be scheduled to change.")
            base, _ = self._materialize(sdef, row)
            merged = dict(base)
            for field, value in set.items():
                if value is None:
                    merged.pop(field, None)
                else:
                    merged[field] = value
            clean = self._clean(sdef, merged)
            planned = {n: clean.get(n) for n in set}
            cur = self.store.x(
                "INSERT INTO schedules (collection, key, set_json, start, end, announced, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (name, row["key"], json.dumps(planned), start_z, end_z, int(bool(announce)), now))
            sid = cur.lastrowid
            path = f"collections/{name}/{row['key']}"
            started = start_z <= now
            if sdef.public and any(n not in sdef.names("private") for n in planned):
                logged = {n: v for n, v in planned.items() if n in sdef.logged_names()}
                detail = {"start": start_z, "end": end_z, "schedule": sid}
                seq = None
                if logged:
                    if announce or started:
                        seq = self.history.append("schedule", path, logged, detail)
                    else:
                        seq = self.history.append_sealed(
                            start_z, {"op": "schedule", "path": path, "values": logged, "detail": detail})
                    self.store.x("UPDATE schedules SET seq=? WHERE id=?", (seq, sid))
                ref = f"schedule:{sid}"
                if started:
                    self._change(now, path, "changed")
                else:
                    if announce:
                        self._change(now, path, "change announced")
                    self._timer(start_z, path, "scheduled change started", ref)
                if end_z is not None:
                    self._timer(end_z, path, "scheduled change ended", ref)
            self._bump()
        return {"schedule": {"id": sid, "set": planned, "start": start_z, "end": end_z,
                             "announced": bool(announce)},
                "record": self.get(name, key)}

    def unschedule(self, name: str, key: str, schedule: int) -> dict:
        """Cancel a scheduled change. One that was hidden and had not started leaves no public trace."""
        sdef = self._sdef(name, "collection")
        with self.store.tx():
            row = self.store.one("SELECT * FROM schedules WHERE id=? AND collection=? AND key=?",
                                 (schedule, name, str(key)))
            if not row:
                raise not_found(f"Scheduled change {schedule} on {name}/{key}",
                                f"`get {name} {key}` lists its scheduled changes.")
            now = self.now_z()
            path = f"collections/{name}/{key}"
            self.store.x("DELETE FROM schedules WHERE id=?", (schedule,))
            self.store.x("DELETE FROM timers WHERE ref=?", (f"schedule:{schedule}",))
            if sdef.public and any(n not in sdef.names("private") for n in json.loads(row["set_json"])):
                started = row["start"] <= now
                if row["announced"] or started:
                    self.history.append("unschedule", path, detail={"schedule": schedule})
                    self._change(now, path, "changed" if started else "announced change cancelled")
                elif row["seq"]:
                    self.history.never_reveal(row["seq"])
            self._bump()
        return {"unscheduled": schedule, "record": self.get(name, key)}

    def confirm(self, name: str, key: str) -> dict:
        """Record that this information is still true as of now, without changing it."""
        sdef = self._sdef(name, "collection")
        with self.store.tx():
            row = self._need_row(sdef, key)
            now = self.now_z()
            self.store.x("UPDATE records SET confirmed=? WHERE collection=? AND key=?", (now, name, row["key"]))
            valid_from, _ = self._validity(sdef, json.loads(row["data"]))
            if sdef.public and not (valid_from and valid_from > now):
                path = f"collections/{name}/{row['key']}"
                self.history.append("confirm", path)
                self._change(now, path, "confirmed")
            self._bump()
        return self.get(name, key)

    # ------------------------------------------------------------ tree operations

    def _node(self, sdef: StructureDef, node_id: str):
        row = self.store.one("SELECT * FROM nodes WHERE tree=? AND id=?", (sdef.name, node_id))
        if not row:
            raise not_found(f"Node '{node_id}' in tree '{sdef.name}'")
        return row

    def _children(self, tree: str, parent: Optional[str]) -> list:
        if parent is None:
            return self.store.q("SELECT * FROM nodes WHERE tree=? AND parent IS NULL ORDER BY position, id", (tree,))
        return self.store.q("SELECT * FROM nodes WHERE tree=? AND parent=? ORDER BY position, id", (tree, parent))

    def _place(self, tree: str, node_id: str, parent: Optional[str], position: Optional[int]) -> None:
        """Put a node among its new siblings at `position` (end if omitted) and renumber them."""
        siblings = [r["id"] for r in self._children(tree, parent) if r["id"] != node_id]
        index = len(siblings) if position is None else max(0, min(len(siblings), int(position)))
        siblings.insert(index, node_id)
        for i, sid in enumerate(siblings):
            self.store.x("UPDATE nodes SET parent=?, position=? WHERE tree=? AND id=?"
                         if sid == node_id else
                         "UPDATE nodes SET position=? WHERE tree=? AND id=?",
                         (parent, i, tree, sid) if sid == node_id else (i, tree, sid))

    def _node_log(self, sdef: StructureDef, node_id: str, values: dict, what: str) -> None:
        self._bump()
        if sdef.public:
            path = f"trees/{sdef.name}/{node_id}"
            self.history.append("set", path, values)
            self._change(self.now_z(), path, what)

    def _node_add(self, sdef: StructureDef, data: Any, parent: Optional[str], position: Optional[int]) -> dict:
        if isinstance(data, str):
            data = {"label": data}
        if not isinstance(data, dict):
            raise SiteError("invalid", "A tree node is an object with an id and a label.",
                            'Example: {"id": "road", "label": "Road bikes"}')
        data = dict(data)
        label = data.pop("label", None)
        node_id = data.pop("id", None) or re.sub(r"[^a-z0-9]+", "-", str(label or "").lower()).strip("-") \
            or secrets.token_hex(4)
        node_id, label = str(node_id), str(label or node_id)
        data.pop("children", None)
        if not KEY_RE.match(node_id):
            raise SiteError("bad_key", f"'{node_id}' cannot be used as a node id.",
                            "Ids may not contain spaces, '/', '?' or '#'.")
        with self.store.tx():
            if self.store.one("SELECT 1 FROM nodes WHERE tree=? AND id=?", (sdef.name, node_id)):
                raise SiteError("exists", f"Node '{node_id}' already exists in '{sdef.name}'.", status=409)
            if parent is not None:
                self._node(sdef, str(parent))
            self.store.x("INSERT INTO nodes (tree, id, parent, position, label, data, updated) VALUES (?,?,?,?,?,?,?)",
                         (sdef.name, node_id, parent, 0, label, json.dumps(data), self.now_z()))
            self._place(sdef.name, node_id, parent, position)
            values = {"label": label, "parent": parent}
            if data:
                values["data"] = data
            self._node_log(sdef, node_id, values, "added")
        return self._node_view(sdef, node_id)

    def _node_change(self, sdef: StructureDef, node_id: str, set: dict) -> dict:
        if not isinstance(set, dict) or not set:
            raise SiteError("invalid", "`set` must name at least one thing to change.", 'Example: {"label": "New"}')
        if "id" in set or "parent" in set or "children" in set:
            raise SiteError("invalid", "A node's id cannot change; use `move` to change its parent.")
        with self.store.tx():
            row = self._node(sdef, node_id)
            data = json.loads(row["data"])
            values: dict = {}
            label = row["label"]
            for k, v in set.items():
                if k == "label":
                    label = str(v)
                    values["label"] = label
                elif v is None:
                    data.pop(k, None)
                else:
                    data[k] = v
            if any(k != "label" for k in set):
                values["data"] = data
            self.store.x("UPDATE nodes SET label=?, data=?, updated=? WHERE tree=? AND id=?",
                         (label, json.dumps(data), self.now_z(), sdef.name, node_id))
            self._node_log(sdef, node_id, values, "changed")
        return self._node_view(sdef, node_id)

    def _descendants(self, tree: str, node_id: str) -> list:
        out, todo = [], [node_id]
        while todo:
            current = todo.pop()
            for child in self._children(tree, current):
                out.append(child["id"])
                todo.append(child["id"])
        return out

    def move(self, name: str, key: str, parent: Optional[str] = None, position: Optional[int] = None) -> dict:
        """Move a tree node under another parent (null for the top level) and/or to a position."""
        sdef = self._sdef(name, "tree")
        node_id = str(key)
        with self.store.tx():
            self._node(sdef, node_id)
            if parent is not None:
                parent = str(parent)
                self._node(sdef, parent)
                if parent == node_id or parent in self._descendants(name, node_id):
                    raise SiteError("cycle", f"'{node_id}' cannot be moved under itself or its own descendant.")
            self._place(name, node_id, parent, position)
            self.store.x("UPDATE nodes SET updated=? WHERE tree=? AND id=?", (self.now_z(), name, node_id))
            self._node_log(sdef, node_id, {"parent": parent}, "moved")
        return self._node_view(sdef, node_id)

    def _node_remove(self, sdef: StructureDef, node_id: str, recursive: bool) -> dict:
        with self.store.tx():
            self._node(sdef, node_id)
            below = self._descendants(sdef.name, node_id)
            if below and not recursive:
                raise SiteError("has_children", f"Node '{node_id}' has {len(below)} node(s) under it.",
                                "Pass recursive=true to remove them too, or move them first.", status=409)
            doomed = [node_id] + below
            self._check_unreferenced(sdef.name, doomed)
            now = self.now_z()
            for nid in doomed:
                self.store.x("DELETE FROM nodes WHERE tree=? AND id=?", (sdef.name, nid))
                if sdef.public:
                    self.history.append("delete", f"trees/{sdef.name}/{nid}")
                    self._change(now, f"trees/{sdef.name}/{nid}", "removed")
            self._bump()
        return {"removed": doomed}

    def _nest(self, tree: str, row, depth: Optional[int]) -> dict:
        node = {"id": row["id"], "label": row["label"], **json.loads(row["data"])}
        children = self._children(tree, row["id"])
        if children:
            if depth is not None and depth <= 0:
                node["children_count"] = len(children)
            else:
                node["children"] = [self._nest(tree, c, None if depth is None else depth - 1) for c in children]
        return node

    def _node_view(self, sdef: StructureDef, node_id: str) -> dict:
        row = self._node(sdef, node_id)
        node = self._nest(sdef.name, row, None)
        node["parent"] = row["parent"]
        return node

    def read_tree(self, name: str, node: Optional[str] = None, depth: Optional[int] = None) -> dict:
        """A tree, or the subtree under one node, optionally cut off at a depth to save context."""
        sdef = self._sdef(name, "tree")
        if depth is not None:
            try:
                depth = int(depth)
            except (TypeError, ValueError):
                raise SiteError("bad_page", "depth must be a whole number.")
        if node is not None:
            nodes = [self._nest(name, self._node(sdef, str(node)), depth)]
        else:
            nodes = [self._nest(name, r, None if depth is None else depth - 1) for r in self._children(name, None)]
        updated = self.store.one("SELECT MAX(updated) AS u FROM nodes WHERE tree=?", (name,))["u"]
        out = {"tree": name, "nodes": nodes, "as_of": self.now_z()}
        if updated:
            out["updated"] = updated
        return out

    # ------------------------------------------------------------ reader views

    def readable(self, name: str, owner: bool) -> StructureDef:
        """The structure, if this reader may see it. Private structures look absent to everyone else."""
        row = self.store.one("SELECT def FROM structures WHERE name=?", (name,))
        sdef = StructureDef(**json.loads(row["def"])) if row else None
        if sdef is None or (sdef.access == "private" and not owner):
            raise not_found(f"'{name}'", "The manifest at /.well-known/aisite.json lists what this site offers.")
        if sdef.access == "invite" and not owner and not self._reader_may(name):
            raise SiteError("needs_key", f"'{name}' is invite-only.",
                            "Send the key you were given as: Authorization: Bearer <key>", status=401)
        return sdef

    def read_record(self, name: str, key: str, owner: bool = False) -> dict:
        self.tick()
        sdef = self.readable(name, owner)
        if sdef.type != "collection":
            raise SiteError("wrong_structure", f"'{name}' is a tree; read it at /trees/{name}.")
        row = self._row(name, key)
        view = self._view(sdef, row, self._schedules(name, key).get(key, []), self.now_z(), owner) if row else None
        if view is None:
            raise not_found(f"Record '{key}' in '{name}'")
        view["collection"] = name
        view["as_of"] = self.now_z()
        return view

    def hours(self, date: Any = None, key: Optional[str] = None, owner: bool = False) -> dict:
        """Open or closed on a date, for every Hours record or one of them."""
        now_dt = self.now()
        defs = self._defs()
        closures = []
        for d in defs.values():
            if d.kind == "Closure" and (owner or d.public):
                closures += [(v["key"], v["data"], v["updated"])
                             for v in self.list(d.name, owner=owner, limit=1000)["items"]]
        closures.sort(key=lambda c: c[2])
        results = []
        for d in defs.values():
            if d.kind != "Hours" or not (owner or d.public):
                continue
            for v in self.list(d.name, owner=owner, limit=1000)["items"]:
                if key is not None and v["key"] != key:
                    continue
                tz = schema.record_zone(d, v["data"], self.tz)
                day = timeview.resolve_date(date or "today", tz, now_dt)
                mine = [(k, c) for k, c, _ in closures if c.get("hours") in (None, v["key"])]
                answer = timeview.hours_on(v["data"], mine, day, tz, now_dt)
                results.append({"key": v["key"], "collection": d.name,
                                **{n: v["data"][n] for n in ("name", "place") if n in v["data"]}, **answer})
        if key is not None and not results:
            raise not_found(f"Hours '{key}'", "GET /hours lists every hours record.")
        return {"results": results, "as_of": self.now_z()}

    def changes(self, since: int = 0, limit: int = 200) -> dict:
        """What changed after a cursor: paths only, so an agent re-reads just what it cares about."""
        self.tick()
        try:
            since, limit = max(0, int(since)), max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            raise SiteError("bad_page", "since and limit must be whole numbers.")
        rows = self.store.q("SELECT * FROM changes WHERE id > ? ORDER BY id LIMIT ?", (since, limit + 1))
        page = rows[:limit]
        return {"changes": [{"id": r["id"], "time": r["time"], "path": r["path"], "what": r["what"]} for r in page],
                "cursor": page[-1]["id"] if page else since, "more": len(rows) > limit, "as_of": self.now_z()}

    def first_published(self) -> str:
        row = self.store.one("SELECT time FROM log WHERE seq=1")
        return row["time"] if row else ""

    def manifest(self, owner: bool = False, prefix: str = "") -> dict:
        """The single entry point an agent reads first."""
        config = self.config
        structures = []
        has_hours = False
        for d in self._defs().values():
            if d.access == "private" and not owner:
                continue
            base = f"{prefix}/{ {'tree': 'trees', 'form': 'forms'}.get(d.type, 'collections') }/{d.name}"
            item = {"name": d.name, "type": d.type, "access": d.access, "href": base}
            if d.kind:
                item["kind"] = d.kind
            if d.semantic:
                item["semantic"] = d.semantic
            if d.description:
                item["description"] = d.description
            if d.type == "collection":
                item["schema"] = base + "/schema"
            if d.access == "invite" and not owner and not self._reader_may(d.name):
                item["note"] = "Needs a key from the site owner."
            actions = [op.name for op in ops_mod.OPS.values() if op.visitor and op.setting in d.settings]
            if actions:
                item["actions"] = actions
            has_hours = has_hours or (d.kind == "Hours" and (owner or d.public))
            structures.append(item)
        out = {
            "aisites": VERSION, **{k: v for k, v in config.items() if v},
            "first_published": self.first_published(), "as_of": self.now_z(),
            "public_key": self.public_key, "structures": structures,
            "links": {"actions": f"{prefix}/actions", "changes": f"{prefix}/changes", "stream": f"{prefix}/changes/stream", "history": f"{prefix}/history",
                      "head": f"{prefix}/history/head",
                      "overview": f"{prefix}/llms.txt", "openapi": f"{prefix}/openapi.json"},
            "reading": [
                "Collections: GET href. Filter with field=value or field.gte/.gt/.lte/.lt/.ne, pick fields "
                "with fields=a,b, page with limit and cursor, sort with sort=field or sort=-field, and use "
                "format=table for compact rows.",
                "Each record states when it was last updated or confirmed, when live values were read, and "
                "next_change if a change is already announced.",
                "Dates in filters accept today and tomorrow, resolved in the site's time zone.",
                "GET /changes?since=<cursor> lists what changed; GET /history shows past values.",
            ],
        }
        if has_hours:
            out["answers"] = {"open_on_date": f"{prefix}/hours?date=YYYY-MM-DD (or today, tomorrow)"}
        return out

    # ------------------------------------------------------------ Python door

    def __getitem__(self, name: str) -> "Handle":
        self._sdef(name)
        return Handle(self, name)

    def close(self) -> None:
        self.store.close()


class Handle:
    """`site["products"]`: the operations of one structure, for use from Python."""

    def __init__(self, site: Site, name: str):
        self.site, self.name = site, name

    def add(self, data: Any = None, parent: Optional[str] = None, position: Optional[int] = None, **fields: Any):
        return self.site.add(self.name, data if data is not None else fields, parent, position)

    def get(self, key: str):
        return self.site.get(self.name, key)

    def list(self, where: Optional[dict] = None, **options: Any):
        return self.site.list(self.name, where, **options)

    def change(self, key: str, set: Optional[dict] = None, **fields: Any):
        return self.site.change(self.name, key, set if set is not None else fields)

    def remove(self, key: str, recursive: bool = False):
        return self.site.remove(self.name, key, recursive)

    def move(self, key: str, parent: Optional[str] = None, position: Optional[int] = None):
        return self.site.move(self.name, key, parent, position)

    def schedule(self, key: str, set: Optional[dict] = None, start: Any = None, end: Any = None, on: Any = None,
                 announce: bool = False, **fields: Any):
        return self.site.schedule(self.name, key, set if set is not None else fields, start, end, on, announce)

    def unschedule(self, key: str, schedule: int):
        return self.site.unschedule(self.name, key, schedule)

    def confirm(self, key: str):
        return self.site.confirm(self.name, key)

    def describe(self):
        return self.site.describe(self.name)
