"""The visitor door: how a visiting agent acts on a site, safely.

A visitor operation is reached at POST /actions/{target}/{op}. This module supplies what every such
operation needs so each feature does not reinvent it:

- retry safety: a changing call carries an Idempotency-Key; repeating it replays the first answer
- preview: ?dry_run=true runs the operation and rolls it back
- quotas: per key and, for callers without one, per network address
- custom actions: Python functions a self-hosted site registers with `@site.action(...)`

Reads are never limited. Only changing calls count against a quota.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import canon, ops, timeview
from .errors import SiteError

DEFAULT_LIMITS = {
    "key_per_day": 1000,       # changing visitor calls per key per UTC day
    "anonymous_per_day": 20,   # the same, per network address, for callers without a key
    "concurrent": 4,           # changing calls one caller may have in flight
    "trust_proxy": False,      # read the caller's address from X-Forwarded-For (only behind your own proxy)
}
KEEP_REPLIES_HOURS = 48


class PaymentHandoff(BaseModel):
    """Where the caller's user completes payment. The site never sees card details."""
    amount: Decimal
    currency: str
    url: str
    expires: Optional[str] = None
    note: str = ""


@dataclass
class CustomAction:
    name: str
    summary: str
    fn: Callable[..., Any]
    input: Optional[type] = None
    open: bool = False
    mutates: bool = True

    def schema(self) -> dict:
        return self.input.model_json_schema() if self.input else {"type": "object"}


