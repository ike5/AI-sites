"""The web door: read routes for visiting agents and admin routes for the owner."""

import json
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from . import crawl, ops, questions, reading, schema, timeview
from .errors import SiteError
from .reading import tag_of
from .site import Site, VERSION

RESERVED = {"limit", "cursor", "fields", "sort", "format", "q", "with"}
VARIANTS = ("/ask", "/find", "/changes", "/history", "/actions", "/bookable", "/forms", "/identity")


def create_app(site: Site) -> FastAPI:
    app = FastAPI(title=site.config.get("name", "AI site"), openapi_url=None, docs_url=None, redoc_url=None)
    app.state.site = site

    # ------------------------------------------------------------ helpers

    def prefix(request: Request) -> str:
        return request.scope.get("root_path", "").rstrip("/")

    def base(request: Request) -> str:
        """The site's full address, for links an assistant can open without building them."""
        return (request.scope.get("aisites.base") or site.config.get("url")
                or str(request.base_url)).rstrip("/")

    def token(request: Request) -> Optional[str]:
        auth = request.headers.get("authorization", "")
        return auth[7:].strip() if auth[:7].lower() == "bearer " else None

    def principal(request: Request) -> Optional[ops.Principal]:
        """The caller a bearer token names; None without a token. An unknown token is refused."""
        t = token(request)
        if t is None:
            return None
        found = site.resolve(t)
        if found is None:
            raise SiteError("bad_key", "That key is not valid for this site.",
                            "Public data needs no key; omit the Authorization header.", status=401)
        return found

    def is_owner(request: Request) -> bool:
        # A role key is valid here but reads like a visitor; roles act through the admin and MCP doors.
        found = principal(request)
        site.set_reader(found)  # lets a key with `read` on an invite-only structure see it
        return found is not None and found.kind == "owner"

    def headers(request: Request, etag: str) -> dict:
        head = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Authorization",
                "Link": f'<{prefix(request)}/.well-known/aisite.json>; rel="describedby"'}
        # Addresses that repeat facts found elsewhere stay open to fetch but ask not to be indexed, and a
        # site whose owner turned search off asks that of every address.
        inside = request.url.path[len(prefix(request)):]
        if request.url.query or inside.startswith(VARIANTS) or not crawl.use_of(site)["search"]:
            head["X-Robots-Tag"] = "noindex"
        return head

    def reply(request: Request, payload: Any, owner: bool, title: str) -> Response:
        """Always JSON: a site has no pages for people. Every reply is cheap to revalidate."""
        etag = tag_of(payload)
        head = headers(request, etag)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=head)
        return JSONResponse(payload, headers=head)

    @app.exception_handler(SiteError)
    async def site_error(request: Request, e: SiteError):
        head = {"WWW-Authenticate": "Bearer"} if e.status == 401 else {}
        if getattr(e, "retry_after", None):
            head["Retry-After"] = str(e.retry_after)
        return JSONResponse(e.as_dict(), status_code=e.status, headers=head)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, e: HTTPException):
        err = SiteError("not_found" if e.status_code == 404 else "http_error", str(e.detail),
                        f"Start at {prefix(request)}/.well-known/aisite.json to see what this site offers.",
                        status=e.status_code)
        return JSONResponse(err.as_dict(), status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, e: RequestValidationError):
        return JSONResponse(SiteError("invalid", "A value in the address is not of the expected type.").as_dict(),
                            status_code=400)

    @app.exception_handler(Exception)
    async def crash(request: Request, e: Exception):
        return JSONResponse(SiteError("server_error", "The site failed to answer.").as_dict(), status_code=500)

    # ------------------------------------------------------------ discovery

    @app.get("/")
    @app.get("/.well-known/aisite.json")
    def manifest(request: Request):
        owner = is_owner(request)
        return reply(request, questions.front(site, owner, base(request)), owner, site.config.get("name", "Site"))

    @app.get("/map")
    def site_map(request: Request):
        owner = is_owner(request)
        return reply(request, site.manifest(owner, prefix(request)), owner, "Map")

    @app.get("/llms.txt")
    def overview(request: Request):
        owner = is_owner(request)
        text = llms_txt(site, owner, prefix(request), base(request))
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                                 headers=headers(request, tag_of(text)))

    @app.get("/openapi.json")
    def openapi(request: Request):
        owner = is_owner(request)
        doc = openapi_doc(site, owner, prefix(request))
        return JSONResponse(doc, headers=headers(request, tag_of(doc)))

    # ------------------------------------------------------------ collections

    @app.get("/collections/{name}")
    def list_records(request: Request, name: str):
        owner = is_owner(request)
        sdef = site.readable(name, owner)
        q = request.query_params
        where = {k: v for k, v in q.items() if k not in RESERVED}
        fields = [f for f in q.get("fields", "").split(",") if f] or None
        out = site.list(name, where, q.get("sort"), q.get("limit", 100), q.get("cursor"), fields, owner=owner,
                        q=q.get("q"), follow=q.get("with"))
        if q.get("format") == "table":
            own = [f for f in fields or [] if "." not in f] or \
                [f.name for f in sdef.fields if owner or f.policy != "private"]
            out = reading.table(out, sdef, own, [f for f in fields or [] if "." in f])
        out["schema"] = f"{prefix(request)}/collections/{name}/schema"
        return reply(request, out, owner, name)

    @app.get("/collections/{name}/schema")
    def collection_schema(request: Request, name: str):
        owner = is_owner(request)
        sdef = site.readable(name, owner)
        if sdef.type != "collection":
            raise SiteError("wrong_structure", f"'{name}' is a tree; read it at /trees/{name}.")
        return reply(request, schema.public_schema(sdef, prefix(request), include_private=owner), owner,
                     f"{name} schema")

    @app.get("/collections/{name}/{key}")
    def get_record(request: Request, name: str, key: str):
        owner = is_owner(request)
        out = site.read_record(name, key, owner, request.query_params.get("with"))
        if site.readable(name, owner).public:
            out["history"] = f"{prefix(request)}/collections/{name}/{key}/history"
        return reply(request, out, owner, f"{name} / {key}")

    @app.get("/collections/{name}/{key}/history")
    def record_history(request: Request, name: str, key: str):
        owner = is_owner(request)
        site.readable(name, owner)
        return history_reply(request, owner, f"collections/{name}/{key}", f"{name} / {key} history")

    # ------------------------------------------------------------ trees, hours, changes, history

    @app.get("/trees/{name}")
    def get_tree(request: Request, name: str):
        owner = is_owner(request)
        sdef = site.readable(name, owner)
        if sdef.type != "tree":
            raise SiteError("wrong_structure", f"'{name}' is a collection; read it at /collections/{name}.")
        q = request.query_params
        return reply(request, site.read_tree(name, q.get("node"), q.get("depth")), owner, name)

    @app.get("/find")
    def find(request: Request):
        owner = is_owner(request)
        q = request.query_params
        return reply(request, site.find(q.get("q"), q.get("limit", 10), owner), owner, "Find")

    @app.get("/hours")
    def hours_all(request: Request):
        owner = is_owner(request)
        date = request.query_params.get("date")
        return reply(request, site.hours(date, None, owner), owner, "Opening hours")

    @app.get("/hours/{key}")
    def hours_one(request: Request, key: str):
        owner = is_owner(request)
        date = request.query_params.get("date")
        return reply(request, site.hours(date, key, owner), owner, "Opening hours")

    @app.get("/changes")
    def changes(request: Request):
        owner = is_owner(request)
        q = request.query_params
        return reply(request, site.changes(q.get("since", 0), q.get("limit", 200)), owner, "Changes")

    def history_reply(request: Request, owner: bool, path: Optional[str], title: str) -> Response:
        q = request.query_params
        try:
            after, limit = max(0, int(q.get("after", 0))), max(1, min(1000, int(q.get("limit", 100))))
        except ValueError:
            raise SiteError("bad_page", "after and limit must be whole numbers.")
        now = site.now()
        since = timeview.to_z(timeview.resolve_instant(q["since"], site.tz, now)) if q.get("since") else None
        until = timeview.to_z(timeview.resolve_instant(q["until"], site.tz, now)) if q.get("until") else None
        entries = site.history.entries(after, limit + 1, path or q.get("path"), since, until)
        page = entries[:limit]
        seq, head_hash, _ = site.history.head()
        out = {"entries": page, "head": {"seq": seq, "hash": head_hash},
               "first_published": site.first_published(), "as_of": site.now_z()}
        if len(entries) > limit:
            out["next_after"] = page[-1]["seq"]
        return reply(request, out, owner, title)

    @app.get("/history")
    def history(request: Request):
        return history_reply(request, is_owner(request), None, "History")

    # Signed head and checkpoints. Registered before /history/{seq}, which would otherwise capture them.

    @app.get("/history/head")
    def history_head(request: Request):
        owner = is_owner(request)
        return reply(request, site.head_view(), owner, "Signed history head")

    @app.get("/history/checkpoints")
    def history_checkpoints(request: Request):
        owner = is_owner(request)
        q = request.query_params
        return reply(request, site.checkpoints(q.get("after", 0), q.get("limit", 100)), owner, "Checkpoints")

    @app.get("/history/{seq}")
    def history_entry(request: Request, seq: int):
        owner = is_owner(request)
        entry = site.history.entry(seq)
        if entry is None:
            raise SiteError("not_found", f"History entry {seq} does not exist.", status=404)
        return reply(request, entry, owner, f"History entry {seq}")

    # ------------------------------------------------------------ admin door

    @app.get("/admin/ops")
    def admin_ops(request: Request):
        return JSONResponse({"operations": ops.describe_ops(),
                             "call": "POST {prefix}/admin/{target}/{op} with a JSON body of the other arguments "
                                     "and Authorization: Bearer <owner key or role key>. Operations without a target: "
                                     "POST {prefix}/admin/{op}.".replace("{prefix}", prefix(request))})

    async def admin(request: Request, op: str, target: Optional[str]) -> Response:
        if token(request) is None:
            raise SiteError("needs_key", "Admin operations need the site owner's key.",
                            "Send it as: Authorization: Bearer <owner key>", status=401)
        caller = principal(request)
        raw = await request.body()
        try:
            args = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise SiteError("invalid", "The request body is not valid JSON.")
        if not isinstance(args, dict):
            raise SiteError("invalid", "The request body must be a JSON object of arguments.")
        if target is not None:
            args["target"] = target
        result = await run_in_threadpool(ops.run, site, caller, op, args)
        return JSONResponse(result)

    @app.post("/admin/{op}")
    async def admin_site(request: Request, op: str):
        return await admin(request, op, None)

    @app.post("/admin/{target}/{op}")
    async def admin_target(request: Request, target: str, op: str):
        return await admin(request, op, target)

    # Feature modules add their own routes; `ctx` hands them the helpers above.
    from types import SimpleNamespace
    from . import actions, bookings, forms, identity, stream
    ctx = SimpleNamespace(prefix=prefix, base=base, token=token, principal=principal, is_owner=is_owner,
                          headers=headers, reply=reply)
    for feature in (stream, actions, bookings, forms, questions, identity, crawl):
        feature.routes(app, site, ctx)

    from .mcp_door import McpHttp
    app.mount("/mcp", McpHttp(site))
    app.add_middleware(HeadAsGet)
    return app


