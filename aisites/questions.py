"""Questions: the short list of things a site can be asked, each at one address.

Opened with nothing added, a question either answers straight away or lists its options as ready-made
links, the next level of a tree that any assistant able to follow a link can walk. Opened with an
argument, it answers: resolved here, with related records attached and the times needed to trust them.

Every readable structure is a question, and its built-in kind decides what an answer brings along (an
offer comes with its product, its place and that place's hours today). An owner can add questions of
their own, stored as data. All of it runs on the one read in reading.py, for the reader who is asking,
so a question never shows more than a direct read would.

Three levels of assistant read the same answers:
  1. following links only: open a question, then one of its options;
  2. filling in an address: add what the question takes;
  3. combining: several questions at once, an answer only if it changed, lean output, a size limit.
A level is shown by the form used and changes the shape of an answer, never what can be seen.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from . import kinds, ops, reading, schema
from .errors import SiteError, not_found
from .schema import StructureDef

FORMAT = 1            # version of the first response and of answers, apart from the software's version
OPTIONS_PAGE = 50
MAX_AT_ONCE = 8
MAX_BODY = 4096
MAX_INLINE = 12
CONTROL = ("limit", "cursor", "links", "format", "max", "unless")
DOES = {
    "Hours": "Open or closed on a date, with the reason and when that next changes",
    "Notice": "Announcements in force now",
    "Offer": "Price and stock of a product, with where it is and that place's hours today",
    "Product": "Products by name, with where each is offered",
    "Place": "Locations, with their hours today",
    "Slot": "Bookable time slots from today onwards",
    "Event": "Calendar entries from today onwards",
    "Page": "Pages of text such as policies, by title",
    "Business": "Who runs this site and how to reach them",
}
ORDER = tuple(DOES)
LISTED = ("Notice", "Slot", "Event")   # kinds answered as a list straight away, with no choosing first
HOW = [
    "1 Follow links: open a question's url. It answers, or lists options as links to open.",
    "2 Fill in: add what it takes, e.g. ?q=words. Fields of its records filter too, e.g. ?city=Madrid.",
    "3 Combine: /ask/a,b or POST /ask asks several at once; unless=<tag> answers only if changed; "
    "links=0, format=table and max=<bytes> trim an answer.",
]


def _off(value: Any) -> bool:
    return str(value).lower() in ("0", "false", "no", "off")


class _Asker:
    """Answers questions for one reader. `base` is the site's full address, used to build links."""

    def __init__(self, site: Any, owner: bool, base: str, links: bool = True):
        self.site, self.owner, self.base, self.links = site, owner, base, links
        self.look = reading.Reading(site, owner)

    def url(self, qid: str, **params: Any) -> str:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        return f"{self.base}/ask/{qid}" + ("?" + query if query else "")

    def option(self, qid: str, label: str, **params: Any) -> dict:
        # Without links, the value to fill in is given instead of the address that already holds it.
        return {"label": label, "url": self.url(qid, **params)} if self.links else {"label": label, **params}

    # -- which question

    def answer(self, qid: str, args: dict, fmt: Optional[str]) -> dict:
        defined = self.site.defined_questions().get(qid)
        if defined is not None:
            return self.defined(qid, defined, args, fmt)
        sdef = self.look.defs.get(qid)
        if sdef is None or sdef.type == "form" or sdef.kind == "Closure" or (sdef.access == "private" and not self.owner):
            # A kind's name stands for the site's structures of that kind, so the address is the same everywhere.
            of_kind = [d for d in self.look.defs.values() if d.kind == qid and d.type == "collection"
                       and d.kind != "Closure" and (self.owner or self.look.may_read(d))]
            if len(of_kind) == 1:
                return self.answer(of_kind[0].name, args, fmt)
            if of_kind:
                return {"answers": [{"question": d.name, **self.answer(d.name, dict(args), fmt)} for d in of_kind]}
            raise not_found(f"Question '{qid}'", f"GET {self.base}/ask lists what this site answers.")
        self.site.readable(sdef.name, self.owner)
        if sdef.type == "tree":
            return self.tree(sdef, args)
        if sdef.kind == "Hours":
            return self.hours(sdef, args)
        return self.collection(sdef, args, fmt)

    def takes(self, qid: str) -> Optional[set]:
        """Names a question accepts, for sharing arguments between several asked at once. None: unknown."""
        defined = self.site.defined_questions().get(qid)
        if defined is not None:
            return set(defined.get("takes") or [])
        sdef = self.look.defs.get(qid)
        if sdef is None:
            of_kind = [d for d in self.look.defs.values() if d.kind == qid]
            return set().union(*(self.takes(d.name) or set() for d in of_kind)) if of_kind else None
        if sdef.type == "tree":
            return {"node"}
        if sdef.kind == "Hours":
            return {"date", "key"}
        return {"q", "key"} | {f.name for f in sdef.fields} | set(self.look.links(sdef, True))

    # -- hours and trees

    def hours(self, sdef: StructureDef, args: dict) -> dict:
        date = args.get("date")
        out = self.site.hours(date, args.get("key"), self.owner)
        out["results"] = [r for r in out["results"] if r["collection"] == sdef.name]
        if date in (None, "today"):
            out["options"] = [self.option(sdef.name, "tomorrow", date="tomorrow", key=args.get("key"))]
        return out

    def tree(self, sdef: StructureDef, args: dict) -> dict:
        node = args.get("node")
        out = self.site.read_tree(sdef.name, node, 1)
        level = out["nodes"][0].get("children", []) if node is not None and out["nodes"] else out["nodes"]
        if node is None:
            out = {"tree": sdef.name, "as_of": out["as_of"]}
        options = [self.option(sdef.name, n["label"], node=n["id"]) for n in level
                   if node is None or n.get("children_count")]
        if options:
            out.update({"choose": "node", "options": options})
        return out

    # -- collections

    def collection(self, sdef: StructureDef, args: dict, fmt: Optional[str]) -> dict:
        args = dict(args)
        key, q = args.pop("key", None), args.pop("q", None)
        limit, cursor = args.pop("limit", None), args.pop("cursor", None)
        name = sdef.name
        if key is not None:
            return self.site.read_record(name, key, self.owner, self.around(sdef))
        if sdef.kind == "Offer":
            if not q and not args:
                return self.choose_product(sdef, limit, cursor)
            return self.offers(sdef, q, args, limit, cursor, fmt)
        if sdef.kind in LISTED:
            dated = sdef.kind in ("Slot", "Event")
            where = {**({"start.gte": "today"} if dated and not any(k.startswith("start") for k in args) else {}),
                     **args}
            out = self.site.list(name, where, "start" if dated else None, limit or OPTIONS_PAGE, cursor, None,
                                 self.owner, q)
            if "booking" in sdef.settings:
                out["book"] = f"{self.base}/bookable/{name}"
            return self.paged(out, name, cursor, limit, q=q, **args)
        found = self.site.list(name, args, None, limit or OPTIONS_PAGE, cursor, None, self.owner, q)
        if found["total"] == 1 and not cursor:
            return self.site.read_record(name, found["items"][0]["key"], self.owner, self.around(sdef))
        if q or args:
            # Several matches: their short fields, so the answer is often here without another request.
            long = [f.name for f in sdef.fields if f.type == "text"]
            for item in found["items"]:
                item["data"] = {k: v for k, v in item["data"].items() if k not in long}
                if self.links:
                    item["url"] = self.url(name, key=item["key"])
            return self.paged(found, name, cursor, limit, q=q, **args)
        out = {"choose": "key", "total": found["total"], "as_of": found["as_of"],
               "options": [self.option(name, reading.label_of(sdef, v), key=v["key"]) for v in found["items"]]}
        if "next" in found:
            out["next"] = found["next"]
        return self.paged(out, name, cursor, limit)

    def around(self, sdef: StructureDef) -> list:
        """What a single record is shown with: what it refers to, what refers to it, and where offers are."""
        paths = []
        for name, link in self.look.links(sdef, True).items():
            paths.append(name)
            if link["how"] == "back" and link["sdef"].kind == "Offer":
                paths += [f"{name}.{n}" for n, inner in self.look.links(link["sdef"], False).items()
                          if inner["how"] == "forward" and inner["sdef"].name != sdef.name]
        kept: list = []
        for path in paths:  # a longer path includes the shorter one it starts with
            if not any(other.startswith(path + ".") for other in paths) and len(kept) < reading.MAX_PATHS:
                kept.append(path)
        return kept

    def offers(self, sdef: StructureDef, q: Any, where: dict, limit: Any, cursor: Any, fmt: Optional[str]) -> dict:
        links = self.look.links(sdef, True)
        hide = self.look.hidden(sdef, True)
        own = [f for f in ("price", "price_currency", "stock", "region") if sdef.field(f) and f not in hide]
        follow, fields = [], list(own)
        for hop, wanted in (("product", ("name", "price", "price_currency")), ("place", ("name", "address", "city"))):
            link = links.get(hop)
            if link is None or link["how"] != "forward" or link["sdef"].type != "collection":
                fields += [hop] if sdef.field(hop) and hop not in hide else []
                continue
            follow.append(hop)
            shown = self.look.hidden(link["sdef"], False)
            fields += [f"{hop}.{f}" for f in wanted if link["sdef"].field(f) and f not in shown]
            if hop == "place":
                follow += [f"place.{n}" for n, inner in self.look.links(link["sdef"], False).items()
                           if inner["how"] == "back" and inner["sdef"].kind == "Hours"]
        out = self.site.list(sdef.name, where, None, limit or OPTIONS_PAGE, cursor, fields, self.owner, q, follow)
        if fmt == "table":
            out = reading.table(out, sdef, [f for f in fields if "." not in f], [f for f in fields if "." in f])
        return self.paged(out, sdef.name, cursor, limit, q=q, **where)

    def choose_product(self, sdef: StructureDef, limit: Any, cursor: Any) -> dict:
        offered = sorted({k for v in self.site._views(sdef, self.owner, self.look.now).values()
                          for k in reading._strings(v["data"].get("product"))})
        link = self.look.links(sdef, True).get("product")
        named = self.look.views(link["sdef"]) if link and link["how"] == "forward" and \
            link["sdef"].type == "collection" else {}
        labelled = sorted((reading.label_of(link["sdef"], named[k]) if k in named else k, k) for k in offered)
        return self.choices(sdef.name, "product", labelled, limit, cursor)

    def choices(self, qid: str, name: str, labelled: list, limit: Any, cursor: Any) -> dict:
        try:
            offset = max(0, int(cursor or 0))
            size = max(1, min(OPTIONS_PAGE, int(limit or OPTIONS_PAGE)))
        except (TypeError, ValueError):
            raise SiteError("bad_page", "limit and cursor must be whole numbers.")
        out = {"choose": name, "total": len(labelled), "as_of": self.look.now,
               "options": [self.option(qid, label, **{name: key}) for label, key in labelled[offset:offset + size]]}
        if offset + size < len(labelled):
            out["next"] = str(offset + size)
        return self.paged(out, qid, cursor, limit)

    def paged(self, out: dict, qid: str, cursor: Any, limit: Any, **params: Any) -> dict:
        """Remember how to reach the rest, as a link a reader can follow rather than a number to fill in."""
        out["_page"] = (qid, int(cursor or 0), {**params, "limit": limit})
        if "next" in out:
            out["next"] = self.url(qid, **params, limit=limit, cursor=out["next"]) if self.links else out["next"]
        return out

    # -- questions the owner defined

    def defined(self, qid: str, d: dict, args: dict, fmt: Optional[str]) -> dict:
        args = dict(args)
        sdef = self.site.readable(d["read"], self.owner)
        allowed = list(d.get("takes") or [])
        limit, cursor = args.pop("limit", None), args.pop("cursor", None)
        unknown = sorted(set(args) - set(allowed))
        if unknown:
            raise SiteError("bad_filter", f"'{qid}' does not take: {', '.join(unknown)}.",
                            ("It takes: " + ", ".join(allowed) + ".") if allowed else "It takes nothing.")
        q = args.pop("q", None)
        where = {**(d.get("where") or {}), **args}
        choose = d.get("options")
        if choose and not args and not q:
            rows = self.site.list(sdef.name, d.get("where") or {}, None, 1000, None, None, self.owner)["items"]
            values = sorted({k for v in rows for k in reading._strings(v["data"].get(choose))})
            link = self.look.links(sdef, True).get(choose)
            named = self.look.views(link["sdef"]) if link and link["how"] == "forward" and \
                link["sdef"].type == "collection" else {}
            labelled = sorted((reading.label_of(link["sdef"], named[k]) if k in named else k, k) for k in values)
            return self.choices(qid, choose, labelled, limit, cursor)
        fields = d.get("fields")
        out = self.site.list(sdef.name, where, d.get("sort"), limit or OPTIONS_PAGE, cursor, fields, self.owner,
                             q, d.get("with"))
        if fmt == "table":
            own = [f for f in fields or [] if "." not in f] or \
                [f.name for f in sdef.fields if f.name not in self.look.hidden(sdef, True)]
            out = reading.table(out, sdef, own, [f for f in fields or [] if "." in f])
        return self.paged(out, qid, cursor, limit, q=q, **args)