class ActionsMixin:
    # ------------------------------------------------------------ limits

    @property
    def limits(self) -> dict:
        return {**DEFAULT_LIMITS, **json.loads(self.store.meta("limits", "{}"))}

    def set_limits(self, **changes: Any) -> dict:
        """Change the quotas for visitor calls. Unknown names are refused."""
        unknown = sorted(set(changes) - set(DEFAULT_LIMITS))
        if unknown:
            raise SiteError("invalid", f"Unknown limit: {', '.join(unknown)}.",
                            "Limits: " + ", ".join(DEFAULT_LIMITS) + ".")
        for name, value in changes.items():
            want = type(DEFAULT_LIMITS[name])
            if isinstance(value, bool) != (want is bool) or not isinstance(value, want) or \
                    (want is int and value < 0):
                raise SiteError("invalid", f"`{name}` must be a {'true/false value' if want is bool else 'whole number'}.")
        stored = {**json.loads(self.store.meta("limits", "{}")), **changes}
        self.store.set_meta("limits", json.dumps(stored))
        return self.limits

    def anonymous(self, address: str) -> ops.Principal:
        """A caller without a key. The address is hashed, so the site keeps no list of who visited."""
        digest = hashlib.sha256(f"{self.public_key}|{address}".encode()).hexdigest()[:16]
        return ops.Principal("anonymous", f"anon:{digest}")

    def _charge(self, caller: ops.Principal) -> None:
        """Count one changing call against the caller's daily quota, or refuse it."""
        if caller.kind == "owner":
            return
        limits = self.limits
        allowed = limits["anonymous_per_day"] if caller.kind == "anonymous" else limits["key_per_day"]
        now = self.now()
        day = now.strftime("%Y-%m-%d")
        with self.store.tx():
            row = self.store.one("SELECT count FROM usage WHERE caller=? AND day=?", (caller.id, day))
            used = row["count"] if row else 0
            if used >= allowed:
                midnight = datetime.combine(now.date() + timedelta(days=1), time(0, 0), tzinfo=timeview.UTC)
                err = SiteError("quota_exceeded", f"This caller has used its {allowed} changing calls for today.",
                                "Reads are not limited. Try again after the time in Retry-After."
                                + ("" if caller.kind != "anonymous" else " A key from the site owner has a "
                                                                         "higher limit."), status=429)
                err.retry_after = max(1, int((midnight - now).total_seconds()))
                raise err
            self.store.x("INSERT INTO usage (caller, day, count) VALUES (?,?,1) "
                         "ON CONFLICT(caller, day) DO UPDATE SET count = count + 1", (caller.id, day))
            self.store.x("DELETE FROM usage WHERE day < ?", ((now - timedelta(days=2)).strftime("%Y-%m-%d"),))

    def _enter(self, caller: ops.Principal) -> None:
        with self._flight_lock:
            if caller.kind != "owner" and self._in_flight.get(caller.id, 0) >= self.limits["concurrent"]:
                err = SiteError("too_many_at_once", "This caller already has several changing calls in progress.",
                                "Wait for them to finish, then retry.", status=429)
                err.retry_after = 1
                raise err
            self._in_flight[caller.id] = self._in_flight.get(caller.id, 0) + 1

    def _leave(self, caller: ops.Principal) -> None:
        with self._flight_lock:
            left = self._in_flight.get(caller.id, 1) - 1
            if left > 0:
                self._in_flight[caller.id] = left
            else:
                self._in_flight.pop(caller.id, None)

    # ------------------------------------------------------------ custom actions

    def action(self, name: str, summary: str = "", input: Optional[type] = None, open: bool = False,
               mutates: bool = True) -> Callable:
        """Register a Python function as a visitor action on this site instance (self-hosting only).

        The function receives the caller and the validated input: `fn(caller, input)` when `input` is a
        Pydantic model, else `fn(caller, **arguments)`. `open` lets callers without a key use it.
        A site served from a folder by the multi-site host cannot have custom actions.
        """
        if name in ops.OPS or "/" in name:
            raise SiteError("invalid", f"'{name}' cannot be used as an action name.")

        def register(fn: Callable) -> Callable:
            self._actions[name] = CustomAction(name, summary or (fn.__doc__ or "").strip(), fn, input, open, mutates)
            return fn
        return register

    def _run_custom(self, caller: ops.Principal, action: CustomAction, args: dict) -> Any:
        if caller.kind == "anonymous" and not action.open:
            raise SiteError("needs_key", f"`{action.name}` needs a key from the site owner.",
                            "Send it as: Authorization: Bearer <key>", status=401)
        if action.input is not None:
            try:
                result = action.fn(caller, action.input(**args))
            except ValidationError as e:
                first = e.errors()[0]
                raise SiteError("invalid", f"{'.'.join(map(str, first['loc'])) or 'input'}: {first['msg']}",
                                "GET /actions describes this action's arguments.")
        else:
            result = action.fn(caller, **args)
        return result.model_dump(mode="json") if isinstance(result, BaseModel) else result

    # ------------------------------------------------------------ acting

    def act(self, caller: ops.Principal, op: str, target: Optional[str] = None, args: Optional[dict] = None,
            idempotency_key: Optional[str] = None, dry_run: bool = False) -> tuple:
        """Run a visitor operation or custom action for a caller. Returns (result, replayed).

        `replayed` is True when the answer is the stored one from an earlier call with the same key.
        """
        args = dict(args or {})
        custom = self._actions.get(op) if target is None else None
        spec = None if custom else ops.OPS.get(op)
        if custom is None and (spec is None or not spec.visitor):
            raise SiteError("unknown_action", f"'{op}' is not something a visitor can do here.",
                            "GET /actions lists the actions this site offers.", status=404)
        if target is not None:
            args["target"] = target
        mutates = custom.mutates if custom else spec.mutates

        def run_once(preview: bool = False) -> Any:
            if custom is None:
                return ops.run(self, caller, op, args, dry_run=preview)
            if not preview or not mutates:
                with self.store.tx():
                    result = self._run_custom(caller, custom, args)
                    if mutates:
                        self.write_audit(caller.id, caller.role or caller.kind, op, None, None, {}, True)
                return result
            try:
                with self.store.tx():
                    raise ops.Preview(self._run_custom(caller, custom, args))
            except ops.Preview as p:
                return {"dry_run": True, "result": p.result}

        if not mutates:
            return run_once(), False
        if dry_run:
            return run_once(preview=True), False
        if not idempotency_key or len(idempotency_key) > 200:
            raise SiteError("needs_idempotency_key", "A changing action needs an Idempotency-Key header.",
                            "Send a fresh random value, and the same value again if you retry this call.")
        scope = f"{target or ''}/{op}"
        request_hash = canon.sha256(canon.canonical(args))

        def stored() -> Optional[tuple]:
            row = self.store.one("SELECT request_hash, response FROM idempotency WHERE caller=? AND scope=? "
                                 "AND idem_key=?", (caller.id, scope, idempotency_key))
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise SiteError("idempotency_mismatch", "That Idempotency-Key was already used with "
                                "different arguments.", "Use a new key for a new request.", status=409)
            return json.loads(row["response"]), True

        found = stored()
        if found:
            return found
        self._enter(caller)
        try:
            self._charge(caller)
            with self.store.tx():
                found = stored()  # another thread may have finished the same call meanwhile
                if found:
                    return found
                result = run_once()
                now = self.now()
                self.store.x("INSERT INTO idempotency (caller, scope, idem_key, request_hash, response, created) "
                             "VALUES (?,?,?,?,?,?)", (caller.id, scope, idempotency_key, request_hash,
                                                      json.dumps(result), timeview.to_z(now)))
                self.store.x("DELETE FROM idempotency WHERE created < ?",
                             (timeview.to_z(now - timedelta(hours=KEEP_REPLIES_HOURS)),))
            return result, False
        finally:
            self._leave(caller)

    def available_actions(self, caller: Optional[ops.Principal] = None, prefix: str = "") -> dict:
        """What a visitor can do here, with each action's arguments."""
        owner = caller is not None and caller.kind == "owner"
        out = []
        for d in self._defs().values():
            if d.access == "private" and not owner:
                continue
            for op in ops.OPS.values():
                block = d.settings.get(op.setting) if op.visitor and op.setting else None
                if block is None:
                    continue
                out.append({"action": op.name, "target": d.name, "href": f"{prefix}/actions/{d.name}/{op.name}",
                            "summary": op.summary, "changes_site": op.mutates,
                            "needs_key": not block.get("open", False),
                            "arguments": {**op.schema(), "properties": {
                                k: v for k, v in op.params.items() if k != "target"},
                                "required": [r for r in op.required if r != "target"]}})
        for a in self._actions.values():
            out.append({"action": a.name, "href": f"{prefix}/actions/{a.name}", "summary": a.summary,
                        "changes_site": a.mutates, "needs_key": not a.open, "arguments": a.schema()})
        return {"actions": out, "calling": [
            "POST the href with a JSON object of arguments.",
            "A changing action needs an Idempotency-Key header: any fresh random value. Send the same value "
            "when retrying and the first answer is returned without acting twice.",
            "Add ?dry_run=true to see what would happen without doing it.",
            "Where needs_key is true, send the key you were given as Authorization: Bearer <key>.",
        ], "as_of": self.now_z()}


