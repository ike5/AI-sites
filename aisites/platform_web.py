"""The hosted service's web side: a JSON door for AIs and two small pages for people.

Every step works without a browser except one: the person confirms the emailed code. The pages are
server-rendered, need no JavaScript, and exist as the fallback for people.
"""

import hmac
import json
from html import escape
from typing import Any, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from . import html, templates
from .errors import SiteError
from .platform import SESSION_DAYS, Platform

COOKIE = "aisites_session"
FORM_STYLE = """
form { margin: 0 0 20px; }
label { display: block; margin: 12px 0 4px; color: var(--muted); }
input, select { font: inherit; padding: 8px 10px; width: 100%; max-width: 420px; box-sizing: border-box;
  border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); }
button { font: inherit; padding: 8px 14px; margin-top: 14px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--fg); color: var(--bg); cursor: pointer; }
button.quiet { background: transparent; color: var(--fg); margin-top: 0; padding: 4px 10px; }
h2 { font-size: 1.1rem; margin: 28px 0 8px; }
.phrase { font: 600 1.4rem ui-monospace, monospace; padding: 10px 14px; border: 1px solid var(--line);
  border-radius: 6px; display: inline-block; }
.secret { font: 0.95rem ui-monospace, monospace; padding: 10px 14px; border: 1px solid var(--line);
  border-radius: 6px; overflow-wrap: anywhere; }
.warn { border-left: 3px solid #c77700; padding: 6px 12px; margin: 12px 0; }
td form { margin: 0; }
"""