def _fit(out: dict, most: Any, asker: _Asker) -> None:
    """Cut a list answer down to `most` bytes, leaving a link to the rest."""
    try:
        most = max(200, int(most))
    except (TypeError, ValueError):
        raise SiteError("bad_page", "max must be a whole number of bytes.")
    key = next((k for k in ("items", "rows", "options") if isinstance(out.get(k), list)), None)
    page = out.get("_page")
    if key is None or page is None:
        return
    def size() -> int:
        return len(json.dumps({k: v for k, v in out.items() if k != "_page"}, separators=(",", ":")))

    if size() <= most:
        return
    before = len(out[key])
    while len(out[key]) > 1 and size() > most - 150:   # room for the note below and the link to the rest
        out[key].pop()
    if len(out[key]) < before:
        qid, offset, params = page
        out["cut"] = True
        rest = offset + len(out[key])
        out["next"] = asker.url(qid, **{**params, "limit": None}, cursor=rest, max=most) if asker.links else str(rest)


def _records(node: Any) -> Any:
    """Every record in an answer: the ones asked for and the ones attached to them."""
    if isinstance(node, list):
        for item in node:
            yield from _records(item)
    elif isinstance(node, dict):
        if "key" in node:
            yield node
        for name in ("items", "with"):
            inner = node.get(name)
            yield from _records(list(inner.values()) if isinstance(inner, dict) else inner)


