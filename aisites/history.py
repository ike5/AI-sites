"""The append-only, hash-chained change log.

Each entry commits to the values it introduces with salted hashes. Values and salts are stored beside the
entry, so anyone can check them, and can later be withheld (redaction, Milestone 2) without breaking the
chain. A sealed entry hides a planned change until it starts, while proving when it was planned.
"""

import json
from typing import Any, Callable, Optional

from . import canon
from .store import Store


class History:
    def __init__(self, store: Store, now_z: Callable[[], str]):
        self.store = store
        self.now_z = now_z

    def head(self) -> tuple:
        row = self.store.one("SELECT seq, hash, time FROM log ORDER BY seq DESC LIMIT 1")
        return (row["seq"], row["hash"], row["time"]) if row else (0, canon.GENESIS_PREV, "")

    def _append(self, op: str, path: str, commit: dict, detail: dict) -> int:
        seq, prev, last_time = self.head()
        body = {
            "seq": seq + 1,
            "time": max(self.now_z(), last_time),  # never goes backwards, even if the clock does
            "op": op, "path": path, "commit": commit, "detail": detail, "prev": prev,
        }
        self.store.x(
            "INSERT INTO log (seq, time, op, path, commit_json, detail_json, prev, hash) VALUES (?,?,?,?,?,?,?,?)",
            (body["seq"], body["time"], op, path, json.dumps(commit, sort_keys=True),
             json.dumps(detail, sort_keys=True), prev, canon.entry_hash(body)))
        return body["seq"]

    def append(self, op: str, path: str, values: Optional[dict] = None, detail: Optional[dict] = None) -> int:
        """Record a change. `values` maps each changed field to its new value (None means removed)."""
        with self.store.tx():
            commit, rows = {}, []
            for field, value in (values or {}).items():
                salt = canon.new_salt()
                commit[field] = canon.commit(salt, field, value)
                rows.append((field, json.dumps(value), salt))
            seq = self._append(op, path, commit, detail or {})
            for field, value_json, salt in rows:
                self.store.x("INSERT INTO log_values (seq, field, value_json, salt) VALUES (?,?,?,?)",
                             (seq, field, value_json, salt))
            return seq

    def append_sealed(self, reveal_at: str, inner: dict) -> int:
        """Record a change that stays hidden until `reveal_at`. Only a single commitment is public."""
        with self.store.tx():
            salt = canon.new_salt()
            seq = self._append("sealed", "sealed", {"sealed": canon.commit(salt, "sealed", inner)}, {})
            self.store.x("INSERT INTO log_sealed (seq, reveal_at, inner_json, salt, path) VALUES (?,?,?,?,?)",
                         (seq, reveal_at, json.dumps(inner, sort_keys=True), salt, inner.get("path", "")))
            return seq

    def never_reveal(self, seq: int) -> None:
        """A hidden change cancelled before it started stays sealed for good."""
        self.store.x("UPDATE log_sealed SET reveal_at=NULL WHERE seq=?", (seq,))

    def never_reveal_path(self, path: str, after: str) -> None:
        self.store.x("UPDATE log_sealed SET reveal_at=NULL WHERE path=? AND reveal_at>?", (path, after))

    def _entry(self, row: Any, now_z: str) -> dict:
        entry = {
            "seq": row["seq"], "time": row["time"], "op": row["op"], "path": row["path"],
            "commit": json.loads(row["commit_json"]), "detail": json.loads(row["detail_json"]),
            "prev": row["prev"], "hash": row["hash"],
        }
        values = self.store.q("SELECT field, value_json, salt FROM log_values WHERE seq=?", (row["seq"],))
        if values:
            entry["values"] = {v["field"]: json.loads(v["value_json"]) for v in values}
            entry["salts"] = {v["field"]: v["salt"] for v in values}
        redacted = self.store.q("SELECT field, at, by_seq FROM log_redactions WHERE seq=?", (row["seq"],))
        if redacted:
            entry["redacted"] = {r["field"]: {"at": r["at"], "by": r["by_seq"]} for r in redacted}
        if row["op"] == "sealed" and "sealed" not in entry.get("redacted", {}):
            sealed = self.store.one("SELECT reveal_at, inner_json, salt FROM log_sealed WHERE seq=?",
                                    (row["seq"],))
            if sealed and sealed["reveal_at"] and sealed["reveal_at"] <= now_z:
                entry["revealed"] = json.loads(sealed["inner_json"])
                entry["salt"] = sealed["salt"]
                entry["revealed_at"] = sealed["reveal_at"]
        return entry

    # ------------------------------------------------------------ redaction

    def redact(self, target: int, fields: list, detail: dict) -> int:
        """Destroy stored values (or a sealed entry's content) and log the redaction. Returns its seq.

        The hash covers only commitments, so the chain still verifies. Callers check the rules first.
        """
        with self.store.tx():
            row = self.store.one("SELECT op, path FROM log WHERE seq=?", (target,))
            at = self.now_z()
            seq = self._append("redact", row["path"], {}, detail)
            for field in fields:
                if row["op"] == "sealed":
                    # The inner path would give the content away, so it goes too.
                    self.store.x("UPDATE log_sealed SET inner_json='{}', salt='', reveal_at=NULL, path='' "
                                 "WHERE seq=?", (target,))
                else:
                    self.store.x("DELETE FROM log_values WHERE seq=? AND field=?", (target, field))
                self.store.x("INSERT INTO log_redactions (seq, field, at, by_seq) VALUES (?,?,?,?)",
                             (target, field, at, seq))
            return seq

    def entry(self, seq: int) -> Optional[dict]:
        row = self.store.one("SELECT * FROM log WHERE seq=?", (seq,))
        return self._entry(row, self.now_z()) if row else None

    def entries(self, after: int = 0, limit: int = 100, path: Optional[str] = None,
                since: Optional[str] = None, until: Optional[str] = None) -> list:
        """Entries after a sequence number, oldest first, optionally limited to a path prefix or time span."""
        now_z = self.now_z()
        sql = ["SELECT l.* FROM log l LEFT JOIN log_sealed s ON s.seq = l.seq WHERE l.seq > ?"]
        params: list = [after]
        if path:
            prefix = path.rstrip("/") + "/"
            sql.append("AND (l.path = ? OR substr(l.path, 1, ?) = ? OR (s.reveal_at IS NOT NULL AND "
                       "s.reveal_at <= ? AND (s.path = ? OR substr(s.path, 1, ?) = ?)))")
            params += [path, len(prefix), prefix, now_z, path, len(prefix), prefix]
        if since:
            sql.append("AND l.time >= ?")
            params.append(since)
        if until:
            sql.append("AND l.time < ?")
            params.append(until)
        sql.append("ORDER BY l.seq LIMIT ?")
        params.append(limit)
        return [self._entry(r, now_z) for r in self.store.q(" ".join(sql), tuple(params))]


