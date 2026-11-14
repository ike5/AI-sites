"""Redaction: removing a past value from the public history while its hash chain still verifies.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .errors import SiteError, not_found
from .ops import OPS, Op, register
from .schema import StructureDef


class RedactionMixin:
    # ------------------------------------------------------------ redaction

    def _still_current(self, path: str, field: str, value: Any) -> bool:
        """Is this logged value what the site serves, or plans to serve, right now?"""
        if value is None:
            return False  # a cleared field protects nothing
        parts = path.split("/")
        if path == "site":
            return self.config.get(field) == value
        if parts[0] == "collections" and len(parts) == 3:
            row = self._row(parts[1], parts[2])
            if not row:
                return False
            if json.loads(row["data"]).get(field) == value:
                return True
            return any(s["set"].get(field) == value
                       for s in self._schedules(parts[1], parts[2]).get(row["key"], []))
        if parts[0] == "trees" and len(parts) == 3:
            row = self.store.one("SELECT * FROM nodes WHERE tree=? AND id=?", (parts[1], parts[2]))
            if not row:
                return False
            now = {"label": row["label"], "parent": row["parent"], "data": json.loads(row["data"])}
            return field in now and now[field] == value
        if parts[0] == "schemas" and len(parts) == 2:
            row = self.store.one("SELECT def FROM structures WHERE name=?", (parts[1],))
            return bool(row) and field == "definition" and \
                self._public_def(StructureDef(**json.loads(row["def"]))) == value
        return False

    def redact(self, seq: int, fields: Optional[list] = None, reason: str = "") -> dict:
        """Remove values from a past history entry, for example after a takedown request.

        The value and its salt are deleted; the entry's hash chain still verifies because it covers only
        the commitment. This removes the value from this site's history. It cannot recall copies that
        visitors, caches or archives already fetched. Refused while the site still serves or plans to
        serve the value. A sealed entry is redacted whole and can never be revealed.
        """
        row = self.store.one("SELECT * FROM log WHERE seq=?", (seq,))
        if not row:
            raise not_found(f"History entry {seq}", "See /history for sequence numbers.")
        if row["op"] not in ("set", "schedule", "genesis", "sealed"):
            raise SiteError("nothing_to_redact", f"Entry {seq} ({row['op']}) carries no values.",
                            "Only set, schedule, genesis and sealed entries hold values.")
        reason = " ".join(str(reason or "").split())[:200].strip()
        sealed = row["op"] == "sealed"
        done = {r["field"] for r in self.store.q("SELECT field FROM log_redactions WHERE seq=?", (seq,))}
        with self.store.tx():
            if sealed:
                if fields:
                    raise SiteError("invalid", "A sealed entry is redacted whole.", "Omit `fields`.")
                if "sealed" in done:
                    raise SiteError("already_redacted", f"Entry {seq} is already redacted.")
                inner = json.loads(self.store.one("SELECT inner_json FROM log_sealed WHERE seq=?",
                                                  (seq,))["inner_json"])
                targets, current = ["sealed"], []
                if self.store.one("SELECT 1 FROM schedules WHERE seq=?", (seq,)):
                    current.append("schedule")
                current += [f for f, v in (inner.get("values") or {}).items()
                            if self._still_current(inner.get("path", ""), f, v)]
            else:
                values = {r["field"]: json.loads(r["value_json"])
                          for r in self.store.q("SELECT field, value_json FROM log_values WHERE seq=?", (seq,))}
                if fields is not None and not (isinstance(fields, list) and fields):
                    raise SiteError("invalid", "`fields` must name at least one field.",
                                    "Omit it to redact every field the entry still has.")
                if not values and fields is None:
                    raise SiteError("nothing_to_redact", f"Entry {seq} has no values left to redact.")
                targets = list(values) if fields is None else [str(f) for f in fields]
                for f in targets:
                    if f in done:
                        raise SiteError("already_redacted", f"Field '{f}' of entry {seq} is already redacted.",
                                        "Fields still available: " + (", ".join(values) or "none") + ".")
                    if f not in values:
                        raise SiteError("unknown_field", f"Entry {seq} has no field '{f}'.",
                                        "Fields available: " + (", ".join(values) or "none") + ".")
                current = [f for f in targets if self._still_current(row["path"], f, values[f])]
            if current:
                raise SiteError("still_current", f"Entry {seq} holds a value the site still uses: "
                                f"{', '.join(current)}.",
                                "Change or remove it first (for example with `change`, `unschedule` or "
                                "`remove`), then redact.", status=409)
            detail: dict = {"target": seq, "fields": targets}
            if reason:
                detail["reason"] = reason
            by = self.history.redact(seq, targets, detail)
            self._change(self.now_z(), row["path"], "history redacted")
            self._bump()
        self.store.scrub()  # the value must be gone from the files on disk, not just from the tables
        return {"entry": self.history.entry(seq), "redact_seq": by}


register(
    Op("redact", "Remove a past value from the public history (for example after a takedown request). The "
                 "chain still verifies. Refused while the value is still current.",
       lambda site, seq, fields=None, reason="": site.redact(seq, fields, reason),
       {"seq": {"type": "integer", "description": "Sequence number of the history entry."},
        "fields": {"type": "array", "description": "Fields to redact; omit for every field the entry still "
                                                   "has. Omit for a sealed entry."},
        "reason": {"type": "string", "description": "Optional public note, one line, up to 200 characters."}},
       ("seq",)),
)
OPS["redact"].positional, OPS["redact"].flags = ('seq',), {}
