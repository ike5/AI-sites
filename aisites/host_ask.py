"""One question across every site a host serves.

A built-in kind means the same thing on every site, so a host can put one question to all of them at
once: `GET /ask/Offer?q=ibuprofen` returns the matching offers from each site, in one request instead of
one per site. Each site is asked exactly as a visitor would ask it, so nothing is shown here that the
site does not already show, and a site can leave itself out (`listed` in its settings).

With nothing added, a kind's address lists the sites that answer it, each a link to that site's own
question, so an assistant that can only follow links can still walk from the host to an answer.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlencode

from . import crawl, questions, reading
from .errors import SiteError, not_found

MAX_SITES = 50
CONTROL = ("links", "format", "max", "unless", "cursor")
SKIPPED = ("not_found", "bad_filter", "bad_fields", "bad_with", "needs_key", "bad_page")


def _sites(host: Any) -> list:
    """(name, site) for every site that takes part: served here, not suspended, and not left out by its owner."""
    platform = host.platform
    out = []
    for name in host.names():
        if platform.is_suspended(name):
            continue
        app = host.app_for(name)
        site = app.state.site if app is not None else None
        if site is not None and site.config.get("listed", True) is not False:
            out.append((name, site))
    return out


def _kinds(site: Any) -> set:
    return {d.kind for d in site._defs().values()
            if d.kind and d.kind != "Closure" and d.type == "collection" and d.access == "public"}


def enabled(host: Any) -> bool:
    return bool(host.platform.config.get("ask_across_sites", True))


def listing(host: Any, base: str = "") -> list:
    """The kinds that can be asked across this host, with how many sites answer each."""
    if not enabled(host):
        return []
    counts: dict = {}
    for _, site in _sites(host):
        for kind in _kinds(site):
            counts[kind] = counts.get(kind, 0) + 1
    order = {kind: i for i, kind in enumerate(questions.ORDER)}
    return [{"id": kind, "does": questions.DOES.get(kind, f"Records of kind {kind}") + ", on every site here",
             "url": f"{base}/ask/{kind}", "sites": counts[kind]}
            for kind in sorted(counts, key=lambda k: (order.get(k, 99), k))]


def _page(args: dict) -> int:
    try:
        return max(0, int(args.pop("cursor", 0) or 0))
    except (TypeError, ValueError):
        raise SiteError("bad_page", "cursor must be a whole number.")


def ask(host: Any, kind: str, args: Optional[dict] = None, base: str = "") -> dict:
    if not enabled(host):
        raise not_found("Asking across sites", "This host has switched it off. Ask each site at /s/<name>/ask.")
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    platform = host.platform
    taking = [(name, site) for name, site in _sites(host) if kind in _kinds(site)]
    if not taking:
        raise not_found(f"Question '{kind}'", f"GET {base}/ask lists what can be asked across this host. "
                                              "A site's own questions are at /s/<name>/ask.")
    offset = _page(args)
    links = str(args.get("links", "1")).lower() not in ("0", "false", "no", "off")
    unless, most = args.pop("unless", None), args.pop("max", None)
    narrowed = [k for k in args if k not in CONTROL]
    page = taking[offset:offset + MAX_SITES]
    now = platform.now_z()
    out: dict = {"question": kind}

    def address(more: dict) -> str:
        return f"{base}/ask/{kind}?" + urlencode({**args, **more})

    if not narrowed:
        # Nothing to narrow by: say who can answer, and let the asker go to one of them.
        out.update({"choose": "site", "total": len(taking), "options": [
            {"label": site.config.get("name") or name,
             **({"url": platform.link(f"/s/{name}/ask/{kind}")} if links else {"site": name})}
            for name, site in page]})
    else:
        answers, skipped = [], 0
        for name, site in page:
            try:
                answer = questions.ask(site, kind, dict(args), False, platform.link(f"/s/{name}"))
            except SiteError as e:
                if e.code not in SKIPPED:
                    raise
                skipped += 1   # this site cannot be asked that way, for example it has no such field
                continue
            if _empty(answer):
                continue
            for repeated in ("question", "tag", "as_of"):
                answer.pop(repeated, None)
            answers.append({"site": name, "name": site.config.get("name") or name,
                            **({"url": platform.link(f"/s/{name}/")} if links else {}), "answer": answer})
        out.update({"sites": answers, "asked": len(page)})
        if skipped:
            out["could_not_answer"] = skipped
    if offset + MAX_SITES < len(taking):
        out["next"] = address({"cursor": offset + MAX_SITES}) if links else str(offset + MAX_SITES)
    if most is not None:
        _fit(out, most, offset, address, links)
    out["as_of"] = now
    out["tag"] = reading.tag_of(out)[3:-1]
    if unless is not None and str(unless) == out["tag"]:
        return {"question": kind, "unchanged": True, "tag": out["tag"], "as_of": now}
    return out


def _empty(answer: dict) -> bool:
    for name in ("items", "rows", "results", "options", "answers"):
        if name in answer:
            return not answer[name]
    return False


def _fit(out: dict, most: Any, offset: int, address: Any, links: bool) -> None:
    try:
        most = max(200, int(most))
    except (TypeError, ValueError):
        raise SiteError("bad_page", "max must be a whole number of bytes.")
    key = "sites" if "sites" in out else "options"

    def size() -> int:
        return len(json.dumps(out, separators=(",", ":")))

    if size() <= most:
        return
    before = len(out[key])
    while len(out[key]) > 1 and size() > most - 150:
        out[key].pop()
    if len(out[key]) < before:
        # Sites are cut whole, so the rest starts at the first site not shown.
        shown = out.get("asked", before) - (before - len(out[key])) if key == "sites" else len(out[key])
        out["cut"] = True
        out["next"] = address({"cursor": offset + max(1, shown), "max": most}) if links else str(offset + max(1, shown))


DIRECTORY_PAGE = 100


def served(host: Any) -> list:
    """(name, site) for every site here that is not suspended, whatever it allows. For robots.txt."""
    out = []
    for name in host.names():
        if not host.platform.is_suspended(name):
            app = host.app_for(name)
            if app is not None:
                out.append((name, app.state.site))
    return out


def findable(host: Any) -> list:
    """Sites a search engine is pointed at: listed by their owner and open to search."""
    return [(name, site) for name, site in _sites(host) if crawl.use_of(site)["search"]]


def directory(host: Any, cursor: Any = None, base: str = "") -> dict:
    """Every site this host serves that may be found, a page at a time, for a crawler that knows only the host."""
    from .site import VERSION
    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        raise SiteError("bad_page", "cursor must be a whole number.")
    platform = host.platform
    sites = findable(host)
    out = {"aisites_host": VERSION, "name": platform.config["name"],
           "sites": [{"site": name, **crawl.summary(site, platform.link(f"/s/{name}"))}
                     for name, site in sites[offset:offset + DIRECTORY_PAGE]],
           "total": len(sites),
           "how": "Each site's `crawl` address is everything public on it in one feed; add ?since=<cursor> "
                  "for what changed. Each site's `use` says what it may be used for.",
           "as_of": platform.now_z()}
    if offset + DIRECTORY_PAGE < len(sites):
        out["next"] = f"{base}/.well-known/aisites.json?cursor={offset + DIRECTORY_PAGE}"
    return out


def find(host: Any, q: Any, limit: Any = 10, base: str = "") -> dict:
    """Records matching words on any site here: a few from each, as candidates to read next."""
    if not enabled(host):
        raise not_found("Searching across sites", "This host has switched it off. Search each site at /s/<name>/find.")
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        raise SiteError("bad_page", "limit must be a whole number.")
    platform = host.platform
    found, total = [], 0
    for name, site in _sites(host)[:MAX_SITES * 4]:
        got = site.find(q, 3, owner=False)
        total += got["total"]
        if got["matches"] and len(found) < limit:
            found.append({"site": name, "name": site.config.get("name") or name,
                          "url": platform.link(f"/s/{name}/"), "matches": got["matches"],
                          **({"more": got["total"] - len(got["matches"])} if got["total"] > len(got["matches"]) else {})})
    return {"q": str(q), "sites": found, "total": total, "as_of": platform.now_z()}
