"""Roles, keys and the private audit trail.

A role is a name plus grants; a key belongs to a role. Staff roles are granted editing operations, guest
roles visitor operations. Nothing here touches the public history, the change feed or the manifest.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Optional

from . import schema
from .errors import SiteError, not_found
from .ops import _I, _S, OPS, Op, Principal, grantable, register


def _hash_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AccessMixin:
    # ------------------------------------------------------------ roles, keys and the audit trail
    #
    # Roles, keys and audit rows are private bookkeeping: nothing here touches the public history, the
    # change feed or the manifest. The permission check itself lives in `ops.run`.

    # Set by a host (host.py) so that tokens it issued through its sign-in flow are recognised here.
    # Takes the token and returns a Principal or None. A site served on its own never has one.
    token_resolver: Any = None

    def resolve(self, token: Optional[str]) -> Optional["Principal"]:
        """Who a bearer token belongs to: the owner, an unrevoked role key, or nobody (None)."""
        from .ops import OWNER, Principal
        if not token:
            return None
        if self.check_owner(token):
            return OWNER
        row = self.store.one("SELECT id, role FROM keys WHERE hash=? AND revoked IS NULL", (_hash_key(token),))
        if row:
            return Principal("role", row["id"], row["role"])
        return self.token_resolver(token) if self.token_resolver else None

    def _grant_choices(self) -> str:
        return ", ".join(grantable())

    def _check_grants(self, grants: Any) -> list:
        if not isinstance(grants, list) or not grants:
            raise SiteError("invalid", "`grants` must be a non-empty list of grants.",
                            'Example: [{"target": "games", "ops": ["get", "change"], "fields": ["price"]}]')
        defs = self._defs()
        out = []
        for i, g in enumerate(grants):
            where = f"Grant {i + 1}"
            if not isinstance(g, dict):
                raise SiteError("invalid", f"{where} is not an object.",
                                'A grant is {"target": <structure or "*">, "ops": [...], "fields": [...]}.')
            extra = sorted(set(g) - {"target", "ops", "fields"})
            if extra:
                raise SiteError("invalid", f"{where} has unknown entries: {', '.join(extra)}.",
                                "A grant takes: target, ops, fields.")
            target = g.get("target")
            if target != "*" and target not in defs:
                raise SiteError("invalid", f"{where}: '{target}' is not a structure.",
                                "Use one of: " + ", ".join(list(defs) + ['"*" for every structure']) + ".")
            ops_ = g.get("ops")
            if not isinstance(ops_, list) or not ops_ or not all(isinstance(o, str) for o in ops_):
                raise SiteError("invalid", f"{where}: `ops` must be a non-empty list of operation names.",
                                "Operations a role may be given: " + self._grant_choices() + ".")
            bad = [o for o in ops_ if o not in grantable()]
            if bad:
                raise SiteError("invalid", f"{where}: {', '.join(bad)} cannot be given to a role.",
                                "Operations a role may be given: " + self._grant_choices() +
                                ". Everything else is for the site owner only.")
            clean = {"target": target, "ops": sorted(set(ops_), key=ops_.index)}
            if "fields" in g:
                fields = g["fields"]
                if target == "*":
                    raise SiteError("invalid", f"{where}: a grant on \"*\" cannot be limited to fields.",
                                    "Name the structure as the target to limit fields.")
                sdef = defs[target]
                if sdef.type == "tree":
                    raise SiteError("invalid", f"{where}: '{target}' is a tree and has no fields.",
                                    "Omit `fields` for a tree.")
                if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
                    raise SiteError("invalid", f"{where}: `fields` must be a non-empty list of field names.",
                                    f"Fields of '{target}': " + ", ".join(sdef.names()) + ".")
                unknown = [f for f in fields if not sdef.field(f)]
                if unknown:
                    raise SiteError("invalid", f"{where}: {', '.join(unknown)} is not a field of '{target}'.",
                                    f"Fields of '{target}': " + ", ".join(sdef.names()) + ".")
                clean["fields"] = sorted(set(fields), key=fields.index)
            out.append(clean)
        return out

    def set_role(self, role: str, grants: list, description: str = "") -> dict:
        """Create or replace a role. Takes effect at once for every key of that role."""
        schema.check_name(role, "role")
        clean = self._check_grants(grants)
        with self.store.tx():
            self.store.x("INSERT INTO roles (name, grants, description) VALUES (?,?,?) ON CONFLICT(name) "
                         "DO UPDATE SET grants=excluded.grants, description=excluded.description",
                         (role, json.dumps(clean), description or ""))
        return {"role": role, "description": description or "", "grants": clean}

    def _need_role(self, role: str):
        row = self.store.one("SELECT * FROM roles WHERE name=?", (role,))
        if not row:
            names = [r["name"] for r in self.store.q("SELECT name FROM roles ORDER BY name")]
            raise not_found(f"Role '{role}'", "Existing roles: " + (", ".join(names) or "none yet") +
                            ". Create one with `set_role`.")
        return row

    def drop_role(self, role: str) -> dict:
        with self.store.tx():
            self._need_role(role)
            live = [r["id"] for r in self.store.q(
                "SELECT id FROM keys WHERE role=? AND revoked IS NULL ORDER BY created, id", (role,))]
            if live:
                raise SiteError("in_use", f"Role '{role}' still has {len(live)} unrevoked key(s): "
                                + ", ".join(live) + ".", "Revoke them with `revoke_key` first.", status=409)
            self.store.x("DELETE FROM roles WHERE name=?", (role,))
        return {"dropped_role": role}

    def issue_key(self, role: str, label: str = "") -> dict:
        """Issue a key for a role. The secret is returned only here; the site keeps just its hash."""
        self._need_role(role)
        secret = "ais_key_" + secrets.token_urlsafe(24)
        key_id = "key_" + secrets.token_hex(6)
        now = self.now_z()
        self.store.x("INSERT INTO keys (id, hash, role, label, created) VALUES (?,?,?,?,?)",
                     (key_id, _hash_key(secret), role, label or "", now))
        return {"id": key_id, "role": role, "label": label or "", "created": now, "secret": secret,
                "note": "Keep this key safe; it is stored only as a hash and cannot be shown again."}

    def revoke_key(self, id: str) -> dict:
        """Revoke a role key. It stops working on its next request."""
        with self.store.tx():
            row = self.store.one("SELECT * FROM keys WHERE id=?", (id,))
            if not row:
                raise not_found(f"Key '{id}'", "`list_keys` shows every key's id.")
            revoked = row["revoked"] or self.now_z()
            self.store.x("UPDATE keys SET revoked=? WHERE id=?", (revoked, id))
        return {"id": id, "role": row["role"], "revoked": revoked}

    def list_keys(self) -> dict:
        """Roles and keys, without any secret or hash."""
        roles = [{"role": r["name"], "description": r["description"], "grants": json.loads(r["grants"])}
                 for r in self.store.q("SELECT * FROM roles ORDER BY name")]
        keys = [{k: r[k] for k in ("id", "label", "role", "created", "revoked")}
                for r in self.store.q("SELECT * FROM keys ORDER BY created, id")]
        return {"roles": roles, "keys": keys}

    def _prune_grants(self, target: str, field: Optional[str] = None) -> None:
        """Forget grants on a dropped structure or removed field, so a later namesake does not inherit them."""
        for r in self.store.q("SELECT name, grants FROM roles"):
            kept, dirty = [], False
            for g in json.loads(r["grants"]):
                if g.get("target") != target:
                    kept.append(g)
                elif field is None:
                    dirty = True
                elif field in g.get("fields", []):
                    dirty = True
                    rest = [f for f in g["fields"] if f != field]
                    if rest:  # a grant left with no fields would read as "every field", so it goes
                        kept.append({**g, "fields": rest})
                else:
                    kept.append(g)
            if dirty:
                self.store.x("UPDATE roles SET grants=? WHERE name=?", (json.dumps(kept), r["name"]))

    def role_grants(self, role: str, key_id: str = "") -> list:
        """The current grants of a role, checked on every call so changes and revocations apply at once."""
        if key_id:
            row = self.store.one("SELECT revoked FROM keys WHERE id=?", (key_id,))
            if not row or row["revoked"]:
                raise SiteError("bad_key", "That key has been revoked.",
                                "Ask the site owner for a new key.", status=401)
        row = self.store.one("SELECT grants FROM roles WHERE name=?", (role,))
        return json.loads(row["grants"]) if row else []

    def scheduled_fields(self, name: str, key: str, schedule: int) -> Optional[list]:
        row = self.store.one("SELECT set_json FROM schedules WHERE id=? AND collection=? AND key=?",
                             (schedule, name, str(key)))
        return list(json.loads(row["set_json"])) if row else None

    def write_audit(self, who: str, role: str, op: str, target: Optional[str], key: Optional[str],
                    detail: dict, ok: bool) -> None:
        self.store.x("INSERT INTO audit (time, who, role, op, target, key, detail, ok) VALUES (?,?,?,?,?,?,?,?)",
                     (self.now_z(), who, role, op, target, key, json.dumps(detail, sort_keys=True), int(ok)))

    def audit(self, who: Optional[str] = None, target: Optional[str] = None, limit: int = 100,
              cursor: Optional[str] = None) -> dict:
        """The private audit trail, newest first.

        It records calls made through `ops.run` (every door). Calling `Site` methods directly from Python
        bypasses `ops.run` and is not audited.
        """
        try:
            limit = max(1, min(1000, int(limit)))
            before = int(cursor) if cursor else None
        except (TypeError, ValueError):
            raise SiteError("bad_page", "limit and cursor must be whole numbers.")
        sql, params = "SELECT * FROM audit WHERE 1=1", []
        for col, value in (("who", who), ("target", target)):
            if value is not None:
                sql += f" AND {col}=?"
                params.append(value)
        if before is not None:
            sql += " AND id < ?"
            params.append(before)
        rows = self.store.q(sql + " ORDER BY id DESC LIMIT ?", tuple(params + [limit + 1]))
        page = rows[:limit]
        out = {"entries": [{"id": r["id"], "time": r["time"], "who": r["who"], "role": r["role"], "op": r["op"],
                            "target": r["target"], "key": r["key"], "detail": json.loads(r["detail"]),
                            "ok": bool(r["ok"])} for r in page], "as_of": self.now_z()}
        if len(rows) > limit:
            out["next"] = str(page[-1]["id"])
        return out


register(
    Op("set_role", "Create or replace a role: a name and grants limiting it to certain structures, operations "
                   "and fields. Takes effect at once.",
       lambda site, role, grants, description="": site.set_role(role, grants, description),
       {"role": {"type": "string", "description": "Role name, e.g. pricing."},
        "grants": {"type": "array", "description": 'Grants, each {"target": <structure name or "*">, "ops": '
                                                   '[operation names], "fields": [optional: only these]}.'},
        "description": _S},
       ("role", "grants")),
    Op("drop_role", "Delete a role. Fails while unrevoked keys still use it.",
       lambda site, role: site.drop_role(role), {"role": {"type": "string"}}, ("role",)),
    Op("issue_key", "Issue a key for a role. The secret is shown only in this reply.",
       lambda site, role, label="": site.issue_key(role, label),
       {"role": {"type": "string"}, "label": {"type": "string", "description": "Who or what the key is for."}},
       ("role",)),
    Op("revoke_key", "Revoke a role key by its id. It stops working at once.",
       lambda site, id: site.revoke_key(id),
       {"id": {"type": "string", "description": "Key id, from list_keys."}}, ("id",)),
    Op("list_keys", "List roles with their grants, and keys with id, label, role, created and revoked.",
       lambda site: site.list_keys(), {}, mutates=False),
    Op("audit", "Read the private audit trail of changes, newest first: who made each and whether it was "
                "allowed.",
       lambda site, **kw: site.audit(**kw),
       {"who": {"type": "string", "description": "A key id, owner or local."},
        "target": {"type": "string", "description": "Only entries for this structure."},
        "limit": _I, "cursor": _S},
       mutates=False),
)
OPS["set_role"].positional, OPS["set_role"].flags = ('role',), {'grants': '--grant'}
OPS["drop_role"].positional, OPS["drop_role"].flags = ('role',), {}
OPS["issue_key"].positional, OPS["issue_key"].flags = ('role',), {}
OPS["revoke_key"].positional, OPS["revoke_key"].flags = ('id',), {}
OPS["list_keys"].positional, OPS["list_keys"].flags = (), {}
OPS["audit"].positional, OPS["audit"].flags = (), {}
