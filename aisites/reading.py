"""Reading across records: matching by words, and following the references between structures.

A site's records form a small graph. A field that names a record in another structure is a link
(`refs`, kept on the field as `choices`). One read can match records by words, filter on a linked
record's field, and bring linked records back attached, so a question that touches several structures
takes one request instead of one per structure.

Everything attached is built by the same view as a direct read, for the same reader, so nothing is
shown here that the reader could not have fetched directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Optional

from . import schema
from .errors import SiteError
from .schema import FieldSpec, StructureDef

MAX_STEPS = 3    # how far from the record asked about a link may lead
MAX_PATHS = 5    # how many separate chains of links one read may follow
MAX_BACK = 20    # records attached per parent when the link points back at it
OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "has")
TEXT = ("string", "text")
LABELS = ("name", "title", "label")
EVIDENCE = ("updated", "confirmed", "live_as_of", "next_change")
_SPLIT = re.compile(r"[\W_]+")


def tag_of(payload: Any) -> str:
    """A validator made from the answer itself, so it changes exactly when the answer does.

    Counters cannot do this: an answer such as "open now" changes with the clock and with no write at all.
    The time the answer was produced (`as_of`) is left out, or no two answers would ever match.
    """
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items() if k != "as_of"}
    body = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                                               default=str)
    return 'W/"' + hashlib.sha256(body.encode()).hexdigest()[:20] + '"'


# ---------------------------------------------------------------- words

def words(text: Any) -> list:
    """Lower-case words with accents removed, so "Ibuprofeno" and "ibuprofeno" are the same word."""
    flat = unicodedata.normalize("NFKD", str(text)).casefold()
    flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
    return [w for w in _SPLIT.split(flat) if w]


def has(text_words: list, wanted: list) -> bool:
    """Every wanted word begins some word of the text: "ibuprofen" finds "ibuprofeno"."""
    return bool(wanted) and all(any(w.startswith(q) for w in text_words) for q in wanted)


def _strings(value: Any) -> list:
    return [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)]


def label_of(sdef: StructureDef, view: dict) -> str:
    """What to call a record when listing it: its name or title, else its first text, else its key."""
    data = view["data"]
    for name in LABELS:
        if isinstance(data.get(name), str) and data[name]:
            return data[name]
    for f in sdef.fields:
        if (f.type in TEXT and not f.many and not f.choices and f.name != sdef.key
                and isinstance(data.get(f.name), str) and data[f.name]):
            return data[f.name]
    return view["key"]


def text_words(sdef: StructureDef, view: dict, labels_only: bool = False) -> list:
    """The words of a record that a reader can see. `view` already has hidden fields removed."""
    out = words(view["key"])
    for f in sdef.fields:
        if f.type in TEXT and (not labels_only or f.name in LABELS or f.name == "aliases"):
            for value in _strings(view["data"].get(f.name)):
                out += words(value)
    if labels_only:
        out += words(label_of(sdef, view))
    return out


def match(f: FieldSpec, op: str, want: Any, value: Any, tz: Any) -> bool:
    if op == "has":
        return has([w for v in _strings(value) for w in words(v)], want)
    if value is None:
        return op == "ne"
    if f.many:
        got = [schema.comparable(FieldSpec(name=f.name, type=f.type), v, tz) for v in value]
        return (want in got) if op == "eq" else (want not in got) if op == "ne" else False
    got = schema.comparable(f, value, tz)
    return {"eq": got == want, "ne": got != want, "gt": got > want, "gte": got >= want,
            "lt": got < want, "lte": got <= want}[op]


# ---------------------------------------------------------------- one read

def _no_link(sdef: StructureDef, hop: str, choices: list) -> SiteError:
    return SiteError("bad_with", f"'{hop}' is not something that can be followed from '{sdef.name}'.",
                     ("From here you can follow: " + ", ".join(choices) + ".") if choices else
                     f"'{sdef.name}' has no references to follow.")


class Reading:
    """One read's view of the site: who is looking, with each structure's visible records loaded once."""

    def __init__(self, site: Any, owner: bool):
        self.site, self.owner = site, owner
        # Someone working under a role has grants on named structures only, so what is linked to the
        # structure they asked about is shown to them as it would be to a visitor.
        self.full = owner and getattr(site._viewer, "limit", None) is None
        self.now_dt, self.now = site.now(), site.now_z()
        self.defs = site._defs()
        self._views: dict = {}
        self._links: dict = {}
        self._hours: Optional[dict] = None

    # -- what this reader may reach

    def may_read(self, sdef: Optional[StructureDef]) -> bool:
        if sdef is None or sdef.type == "form":
            return False
        return (self.full or sdef.access == "public"
                or (sdef.access == "invite" and self.site._reader_may(sdef.name)))

    def hidden(self, sdef: StructureDef, root: bool) -> frozenset:
        return self.site._hidden_from(sdef, self.owner if root else self.full)

    def views(self, sdef: StructureDef) -> dict:
        """key -> view for every record of a linked collection this reader can see."""
        if sdef.name not in self._views:
            self._views[sdef.name] = self.site._views(sdef, self.full, self.now)
        return self._views[sdef.name]

    def links(self, sdef: StructureDef, root: bool) -> dict:
        """Every link out of a structure: its own reference fields, and collections that refer back to it."""
        if (sdef.name, root) not in self._links:
            self._links[(sdef.name, root)] = self._find_links(sdef, root)
        return self._links[(sdef.name, root)]

    def _find_links(self, sdef: StructureDef, root: bool) -> dict:
        hide = self.hidden(sdef, root)
        out: dict = {}
        for f in sdef.fields:
            if f.choices and f.name not in hide:
                ttype, tname = next(iter(f.choices.items()))
                target = self.defs.get(tname)
                if self.may_read(target):
                    out[f.name] = {"how": "forward", "sdef": target, "many": f.many}
        for other in self.defs.values():
            if other.type != "collection" or other.name in out or not self.may_read(other):
                continue
            private = self.site._hidden_from(other, self.full)
            via = [f.name for f in other.fields
                   if f.choices == {"collection": sdef.name} and f.name not in private]
            if via:
                out[other.name] = {"how": "back", "sdef": other, "via": via}
        return out

    # -- filters

    def condition(self, sdef: StructureDef, key: str, raw: Any) -> tuple:
        """Check one filter such as price.lt, name.has or place.city, and prepare it for matching."""
        chain, f, op = self.resolve(sdef, key)
        if op == "has":
            want: Any = words(raw)
            if not want:
                raise SiteError("bad_filter", f"'{key}' needs at least one word to look for.")
        else:
            want = schema.filter_value(f, raw, self.site.tz, self.now_dt)
        return chain, f, op, want

    def resolve(self, sdef: StructureDef, key: str) -> tuple:
        """What a filter name refers to: the references it passes through, the field, and the operator."""
        parts = key.split(".")
        op = parts.pop() if len(parts) > 1 and parts[-1] in OPS else "eq"
        *path, fname = parts
        if len(path) > MAX_STEPS - 1:
            raise SiteError("bad_filter", f"'{key}' reaches too far.",
                            f"A filter may pass through at most {MAX_STEPS - 1} references.")
        current, root, chain = sdef, True, []
        for hop in path:
            link = self.links(current, root).get(hop)
            if link is None or link["how"] != "forward" or link["sdef"].type != "collection":
                # A field this reader cannot see is answered exactly like one that does not exist.
                if not self._shows(current, root, hop):
                    raise self._no_field(current, root, hop)
                if len(path) == 1:
                    raise SiteError("bad_filter", f"'.{fname}' is not a filter operator.",
                                    "Use field=value or field.gte, .gt, .lte, .lt, .ne, .has.")
                raise SiteError("bad_filter", f"'{hop}' does not refer to another collection.",
                                "A filter can pass through a field that refers to another collection.")
            chain.append((hop, link["sdef"]))
            current, root = link["sdef"], False
        f = current.field(fname)
        if f is None or not self._shows(current, root, fname):
            raise self._no_field(current, root, fname)
        if op == "has" and f.type not in TEXT:
            raise SiteError("bad_filter", f"'.has' works on text, and '{fname}' is {f.type}.",
                            "Use field=value or field.gte, .gt, .lte, .lt, .ne.")
        return chain, f, op

    def _shows(self, sdef: StructureDef, root: bool, name: str) -> bool:
        f = sdef.field(name)
        owner = self.owner if root else self.full
        return f is not None and name not in self.hidden(sdef, root) and (owner or f.policy != "private")

    def _no_field(self, sdef: StructureDef, root: bool, name: str) -> SiteError:
        shown = [f.name for f in sdef.fields if self._shows(sdef, root, f.name)]
        return SiteError("bad_filter", f"'{name}' is not a field of '{sdef.name}'.",
                         "Fields: " + ", ".join(shown) + ".")

    def test(self, cond: tuple, view: dict, tz: Any) -> bool:
        chain, f, op, want = cond
        found = [(view, tz)]
        for hop, target in chain:
            nxt = []
            for v, _ in found:
                for k in _strings(v["data"].get(hop)):
                    linked = self.views(target).get(k)
                    if linked is not None:
                        nxt.append((linked, schema.record_zone(target, linked["data"], self.site.tz)))
            found = nxt
        return any(match(f, op, want, v["data"].get(f.name), z) for v, z in found)

    def says(self, sdef: StructureDef, view: dict, wanted: list) -> bool:
        """The record's own words, or the name of something it refers to, hold every wanted word."""
        bag = text_words(sdef, view)
        for name, link in self.links(sdef, True).items():
            if link["how"] != "forward":
                continue
            for k in _strings(view["data"].get(name)):
                if link["sdef"].type == "tree":
                    node = self.site.store.one("SELECT label FROM nodes WHERE tree=? AND id=?",
                                               (link["sdef"].name, k))
                    bag += words(node["label"]) if node else []
                else:
                    linked = self.views(link["sdef"]).get(k)
                    bag += text_words(link["sdef"], linked, labels_only=True) if linked else []
        return has(bag, wanted)

    # -- what to attach

    def plan(self, sdef: StructureDef, follow: Any, fields: Optional[list]) -> tuple:
        """Turn `with` paths and dotted field names into (fields of the record itself, tree of links)."""
        if isinstance(follow, str):
            follow = follow.split(",")
        paths = [[p for p in str(path).strip().split(".") if p] for path in (follow or []) if str(path).strip()]
        own: list = []
        chosen: list = []
        for name in fields or []:
            *path, fname = str(name).split(".")
            if path:
                chosen.append((path, fname))
                paths.append(path)
            else:
                own.append(fname)
        tree: dict = {}
        for path in paths:
            if len(path) > MAX_STEPS:
                raise SiteError("bad_with", f"'{'.'.join(path)}' reaches too far.",
                                f"A read may follow at most {MAX_STEPS} references in a row.")
            current, root, level = sdef, True, tree
            for hop in path:
                if hop not in level:
                    links = self.links(current, root)
                    if hop not in links:
                        raise _no_link(current, hop, sorted(links))
                    level[hop] = {**links[hop], "fields": None, "next": {}}
                node = level[hop]
                current, root, level = node["sdef"], False, node["next"]
        if _leaves(tree) > MAX_PATHS:
            raise SiteError("bad_with", "That read follows too many references at once.",
                            f"Follow at most {MAX_PATHS} chains of references in one read.")
        for path, fname in chosen:
            node = tree[path[0]]
            for hop in path[1:]:
                node = node["next"][hop]
            target = node["sdef"]
            if target.type == "collection" and (target.field(fname) is None or fname in self.hidden(target, False)):
                raise SiteError("bad_fields", f"'{fname}' is not a field of '{target.name}'.")
            node["fields"] = (node["fields"] or []) + [fname]
        return own or None, tree

    def attach(self, view: dict, tree: dict) -> None:
        """Add a `with` block to a view: the records it links to, each trimmed to what was asked for."""
        if not tree:
            return
        out: dict = {}
        for hop, node in tree.items():
            target = node["sdef"]
            if node["how"] == "forward":
                items = [i for i in (self._item(target, k, node) for k in _strings(view["data"].get(hop))) if i]
                out[hop] = items if node["many"] else (items[0] if items else None)
            else:
                found = [v for v in self.views(target).values()
                         if any(view["key"] in _strings(v["data"].get(f)) for f in node["via"])]
                out[hop] = [self._item(target, v["key"], node) for v in found[:MAX_BACK]]
                if len(found) > MAX_BACK:
                    out[hop + "_more"] = len(found) - MAX_BACK
        view["with"] = out

    def _item(self, target: StructureDef, key: str, node: dict) -> Optional[dict]:
        if target.type == "tree":
            row = self.site.store.one("SELECT id, label FROM nodes WHERE tree=? AND id=?", (target.name, key))
            return {"key": row["id"], "data": {"label": row["label"]}} if row else None
        view = self.views(target).get(key)
        if view is None:
            return None
        if target.kind == "Hours":
            # The answer for today, not the weekly pattern: the reader should not have to apply the rules.
            answer = self.hours().get((target.name, key)) or {}
            out = {"key": key, **{k: v for k, v in answer.items()
                                  if k not in ("key", "collection", "place", "weekday")
                                  and not (k == "source" and v == "weekly")}}
        else:
            data = view["data"]
            out = {"key": key, "data": {k: v for k, v in data.items() if node["fields"] is None or k in node["fields"]}}
            out.update({k: view[k] for k in EVIDENCE if k in view})
            if "live_as_of" in out and not any(n in out["data"] for n in target.live_names()):
                del out["live_as_of"]
        if node["next"]:
            nested = {"key": key, "data": view["data"]}
            self.attach(nested, node["next"])
            out["with"] = nested["with"]
        return out

    def hours(self) -> dict:
        if self._hours is None:
            self._hours = {(r["collection"], r["key"]): r
                           for r in self.site.hours(None, None, self.full)["results"]}
        return self._hours


