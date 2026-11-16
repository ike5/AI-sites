"""The operation table: one definition of every operation, shared by all four doors.

The Python handles, the command line, the admin web API and the MCP tools all call `run`, so an
operation has the same name, arguments, rules and result wherever it is invoked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from .errors import SiteError
if TYPE_CHECKING:
    from .site import Site

# The only operations a staff role can be granted (plus `describe` of a structure it has a grant on).
ROLE_OPS = ("add", "get", "list", "change", "remove", "move", "schedule", "unschedule", "confirm")


@dataclass(frozen=True)
class Principal:
    """Who is calling: kind "owner", "role" or "anonymous".

    `id` is what the audit trail records: "owner", "local" (a door working on the site folder directly),
    a role key's id, or a contact's id. `role` is the role name of a role key or contact. Grants are not
    carried here but read on each call, so a changed role or a revoked key applies at once. `token` names
    the token a contact is acting through, which may allow less than the contact's role does.
    """
    kind: str = "anonymous"
    id: str = ""
    role: str = ""
    token: str = ""


OWNER = Principal("owner", "owner")
LOCAL = Principal("owner", "local")
ANONYMOUS = Principal("anonymous", "anonymous")

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
    # Command line: arguments given by position, where name=value pairs go ("*" means they are the
    # operation's own arguments), and friendlier flag names for some arguments.
    positional: tuple = ()
    pairs: Optional[str] = None
    flags: dict = field(default_factory=dict)
    # A visitor operation is reached through /actions by guests, not through the admin door.
    visitor: bool = False
    # The structure setting that switches a visitor operation on, e.g. "booking".
    setting: Optional[str] = None
    # A staff operation a role may be granted on a target, beyond the record operations in ROLE_OPS.
    grantable: bool = False

    def schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": list(self.required),
                "additionalProperties": False}


def _list(site: Site, target: str, where=None, sort=None, limit=100, cursor=None, fields=None,
          node=None, depth=None, q=None, **more):
    if site.describe(target)["type"] == "tree":
        return site.read_tree(target, node, depth)
    return site.list(target, where, sort, limit, cursor, fields, q=q, follow=more.get("with"))


_WITH = {"type": "array", "description": 'References to attach, e.g. ["place", "place.hours"]. A collection '
                                         "that refers back to these records can be attached by its name."}


OPS: dict = {}


def register(*new: Op) -> None:
    """Add operations to the table. Each module registers the operations it implements."""
    for op in new:
        if op.name in OPS:
            raise ValueError(f"operation '{op.name}' is already registered")
        OPS[op.name] = op


register(
    Op("describe", "Show the site's settings and structures in short form, or one structure's full definition.",
       lambda site, target=None, full=False: site.describe(target, full),
       {"target": _TARGET,
        "full": {"type": "boolean", "description": "With no target: every field definition and the fields of "
                                                   "each built-in kind. Several times larger."}},
       mutates=False),
    Op("configure", "Change site settings: name, description, timezone (region name), language, currency.",
       lambda site, **kw: site.configure(**kw),
       {"name": _S, "description": _S, "timezone": _S, "language": _S, "currency": _S,
        "url": {"type": "string", "description": "The site's full public address, used in the links it gives "
                                                 "out. Needed only when it cannot be told from the request."},
        "listed": {"description": "true or false: whether a host may include this site when one question is "
                                  "put to every site it serves. Default true."},
        "search": {"description": "true or false: whether search engines may index this site to answer and "
                                  "cite it. Default true."},
        "training": {"description": "true or false: whether this site may be collected to train models. "
                                    "Default true."}}),
    Op("define", "Create a structure: a collection (optionally of a built-in kind) or a tree.",
       lambda site, target, **kw: site.define(target, **kw),
       {"target": _TARGET,
        "type": {"type": "string", "enum": ["collection", "tree", "form"]},
        "kind": {"type": "string", "description": "A built-in kind such as Product, Hours or Notice; "
                                                  "`describe` lists them."},
        "key": {"type": "string", "description": "Name of the key field; defaults to the kind's, or 'id'."},
        "access": {"type": "string", "enum": ["public", "invite", "private"]},
        "description": _S,
        "fields": {"type": "array", "description": "Extra fields, each an object or shorthand "
                                                   "'name[!]:type[[]][:policy]', e.g. 'stock:integer:live'."},
        "refs": {"type": "object", "description": "Turn fields into choices: {field: structure name}."},
        "settings": {"type": "object", "description": "Feature settings by name, e.g. {\"booking\": {...}}. null removes one."}},
       ("target",)),
    Op("alter", "Change a structure: add or remove fields, change access or description, set references.",
       lambda site, target, **kw: site.alter(target, **kw),
       {"target": _TARGET, "add": _A, "remove": _A,
        "access": {"type": "string", "enum": ["public", "invite", "private"]},
        "description": _S, "refs": _O,
        "settings": {"type": "object", "description": "Feature settings by name, e.g. {\"booking\": {...}}. null removes one."}},
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
       lambda site, target, key, **more: site.get(target, key, more.get("with")),
       {"target": _TARGET, "key": _KEY, "with": _WITH},
       ("target", "key"), mutates=False),
    Op("list", "List records with filters, sorting and paging, or read a tree.",
       _list,
       {"target": _TARGET,
        "where": {"type": "object", "description": 'Filters, e.g. {"region": "MX", "price.lt": 50, '
                                                   '"start.gte": "today", "place.city": "Madrid"}. '
                                                   'Operators: .gte .gt .lte .lt .ne .has'},
        "q": {"type": "string", "description": "Words to find in the record or in the names of what it refers to."},
        "with": _WITH,
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
    Op("hours", "Open or closed on a date, as visitors would be told.",
       lambda site, date=None, key=None: site.hours(date, key, owner=True),
       {"date": {"description": "YYYY-MM-DD, today or tomorrow. Defaults to today."},
        "key": {"type": "string", "description": "One hours record; omit for all."}},
       mutates=False),
)

for _name, _positional, _pairs, _flags in [
    ("describe", ("target?",), None, {}), ("configure", (), "*", {}),
    ("define", ("target",), None, {"fields": "--field", "refs": "--ref", "settings": "--setting"}),
    ("alter", ("target",), None, {"refs": "--ref", "settings": "--setting"}), ("drop", ("target",), None, {}),
    ("add", ("target",), "data", {}), ("get", ("target", "key"), None, {}), ("list", ("target",), "where", {}),
    ("change", ("target", "key"), "set", {}), ("remove", ("target", "key"), None, {}),
    ("move", ("target", "key"), None, {}), ("schedule", ("target", "key"), "set", {}),
    ("unschedule", ("target", "key", "schedule"), None, {}), ("confirm", ("target", "key"), None, {}),
    ("hours", (), None, {}),
]:
    OPS[_name].positional, OPS[_name].pairs, OPS[_name].flags = _positional, _pairs, _flags

_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}


def describe_ops() -> list:
    return [{"op": op.name, "summary": op.summary, "changes_site": op.mutates, "arguments": op.schema()}
            for op in OPS.values()]


class Preview(Exception):
    """Raised inside a transaction to undo it and hand back what the operation would have returned."""

    def __init__(self, result: Any):
        self.result = result


def grantable() -> tuple:
    """Every operation a role can be granted: staff editing, `read` of invite-only data, visitor actions."""
    return ROLE_OPS + ("describe", "read") + tuple(n for n, o in OPS.items() if o.visitor or o.grantable)


def run(site: Site, principal: Principal, op: str, args: Optional[dict] = None, dry_run: bool = False) -> Any:
    """Run one operation for a caller. Every door ends up here.

    Each call to an operation that changes the site leaves one row in the private audit trail, whether
    it was allowed or refused. With `dry_run`, a changing operation is run and rolled back, leaving no
    trace, and the answer is wrapped as {"dry_run": true, "result": ...}.
    """
    spec = OPS.get(op)
    if spec is None:
        raise SiteError("unknown_operation", f"'{op}' is not an operation.",
                        "Operations: " + ", ".join(OPS) + ".", status=404)
    if not spec.mutates:
        return _run(site, principal, spec, args)
    if dry_run:
        try:
            with site.store.tx():
                raise Preview(_run(site, principal, spec, args))
        except Preview as p:
            return {"dry_run": True, "result": p.result}
    try:
        with site.store.tx():
            result = _run(site, principal, spec, args)
            _audit(site, principal, spec, args, result, None)
    except Exception as e:
        # The failed change was rolled back with its transaction; the refusal is recorded on its own.
        _audit(site, principal, spec, args, None, e)
        raise
    return result


def _run(site: Site, principal: Principal, spec: Op, args: Optional[dict]) -> Any:
    op = spec.name
    if principal.kind not in ("owner", "role") and not (principal.kind == "anonymous" and spec.visitor):
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
    if spec.visitor:
        # Visitor operations act for the caller, so they are told who is calling.
        if principal.kind == "anonymous":
            _check_open(site, spec, args)
        elif principal.kind == "role":
            _check_role(site, principal, op, args)
        return spec.fn(site, principal, **args)
    if op in ROLE_OPS:
        target = site._defs().get(args.get("target"))
        if target is not None and target.type == "form":
            raise SiteError("wrong_structure", f"'{target.name}' is a form; its messages are not ordinary records.",
                            "Use `inbox`, `read_message` and `reply`.")
    if principal.kind != "role":
        return spec.fn(site, **args)
    hidden = _check_role(site, principal, op, args)
    from . import identity
    if identity.is_contact(principal):
        hidden = identity.outsider(site, principal, op, args, hidden)
        args = identity.own_only(site, principal, op, args)
    with site.viewing(args.get("target"), hidden):
        result = spec.fn(site, **args)
    return _hide(op, result, hidden)


# ---------------------------------------------------------------- roles
#
# A role may use only ROLE_OPS (and `describe` of a structure it has a grant on). Any other operation,
# including ones added later, is for the owner alone.

def _forbidden(message: str, hint: str) -> SiteError:
    return SiteError("forbidden", message, hint, status=403)


def _summary(grants: list) -> str:
    return "; ".join(f"{', '.join(g['ops'])} on {'every structure' if g['target'] == '*' else repr(g['target'])}"
                     + (f" (fields: {', '.join(g['fields'])})" if "fields" in g else "") for g in grants) \
        or "nothing yet"


def _check_role(site: Site, principal: Principal, op: str, args: dict) -> set:
    """Refuse what the role's grants do not allow. Returns the private fields it may not see."""
    grants = site.grants_of(principal)
    who = f"Role '{principal.role}'"
    target = args.get("target")
    if op != "describe" and op not in ROLE_OPS and not (OPS[op].visitor or OPS[op].grantable):
        raise _forbidden(f"{who} cannot use `{op}`; only the site owner can.",
                         f"This role may: {_summary(grants)}.")
    if target is None:
        raise _forbidden(f"{who} cannot describe the whole site.",
                         f"Describe one structure it works on. This role may: {_summary(grants)}.")
    on = [g for g in grants if g["target"] in (target, "*")]
    if not on:
        raise _forbidden(f"{who} has no access to '{target}'.", f"This role may: {_summary(grants)}.")
    granted = on if op == "describe" else [g for g in on if op in g["ops"]]
    if not granted:
        raise _forbidden(f"{who} may not `{op}` in '{target}'.", f"In '{target}' it may: {_summary(on)}.")
    if OPS[op].visitor or OPS[op].grantable:
        return set()  # field limits and hidden fields concern record editing, not these operations
    if op != "describe" and all("fields" in g for g in granted):
        allowed = sorted({f for g in granted for f in g["fields"]})
        if op in ("remove", "move"):
            raise _forbidden(f"{who} may not `{op}` in '{target}': its grant there covers only some fields "
                             f"({', '.join(allowed)}) and `{op}` affects the whole record.",
                             f"In '{target}' it may: {_summary(on)}.")
        names: list = []
        if op == "add" and isinstance(args.get("data"), dict):
            names = list(args["data"])
        elif op in ("change", "schedule"):
            names = list(args["set"])
        elif op == "unschedule":
            names = site.scheduled_fields(target, args["key"], args["schedule"]) or []
        bad = sorted(set(map(str, names)) - set(allowed))
        if bad:
            what = "a scheduled change to" if op == "unschedule" else ""
            verb = {"add": "set", "unschedule": "cancel"}.get(op, op)
            plural = "fields" if len(bad) > 1 else "field"
            raise _forbidden(f"{who} may not {verb} {what + ' ' if what else ''}{plural} "
                             f"{', '.join(bad)} in '{target}'.",
                             f"In '{target}' it may {op} only: {', '.join(allowed)}.")
    defs = site._defs()
    sdef = defs.get(target)
    if sdef is not None and sdef.type == "collection" and op in ("add", "change", "schedule"):
        setting = args.get("data") if op == "add" else args.get("set")
        for name in (setting if isinstance(setting, dict) else {}):
            f = sdef.field(str(name))
            if f is None or not f.choices:
                continue
            source = next(iter(f.choices.values()))
            # Accepting or rejecting a value would reveal which keys exist in the source.
            readable = (source in defs and defs[source].access == "public") or any(
                g["target"] in (source, "*") and {"get", "list"} & set(g["ops"]) for g in grants)
            if not readable:
                raise _forbidden(f"{who} may not set '{name}' in '{target}': its values come from '{source}', "
                                 "which this role cannot read.",
                                 f"Ask the site owner to grant `get` or `list` on '{source}'.")
    if sdef is None or sdef.type != "collection" or any("fields" not in g for g in on):
        return set()
    hidden = set(sdef.names("private")) - {f for g in on for f in g["fields"]}
    if op == "list" and hidden:
        asked = [("filter by", str(k).partition(".")[0]) for k in (args.get("where") or {})]
        asked += [("sort by", str(args["sort"]).lstrip("-"))] if args.get("sort") else []
        asked += [("select", str(f)) for f in (args.get("fields") or [])]
        visible = "Fields it can see: " + ", ".join(n for n in sdef.names() if n not in hidden) + "."
        for verb, name in asked:
            if name in hidden:
                raise _forbidden(f"{who} cannot see field '{name}' of '{target}', so it cannot {verb} it.",
                                 visible)
            if not sdef.field(name):
                # Answered here so the site's own hint, which lists every field, is not shown.
                raise SiteError({"filter by": "bad_filter", "sort by": "bad_sort", "select": "bad_fields"}[verb],
                                f"'{name}' is not a field of '{target}'.", visible)
    return hidden