class HeadAsGet:
    """Answer HEAD like GET without the body, so agents can check ETags cheaply."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            return await self.app(scope, receive, send)

        async def send_without_body(message: dict) -> None:
            if message["type"] == "http.response.body":
                message = {"type": "http.response.body", "body": b""}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)


def llms_txt(site: Site, owner: bool, prefix: str, base: str = "") -> str:
    m = site.manifest(owner, prefix)
    lines = [f"# {m.get('name', 'Site')}", ""]
    if m.get("description"):
        lines += [f"> {m['description']}", ""]
    lines += [
        "This site publishes its information for AI agents as JSON.",
        f"Start at {base or prefix}/.well-known/aisite.json, the same list of questions as below.", "",
        "## What you can ask", "",
    ]
    lines += [f"- [{q['does']}]({q['url']})" + (f" (takes {', '.join(q['takes'])})" if q.get("takes") else "")
              for q in questions.listing(site, owner, base or prefix)]
    lines += [""] + [f"- {line}" for line in questions.HOW] + [
        "", "## About this site", "",
        f"- Time zone: {m.get('timezone', 'UTC')}",
    ]
    if m.get("language"):
        lines.append(f"- Language: {m['language']}")
    if m.get("currency"):
        lines.append(f"- Currency: {m['currency']}")
    lines += [f"- First published: {m['first_published']}",
              f"- Fields and references, for writing your own reads: {base or prefix}/map"]
    return "\n".join(lines) + "\n"


def openapi_doc(site: Site, owner: bool, prefix: str) -> dict:
    """An OpenAPI description generated from the structures this reader may see."""
    m = site.manifest(owner, prefix)
    page = [{"name": n, "in": "query", "schema": {"type": "string"}}
            for n in ("limit", "cursor", "sort", "fields", "q", "with")]
    ok = {"200": {"description": "OK"}}
    word = {"type": "string"}
    paths: dict = {
        "/.well-known/aisite.json": {"get": {"summary": "The questions this site answers", "responses": ok}},
        "/ask": {"get": {"summary": "Every question, with what each takes", "responses": ok},
                 "post": {"summary": "Several questions in one request: {\"ask\": [{\"id\": ..., ...arguments}]}",
                          "responses": ok}},
        "/ask/{id}": {"get": {
            "summary": "One question: its options as links, or its answer. Join ids with commas to ask several.",
            "parameters": [{"name": "id", "in": "path", "required": True, "schema": word,
                            "description": "One of: " + ", ".join(q["id"] for q in questions.listing(site, owner))
                                           + ", or the name of a kind such as Offer."}]
            + [{"name": n, "in": "query", "schema": word, "description": d} for n, d in (
                ("q", "Words to find"), ("key", "One record"), ("unless", "A tag: answer only if it changed"),
                ("links", "0 to leave links out"), ("format", "table for compact rows"),
                ("max", "Most bytes to return"))],
            "responses": ok}},
        "/find": {"get": {"summary": "Records anywhere on the site matching words",
                          "parameters": [{"name": "q", "in": "query", "required": True, "schema": word}],
                          "responses": ok}},
        "/map": {"get": {"summary": "Every structure with its fields and references", "responses": ok}},
        "/changes": {"get": {"summary": "What changed since a cursor",
                             "parameters": [{"name": "since", "in": "query", "schema": {"type": "integer"}}],
                             "responses": {"200": {"description": "OK"}}}},
        "/history": {"get": {"summary": "Paged change history",
                             "parameters": [{"name": n, "in": "query", "schema": {"type": "string"}}
                                            for n in ("after", "limit", "path", "since", "until")],
                             "responses": {"200": {"description": "OK"}}}},
    }
    schemas = {}
    if any(s.get("actions") or s["type"] == "form" for s in m["structures"]):
        paths["/actions"] = {"get": {"summary": "What a visitor can do here, with each action's arguments",
                                     "responses": {"200": {"description": "OK"}}}}
    paths["/changes/stream"] = {"get": {"summary": "The change feed as server-sent events",
                                        "parameters": [{"name": "since", "in": "query", "schema": {"type": "integer"}}],
                                        "responses": {"200": {"description": "text/event-stream"}}}}
    for s in m["structures"]:
        name = s["name"]
        if s["type"] == "form":
            paths[f"/forms/{name}"] = {"get": {"summary": s.get("description") or f"How to send a message to {name}",
                                               "responses": {"200": {"description": "OK"}}}}
            continue
        if "reserve" in s.get("actions", []):
            paths[f"/bookable/{name}"] = {"get": {"summary": f"How to book in {name}",
                                                  "responses": {"200": {"description": "OK"}}}}
        if s["type"] == "tree":
            paths[f"/trees/{name}"] = {"get": {
                "summary": s.get("description") or f"Tree {name}",
                "parameters": [{"name": "node", "in": "query", "schema": {"type": "string"}},
                               {"name": "depth", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "OK"}}}}
            continue
        try:
            sdef = site.readable(name, owner)
        except SiteError:
            continue
        schemas[name] = schema.public_schema(sdef, prefix, include_private=owner)
        filters = [{"name": f, "in": "query", "schema": {"type": "string"},
                    "description": "Equality filter; append .gte .gt .lte .lt .ne for comparisons."}
                   for f in schemas[name]["properties"]]
        ref = {"$ref": f"#/components/schemas/{name}"}
        paths[f"/collections/{name}"] = {"get": {
            "summary": s.get("description") or f"List {name}", "parameters": page + filters,
            "responses": {"200": {"description": "Matching records"}}}}
        paths[f"/collections/{name}/{{key}}"] = {"get": {
            "summary": f"One {name} record",
            "parameters": [{"name": "key", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "The record", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"key": {"type": "string"}, "data": ref}}}}}}}}
        if sdef.kind == "Hours":
            paths["/hours"] = {"get": {"summary": "Open or closed on a date",
                                       "parameters": [{"name": "date", "in": "query", "schema": {"type": "string"},
                                                       "description": "YYYY-MM-DD, today or tomorrow"}],
                                       "responses": {"200": {"description": "OK"}}}}
    return {"openapi": "3.1.0",
            "info": {"title": m.get("name", "Site"), "version": VERSION, "description": m.get("description", "")},
            "servers": [{"url": prefix or "/"}], "paths": paths, "components": {"schemas": schemas}}