def _state_once(out: dict) -> None:
    """A time that every record in the answer shares is stated once beside them, not in each."""
    records = [r for r in _records(out) if "data" in r]
    for stamp in ("updated", "live_as_of"):
        having = [r for r in records if stamp in r]
        if len(having) > 1 and len({r[stamp] for r in having}) == 1 and \
                (stamp == "live_as_of" or len(having) == len(records)):
            value = having[0][stamp]
            for r in having:
                del r[stamp]
            out[stamp] = value


def ask(site: Any, qid: str, args: Optional[dict] = None, owner: bool = False, base: str = "") -> dict:
    """One question for one reader: its options when it needs narrowing, otherwise its answer."""
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    links = not _off(args.pop("links", "1"))
    unless, most, fmt = args.pop("unless", None), args.pop("max", None), args.pop("format", None)
    asker = _Asker(site, owner, base, links)
    out = {"question": qid, **asker.answer(str(qid), args, fmt)}
    if "rows" not in out:
        _state_once(out)
    if most is not None:
        _fit(out, most, asker)
    out.pop("_page", None)
    for inner in out.get("answers", []):
        inner.pop("_page", None)
    out.setdefault("as_of", site.now_z())
    # The same idea as an ETag, usable by a reader that can only change the address it asks for.
    out["tag"] = reading.tag_of(out)[3:-1]
    if unless is not None and str(unless) == out["tag"]:
        return {"question": qid, "unchanged": True, "tag": out["tag"], "as_of": out["as_of"]}
    return out