def _check_open(site: Site, spec: Op, args: dict) -> None:
    """A caller without a key may use a visitor operation only where the owner has opened it."""
    sdef = site._defs().get(args.get("target"))
    block = sdef.settings.get(spec.setting) if sdef is not None and sdef.access != "private" else None
    if not block or not block.get("open"):
        raise SiteError("needs_key", f"`{spec.name}` on '{args.get('target')}' needs a key from the site owner.",
                        "Send it as: Authorization: Bearer <key>", status=401)


def _hide(op: str, result: Any, hidden: set) -> Any:
    """Remove private fields a role may not see from whatever an operation returns."""
    if not hidden or not isinstance(result, dict):
        return result
    if op == "describe":
        result["fields"] = [f for f in result.get("fields", []) if f.get("name") not in hidden]
    elif op == "list":
        for item in result.get("items", []):
            _without_fields(item, hidden)
    elif op in ("schedule", "unschedule"):
        _without_fields(result.get("record"), hidden)
        if isinstance(result.get("schedule"), dict):
            result["schedule"]["set"] = {k: v for k, v in result["schedule"]["set"].items() if k not in hidden}
    elif "data" in result:
        _without_fields(result, hidden)
    return result


def _without_fields(view: dict, hidden: set) -> dict:
    """A record view with some fields removed everywhere they appear, including scheduled changes."""
    if not hidden or not isinstance(view, dict):
        return view
    view["data"] = {k: v for k, v in view.get("data", {}).items() if k not in hidden}
    if "instants" in view:
        view["instants"] = {k: v for k, v in view["instants"].items() if k not in hidden}
        if not view["instants"]:
            del view["instants"]
    for name, setkey in (("schedules", "set"), ("upcoming_changes", "set"), ("active_changes", "fields")):
        if name not in view:
            continue
        kept = []
        for s in view[name]:
            inner = s[setkey]
            trimmed = ({k: v for k, v in inner.items() if k not in hidden} if isinstance(inner, dict)
                       else [k for k in inner if k not in hidden])
            if trimmed or not inner:
                kept.append({**s, setkey: trimmed})
        if kept:
            view[name] = kept
        else:
            del view[name]
    return view


