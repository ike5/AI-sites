"""The hosted service's web side: a JSON door for AIs and two small pages for people.

Every step works without a browser except one: the person confirms the emailed code. The pages are
server-rendered, need no JavaScript, and exist as the fallback for people.
"""

import hmac
import json
import re
import secrets
from html import escape
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from . import host_ask, html, templates
from .errors import SiteError
from .passkeys import RESET_HOURS
from .platform import SESSION_DAYS, Platform
from .reading import tag_of

COOKIE = "aisites_session"
NEXT = re.compile(r"^req_[A-Za-z0-9_-]{10,60}$")
FORM_STYLE = """
form { margin: 0 0 20px; }
label { display: block; margin: 12px 0 4px; color: var(--muted); }
input, select { font: inherit; padding: 8px 10px; width: 100%; max-width: 420px; box-sizing: border-box;
  border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); }
button { font: inherit; padding: 8px 14px; margin-top: 14px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--fg); color: var(--bg); cursor: pointer; display: block; }
button.quiet { background: transparent; color: var(--fg); margin-top: 0; padding: 4px 10px; display: inline-block; }
.choice { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
.choice button { margin-top: 0; }
.choice button.quiet { padding: 8px 14px; }
.claim { overflow-wrap: anywhere; }
form.end { margin-top: 28px; }
h2 { font-size: 1.1rem; margin: 28px 0 8px; }
.secret { font: 0.95rem ui-monospace, monospace; padding: 10px 14px; border: 1px solid var(--line);
  border-radius: 6px; overflow-wrap: anywhere; }
.warn { border-left: 3px solid #c77700; padding: 6px 12px; margin: 12px 0; }
td form { margin: 0; }
"""


# The only script these pages ever run, and only on pages that offer a passkey. A form marked with
# data-passkey is held back on submit until the browser's passkey step is done, then sent as usual.
PASSKEY_JS = """
(() => {
  const enc = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  const dec = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  const post = async (url, data) => {
    const r = await fetch(url, {method: 'POST', credentials: 'same-origin',
                                headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error ? j.error.message + (j.error.hint ? ' ' + j.error.hint : '') : 'That did not work.');
    return j;
  };
  const pack = c => {
    const r = c.response, out = {id: c.id, rawId: enc(c.rawId), type: c.type, response: {clientDataJSON: enc(r.clientDataJSON)}};
    if (r.attestationObject) out.response.attestationObject = enc(r.attestationObject);
    else {
      out.response.authenticatorData = enc(r.authenticatorData);
      out.response.signature = enc(r.signature);
      if (r.userHandle) out.response.userHandle = enc(r.userHandle);
    }
    return out;
  };
  const use = async o => {
    o.challenge = dec(o.challenge);
    (o.allowCredentials || []).forEach(c => { c.id = dec(c.id); });
    return pack(await navigator.credentials.get({publicKey: o}));
  };
  const make = async o => {
    o.challenge = dec(o.challenge); o.user.id = dec(o.user.id);
    (o.excludeCredentials || []).forEach(c => { c.id = dec(c.id); });
    return pack(await navigator.credentials.create({publicKey: o}));
  };
  const set = (form, name, value) => {
    let el = form.querySelector('input[type=hidden][name="' + name + '"]');
    if (!el) { el = document.createElement('input'); el.type = 'hidden'; el.name = name; form.appendChild(el); }
    el.value = value;
  };
  const say = (form, text) => {
    let el = form.querySelector('.passkey-error');
    if (!el) { el = document.createElement('p'); el.className = 'warn passkey-error'; form.appendChild(el); }
    el.textContent = text;
  };
  const confirmWith = async (form, action) => {
    const csrf = form.querySelector('input[name=csrf]').value;
    const start = await post(form.dataset.base + '/account/passkey/confirm/options', {csrf, action});
    await post(form.dataset.base + '/account/passkey/confirm',
               {csrf, challenge: start.challenge_id, credential: await use(start.options)});
  };
  document.querySelectorAll('form[data-passkey]').forEach(form => form.addEventListener('submit', async e => {
    const how = form.dataset.passkey, by = e.submitter;
    if (form.dataset.done || (by && by.value === 'deny')) return;
    e.preventDefault();
    try {
      if (!window.PublicKeyCredential) throw new Error('This browser cannot use passkeys.');
      const base = form.dataset.base;
      if (how === 'confirm') await confirmWith(form, form.dataset.action);
      if (how === 'signin' || how === 'ticket') {
        const start = await post(base + form.dataset.options, how === 'ticket' ? {ticket: form.dataset.ticket} : {});
        set(form, 'challenge', start.challenge_id);
        set(form, 'credential', JSON.stringify(await use(start.options)));
      }
      if (how === 'add') {
        if (form.dataset.action) await confirmWith(form, form.dataset.action);
        const csrf = form.querySelector('input[name=csrf]').value;
        const start = await post(base + '/account/passkeys/options', {csrf, name: form.querySelector('[name=name]').value});
        set(form, 'challenge', start.challenge_id);
        set(form, 'credential', JSON.stringify(await make(start.options)));
      }
      if (by && by.name) set(form, by.name, by.value);
      form.dataset.done = '1';
      form.submit();
    } catch (err) {
      say(form, err.name === 'NotAllowedError' ? 'The passkey step was cancelled or timed out.' : err.message);
    }
  }));
})();
"""