def verify_entries(entries: list, prev: str = canon.GENESIS_PREV, start_seq: int = 1) -> list:
    """Check a contiguous run of entries. Returns a list of problems; empty means the run is consistent."""
    problems = []
    seq, last_time = start_seq, ""
    for e in entries:
        where = f"entry {e.get('seq')}"
        if e.get("seq") != seq:
            problems.append(f"{where}: expected sequence number {seq}")
            seq = e.get("seq", seq)
        if e.get("prev") != prev:
            problems.append(f"{where}: does not link to the previous entry")
        if canon.entry_hash(e) != e.get("hash"):
            problems.append(f"{where}: hash does not match its content")
        if e.get("time", "") < last_time:
            problems.append(f"{where}: time goes backwards")
        for field, value in (e.get("values") or {}).items():
            salt = (e.get("salts") or {}).get(field, "")
            if canon.commit(salt, field, value) != e["commit"].get(field):
                problems.append(f"{where}: value of '{field}' does not match its commitment")
        for field in e.get("redacted") or {}:
            if field not in e.get("commit", {}):
                problems.append(f"{where}: '{field}' is marked redacted but was never committed")
        if "revealed" in e and canon.commit(e.get("salt", ""), "sealed", e["revealed"]) != e["commit"].get("sealed"):
            problems.append(f"{where}: revealed content does not match its commitment")
        prev, last_time, seq = e.get("hash"), e.get("time", ""), seq + 1
    return problems