# ---------------------------------------------------------------- audit trail

def _audit(site: Site, principal: Principal, spec: Op, args: Optional[dict], result: Any,
           error: Optional[BaseException]) -> None:
    """One private row per change: who, what and where, and the names of fields set but never values."""
    args = args if isinstance(args, dict) else {}

    def plain(v: Any) -> bool:
        return isinstance(v, (str, int)) and not isinstance(v, bool)

    detail: dict = {}
    for name in ("data", "set"):
        if isinstance(args.get(name), dict):
            detail["fields"] = sorted(map(str, args[name]))
    if spec.name == "configure":
        detail["fields"] = sorted(k for k in args if k in spec.params)
    for name in ("schedule", "role", "id", "seq"):
        if plain(args.get(name)):
            detail[name] = args[name]
    if spec.name == "redact" and isinstance(args.get("fields"), list):
        detail["fields"] = sorted(map(str, args["fields"]))
    if spec.name == "issue_key" and isinstance(result, dict):
        detail["id"] = result.get("id")
    if error is not None:
        detail["error"] = getattr(error, "code", "server_error")
    role = principal.role if principal.kind == "role" else "owner" if principal.kind == "owner" else ""
    site.write_audit(principal.id or principal.kind, role, spec.name,
                     args["target"] if isinstance(args.get("target"), str) else None,
                     str(args["key"]) if plain(args.get("key")) else None, detail, error is None)