def ask_many(site: Any, asks: list, owner: bool = False, base: str = "", shared: bool = False) -> dict:
    """Several questions in one request. One that fails does not spoil the others.

    With `shared`, every question was given the same arguments and each uses the ones it takes.
    """
    if not asks:
        raise SiteError("invalid", "Ask at least one question.")
    if len(asks) > MAX_AT_ONCE:
        raise SiteError("too_many", f"At most {MAX_AT_ONCE} questions can be asked at once.", status=400)
    asker = _Asker(site, owner, base)
    answers = []
    for qid, args in asks:
        try:
            if shared:
                takes = asker.takes(str(qid))
                args = {k: v for k, v in args.items()
                        if k in CONTROL or takes is None or k.split(".")[0] in takes}
            answers.append(ask(site, str(qid), args, owner, base))
        except SiteError as e:
            answers.append({"question": qid, "error": e.as_dict()["error"]})
    return {"answers": answers, "as_of": site.now_z()}


def listing(site: Any, owner: bool = False, base: str = "") -> list:
    """The questions this reader can ask: the owner's own first, then one for each structure."""
    look = reading.Reading(site, owner)
    out = []
    for qid, d in site.defined_questions().items():
        if look.defs.get(d["read"]) is not None and (owner or look.may_read(look.defs[d["read"]])):
            out.append({"id": qid, "does": d["does"], "url": f"{base}/ask/{qid}",
                        **({"takes": d["takes"]} if d.get("takes") else {})})
    made = []
    for sdef in look.defs.values():
        if sdef.type == "form" or sdef.kind == "Closure" or (sdef.access == "private" and not owner):
            continue
        url = f"{base}/ask/{sdef.name}"
        entry: dict = {"id": sdef.name}
        if sdef.kind:
            entry["kind"] = sdef.kind
        # The owner's own description if they wrote one; a kind's default describes storage, not a question.
        stock_text = (kinds.KINDS.get(sdef.kind or "") or {}).get("description")
        entry["does"] = (sdef.description if sdef.description and sdef.description != stock_text else "") \
            or DOES.get(sdef.kind or "") or (
            f"The {sdef.name} tree, one level at a time" if sdef.type == "tree" else f"Records in {sdef.name}")
        entry["url"] = url
        if sdef.type == "tree":
            entry["takes"] = ["node"]
        elif sdef.kind == "Hours":
            entry["takes"], entry["try"] = ["date", "key"], [url + "?date=tomorrow"]
        elif sdef.kind == "Offer":
            entry["takes"] = ["q"] + [f for f in ("product", "place", "region") if sdef.field(f)]
        else:
            entry["takes"] = ["q", "key"]
        if sdef.access == "invite" and not owner and not site._reader_may(sdef.name):
            entry["needs"] = "a key from the site owner"
        rank = ORDER.index(sdef.kind) if sdef.kind in ORDER else len(ORDER) + (sdef.type == "tree")
        made.append((rank, sdef.name, entry))
    return out + [entry for _, _, entry in sorted(made, key=lambda m: m[:2])]


