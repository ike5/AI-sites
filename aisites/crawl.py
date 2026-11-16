"""Crawling: how a search engine finds a site, reads all of it, and keeps its copy fresh. No web pages.

A site has nothing to show in a browser, so it cannot be indexed the way web pages are. Instead it gives a
crawler four things:

  finding     robots.txt and a sitemap (plain text and XML, for crawlers that exist today) point at the
              first response; a host lists every site it serves
  reading     GET /crawl is everything a visitor could read, in one feed, with each field's meaning
              stated once, a stable address per record, and the signed point in history it was taken at
  refreshing  GET /crawl?since=<cursor> is only what changed since, including what was removed
  permission  `use` says whether the site may be indexed for search and collected for training

The feed is built from the same views a visitor gets, so a crawler is shown nothing more.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote
from xml.sax.saxutils import escape as xml

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from . import ops
from .errors import SiteError

PAGE_BYTES = 200_000
SMALLEST_PAGE = 2_000
RECORD_KEYS = ("updated", "confirmed", "live_as_of", "instants", "active_changes", "upcoming_changes", "next_change")

# What today's crawlers call themselves in robots.txt. These names change; this is the one place to update.
TRAINING_CRAWLERS = ("GPTBot", "ClaudeBot", "Google-Extended", "CCBot", "Applebot-Extended",
                     "meta-externalagent", "Bytespider")
SEARCH_CRAWLERS = ("OAI-SearchBot", "Claude-SearchBot", "PerplexityBot", "Bingbot", "Googlebot", "DuckDuckBot",
                   "Applebot")
HOW = ("Read this feed once, following `next` until there is none, and keep `cursor` from the first page. "
       "After that, ?since=<cursor> gives only what changed, with a new cursor. `structures` says what each "
       "field means; `record_address` is where any record lives, to cite; `use` says what this site may be "
       "used for.")


def use_of(site: Any) -> dict:
    """What the owner allows: being indexed to answer and be cited, and being collected to train models."""
    config = site.config
    return {"search": config.get("search", True) is not False, "training": config.get("training", True) is not False}


def _public(site: Any) -> list:
    return [d for d in site._defs().values() if d.access == "public" and d.type != "form"]


def _structure(sdef: Any) -> dict:
    out: dict = {"name": sdef.name}
    if sdef.type == "tree":
        out["type"] = "tree"
    if sdef.kind:
        out["kind"] = sdef.kind
    if sdef.semantic:
        out["means"] = sdef.semantic
    if sdef.description:
        out["description"] = sdef.description
    shown = [f for f in sdef.fields if f.policy != "private"]
    if shown:
        # A field's schema.org term where it has one, otherwise its type.
        out["fields"] = {f.name: f.semantic or (f.type + ("[]" if f.many else "")) for f in shown}
    refs = {f.name: next(iter(f.choices.values())) for f in shown if f.choices}
    if refs:
        out["refs"] = refs
    return out


def _record(sdef: Any, view: dict) -> dict:
    return {"in": sdef.name, "key": view["key"], "data": view["data"],
            **{k: view[k] for k in RECORD_KEYS if k in view}}


def _address(base: str) -> str:
    """Where any record lives, stated once instead of in every record. A crawler builds addresses itself."""
    return base + "/collections/{in}/{key}"


def _size(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def _budget(most: Any) -> int:
    if most in (None, ""):
        return PAGE_BYTES
    try:
        return max(SMALLEST_PAGE, min(PAGE_BYTES, int(most)))
    except (TypeError, ValueError):
        raise SiteError("bad_page", "max must be a whole number of bytes.")


def _whole(value: Any, name: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        raise SiteError("bad_page", f"{name} must be a whole number.")


def feed(site: Any, base: str = "", since: Any = None, after: Any = None, cursor: Any = None,
         most: Any = None) -> dict:
    """Everything public on a site, or with `since`, only what changed after that cursor."""
    use = use_of(site)
    if not use["search"] and not use["training"]:
        raise SiteError("crawl_off", "This site asks not to be crawled.",
                        "It still answers questions put to it for a person; see /.well-known/aisite.json.",
                        status=403)
    site.tick()
    budget = _budget(most)
    latest = site.store.one("SELECT COALESCE(MAX(id), 0) AS m FROM changes")["m"]
    since = _whole(since, "since")
    if since is not None:
        return _changed(site, base, use, since, latest, budget)
    return _everything(site, base, use, str(after) if after else None, _whole(cursor, "cursor"), latest, budget)


def _head(site: Any, base: str) -> dict:
    seq, digest, _ = site.history.head()
    return {"seq": seq, "hash": digest, "signed": f"{base}/history/head"}


def _everything(site: Any, base: str, use: dict, after: Optional[str], cursor: Optional[int], latest: int,
                budget: int) -> dict:
    from .site import VERSION
    from .questions import FORMAT
    public = _public(site)
    now = site.now_z()
    out: dict = {"aisites": VERSION, "format": FORMAT}
    if after is None:
        # Said once, on the first page: what the site is, what it allows, and what its fields mean.
        config = site.config
        out["site"] = {**{k: config[k] for k in ("name", "description", "timezone", "language", "currency")
                          if config.get(k)}, "url": base, "public_key": site.public_key}
        out["use"] = use
        out["how"] = HOW
        out["structures"] = [_structure(d) for d in public]
        trees = [{"name": d.name, "nodes": site.read_tree(d.name)["nodes"]} for d in public if d.type == "tree"]
        if trees:
            out["trees"] = trees
        out["head"] = _head(site, base)
    out["record_address"] = _address(base)
    out["records"] = []
    used, last, cut = _size(out), None, False
    for sdef in sorted((d for d in public if d.type == "collection"), key=lambda d: d.name):
        for key, view in site._views(sdef, False, now).items():   # in key order
            place = f"{sdef.name}/{key}"
            if after is not None and (sdef.name, key) <= tuple(after.split("/", 1)):
                continue
            record = _record(sdef, view)
            size = _size(record) + 1
            if out["records"] and used + size > budget:
                cut = True
                break
            out["records"].append(record)
            used, last = used + size, place
        if cut:
            break
    # The cursor is fixed when the first page is read, so a later ?since= catches whatever changed meanwhile.
    out["cursor"] = latest if cursor is None else min(cursor, latest)
    if cut and last is not None:
        out["next"] = f"{base}/crawl?after={quote(last, safe='')}&cursor={out['cursor']}" \
            + (f"&max={budget}" if budget != PAGE_BYTES else "")
    out["as_of"] = now
    return out


def _changed(site: Any, base: str, use: dict, since: int, latest: int, budget: int) -> dict:
    from .site import VERSION
    from .questions import FORMAT
    now = site.now_z()
    defs = {d.name: d for d in _public(site)}
    out: dict = {"aisites": VERSION, "format": FORMAT, "use": use, "since": since,
                 "record_address": _address(base), "records": [], "removed": []}
    seen, trees, structures, about_site = set(), [], False, False
    used, reached, cut = _size(out), since, False
    for row in site.store.q("SELECT id, path FROM changes WHERE id > ? ORDER BY id", (since,)):
        parts = row["path"].split("/", 2)
        if parts[0] == "collections" and len(parts) == 3 and (parts[1], parts[2]) not in seen:
            name, key = parts[1], parts[2]
            sdef = defs.get(name)
            raw = site._row(name, key) if sdef is not None and sdef.type == "collection" else None
            view = site._view(sdef, raw, site._schedules(name, key).get(key, []), now, False) if raw else None
            item = _record(sdef, view) if view is not None else {"in": name, "key": key}
            size = _size(item) + 1
            if (out["records"] or out["removed"]) and used + size > budget:
                cut = True
                break
            seen.add((name, key))
            (out["records"] if view is not None else out["removed"]).append(item)
            used += size
        elif parts[0] == "schemas":
            structures = True
        elif parts[0] == "trees" and len(parts) >= 2 and parts[1] not in trees:
            trees.append(parts[1])
        elif parts[0] == "site":
            about_site = True
        reached = row["id"]
    if structures:
        # A structure that is no longer listed here is gone, and so are its records.
        out["structures"] = [_structure(d) for d in defs.values()]
    if trees:
        out["trees"] = [{"name": n, "nodes": site.read_tree(n)["nodes"]} for n in trees
                        if n in defs and defs[n].type == "tree"]
    if about_site:
        config = site.config
        out["site"] = {**{k: config[k] for k in ("name", "description", "timezone", "language", "currency")
                          if config.get(k)}, "url": base, "public_key": site.public_key}
    if not out["removed"]:
        del out["removed"]
    out["head"] = _head(site, base)
    out["cursor"] = reached if cut else latest
    if cut:
        out["next"] = f"{base}/crawl?since={reached}" + (f"&max={budget}" if budget != PAGE_BYTES else "")
    out["as_of"] = now
    return out


# ---------------------------------------------------------------- for crawlers that exist today

def _rules(denied: dict) -> list:
    """robots.txt groups: for each set of crawler names, the paths they are asked to stay out of."""
    lines: list = []
    for names, paths in denied.items():
        if paths:
            lines += [f"User-agent: {n}" for n in names] + [f"Disallow: {p}" for p in paths] + [""]
    return lines


def robots(base: str, sites: list) -> str:
    """robots.txt for one address. `sites` is [(path prefix, site)]: one site at "", or a host's sites.

    Only whole-site wishes are written here. Nothing is disallowed for everyone: an assistant fetching for
    a person honours this file too, and must still be able to ask the site its questions.
    """
    denied = {TRAINING_CRAWLERS: [], SEARCH_CRAWLERS: []}
    for prefix, site in sites:
        use = use_of(site)
        if not use["training"]:
            denied[TRAINING_CRAWLERS].append(f"{prefix}/")
        if not use["search"]:
            denied[SEARCH_CRAWLERS].append(f"{prefix}/")
    start = f"{base}/.well-known/aisite.json" if len(sites) == 1 and sites[0][0] == "" \
        else f"{base}/.well-known/aisites.json"
    lines = ["# AI-sites: data for AI assistants and search engines. There are no pages for people here.",
             f"# Start at {start}",
             "# Everything public on a site is in one feed at <site>/crawl ; ?since=<cursor> gives what changed.",
             "# Each site states what it may be used for in `use` (search, training). The rules below say the",
             "# same to crawlers that read only this file.", ""]
    lines += _rules(denied)
    lines += ["User-agent: *", "Allow: /", "", f"Sitemap: {base}/sitemap.xml", ""]
    return "\n".join(lines)


def sitemap(sites: list) -> str:
    """A sitemap of the few addresses worth fetching per site. `sites` is [(full address, site)]."""
    urls = []
    for base, site in sites:
        last = site.store.one("SELECT MAX(time) AS t FROM changes")["t"] or site.first_published()
        for path in ("/.well-known/aisite.json", "/llms.txt", "/map", "/crawl"):
            urls.append(f"<url><loc>{xml(base + path)}</loc><lastmod>{xml(last)}</lastmod></url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>\n")


def summary(site: Any, base: str) -> dict:
    """One site as a host's directory lists it."""
    public = _public(site)
    count = sum(site.store.one("SELECT COUNT(*) AS n FROM records WHERE collection=?", (d.name,))["n"]
                for d in public if d.type == "collection")
    config = site.config
    return {**{k: config[k] for k in ("name", "description", "language") if config.get(k)}, "url": base + "/",
            "crawl": base + "/crawl", "kinds": sorted({d.kind for d in public if d.kind and d.kind != "Closure"}),
            "records": count,
            "updated": site.store.one("SELECT MAX(time) AS t FROM changes")["t"] or site.first_published(),
            "use": use_of(site)}


def routes(app: Any, site: Any, ctx: Any) -> None:
    @app.get("/crawl")
    def crawl(request: Request):
        q = request.query_params
        out = feed(site, ctx.base(request), q.get("since"), q.get("after"), q.get("cursor"), q.get("max"))
        return ctx.reply(request, out, False, "Crawl")

    @app.get("/robots.txt")
    def robots_txt(request: Request):
        return PlainTextResponse(robots(ctx.base(request), [("", site)]))

    @app.get("/sitemap.xml")
    def sitemap_xml(request: Request):
        return Response(sitemap([(ctx.base(request), site)]), media_type="application/xml")


ops.register(
    ops.Op("crawl", "Show what a search engine's crawler is given: everything public, or with `since`, what "
                    "changed after that cursor.",
           lambda site, since=None, after=None, max=None: feed(site, "", since, after, None, max),
           {"since": {"type": "integer", "description": "A cursor from an earlier read."},
            "after": {"type": "string", "description": "Continue a paged read after this collection/key."},
            "max": {"type": "integer", "description": "Most bytes per page."}}, mutates=False),
)
