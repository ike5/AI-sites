"""The operation table: one definition of every operation, shared by all four doors.

The Python handles, the command line, the admin web API and the MCP tools all call `run`, so an
operation has the same name, arguments, rules and result wherever it is invoked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .errors import SiteError
from .site import Site


@dataclass(frozen=True)
class Principal:
    """Who is calling. Milestone 1 knows the owner and everyone else; roles and identities come later."""
    kind: str = "anonymous"
    id: str = ""


OWNER = Principal("owner")
ANONYMOUS = Principal("anonymous")

_S, _I, _B, _O, _A = ({"type": t} for t in ("string", "integer", "boolean", "object", "array"))
_TARGET = {"type": "string", "description": "Name of the structure (collection or tree)."}
_KEY = {"type": "string", "description": "Record key, or node id for a tree."}
_SET = {"type": "object", "description": "Fields to change and their new values. null clears a field."}
_WHEN = {"description": "YYYY-MM-DD (midnight in the site's zone), YYYY-MM-DDTHH:MM (site's zone), a time "
                        "with an offset, or today / tomorrow."}


@dataclass
class Op:
    name: str
    summary: str
    fn: Callable[..., Any]
    params: dict = field(default_factory=dict)
    required: tuple = ()
    mutates: bool = True

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required),
                "additionalProperties": False}


def _list(site: Site, target: str, where=None, sort=None, limit=100, cursor=None, fields=None,
          node=None, depth=None):
    if site.describe(target)["type"] == "tree":
        return site.read_tree(target, node, depth)
    return site.list(target, where, sort, limit, cursor, fields)


OPS = {op.name: op for op in [
    Op("describe", "Show the site's settings, structures and available kinds, or one structure's definition.",
       lambda site, target=None: site.describe(target), {"target": _TARGET}, mutates=False),
    Op("configure", "Change site settings: name, description, timezone (region name), language, currency.",
       lambda site, **kw: site.configure(**kw),
       {"name": _S, "description": _S, "timezone": _S, "language": _S, "currency": _S}),
    Op("define", "Create a structure: a collection (optionally of a built-in kind) or a tree.",
       lambda site, target, **kw: site.define(target, **kw),
       {"target": _TARGET,
        "type": {"type": "string", "enum": ["collection", "tree"]},
        "kind": {"type": "string", "description": "A built-in kind such as Product, Hours or Notice; "
                                                  "`describe` lists them."},
        "key": {"type": "string", "description": "Name of the key field; defaults to the kind's, or 'id'."},
        "access": {"type": "string", "enum": ["public", "invite", "private"]},
        "description": _S,
        "fields": {"type": "array", "description": "Extra fields, each an object or shorthand "
                                                   "'name[!]:type[[]][:policy]', e.g. 'stock:integer:live'."},
        "refs": {"type": "object", "description": "Turn fields into choices: {field: structure name}."}},
       ("target",)),
    Op("alter", "Change a structure: add or remove fields, change access or description, set references.",
       lambda site, target, **kw: site.alter(target, **kw),
       {"target": _TARGET, "add": _A, "remove": _A,
        "access": {"type": "string", "enum": ["public", "invite", "private"]},
        "description": _S, "refs": _O},
       ("target",)),
    Op("drop", "Delete a structure and everything in it.",
       lambda site, target: site.drop(target), {"target": _TARGET}, ("target",)),
    Op("add", "Add a record to a collection, or a node to a tree. Fails if the key already exists.",
       lambda site, target, data, parent=None, position=None: site.add(target, data, parent, position),
       {"target": _TARGET,
        "data": {"description": "Field values for a record, or {id, label} for a tree node."},
        "parent": {"type": "string", "description": "Tree only: id of the parent node."},
        "position": {"type": "integer", "description": "Tree only: index among siblings."}},
       ("target", "data")),
    Op("get", "Read one record (with scheduled changes and visibility), or a tree node with its subtree.",
       lambda site, target, key: site.get(target, key), {"target": _TARGET, "key": _KEY},
       ("target", "key"), mutates=False),
    Op("list", "List records with filters, sorting and paging, or read a tree.",
       _list,
       {"target": _TARGET,
        "where": {"type": "object", "description": 'Filters, e.g. {"region": "MX", "price.lt": 50, '
                                                   '"start.gte": "today"}. Operators: .gte .gt .lte .lt .ne'},
        "sort": {"type": "string", "description": "Field name, or -field for descending."},
        "limit": _I, "cursor": _S, "fields": _A,
        "node": {"type": "string", "description": "Tree only: read the subtree under this node."},
        "depth": {"type": "integer", "description": "Tree only: how many levels to include."}},
       ("target",), mutates=False),
    Op("change", "Change fields of an existing record or tree node now.",
       lambda site, target, key, set: site.change(target, key, set),
       {"target": _TARGET, "key": _KEY, "set": _SET}, ("target", "key", "set")),
    Op("remove", "Remove a record or tree node. Fails if something still refers to it.",
       lambda site, target, key, recursive=False: site.remove(target, key, recursive),
       {"target": _TARGET, "key": _KEY,
        "recursive": {"type": "boolean", "description": "Tree only: also remove the nodes underneath."}},
       ("target", "key")),
    Op("move", "Move a tree node under another parent and/or to a position among its siblings.",
       lambda site, target, key, parent=None, position=None: site.move(target, key, parent, position),
       {"target": _TARGET, "key": _KEY,
        "parent": {"description": "Id of the new parent; null or omitted for the top level."},
        "position": _I},
       ("target", "key")),
    Op("schedule", "Plan a change that starts, and optionally reverts, by itself. Hidden from visitors until "
                   "it starts unless announce is true.",
       lambda site, target, key, set, **kw: site.schedule(target, key, set, **kw),
       {"target": _TARGET, "key": _KEY, "set": _SET,
        "start": _WHEN, "end": {**_WHEN, "description": "When the change reverts. Omit for a permanent change. "
                                                        + _WHEN["description"]},
        "on": {"description": "Shorthand for one whole day: YYYY-MM-DD, today or tomorrow."},
        "announce": {"type": "boolean", "description": "Show the planned change to visitors in advance."}},
       ("target", "key", "set")),
    Op("unschedule", "Cancel a scheduled change by its id (shown by `get`).",
       lambda site, target, key, schedule: site.unschedule(target, key, schedule),
       {"target": _TARGET, "key": _KEY, "schedule": _I}, ("target", "key", "schedule")),
    Op("confirm", "Record that a record is still true as of now, without changing it.",
       lambda site, target, key: site.confirm(target, key), {"target": _TARGET, "key": _KEY},
       ("target", "key")),
    Op("redact", "Remove a past value from the public history (for example after a takedown request). The "
                 "chain still verifies. Refused while the value is still current.",
       lambda site, seq, fields=None, reason="": site.redact(seq, fields, reason),
       {"seq": {"type": "integer", "description": "Sequence number of the history entry."},
        "fields": {"type": "array", "description": "Fields to redact; omit for every field the entry still "
                                                   "has. Omit for a sealed entry."},
        "reason": {"type": "string", "description": "Optional public note, one line, up to 200 characters."}},
       ("seq",)),
    Op("hours", "Open or closed on a date, as visitors would be told.",
       lambda site, date=None, key=None: site.hours(date, key, owner=True),
       {"date": {"description": "YYYY-MM-DD, today or tomorrow. Defaults to today."},
        "key": {"type": "string", "description": "One hours record; omit for all."}},
       mutates=False),
    Op("checkpoint", "Sign the current history head, store it, and send it to the site's anchors. Returns "
                     "the signed checkpoint with each anchor's proof.",
       lambda site: site.checkpoint()),
]}

_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}


def describe_ops() -> list:
    return [{"op": op.name, "summary": op.summary, "changes_site": op.mutates, "arguments": op.schema()}
            for op in OPS.values()]


def run(site: Site, principal: Principal, op: str, args: Optional[dict] = None) -> Any:
    """Run one operation for a caller. Every door ends up here."""
    spec = OPS.get(op)
    if spec is None:
        raise SiteError("unknown_operation", f"'{op}' is not an operation.",
                        "Operations: " + ", ".join(OPS) + ".", status=404)
    if principal.kind != "owner":
        raise SiteError("forbidden", "This operation needs the site owner's key.",
                        "Send it as: Authorization: Bearer <owner key>", status=403)
    args = {k: v for k, v in (args or {}).items() if v is not None or k in ("parent",)}
    unknown = sorted(set(args) - set(spec.params))
    if unknown:
        raise SiteError("invalid", f"`{op}` does not take: {', '.join(unknown)}.",
                        "It takes: " + ", ".join(spec.params) + ".")
    missing = [p for p in spec.required if p not in args]
    if missing:
        raise SiteError("invalid", f"`{op}` needs: {', '.join(missing)}.",
                        "It takes: " + ", ".join(spec.params) + ".")
    for name, value in args.items():
        want = _TYPES.get(spec.params[name].get("type", ""))
        if want and value is not None and (not isinstance(value, want) or (want is int and isinstance(value, bool))):
            raise SiteError("invalid", f"`{name}` must be {spec.params[name]['type']}.")
    return spec.fn(site, **args)