def _page(platform: Platform, title: str, body: str, status: int = 200, script: bool = False) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    script = script and platform.passkeys.unavailable() is None
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{html.STYLE}{FORM_STYLE}</style></head><body><main>"
        f"<h1>{escape(title)}</h1><p class='note'>{escape(platform.config['name'])}</p>{body}</main>"
        + (f"<script nonce='{nonce}'>{PASSKEY_JS}</script>" if script else "") + "</body></html>",
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     # These pages hold approve buttons: never let another page frame them.
                                     "X-Frame-Options": "DENY",
                                     # No script runs here except the one above, on pages that offer a passkey.
                                     "Content-Security-Policy": "frame-ancestors 'none'; script-src "
                                                                + (f"'nonce-{nonce}'" if script else "'none'")})


def create_platform_app(platform: Platform, host: Any = None) -> FastAPI:
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
                            "Sign up at POST /signup, or issue a key on the account page, and send it as "
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

    def same_origin(request: Request) -> None:
        """Refuse a sign-in sent from another site's page, which could sign someone in to an account not theirs."""
        origin, here = request.headers.get("origin"), platform.config["public_url"]
        if origin and here and origin.rstrip("/") != here:
            raise SiteError("bad_form", "That form did not come from this site.",
                            "Open the account page here and try again.", status=403)

    def secure(request: Request) -> bool:
        return platform.config["public_url"].startswith("https://") or request.url.scheme == "https"

    @app.exception_handler(SiteError)
    async def site_error(request: Request, e: SiteError):
        wants_json = request.headers.get("content-type", "").startswith("application/json")
        if request.url.path.startswith(base(request) + "/account") and not wants_json:
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

    def full(request: Request) -> str:
        """This host's whole address, for links an assistant can open as they are."""
        return (platform.config["public_url"] or str(request.base_url)).rstrip("/")

    @app.get("/")
    def describe(request: Request):
        payload = platform.describe()
        across = host_ask.listing(host, full(request)) if host is not None else []
        if across:
            payload["ask"] = across
            payload["asking"] = ("Open a url to see which sites answer it, or add words: " + full(request)
                                 + "/ask/Offer?q=<words> asks every site at once. Each site also answers at "
                                   "/s/<name>/ask.")
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

    if host is not None:
        @app.get("/ask")
        def ask_list(request: Request):
            return JSONResponse({"ask": host_ask.listing(host, full(request)),
                                 "how": "Each id is a kind of thing that means the same on every site. Open its "
                                        "url to see which sites answer it; add words or filters to ask them all.",
                                 "as_of": platform.now_z()})

        @app.get("/ask/{kind}")
        def ask_across(request: Request, kind: str):
            out = host_ask.ask(host, kind, dict(request.query_params), full(request))
            return JSONResponse(out, headers={"ETag": tag_of(out), "Cache-Control": "no-cache"})

        @app.get("/find")
        def find_across(request: Request):
            q = request.query_params
            return JSONResponse(host_ask.find(host, q.get("q"), q.get("limit", 10), full(request)))

    @app.get("/templates")
    def list_templates():
        return JSONResponse({"templates": templates.describe(),
                             "use": "POST /sites with {\"name\": ..., \"template\": <name>} and an account key."})

    @app.post("/signup")
    async def signup(request: Request):
        data = await json_of(request)
        out = await run_in_threadpool(platform.start_registration, data.get("email"), address(request))
        return JSONResponse(out)

    @app.get("/signup/{ticket}")
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

    def next_field(data: Any) -> str:
        """Carry an approval request through sign-in. Only a request id is carried, never an address."""
        wanted = str(data.get("next", ""))
        return f"<input type='hidden' name='next' value='{wanted}'>" if NEXT.match(wanted) else ""

    def sign_in_form(here: str, carry: str = "") -> str:
        with_passkey = "" if platform.passkeys.unavailable() else (
            f"<form method='post' action='{here}/account/passkey/session' data-passkey='signin' "
            f"data-base='{here}' data-options='/account/passkey/options'>{carry}"
            "<button class='quiet'>Sign in with a passkey</button></form>")
        return (f"<form method='post' action='{here}/account/code'>{carry}"
                + field("email", "Email address", "email", extra="required autocomplete='email'")
                + "<button>Send me a code</button></form>" + with_passkey)

    def guarded(session: dict, here: str, action: str) -> str:
        """Attributes for a form whose action hands out control: it asks for the passkey first, if there is one."""
        if not platform.passkeys.has(session["account"]):
            return ""
        return f" data-passkey='confirm' data-base='{here}' data-action='{escape(action, quote=True)}'"

    def set_cookie(request: Request, response: Any, session: dict) -> Any:
        # Limited to the account pages: the sites this host serves share its address and must never see it.
        response.set_cookie(COOKIE, session["token"], max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="lax", secure=secure(request), path=base(request) + "/account")
        return response

    def passkey_section(session: dict, here: str) -> str:
        keys = platform.passkeys
        why = keys.unavailable()
        if why:
            return f"<h2>Passkeys</h2><p class='note'>Not available on this host. {escape(why)}</p>"
        mine = keys.list(session["account"])
        rows = "".join(
            f"<tr><td>{escape(p['name'])}</td><td>{escape(p['created'])}</td><td>{escape(p['last_used'] or 'not yet')}</td>"
            f"<td><form method='post' action='{here}/account/passkeys/{escape(p['id'], quote=True)}/remove'"
            f"{guarded(session, here, 'remove_passkey:' + p['id'])}>{hidden(session)}"
            "<button class='quiet'>Remove</button></form></td></tr>" for p in mine)
        out = ("<h2>Passkeys</h2><p class='note'>A passkey signs you in without a code. Once you have one, handing "
               "out control needs it: approving an assistant, issuing a key, and adding or removing a passkey. "
               "Signing in by email still works for looking at your account.</p>")
        if mine:
            out += ("<div class='scroll'><table><thead><tr><th>Passkey</th><th>Added</th><th>Last used</th><th></th>"
                    f"</tr></thead><tbody>{rows}</tbody></table></div>")
        confirm_first = " data-action='add_passkey'" if mine else ""
        out += (f"<form method='post' action='{here}/account/passkeys' data-passkey='add' data-base='{here}'"
                f"{confirm_first}>{hidden(session)}"
                + field("name", "A name for this passkey, such as the device it is on", extra="maxlength='60'")
                + "<button>Add a passkey</button></form>")
        due = keys.pending_reset(session["account"])
        if due:
            out += (f"<p class='warn'>All passkeys on this account will be removed at {escape(due)}. Using a passkey "
                    f"before then cancels this.</p><form method='post' action='{here}/account/passkeys/reset/cancel'>"
                    f"{hidden(session)}<button class='quiet'>Cancel the removal</button></form>")
        elif mine:
            out += ("<details><summary>I have lost my passkey</summary><p class='note'>Your passkeys can be removed "
                    f"after a wait of {RESET_HOURS} hours. A notice is emailed to you now, and using any passkey "
                    "before the wait ends cancels it.</p>"
                    f"<form method='post' action='{here}/account/passkeys/reset'>{hidden(session)}"
                    + field("email", "Your email address", "email", extra="required autocomplete='email'")
                    + "<button class='quiet'>Remove my passkeys after the wait</button></form></details>")
        return out

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
                    + sign_in_form(here))
            return _page(platform, "Sign in", body, script=True)
        sites = platform.sites(session["account"])
        rows = "".join(
            f"<tr><td><a href='{escape(s['site'], quote=True)}'>{escape(s['name'])}</a></td>"
            f"<td>{escape(s['template'])}</td><td>{escape(s['created'])}</td>"
            f"<td>{'suspended' if s['suspended'] else 'live'}</td>"
            f"<td><form method='post' action='{here}/account/sites/{escape(s['name'], quote=True)}/key'"
            f"{guarded(session, here, 'owner_key:' + s['name'])}>"
            f"{hidden(session)}<button class='quiet'>New owner key</button></form></td></tr>" for s in sites)
        options = "".join(f"<option value='{escape(t['name'], quote=True)}'>{escape(t.get('title') or t['name'])}"
                          f"</option>" for t in templates.describe())
        keys = "".join(
            f"<tr><td>{escape(k['label'] or k['id'])}</td><td>{escape(k['created'])}</td>"
            + (f"<td>revoked {escape(k['revoked'])}</td>" if k["revoked"] else
               f"<td><form method='post' action='{here}/account/keys/{escape(k['id'], quote=True)}/revoke'>"
               f"{hidden(session)}<button class='quiet'>Revoke</button></form></td>") + "</tr>"
            for k in platform.account_keys(session["account"]))
        connected = "".join(
            f"<tr><td class='claim'>{escape(c['assistant'])}</td><td>{escape(c['site'])}</td>"
            f"<td>{escape(c['connected'])}</td><td>{escape(c['last_used'] or 'not yet')}</td>"
            f"<td><form method='post' action='{here}/account/connections/{escape(c['id'], quote=True)}/disconnect'>"
            f"{hidden(session)}<button class='quiet'>Disconnect</button></form></td></tr>"
            for c in platform.connections.connections(session["account"]))
        body = (
            "<h2>Your sites</h2>"
            + (f"<div class='scroll'><table><thead><tr><th>Site</th><th>Template</th><th>Created</th>"
               f"<th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>" if sites else
               "<p class='note'>None yet.</p>")
            + f"<h2>Create a site</h2><form method='post' action='{here}/account/sites'>{hidden(session)}"
            + field("name", "Address name (letters, digits, - and _)", extra="required pattern='[a-z0-9][a-z0-9_\\-]{2,62}'")
            + field("title", "Site title")
            + f"<label for='template'>Start from</label><select id='template' name='template'>{options}</select>"
            + field("timezone", "Time zone (region name)", value="UTC")
            + "<button>Create site</button></form>"
            "<h2>Keys for your AI</h2><p class='note'>An account key lets an assistant create and manage "
            "your sites on this host. Each site also has its own owner key.</p>"
            + (f"<div class='scroll'><table><tbody>{keys}</tbody></table></div>" if keys else "")
            + f"<form method='post' action='{here}/account/keys'{guarded(session, here, 'account_key')}>"
            + hidden(session) + field("label", "What is this key for?") + "<button>Issue a key</button></form>"
            "<h2>Connected assistants</h2><p class='note'>Assistants you approved through their sign-in flow. "
            "Each has full control of the one site shown, until you disconnect it. The name is what the "
            "assistant called itself.</p>"
            + (f"<div class='scroll'><table><thead><tr><th>Assistant</th><th>Site</th><th>Connected</th>"
               f"<th>Last used</th><th></th></tr></thead><tbody>{connected}</tbody></table></div>" if connected
               else "<p class='note'>None.</p>")
            + passkey_section(session, here)
            + f"<form class='end' method='post' action='{here}/account/signout'>{hidden(session)}"
            "<button class='quiet'>Sign out</button></form>")
        return _page(platform, "Your account", body, script=True)

    @app.post("/account/code")
    async def send_code(request: Request):
        data = await form_of(request)
        await run_in_threadpool(platform.send_code, data.get("email", ""), address(request))
        body = ("<p>If that address can receive mail, a six-digit code is on its way. It is valid for ten "
                f"minutes.</p><form method='post' action='{base(request)}/account/session'>{next_field(data)}"
                f"<input type='hidden' name='email' value='{escape(data.get('email', ''), quote=True)}'>"
                + field("code", "Code", extra="required inputmode='numeric' autocomplete='one-time-code'")
                + "<button>Sign in</button></form>")
        return _page(platform, "Enter your code", body)

    @app.post("/account/session")
    async def start_session(request: Request):
        same_origin(request)
        data = await form_of(request)
        session = await run_in_threadpool(platform.sign_in, data.get("email", ""), data.get("code", ""))
        wanted = str(data.get("next", ""))
        after = "/account/authorize?request=" + wanted if NEXT.match(wanted) else "/account"
        return set_cookie(request, RedirectResponse(base(request) + after, status_code=303), session)

    # ------------------------------------------------------------ passkeys

    @app.post("/account/passkey/options")
    async def passkey_sign_in_options(request: Request):
        await json_of(request)
        return JSONResponse(await run_in_threadpool(platform.passkeys.start_using, "sign_in", None, "",
                                                    address(request)))

    @app.post("/account/passkey/session")
    async def passkey_session(request: Request):
        same_origin(request)
        data = await form_of(request)
        account = await run_in_threadpool(platform.passkeys.sign_in, data.get("challenge"), data.get("credential"))
        session = await run_in_threadpool(platform.start_session, account)
        wanted = str(data.get("next", ""))
        after = "/account/authorize?request=" + wanted if NEXT.match(wanted) else "/account"
        return set_cookie(request, RedirectResponse(base(request) + after, status_code=303), session)

    @app.post("/account/passkey/confirm/options")
    async def passkey_confirm_options(request: Request):
        data = await json_of(request)
        session = signed_in(request, data)
        return JSONResponse(await run_in_threadpool(
            platform.passkeys.start_using, "confirm", session["account"], str(data.get("action", ""))[:200]))

    @app.post("/account/passkey/confirm")
    async def passkey_confirm(request: Request):
        data = await json_of(request)
        session = signed_in(request, data)
        action = await run_in_threadpool(platform.passkeys.confirm, session, data.get("challenge"),
                                         data.get("credential"))
        return JSONResponse({"confirmed": action})

    @app.post("/account/confirm/options")
    async def ticket_confirm_options(request: Request):
        data = await json_of(request)
        platform.check_ticket(data.get("ticket"))
        return JSONResponse(await run_in_threadpool(
            platform.passkeys.start_using, "confirm", None, "ticket:" + str(data.get("ticket")), address(request)))

    @app.post("/account/passkeys/options")
    async def passkey_add_options(request: Request):
        data = await json_of(request)
        session = signed_in(request, data)
        platform.passkeys.require(session, "add_passkey")
        return JSONResponse(await run_in_threadpool(platform.passkeys.start_adding, session["account"],
                                                    data.get("name")))

    @app.post("/account/passkeys")
    async def passkey_add(request: Request):
        data = await form_of(request)
        session = signed_in(request, data)
        await run_in_threadpool(platform.passkeys.finish_adding, session["account"], data.get("challenge"),
                                data.get("credential"))
        return RedirectResponse(base(request) + "/account", status_code=303)

    @app.post("/account/passkeys/reset")
    async def passkey_reset(request: Request):
        data = await form_of(request)
        session = signed_in(request, data)
        due = await run_in_threadpool(platform.passkeys.ask_removal, session["account"], data.get("email", ""))
        return _page(platform, "Passkeys will be removed",
                     f"<p>All passkeys on this account will be removed at {escape(due)}. A notice has been sent to "
                     "your email address.</p><p class='note'>Using any passkey before then cancels this. You can "
                     f"also cancel it on <a href='{base(request)}/account'>your account page</a>.</p>")

    @app.post("/account/passkeys/reset/cancel")
    async def passkey_reset_cancel(request: Request):
        data = await form_of(request)
        session = signed_in(request, data)
        platform.passkeys.cancel_removal(session["account"])
        return RedirectResponse(base(request) + "/account", status_code=303)

    @app.post("/account/passkeys/{passkey_id}/remove")
    async def passkey_remove(request: Request, passkey_id: str):
        data = await form_of(request)
        session = signed_in(request, data)
        platform.passkeys.require(session, "remove_passkey:" + passkey_id)
        platform.passkeys.remove(session["account"], passkey_id)
        return RedirectResponse(base(request) + "/account", status_code=303)

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
        platform.passkeys.require(session, "owner_key:" + name)
        out = await run_in_threadpool(platform.new_owner_key, session["account"], name)
        return _page(platform, f"New owner key for {out['name']}",
                     shown_once("", "The previous owner key no longer works.", out["owner_key"],
                                {"Site": out["site"], "MCP connector address": out["mcp"]}, base(request)))

    @app.post("/account/keys")
    async def page_issue_key(request: Request):
        data = await form_of(request)
        session = signed_in(request, data)
        platform.passkeys.require(session, "account_key")
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

    @app.post("/account/connections/{connection}/disconnect")
    async def page_disconnect(request: Request, connection: str):
        data = await form_of(request)
        session = signed_in(request, data)
        await run_in_threadpool(platform.connections.disconnect, session["account"], connection)
        return RedirectResponse(base(request) + "/account", status_code=303)

    # ------------------------------------------------------------ approving an assistant

    @app.get("/account/authorize")
    def authorize_page(request: Request):
        """Where an assistant's sign-in flow brings the person: sign in, then approve or deny."""
        here = base(request)
        pending = platform.connections.pending(request.query_params.get("request"))
        returns_to = urlsplit(pending["returns_to"]).netloc or pending["returns_to"]
        claim = (f"<p>An assistant that calls itself <strong class='claim'>{escape(pending['client_name'])}"
                 f"</strong> is asking to connect. If you approve, you are sent back to it at "
                 f"<strong class='claim'>{escape(returns_to)}</strong>.</p>"
                 "<p class='note'>The name is the assistant's own claim and this host cannot check it. "
                 "The address it returns to is checked.</p>")
        session = platform.session(request.cookies.get(COOKIE))
        if session is None:
            carry = f"<input type='hidden' name='next' value='{pending['id']}'>"
            return _page(platform, "Sign in to connect an assistant", claim
                         + "<p>Sign in with your email address first. We send a one-time code; there is no "
                           "password.</p>" + sign_in_form(here, carry), script=True)
        mine = [s for s in platform.sites(session["account"]) if not s["suspended"]]
        start = (f"<form method='post' action='{here}/account/authorize'"
                 f"{guarded(session, here, 'approve:' + pending['id'])}>{hidden(session)}"
                 f"<input type='hidden' name='request' value='{pending['id']}'>")
        deny = "<button class='quiet' name='decision' value='deny'>Deny</button>"
        if pending["site"] and pending["site"] not in [s["name"] for s in mine]:
            body = (f"<p class='warn'>It asked for the site <strong>{escape(pending['site'])}</strong>, which is "
                    "not one of your sites on this account, so it cannot be approved here.</p>"
                    f"{start}<div class='choice'>{deny}</div></form>")
        elif not mine:
            body = (f"<p class='warn'>You have no site to connect yet. <a href='{here}/account'>Create one on "
                    f"your account page</a>, then connect the assistant again.</p>{start}<div class='choice'>"
                    f"{deny}</div></form>")
        else:
            if pending["site"]:
                which = f"<p>Site: <strong>{escape(pending['site'])}</strong></p>"
            else:
                options = "".join(f"<option value='{escape(s['name'], quote=True)}'>{escape(s['name'])}</option>"
                                  for s in mine)
                which = f"<label for='site'>Which site?</label><select id='site' name='site'>{options}</select>"
            body = (f"{start}{which}"
                    "<p class='warn'>Allowing gives this assistant <strong>full control of that one site</strong>: "
                    "it can read, change and delete everything on it, including private content, exactly as "
                    "the site's owner key can. It gets no access to your other sites or your account. You "
                    "can disconnect it on your account page at any time.</p>"
                    + ("<p class='note'>Allowing will ask for your passkey.</p>"
                       if platform.passkeys.has(session["account"]) else "")
                    + "<div class='choice'><button name='decision' value='allow'>Allow</button>"
                    f"{deny}</div></form>")
        return _page(platform, "Connect an assistant", claim + body, script=True)

    @app.post("/account/authorize")
    async def authorize_decide(request: Request):
        data = await form_of(request)
        session = signed_in(request, data)
        c = platform.connections
        if data.get("decision") == "allow":
            platform.passkeys.require(session, "approve:" + str(data.get("request", "")))
            back = await run_in_threadpool(c.approve, data.get("request"), session["account"], data.get("site"))
        else:
            back = await run_in_threadpool(c.deny, data.get("request"))
        return RedirectResponse(back, status_code=303, headers={"Cache-Control": "no-store"})

    @app.get("/account/confirm")
    def confirm_page(request: Request):
        ticket = request.query_params.get("ticket", "")
        platform.check_ticket(ticket)
        existing = platform.ticket_account(ticket)
        needs = bool(existing and platform.passkeys.has(existing))
        here = base(request)
        with_passkey = (f" data-passkey='ticket' data-base='{here}' data-options='/account/confirm/options' "
                        f"data-ticket='{escape(ticket, quote=True)}'") if needs else ""
        body = (
            "<p>An AI assistant asked to create and manage sites for your email address on this host.</p>"
            "<p class='warn'>Only continue if you asked your own assistant to do this just now. Confirming "
            "gives that assistant a key to create and manage your sites here.</p>"
            + ("<p class='note'>This account has a passkey, so confirming will ask for it as well as the code.</p>"
               if needs else "")
            + f"<form method='post' action='{here}/account/confirm'{with_passkey}>"
            f"<input type='hidden' name='ticket' value='{escape(ticket, quote=True)}'>"
            + field("code", "Code from the email", extra="required inputmode='numeric' autocomplete='one-time-code'")
            + "<button>Allow this assistant</button></form>")
        return _page(platform, "Confirm your assistant", body, script=needs)

    @app.post("/account/confirm")
    async def confirm(request: Request):
        data = await form_of(request)
        await run_in_threadpool(platform.confirm_registration, data.get("ticket", ""), data.get("code", ""),
                                {"challenge": data.get("challenge"), "credential": data.get("credential")})
        return _page(platform, "Confirmed", "<p>Your assistant can now continue. You can close this page.</p>"
                     f"<p class='note'>To review or revoke its key, <a href='{base(request)}/account'>sign in to "
                     "your account</a>.</p>")

    # ------------------------------------------------------------ the sign-in flow's own endpoints

    if platform.connections.available():
        from . import oauth

        def protected_resource(name: str):
            """Tells an assistant which host signs people in for this site's MCP door."""
            if platform.connections.site_of(platform.connections.resource_of(name)) is None:
                raise HTTPException(404, "No such site.")
            return JSONResponse(platform.connections.resource_metadata(name),
                                headers={"Cache-Control": "public, max-age=3600",
                                         "Access-Control-Allow-Origin": "*"})

        app.get("/.well-known/oauth-protected-resource/s/{name}/mcp")(protected_resource)
        app.get("/.well-known/oauth-protected-resource/s/{name}/mcp/")(protected_resource)
        app.router.routes.extend(oauth.routes(platform.connections))

    return app