def _leaves(tree: dict) -> int:
    return sum(_leaves(node["next"]) or 1 for node in tree.values())


# ---------------------------------------------------------------- compact rows

def table(out: dict, sdef: StructureDef, own: list, dotted: list) -> dict:
    """Rows instead of objects. Keys and the times that say how fresh a row is are kept.

    A time shared by every row is stated once beside the rows, not repeated in each.
    """
    items = out["items"]
    columns = ([] if sdef.key in own else ["key"]) + own + dotted
    rows = []
    for item in items:
        row = [] if sdef.key in own else [item["key"]]
        row += [item["data"].get(c) for c in own]
        for path in dotted:
            value: Any = item
            for hop in path.split(".")[:-1]:
                value = (value.get("with") or {}).get(hop) if isinstance(value, dict) else None
                if isinstance(value, list):
                    raise SiteError("bad_format", f"format=table cannot show '{path}': it can hold several records.",
                                    "Leave it out of fields, or read without format=table.")
            row.append(((value or {}).get("data") or {}).get(path.split(".")[-1]) if isinstance(value, dict) else None)
        rows.append(row)
    # A column with nothing in it tells the reader nothing.
    empty = [i for i, c in enumerate(columns) if c != "key" and all(row[i] is None for row in rows)] if rows else []
    columns = [c for i, c in enumerate(columns) if i not in empty]
    rows = [[v for i, v in enumerate(row) if i not in empty] for row in rows]
    result = {"collection": out["collection"], "columns": columns, "rows": rows}
    for stamp in ("updated", "live_as_of"):
        values = [item.get(stamp) for item in items]
        if not any(values):
            continue
        if len(set(values)) == 1:
            result[stamp] = values[0]
        else:
            result["columns"] = result["columns"] + [stamp]
            for row, value in zip(rows, values):
                row.append(value)
    result.update({k: out[k] for k in ("total", "next", "as_of") if k in out})
    return result