def routes(app: Any, site: Any, ctx: Any) -> None:
    def caller_of(request: Request) -> ops.Principal:
        found = ctx.principal(request)
        if found is not None:
            return found
        address = request.client.host if request.client else "unknown"
        if site.limits["trust_proxy"] and request.headers.get("x-forwarded-for"):
            address = request.headers["x-forwarded-for"].split(",")[0].strip()
        return site.anonymous(address)

    @app.get("/actions")
    def list_actions(request: Request):
        return ctx.reply(request, site.available_actions(ctx.principal(request), ctx.prefix(request)),
                         ctx.is_owner(request), "Actions")

    async def act(request: Request, op: str, target: Optional[str]) -> JSONResponse:
        caller = caller_of(request)
        raw = await request.body()
        try:
            args = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise SiteError("invalid", "The request body is not valid JSON.")
        if not isinstance(args, dict):
            raise SiteError("invalid", "The request body must be a JSON object of arguments.")
        dry_run = request.query_params.get("dry_run", "").lower() in ("1", "true", "yes")
        result, replayed = await run_in_threadpool(
            site.act, caller, op, target, args, request.headers.get("idempotency-key"), dry_run)
        return JSONResponse(result, headers={"Idempotent-Replay": "true"} if replayed else {})

    @app.post("/actions/{op}")
    async def act_custom(request: Request, op: str):
        return await act(request, op, None)

    @app.post("/actions/{target}/{op}")
    async def act_on(request: Request, target: str, op: str):
        return await act(request, op, target)


ops.register(
    ops.Op("set_limits", "Change the quotas for visitor calls: key_per_day, anonymous_per_day, concurrent, "
                         "trust_proxy. With no arguments, shows the current limits.",
           lambda site, **kw: site.set_limits(**kw),
           {"key_per_day": {"type": "integer"}, "anonymous_per_day": {"type": "integer"},
            "concurrent": {"type": "integer"},
            "trust_proxy": {"type": "boolean", "description": "Read the caller's address from "
                                                              "X-Forwarded-For. Only behind your own proxy."}},
           pairs="*"),
)