def front(site: Any, owner: bool = False, base: str = "") -> dict:
    """The first thing an assistant reads: what can be asked, in as few bytes as will do."""
    from .site import VERSION
    asks = listing(site, owner, base)
    out = {"aisites": VERSION, "format": FORMAT, **{k: v for k, v in site.config.items() if v},
           "ask": asks[:MAX_INLINE]}
    if len(asks) > MAX_INLINE:
        out["more_questions"] = f"{base}/ask"
    out["how"] = HOW
    if site.available_actions(None, "")["actions"]:
        out["do"] = f"{base}/actions"
    out["more"] = {"find": f"{base}/find?q=", "map": f"{base}/map", "changes": f"{base}/changes",
                   "history": f"{base}/history"}
    out["first_published"] = site.first_published()
    out["as_of"] = site.now_z()
    return out


# ---------------------------------------------------------------- questions the owner defines

class QuestionsMixin:
    def defined_questions(self) -> dict:
        return {r["id"]: json.loads(r["def"]) for r in self.store.q("SELECT id, def FROM questions ORDER BY id")}

    def question(self, id: str, does: Optional[str] = None, read: Optional[str] = None,
                 where: Optional[dict] = None, takes: Optional[list] = None, follow: Any = None,
                 fields: Optional[list] = None, sort: Optional[str] = None, options: Optional[str] = None,
                 remove: bool = False) -> dict:
        """Define, replace or remove a question visitors can ask. It is a saved read, checked by running it."""
        schema.check_name(id, "question")
        if remove:
            if self.store.x("DELETE FROM questions WHERE id=?", (id,)).rowcount != 1:
                raise not_found(f"Question '{id}'", "`ask` with no id lists the questions.")
            return {"removed": id}
        if id in self._defs():
            raise SiteError("name_taken", f"'{id}' is a structure, which is already a question of its own.",
                            "Choose another id.", status=409)
        if not does or not str(does).strip() or len(str(does)) > 200:
            raise SiteError("invalid", "`does` must say in one line what the question answers.")
        sdef = self._sdef(read or "")
        if sdef.type != "collection":
            raise SiteError("wrong_structure", f"A question reads a collection, and '{sdef.name}' is a {sdef.type}.")
        look = reading.Reading(self, False)  # checked as a visitor: it must not need what they cannot see
        takes = [str(t) for t in takes or []]
        for name in takes:
            if name != "q":
                look.resolve(sdef, name)
        if options is not None and (options not in takes or sdef.field(options) is None):
            raise SiteError("invalid", f"`options` must be a field of '{sdef.name}' that is also in `takes`.")
        d = {k: v for k, v in {"does": " ".join(str(does).split()), "read": sdef.name, "where": where or None,
                               "takes": takes or None,
                               "with": (follow.split(",") if isinstance(follow, str) else follow) or None,
                               "fields": fields or None, "sort": sort, "options": options}.items() if v is not None}
        with self.store.tx():
            self.store.x("INSERT INTO questions (id, def) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET def=excluded.def",
                         (id, json.dumps(d)))
            # Run both of its forms now, as the least privileged reader it is for, so a mistake is refused
            # here and not found by a visitor later.
            reader = _Asker(self, sdef.access != "public", "")
            reader.defined(id, d, {}, None)
            reader.defined(id, {**d, "options": None}, {}, None)
        return {"id": id, **d}


