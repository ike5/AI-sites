"""The web door: read routes for visiting agents and admin routes for the owner."""

import json
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from . import html, ops, schema, timeview
from .errors import SiteError
from .site import Site, VERSION

RESERVED = {"limit", "cursor", "fields", "sort", "format"}


def create_app(site: Site) -> FastAPI:
    app = FastAPI(title=site.config.get("name", "AI site"), openapi_url=None, docs_url=None, redoc_url=None)
    app.state.site = site

    # ------------------------------------------------------------ helpers

    def prefix(request: Request) -> str:
        return request.scope.get("root_path", "").rstrip("/")

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
        return found is not None and found.kind == "owner"

    def headers(request: Request, etag: str) -> dict:
        return {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept, Authorization",
                "Link": f'<{prefix(request)}/.well-known/aisite.json>; rel="describedby"'}

    def reply(request: Request, payload: Any, owner: bool, title: str, etag_extra: str = "") -> Response:
        """JSON by default; HTML only when asked for. Every reply is cheap to revalidate."""
        etag = site.etag(owner, etag_extra)
        head = headers(request, etag)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=head)
        fmt = request.query_params.get("format")
        if fmt == "html" or (fmt is None and "text/html" in request.headers.get("accept", "")):
            return HTMLResponse(html.render(payload, title, prefix(request)), headers=head)
        return JSONResponse(payload, headers=head)

    @app.exception_handler(SiteError)
    async def site_error(request: Request, e: SiteError):
        head = {"WWW-Authenticate": "Bearer"} if e.status == 401 else {}
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
        return reply(request, site.manifest(owner, prefix(request)), owner, site.config.get("name", "Site"))

    @app.get("/llms.txt")
    def overview(request: Request):
        owner = is_owner(request)
        return PlainTextResponse(llms_txt(site, owner, prefix(request)), media_type="text/markdown; charset=utf-8",
                                 headers=headers(request, site.etag(owner)))

    @app.get("/openapi.json")
    def openapi(request: Request):
        owner = is_owner(request)
        return JSONResponse(openapi_doc(site, owner, prefix(request)), headers=headers(request, site.etag(owner)))

    # ------------------------------------------------------------ collections

    @app.get("/collections/{name}")
    def list_records(request: Request, name: str):
        owner = is_owner(request)
        sdef = site.readable(name, owner)
        q = request.query_params
        where = {k: v for k, v in q.items() if k not in RESERVED}
        fields = [f for f in q.get("fields", "").split(",") if f] or None
        out = site.list(name, where, q.get("sort"), q.get("limit", 100), q.get("cursor"), fields, owner=owner)
        if q.get("format") == "table":
            cols = fields or [f.name for f in sdef.fields if owner or f.policy != "private"]
            out = {"collection": name, "columns": cols,
                   "rows": [[item["data"].get(c) for c in cols] for item in out["items"]],
                   **{k: out[k] for k in ("total", "next", "as_of") if k in out}}
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
        out = site.read_record(name, key, owner)
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

    @app.get("/hours")
    def hours_all(request: Request):
        owner = is_owner(request)
        date = request.query_params.get("date")
        return reply(request, site.hours(date, None, owner), owner, "Opening hours", _day_tag(site, date))

    @app.get("/hours/{key}")
    def hours_one(request: Request, key: str):
        owner = is_owner(request)
        date = request.query_params.get("date")
        return reply(request, site.hours(date, key, owner), owner, "Opening hours", _day_tag(site, date))

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


def _day_tag(site: Site, date: Optional[str]) -> str:
    """Answers for 'today' change at midnight without any write, so the day is part of the ETag."""
    return "." + timeview.resolve_date(date or "today", site.tz, site.now()).isoformat()


def llms_txt(site: Site, owner: bool, prefix: str) -> str:
    m = site.manifest(owner, prefix)
    lines = [f"# {m.get('name', 'Site')}", ""]
    if m.get("description"):
        lines += [f"> {m['description']}", ""]
    lines += [
        "This site publishes its information for AI agents as JSON.",
        f"Start at {prefix}/.well-known/aisite.json, which lists everything below with schemas.", "",
        f"- Time zone: {m.get('timezone', 'UTC')}",
    ]
    if m.get("language"):
        lines.append(f"- Language: {m['language']}")
    if m.get("currency"):
        lines.append(f"- Currency: {m['currency']}")
    lines += [f"- First published: {m['first_published']}", "", "## What is here", ""]
    for s in m["structures"]:
        kind = f" ({s['kind']})" if s.get("kind") else ""
        note = f" {s['note']}" if s.get("note") else ""
        desc = f": {s['description']}" if s.get("description") else ""
        lines.append(f"- [{s['name']}]({s['href']}) {s['type']}{kind}{desc}{note}")
    if m.get("answers"):
        lines += ["", "## Direct answers", "", f"- Open on a date: {m['answers']['open_on_date']}"]
    lines += ["", "## How to read", ""] + [f"- {line}" for line in m["reading"]]
    return "\n".join(lines) + "\n"


def openapi_doc(site: Site, owner: bool, prefix: str) -> dict:
    """An OpenAPI description generated from the structures this reader may see."""
    m = site.manifest(owner, prefix)
    page = [{"name": n, "in": "query", "schema": {"type": "string"}} for n in ("limit", "cursor", "sort", "fields")]
    paths: dict = {
        "/.well-known/aisite.json": {"get": {"summary": "Site manifest", "responses": {"200": {"description": "OK"}}}},
        "/changes": {"get": {"summary": "What changed since a cursor",
                             "parameters": [{"name": "since", "in": "query", "schema": {"type": "integer"}}],
                             "responses": {"200": {"description": "OK"}}}},
        "/history": {"get": {"summary": "Paged change history",
                             "parameters": [{"name": n, "in": "query", "schema": {"type": "string"}}
                                            for n in ("after", "limit", "path", "since", "until")],
                             "responses": {"200": {"description": "OK"}}}},
    }
    schemas = {}
    for s in m["structures"]:
        name = s["name"]
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