def _page(platform: Platform, title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{html.STYLE}{FORM_STYLE}</style></head><body><main>"
        f"<h1>{escape(title)}</h1><p class='note'>{escape(platform.config['name'])}</p>{body}</main></body></html>",
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def create_platform_app(platform: Platform) -> FastAPI:
    app = FastAPI(title=platform.config["name"], openapi_url=None, docs_url=None, redoc_url=None)

    # ------------------------------------------------------------ helpers

    def base(request: Request) -> str:
        return request.scope.get("root_path", "").rstrip("/")

    def address(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def bearer(request: Request) -> Optional[str]:
        auth = request.headers.get("authorization", "")
        return auth[7:].strip() if auth[:7].lower() == "bearer " else None

    def account_of(request: Request) -> str:
        account = platform.account_for_key(bearer(request))
        if account is None:
            raise SiteError("needs_key", "This needs an account key.",
                            "Register at POST /register, or issue a key on the account page, and send it as "
                            "Authorization: Bearer <account key>.", status=401)
        return account

    async def form_of(request: Request) -> dict:
        raw = (await request.body()).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    async def json_of(request: Request) -> dict:
        raw = await request.body()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise SiteError("invalid", "The request body is not valid JSON.")
        if not isinstance(data, dict):
            raise SiteError("invalid", "The request body must be a JSON object.")
        return data

    def signed_in(request: Request, data: Optional[dict] = None) -> dict:
        """The session for this browser. For a form post, the session's own token must come with it."""
        session = platform.session(request.cookies.get(COOKIE))
        if session is None:
            raise SiteError("needs_sign_in", "Sign in first.", "Open the account page and ask for a code.",
                            status=401)
        if data is not None and not hmac.compare_digest(str(data.get("csrf", "")), session["csrf"]):
            raise SiteError("bad_form", "That form did not come from this page.", "Reload the account page "
                            "and try again.", status=403)
        return session

    def secure(request: Request) -> bool:
        return platform.config["public_url"].startswith("https://") or request.url.scheme == "https"

    @app.exception_handler(SiteError)
    async def site_error(request: Request, e: SiteError):
        if request.url.path.startswith(base(request) + "/account"):
            body = (f"<p>{escape(e.message)}</p>" + (f"<p class='note'>{escape(e.hint)}</p>" if e.hint else "")
                    + f"<p><a href='{base(request)}/account'>Back to the account page</a></p>")
            return _page(platform, "That did not work", body, e.status)
        head = {"WWW-Authenticate": "Bearer"} if e.status == 401 else {}
        if getattr(e, "retry_after", None):
            head["Retry-After"] = str(e.retry_after)
        return JSONResponse(e.as_dict(), status_code=e.status, headers=head)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, e: HTTPException):
        err = SiteError("not_found" if e.status_code == 404 else "http_error", str(e.detail),
                        f"GET {base(request)}/ describes this host. Sites live at {base(request)}/s/<name>/.",
                        status=e.status_code)
        return JSONResponse(err.as_dict(), status_code=e.status_code)

    # ------------------------------------------------------------ for AIs

    @app.get("/")
    def describe(request: Request):
        payload = platform.describe()
        fmt = request.query_params.get("format")
        if fmt == "html" or (fmt is None and "text/html" in request.headers.get("accept", "")):
            return HTMLResponse(html.render(payload, platform.config["name"], base(request)))
        return JSONResponse(payload)

    @app.get("/llms.txt")
    def overview(request: Request):
        d = platform.describe()
        lines = [f"# {d['name']}", "", f"> {d['what']}", "", "## Registering with an AI", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(d["register"]["by_an_ai"], start=1)]
        lines += ["", "## Registering in a browser", "", f"- {d['register']['by_a_person']}", "",
                  "## Templates", ""]
        lines += [f"- `{t['name']}`: {t.get('description') or t.get('title', '')}" for t in templates.describe()]
        lines += ["", f"Sites live at {d['sites_live_at']}. Each site describes itself at "
                      "/.well-known/aisite.json under its own address."]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/markdown; charset=utf-8")

    @app.get("/templates")
    def list_templates():
        return JSONResponse({"templates": templates.describe(),
                             "use": "POST /sites with {\"name\": ..., \"template\": <name>} and an account key."})

    @app.post("/register")
    async def register(request: Request):
        data = await json_of(request)
        out = await run_in_threadpool(platform.start_registration, data.get("email"), address(request))
        return JSONResponse(out)

    @app.get("/register/{ticket}")
    async def collect(request: Request, ticket: str):
        out = await run_in_threadpool(platform.collect, ticket, bearer(request))
        return JSONResponse(out, status_code=200 if out["status"] == "confirmed" else 202)

    @app.get("/sites")
    def my_sites(request: Request):
        return JSONResponse({"sites": platform.sites(account_of(request))})

    @app.post("/sites")
    async def create_site(request: Request):
        account = account_of(request)
        data = await json_of(request)
        allowed = {"name", "template", "title", "timezone", "language", "currency", "description"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise SiteError("invalid", f"Creating a site does not take: {', '.join(unknown)}.",
                            "It takes: " + ", ".join(sorted(allowed)) + ".")
        out = await run_in_threadpool(lambda: platform.create_site(account, **data))
        return JSONResponse(out, status_code=201)

    @app.post("/sites/{name}/key")
    async def site_key(request: Request, name: str):
        account = account_of(request)
        return JSONResponse(await run_in_threadpool(platform.new_owner_key, account, name))

    # ------------------------------------------------------------ for people

    def field(name: str, label: str, kind: str = "text", value: str = "", extra: str = "") -> str:
        return (f"<label for='{name}'>{escape(label)}</label>"
                f"<input id='{name}' name='{name}' type='{kind}' value='{escape(value, quote=True)}' {extra}>")

    def hidden(session: dict) -> str:
        return f"<input type='hidden' name='csrf' value='{escape(session['csrf'], quote=True)}'>"

    def shown_once(title: str, intro: str, secret: str, rows: dict, here: str) -> str:
        details = "".join(f"<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>" for k, v in rows.items())
        return (f"<p>{escape(intro)}</p><p class='secret'>{escape(secret)}</p>"
                "<p class='warn'>Copy it now. It is not stored and cannot be shown again.</p>"
                f"<dl>{details}</dl><p><a href='{here}/account'>Back to your account</a></p>")

    @app.get("/account")
    def account_page(request: Request):
        here = base(request)
        session = platform.session(request.cookies.get(COOKIE))
        if session is None:
            body = ("<p>Sign in or sign up with your email address. We send a one-time code; there is no "
                    "password. The address is used to send the code and is not kept in readable form.</p>"
                    f"<form method='post' action='{here}/account/code'>"
                    + field("email", "Email address", "email", extra="required autocomplete='email'")
                    + "<button>Send me a code</button></form>")
            return _page(platform, "Sign in", body)
        sites = platform.sites(session["account"])
        rows = "".join(
            f"<tr><td><a href='{escape(s['site'], quote=True)}'>{escape(s['name'])}</a></td>"
            f"<td>{escape(s['template'])}</td><td>{escape(s['created'])}</td>"
            f"<td>{'suspended' if s['suspended'] else 'live'}</td>"
            f"<td><form method='post' action='{here}/account/sites/{escape(s['name'], quote=True)}/key'>"
            f"{hidden(session)}<button class='quiet'>New owner key</button></form></td></tr>" for s in sites)
        options = "".join(f"<option value='{escape(t['name'], quote=True)}'>{escape(t.get('title') or t['name'])}"
                          f"</option>" for t in templates.describe())
        keys = "".join(
            f"<tr><td>{escape(k['label'] or k['id'])}</td><td>{escape(k['created'])}</td>"
            + (f"<td>revoked {escape(k['revoked'])}</td>" if k["revoked"] else
               f"<td><form method='post' action='{here}/account/keys/{escape(k['id'], quote=True)}/revoke'>"
               f"{hidden(session)}<button class='quiet'>Revoke</button></form></td>") + "</tr>"
            for k in platform.account_keys(session["account"]))
        body = (
            "<h2>Your sites</h2>"
            + (f"<div class='scroll'><table><thead><tr><th>Site</th><th>Template</th><th>Created</th>"
               f"<th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>" if sites else
               "<p class='note'>None yet.</p>")
            + f"<h2>Create a site</h2><form method='post' action='{here}/account/sites'>{hidden(session)}"
            + field("name", "Address name (letters, digits, - and _)", extra="required pattern='[a-z0-9][a-z0-9_-]{2,62}'")
            + field("title", "Site title")
            + f"<label for='template'>Start from</label><select id='template' name='template'>{options}</select>"
            + field("timezone", "Time zone (region name)", value="UTC")
            + "<button>Create site</button></form>"
            "<h2>Keys for your AI</h2><p class='note'>An account key lets an assistant create and manage "
            "your sites on this host. Each site also has its own owner key.</p>"
            + (f"<div class='scroll'><table><tbody>{keys}</tbody></table></div>" if keys else "")
            + f"<form method='post' action='{here}/account/keys'>{hidden(session)}"
            + field("label", "What is this key for?") + "<button>Issue a key</button></form>"
            f"<form method='post' action='{here}/account/signout'>{hidden(session)}"
            "<button class='quiet'>Sign out</button></form>")
        return _page(platform, "Your account", body)

    @app.post("/account/code")
    async def send_code(request: Request):
        data = await form_of(request)
        await run_in_threadpool(platform.send_code, data.get("email", ""), address(request))
        body = ("<p>If that address can receive mail, a six-digit code is on its way. It is valid for ten "
                f"minutes.</p><form method='post' action='{base(request)}/account/session'>"
                f"<input type='hidden' name='email' value='{escape(data.get('email', ''), quote=True)}'>"
                + field("code", "Code", extra="required inputmode='numeric' autocomplete='one-time-code'")
                + "<button>Sign in</button></form>")
        return _page(platform, "Enter your code", body)

    @app.post("/account/session")
    async def start_session(request: Request):
        data = await form_of(request)
        session = await run_in_threadpool(platform.sign_in, data.get("email", ""), data.get("code", ""))
        response = RedirectResponse(base(request) + "/account", status_code=303)
        # Limited to the account pages: the sites this host serves share its address and must never see it.
        response.set_cookie(COOKIE, session["token"], max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="lax", secure=secure(request), path=base(request) + "/account")
        return response

    @app.post("/account/signout")
    async def sign_out(request: Request):
        data = await form_of(request)
        signed_in(request, data)
        platform.sign_out(request.cookies.get(COOKIE))
        response = RedirectResponse(base(request) + "/account", status_code=303)
        response.delete_cookie(COOKIE, path=base(request) + "/account")
        return response

    @app.post("/account/sites")
    async def page_create_site(request: Request):
        data = await form_of(request)
        session = signed_in(request, data)
        out = await run_in_threadpool(
            platform.create_site, session["account"], data.get("name", ""), data.get("template", "blank"),
            data.get("title", ""), data.get("timezone") or "UTC")
        rows = {"Site": out["site"], "MCP connector address": out["mcp"], "Web API": out["admin"],
                "Header to send": "Authorization: Bearer <owner key>"}
        return _page(platform, f"{out['name']} is ready",
                     shown_once("", "This is your site's owner key. Give it to the AI that will build the site.",
                                out["owner_key"], rows, base(request)))

    @app.post("/account/sites/{name}/key")
    async def page_site_key(request: Request, name: str):
        data = await form_of(request)
        session = signed_in(request, data)
        out = await run_in_threadpool(platform.new_owner_key, session["account"], name)
        return _page(platform, f"New owner key for {out['name']}",
                     shown_once("", "The previous owner key no longer works.", out["owner_key"],
                                {"Site": out["site"], "MCP connector address": out["mcp"]}, base(request)))

    @app.post("/account/keys")
    async def page_issue_key(request: Request):
        data = await form_of(request)
        session = signed_in(request, data)
        key = platform.issue_account_key(session["account"], data.get("label", ""))
        return _page(platform, "Account key issued",
                     shown_once("", "Give this key to your assistant. It can create and manage your sites here.",
                                key["secret"], {"Header to send": "Authorization: Bearer <account key>"},
                                base(request)))

    @app.post("/account/keys/{key_id}/revoke")
    async def page_revoke_key(request: Request, key_id: str):
        data = await form_of(request)
        session = signed_in(request, data)
        platform.revoke_account_key(session["account"], key_id)
        return RedirectResponse(base(request) + "/account", status_code=303)

    @app.get("/account/confirm")
    def confirm_page(request: Request):
        ticket = request.query_params.get("ticket", "")
        phrase = platform.ticket_phrase(ticket)
        body = (
            "<p>An AI assistant asked to create and manage sites for your email address on this host.</p>"
            f"<p>It should have shown you this phrase:</p><p class='phrase'>{escape(phrase)}</p>"
            "<p class='warn'>Only continue if your own assistant showed you exactly this phrase. Confirming "
            "gives that assistant a key to create and manage your sites here.</p>"
            f"<form method='post' action='{base(request)}/account/confirm'>"
            f"<input type='hidden' name='ticket' value='{escape(ticket, quote=True)}'>"
            + field("code", "Code from the email", extra="required inputmode='numeric' autocomplete='one-time-code'")
            + "<button>Allow this assistant</button></form>")
        return _page(platform, "Confirm your assistant", body)

    @app.post("/account/confirm")
    async def confirm(request: Request):
        data = await form_of(request)
        await run_in_threadpool(platform.confirm_registration, data.get("ticket", ""), data.get("code", ""))
        return _page(platform, "Confirmed", "<p>Your assistant can now continue. You can close this page.</p>"
                     f"<p class='note'>To review or revoke its key, <a href='{base(request)}/account'>sign in to "
                     "your account</a>.</p>")

    return app