def _define(site: Any, id: str, given: dict) -> dict:
    given = dict(given)
    return site.question(id, follow=given.pop("with", None), **given)


def routes(app: Any, site: Any, ctx: Any) -> None:
    @app.get("/ask")
    def questions(request: Request):
        owner = ctx.is_owner(request)
        return ctx.reply(request, {"ask": listing(site, owner, ctx.base(request)), "how": HOW,
                                   "as_of": site.now_z()}, owner, "Questions")

    @app.post("/ask")
    async def ask_several(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise SiteError("too_large", f"The request body is over {MAX_BODY} bytes.", status=413)
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise SiteError("invalid", "The request body is not valid JSON.")
        asks = body.get("ask") if isinstance(body, dict) else None
        if not isinstance(asks, list) or not all(isinstance(a, dict) and a.get("id") for a in asks):
            raise SiteError("invalid", "Send {\"ask\": [{\"id\": \"<question>\", ...its arguments}, ...]}.")
        owner = ctx.is_owner(request)
        pairs = [(a["id"], {k: v for k, v in a.items() if k != "id"}) for a in asks]
        out = await run_in_threadpool(ask_many, site, pairs, owner, ctx.base(request))
        return ctx.reply(request, out, owner, "Answers")

    @app.get("/ask/{ids}")
    def ask_one(request: Request, ids: str):
        owner = ctx.is_owner(request)
        args = dict(request.query_params)
        names = [n for n in ids.split(",") if n]
        if len(names) > 1:
            out = ask_many(site, [(n, dict(args)) for n in names], owner, ctx.base(request), shared=True)
        else:
            out = ask(site, ids, args, owner, ctx.base(request))
        return ctx.reply(request, out, owner, ids)


ops.register(
    ops.Op("ask", "Ask the site a question as a visitor would, to check what they are told. "
                  "With no id, lists the questions.",
           lambda site, id=None, args=None: ask(site, id, args) if id else {"ask": listing(site)},
           {"id": {"type": "string", "description": "A question's id, or the name of a kind such as Offer."},
            "args": {"type": "object", "description": 'What to add, e.g. {"q": "ibuprofen"} or {"date": "tomorrow"}.'}},
           positional=("id?",), pairs="args", mutates=False),
    ops.Op("question", "Define, replace or remove a question visitors can ask: a saved read with a one-line purpose.",
           lambda site, id, **kw: _define(site, id, kw),
           {"id": {"type": "string", "description": "Short lowercase name; it becomes the address /ask/<id>."},
            "does": {"type": "string", "description": "One line on what it answers, shown in the site's list."},
            "read": {"type": "string", "description": "The collection it reads."},
            "where": {"type": "object", "description": 'Filters always applied, e.g. {"stock.gt": 0}.'},
            "takes": {"type": "array", "description": 'What the asker may add: "q" or filter names such as '
                                                      '"place.city" or "price.lte".'},
            "with": {"type": "array", "description": 'References to attach, e.g. ["place", "place.hours"].'},
            "fields": {"type": "array", "description": 'Fields to show, e.g. ["price", "place.name"].'},
            "sort": {"type": "string", "description": "Field name, or -field for descending."},
            "options": {"type": "string", "description": "A field in `takes` whose values are offered as links "
                                                          "when the question is opened with nothing added."},
            "remove": {"type": "boolean", "description": "Remove the question instead."}},
           ("id",), positional=("id",), pairs="where"),
    ops.Op("find", "Find records anywhere on the site by words in their names and text.",
           lambda site, q, limit=10: site.find(q, limit),
           {"q": {"type": "string", "description": "Words to look for. Case and accents are ignored."},
            "limit": {"type": "integer"}},
           ("q",), positional=("q",), mutates=False),
)
