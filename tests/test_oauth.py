"""The sign-in flow for assistants: an assistant is approved for one site and gets a token for it."""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from aisites import Site
from aisites.cli import main
from aisites.errors import SiteError
from aisites.host import Host
from aisites.mail import MemoryMailer
from aisites.oauth import Connections
from aisites.platform import Platform
from tests.util import auth

ANA, BO = "ana@example.org", "bo@example.org"
HOST = "https://host.example"
CALLBACK = "https://assistant.example/callback"
MCP_HEAD = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def code_in(mailer):
    return re.search(r"\b(\d{6})\b", mailer.sent[-1]["text"]).group(1)


def tool(name, args):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}


class World:
    """A host, a person's browser and an assistant, with helpers for each step of the flow."""

    def __init__(self, tmp_path, clock, **config):
        self.clock, self.mailer, self.folder = clock, MemoryMailer(), tmp_path / "sites"
        Platform.init(self.folder, clock, name="Test Host", public_url=config.pop("public_url", HOST),
                      **config).close()
        self.host = Host(self.folder, clock, self.mailer)
        self.platform = self.host.platform
        self.browser = TestClient(self.host, base_url=HOST)     # the person: keeps the session cookie
        self.assistant = TestClient(self.host, base_url=HOST)   # the assistant: no cookies of the person's

    # --- the person

    def sign_in(self, email=ANA, browser=None, **extra):
        browser = browser or self.browser
        browser.post("/account/code", data={"email": email, **extra})
        return browser.post("/account/session", data={"email": email, "code": code_in(self.mailer), **extra},
                            follow_redirects=False)

    def csrf(self, browser=None):
        return re.search(r"name='csrf' value='([^']+)'", (browser or self.browser).get("/account").text).group(1)

    def site(self, name, email=ANA, browser=None):
        browser = browser or self.browser
        if "Your sites" not in browser.get("/account").text:
            self.sign_in(email, browser)
        r = browser.post("/account/sites", data={"csrf": self.csrf(browser), "name": name, "template": "notice_board"})
        assert r.status_code == 200, r.text
        return re.search(r"class='secret'>([^<]+)<", r.text).group(1)

    def decide(self, request, decision="allow", browser=None, **extra):
        browser = browser or self.browser
        return browser.post("/account/authorize", follow_redirects=False,
                            data={"csrf": self.csrf(browser), "request": request, "decision": decision, **extra})

    # --- the assistant

    def register(self, **changes):
        body = {"redirect_uris": [CALLBACK], "client_name": "Test assistant", "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], **changes}
        r = self.assistant.post("/register", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def ask(self, client, site=None, verifier=None, **changes):
        """The assistant's authorization request. Returns (approval request id, PKCE verifier)."""
        verifier = verifier or secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz", **changes}
        if site:
            params["resource"] = f"{HOST}/s/{site}/mcp"
        r = self.assistant.get("/authorize", params=params, follow_redirects=False)
        assert r.status_code == 302, r.text
        where = urlsplit(r.headers["location"])
        if where.path != "/account/authorize":
            return r, verifier
        return parse_qs(where.query)["request"][0], verifier

    def token(self, client, verifier, code, **changes):
        return self.assistant.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
            "client_id": client["client_id"], "code_verifier": verifier, **changes})

    def refresh(self, client, refresh_token):
        return self.assistant.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                                   "client_id": client["client_id"]})

    def connect(self, site, client=None):
        """The whole flow for a signed-in person. Returns (client, token answer)."""
        client = client or self.register()
        request, verifier = self.ask(client, site)
        back = self.decide(request)
        assert back.status_code == 303, back.text
        code = parse_qs(urlsplit(back.headers["location"]).query)["code"][0]
        r = self.token(client, verifier, code)
        assert r.status_code == 200, r.text
        return client, r.json()

    def call(self, site, token, name="list", args=None):
        return self.assistant.post(f"/s/{site}/mcp/", json=tool(name, args or {"target": "notices"}),
                                   headers={**MCP_HEAD, **auth(token)})


@pytest.fixture
def world(tmp_path, clock):
    return World(tmp_path, clock)


# ---------------------------------------------------------------- finding the flow

def test_discovery_documents(world):
    world.site("corner")
    server = world.assistant.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == HOST, "identical to what the site's document and the iss answer say"
    assert server["authorization_endpoint"] == HOST + "/authorize"
    assert server["token_endpoint"] == HOST + "/token"
    assert server["registration_endpoint"] == HOST + "/register"
    assert server["revocation_endpoint"] == HOST + "/revoke"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert "none" in server["token_endpoint_auth_methods_supported"]
    assert server["client_id_metadata_document_supported"] is True
    assert server["scopes_supported"] == ["site"]

    for path in ("/.well-known/oauth-protected-resource/s/corner/mcp",
                 "/.well-known/oauth-protected-resource/s/corner/mcp/"):
        about = world.assistant.get(path).json()
        assert about["resource"] == HOST + "/s/corner/mcp"
        assert about["authorization_servers"] == [HOST]
    assert world.assistant.get("/.well-known/oauth-protected-resource/s/nothing/mcp").status_code == 404


def test_the_mcp_door_points_an_unauthenticated_caller_at_the_flow(world):
    world.site("corner")
    r = world.assistant.post("/s/corner/mcp/", json=tool("describe", {}), headers=MCP_HEAD)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == \
        f'Bearer resource_metadata="{HOST}/.well-known/oauth-protected-resource/s/corner/mcp"'
    wrong = world.assistant.post("/s/corner/mcp/", json=tool("describe", {}), headers={**MCP_HEAD, **auth("nope")})
    assert wrong.status_code == 401 and "resource_metadata" in wrong.headers["www-authenticate"]


def test_the_host_tells_ais_about_it(world):
    assert "sign-in flow" in world.assistant.get("/").json()["connecting_an_assistant"]
    world.sign_in()
    key = world.platform.issue_account_key(world.platform.accounts()[0]["account"])["secret"]
    made = world.assistant.post("/sites", json={"name": "corner"}, headers=auth(key)).json()
    assert any("sign-in flow" in line for line in made["connect_your_ai"])


def test_a_host_without_https_does_not_offer_the_flow(tmp_path, clock):
    plain = World(tmp_path, clock, public_url="http://sites.example.org")
    assert not plain.platform.connections.available()
    assert plain.assistant.get("/.well-known/oauth-authorization-server").status_code == 404
    assert plain.assistant.post("/register", json={"redirect_uris": [CALLBACK]}).status_code in (404, 405)
    plain.site("corner")
    r = plain.assistant.post("/s/corner/mcp/", json=tool("describe", {}), headers=MCP_HEAD)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert "owner key" in plain.assistant.get("/").json()["connecting_an_assistant"]

    for address in ("http://localhost:8000", "http://127.0.0.1:8000"):
        plain.platform.configure(public_url=address)
        assert plain.platform.connections.available(), "localhost is allowed for trying it out"
    plain.platform.configure(public_url="https://example.org/hosting")
    assert not plain.platform.connections.available(), "the address must have no path"


def test_self_hosted_single_site_is_unchanged(made):
    from tests.util import client
    site, key = made
    r = client(site).post("/mcp/", json=tool("describe", {}), headers=MCP_HEAD)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert site.resolve("ais_at_anything") is None


# ---------------------------------------------------------------- the whole flow

def test_whole_flow_from_a_person_who_is_not_signed_in(world):
    """Register, ask, sign in on the way, approve, exchange the code, use the token."""
    world.site("corner")
    world.browser.cookies.clear()
    client = world.register()
    assert client["client_id"] and "client_secret" not in client
    request, verifier = world.ask(client, "corner")

    page = world.browser.get(f"/account/authorize?request={request}")
    assert page.status_code == 200 and "Sign in to connect an assistant" in page.text
    assert "Test assistant" in page.text and "assistant.example" in page.text
    assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers[
        "content-security-policy"]

    entered = world.browser.post("/account/code", data={"email": ANA, "next": request})
    assert f"name='next' value='{request}'" in entered.text
    r = world.browser.post("/account/session", data={"email": ANA, "code": code_in(world.mailer), "next": request},
                           follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/account/authorize?request={request}"

    consent = world.browser.get(r.headers["location"])
    assert "Connect an assistant" in consent.text and "full control of that one site" in consent.text
    assert "<strong>corner</strong>" in consent.text and "Allow" in consent.text and "Deny" in consent.text

    back = world.decide(request)
    assert back.status_code == 303
    where = urlsplit(back.headers["location"])
    answer = parse_qs(where.query)
    assert f"{where.scheme}://{where.netloc}{where.path}" == CALLBACK
    assert answer["state"] == ["xyz"] and answer["iss"] == [HOST] and len(answer["code"][0]) > 30

    got = world.token(client, verifier, answer["code"][0])
    assert got.status_code == 200
    tokens = got.json()
    assert tokens["token_type"].lower() == "bearer" and tokens["expires_in"] == 3600 and tokens["scope"] == "site"
    assert tokens["access_token"].startswith("ais_at_") and tokens["refresh_token"].startswith("ais_rt_")
    assert got.headers["cache-control"] == "no-store"

    listed = world.call("corner", tokens["access_token"])
    assert listed.status_code == 200 and '"isError":false' in listed.text.replace(" ", "")
    added = world.call("corner", tokens["access_token"], "add",
                       {"target": "notices", "data": {"id": "hello", "title": "Hello from the assistant"}})
    assert added.status_code == 200, added.text
    assert "Hello from the assistant" in world.assistant.get("/s/corner/collections/notices").text

    # The token is the site's owner on every door of that site, and nothing anywhere else.
    again = world.assistant.post("/s/corner/admin/notices/list", json={}, headers=auth(tokens["access_token"]))
    assert again.status_code == 200 and "Hello from the assistant" in again.text
    assert world.assistant.get("/sites", headers=auth(tokens["access_token"])).status_code == 401


def test_changes_are_attributed_to_the_connection(world):
    owner_key = world.site("corner")
    _, tokens = world.connect("corner")
    world.call("corner", tokens["access_token"], "add", {"target": "notices", "data": {"id": "n1", "title": "One"}})
    connection = world.platform.connections.connections(world.platform.accounts()[0]["account"])[0]
    assert connection["assistant"] == "Test assistant" and connection["site"] == "corner"
    assert connection["last_used"] is not None
    trail = world.assistant.post("/s/corner/admin/audit", json={}, headers=auth(owner_key)).json()
    row = next(e for e in trail["entries"] if e["op"] == "add")
    assert row["who"] == f"assistant:{connection['id']}" and row["role"] == "owner"


def test_person_chooses_the_site_when_the_assistant_names_none(world):
    world.site("corner")
    world.site("garden")
    client = world.register()
    request, verifier = world.ask(client)
    page = world.browser.get(f"/account/authorize?request={request}").text
    assert "<select id='site' name='site'>" in page and "corner" in page and "garden" in page
    back = world.decide(request, site="garden")
    tokens = world.token(client, verifier, parse_qs(urlsplit(back.headers["location"]).query)["code"][0]).json()
    assert world.call("garden", tokens["access_token"]).status_code == 200
    assert world.call("corner", tokens["access_token"]).status_code == 401


# ---------------------------------------------------------------- refusals

def test_deny_returns_the_standard_refusal(world):
    world.site("corner")
    request, _ = world.ask(world.register(), "corner")
    back = world.decide(request, "deny")
    answer = parse_qs(urlsplit(back.headers["location"]).query)
    assert back.status_code == 303 and back.headers["location"].startswith(CALLBACK)
    assert answer["error"] == ["access_denied"] and answer["state"] == ["xyz"] and "code" not in answer
    assert world.decide(request).status_code == 404, "an answered request cannot be answered again"
    assert world.platform.connections.connections(world.platform.accounts()[0]["account"]) == []


def test_allow_needs_the_session_and_its_form_token(world):
    world.site("corner")
    request, _ = world.ask(world.register(), "corner")
    form = {"request": request, "decision": "allow"}
    assert world.browser.post("/account/authorize", data=form, follow_redirects=False).status_code == 403
    assert world.browser.post("/account/authorize", data={**form, "csrf": "forged"},
                              follow_redirects=False).status_code == 403
    stranger = TestClient(world.host, base_url=HOST)
    assert stranger.post("/account/authorize", data={**form, "csrf": world.csrf()},
                         follow_redirects=False).status_code == 401
    assert world.decide(request).status_code == 303, "the request survived the forged attempts"


def test_only_the_sites_owner_can_approve_it(world):
    world.site("corner")
    bo = TestClient(world.host, base_url=HOST)
    world.site("bos-place", BO, bo)
    client = world.register()

    request, _ = world.ask(client, "corner")
    page = bo.get(f"/account/authorize?request={request}").text
    assert "not one of your sites" in page and "value='allow'" not in page
    assert world.decide(request, browser=bo).status_code == 403

    # Naming another site in the form does not override the site the assistant asked for.
    assert world.decide(request, browser=bo, site="bos-place").status_code == 403
    # With no site named by the assistant, the person can still only pick their own.
    open_request, _ = world.ask(client)
    assert world.decide(open_request, browser=bo, site="corner").status_code == 403
    assert world.decide(open_request, browser=bo, site="bos-place").status_code == 303


def test_bad_requests_from_the_assistant(world):
    world.site("corner")
    client = world.register()
    base = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
            "code_challenge": "x" * 43, "code_challenge_method": "S256"}
    get = lambda **changes: world.assistant.get("/authorize", params={**base, **changes}, follow_redirects=False)

    unregistered = get(redirect_uri="https://evil.example/steal")
    assert unregistered.status_code == 400, "never redirect to an address that was not registered"
    assert get(client_id="nobody").status_code == 400
    elsewhere = get(resource="https://other.example/mcp")
    assert elsewhere.status_code == 302 and "error=invalid_request" in elsewhere.headers["location"]
    missing = get(resource=HOST + "/s/nothing/mcp")
    assert missing.status_code == 302 and "error=invalid_request" in missing.headers["location"]
    scope = get(scope="everything")
    assert scope.status_code == 302 and "error=invalid_scope" in scope.headers["location"]
    no_pkce = world.assistant.get("/authorize", follow_redirects=False,
                                  params={k: v for k, v in base.items() if not k.startswith("code_")})
    assert "error=invalid_request" in no_pkce.headers.get("location", "") or no_pkce.status_code == 400

    assert world.browser.get("/account/authorize?request=req_doesnotexist000").status_code == 404
    assert world.browser.get("/account/authorize").status_code == 404


def test_code_checks(world, clock):
    world.site("corner")
    client = world.register()

    def approved():
        request, verifier = world.ask(client, "corner")
        return parse_qs(urlsplit(world.decide(request).headers["location"]).query)["code"][0], verifier

    code, verifier = approved()
    wrong = world.token(client, "not-the-verifier" * 4, code)
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"

    code, verifier = approved()
    assert world.token(client, verifier, code, redirect_uri="https://assistant.example/other").status_code == 400

    code, verifier = approved()
    other = world.register(client_name="Another")
    assert world.token(other, verifier, code).status_code == 400, "a code belongs to the assistant it was issued to"

    code, verifier = approved()
    assert world.token(client, verifier, code).status_code == 200
    again = world.token(client, verifier, code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant", "a code works once"

    code, verifier = approved()
    clock.advance(minutes=3)
    assert world.token(client, verifier, code).status_code == 400, "a code lasts two minutes"


def test_an_approval_request_lasts_ten_minutes(world, clock):
    world.site("corner")
    request, _ = world.ask(world.register(), "corner")
    clock.advance(minutes=11)
    assert world.browser.get(f"/account/authorize?request={request}").status_code == 404
    assert world.decide(request).status_code == 404


# ---------------------------------------------------------------- what a token is good for

def test_a_token_is_for_one_site_only(world):
    world.site("corner")
    world.site("garden")
    _, tokens = world.connect("corner")
    assert world.call("corner", tokens["access_token"]).status_code == 200
    assert world.call("garden", tokens["access_token"]).status_code == 401
    elsewhere = world.assistant.post("/s/garden/admin/notices/list", json={}, headers=auth(tokens["access_token"]))
    assert elsewhere.status_code in (401, 403)
    assert world.call("corner", tokens["refresh_token"]).status_code == 401, "a refresh token is not a key"


def test_access_expires_and_refresh_rotates(world, clock):
    world.site("corner")
    client, tokens = world.connect("corner")
    clock.advance(minutes=59)
    assert world.call("corner", tokens["access_token"]).status_code == 200
    clock.advance(minutes=2)
    assert world.call("corner", tokens["access_token"]).status_code == 401, "an access token lasts an hour"

    r = world.refresh(client, tokens["refresh_token"])
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["access_token"] != tokens["access_token"] and fresh["refresh_token"] != tokens["refresh_token"]
    assert world.call("corner", fresh["access_token"]).status_code == 200
    assert world.refresh(client, tokens["refresh_token"]).status_code == 400, "the old refresh token is spent"
    account = world.platform.accounts()[0]["account"]
    assert len(world.platform.connections.connections(account)) == 1, "refreshing keeps the same connection"

    other = world.register(client_name="Another")
    assert world.refresh(other, fresh["refresh_token"]).status_code == 400

    clock.advance(days=31)
    assert world.refresh(client, fresh["refresh_token"]).status_code == 400, "unused for 30 days, it lapses"
    assert world.platform.connections.connections(account) == [], "and the connection is no longer listed"
    assert world.platform.store.one("SELECT COUNT(*) AS n FROM oauth_tokens")["n"] == 0


def test_disconnect_and_revoke_end_a_connection_at_once(world):
    world.site("corner")
    client, tokens = world.connect("corner")
    page = world.browser.get("/account").text
    assert "Connected assistants" in page and "Test assistant" in page
    connection = re.search(r"/account/connections/(con_[0-9a-f]+)/disconnect", page).group(1)

    stranger = TestClient(world.host, base_url=HOST)
    world.sign_in(BO, stranger)
    assert stranger.post(f"/account/connections/{connection}/disconnect",
                         data={"csrf": world.csrf(stranger)}).status_code == 404
    assert world.browser.post(f"/account/connections/{connection}/disconnect", data={"csrf": "forged"},
                              follow_redirects=False).status_code == 403
    assert world.call("corner", tokens["access_token"]).status_code == 200

    r = world.browser.post(f"/account/connections/{connection}/disconnect", data={"csrf": world.csrf()},
                           follow_redirects=False)
    assert r.status_code == 303
    assert world.call("corner", tokens["access_token"]).status_code == 401
    assert world.refresh(client, tokens["refresh_token"]).status_code == 400
    assert "Test assistant" not in world.browser.get("/account").text

    # The assistant ending it itself, through the standard revocation endpoint.
    client, tokens = world.connect("corner")
    gone = world.assistant.post("/revoke", data={"token": tokens["refresh_token"], "client_id": client["client_id"]})
    assert gone.status_code == 200
    assert world.call("corner", tokens["access_token"]).status_code == 401
    assert world.refresh(client, tokens["refresh_token"]).status_code == 400

    # One assistant cannot end another's connection, and an unknown token gets the same quiet answer.
    client, tokens = world.connect("corner")
    other = world.register(client_name="Another")
    for token in (tokens["access_token"], "ais_at_never_issued"):
        assert world.assistant.post("/revoke", data={"token": token, "client_id": other["client_id"]}).status_code == 200
    assert world.call("corner", tokens["access_token"]).status_code == 200
    assert world.assistant.post("/revoke", data={"token": "x", "client_id": "nobody"}).status_code == 401


def test_an_assistant_that_did_not_ask_to_renew_gets_no_refresh_token(world, clock):
    world.site("corner")
    client = world.register(grant_types=["authorization_code"])
    _, tokens = world.connect("corner", client)
    assert tokens.get("refresh_token") is None
    assert world.call("corner", tokens["access_token"]).status_code == 200


def test_suspension_and_change_of_owner_stop_a_token(world, tmp_path):
    world.site("corner")
    _, tokens = world.connect("corner")
    world.platform.suspend("corner")
    assert world.call("corner", tokens["access_token"]).status_code == 403
    assert world.platform.connections.principal_for(tokens["access_token"], "corner") is None
    request, _ = world.ask(world.register(), "corner")
    assert "cannot be approved" in world.browser.get(f"/account/authorize?request={request}").text
    assert world.decide(request).status_code == 403
    world.platform.suspend("corner", False)
    assert world.call("corner", tokens["access_token"]).status_code == 200

    world.platform.store.x("UPDATE sites SET account='acct_someone_else' WHERE name='corner'")
    assert world.call("corner", tokens["access_token"]).status_code == 401


def test_the_owner_key_still_works_beside_the_flow(world):
    key = world.site("corner")
    assert world.call("corner", key).status_code == 200


# ---------------------------------------------------------------- what the assistant says about itself

def test_an_assistants_name_cannot_inject_markup(world):
    world.site("corner")
    nasty = "<script>alert(1)</script>\n<b>Trusted by Test Host</b>" + "x" * 300
    client = world.register(client_name=nasty)
    request, verifier = world.ask(client, "corner")
    page = world.browser.get(f"/account/authorize?request={request}").text
    assert "<script>" not in page and "<b>Trusted" not in page and "&lt;script&gt;" in page
    assert "x" * 81 not in page, "a long name is cut short"
    back = world.decide(request)
    world.token(client, verifier, parse_qs(urlsplit(back.headers["location"]).query)["code"][0])
    account = world.browser.get("/account").text
    assert "<script>" not in account and "&lt;script&gt;" in account

    unnamed = world.register(client_name=None)
    request, _ = world.ask(unnamed, "corner")
    assert "An assistant" in world.browser.get(f"/account/authorize?request={request}").text


def test_sign_in_only_returns_to_an_approval_request(world):
    for attempt in ("https://evil.example/", "//evil.example", "/account/../s/x", "req_x' onmouseover='a"):
        page = world.browser.post("/account/code", data={"email": ANA, "next": attempt})
        assert "name='next'" not in page.text
        r = world.browser.post("/account/session", follow_redirects=False,
                               data={"email": ANA, "code": code_in(world.mailer), "next": attempt})
        assert r.status_code == 303 and r.headers["location"] == "/account"
        world.browser.cookies.clear()


def test_registration_is_limited(world, clock):
    for _ in range(20):
        world.register()
    r = world.assistant.post("/register", json={"redirect_uris": [CALLBACK]})
    assert r.status_code == 429 and r.headers["retry-after"] == "3600"
    clock.advance(hours=2)
    used = world.register(client_name="Used")
    world.site("corner")
    world.connect("corner", used)
    assert world.platform.store.one("SELECT COUNT(*) AS n FROM oauth_clients")["n"] == 21

    clock.advance(days=8)
    world.register()
    left = [json.loads(r["info"]).get("client_name") for r in world.platform.store.q("SELECT info FROM oauth_clients")]
    assert sorted(left) == ["Test assistant", "Used"], "registrations never used are pruned after a week"

    bad = world.assistant.post("/register", json={"redirect_uris": [CALLBACK], "scope": "everything"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_client_metadata"


# ---------------------------------------------------------------- assistants identified by an address

DOCUMENT_ID = "https://assistant.example/oauth/client.json"


def document(**changes):
    return {"client_id": DOCUMENT_ID, "client_name": "Published assistant", "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], **changes}


def test_an_assistant_identified_by_a_published_document(world):
    from aisites.client_metadata import ClientMetadataFetcher
    fetched = []

    def fetch(url, address, timeout, max_bytes):
        fetched.append(url)
        return 200, {"content-type": "application/json"}, json.dumps(document()).encode()

    world.platform.connections.fetcher = ClientMetadataFetcher(fetch=fetch, resolve=lambda host: ["93.184.216.34"])
    world.site("corner")
    client = {"client_id": DOCUMENT_ID}
    _, tokens = world.connect("corner", client)
    assert world.call("corner", tokens["access_token"]).status_code == 200
    assert fetched == [DOCUMENT_ID], "fetched once, then answered from the cache"
    assert "Published assistant" in world.browser.get("/account").text
    assert world.refresh(client, tokens["refresh_token"]).status_code == 200

    # Its return address is whatever its document lists, and nothing else.
    r = world.assistant.get("/authorize", follow_redirects=False, params={
        "response_type": "code", "client_id": DOCUMENT_ID, "redirect_uri": "https://evil.example/cb",
        "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    assert r.status_code == 400


def test_published_documents_can_be_refused_or_switched_off(world):
    from aisites.client_metadata import ClientMetadataFetcher
    connections = world.platform.connections
    calls = []

    def fetch(url, address, timeout, max_bytes):
        calls.append(url)
        return 200, {"content-type": "application/json"}, json.dumps(document(client_id="https://other/x")).encode()

    connections.fetcher = ClientMetadataFetcher(fetch=fetch, resolve=lambda host: ["10.0.0.5"])
    assert connections.client(DOCUMENT_ID) is None and calls == [], "a private address is never fetched"
    connections.fetcher = ClientMetadataFetcher(fetch=fetch, resolve=lambda host: ["93.184.216.34"])
    assert connections.client(DOCUMENT_ID) is None and len(calls) == 1, "a document naming another address"
    assert connections.client("http://assistant.example/client.json") is None

    good = lambda url, address, timeout, max_bytes: (
        200, {"content-type": "application/json"}, json.dumps(document()).encode())
    connections.fetcher = ClientMetadataFetcher(fetch=good, resolve=lambda host: ["93.184.216.34"])
    assert connections.client(DOCUMENT_ID).client_name == "Published assistant"
    world.platform.configure(client_metadata_urls=False)
    assert connections.client(DOCUMENT_ID) is None
    fresh = TestClient(Host(world.folder, world.clock, world.mailer), base_url=HOST)
    assert "client_id_metadata_document_supported" not in fresh.get("/.well-known/oauth-authorization-server").json()


# ---------------------------------------------------------------- set-up

def test_cli_says_when_the_flow_is_off(tmp_path, capsys):
    folder = str(tmp_path / "sites")
    assert main(["host", "init", folder, "--public-url", "http://sites.example.org"]) == 0
    assert "sign-in flow for assistants is off" in capsys.readouterr().out
    assert main(["host", "init", folder, "--public-url", "https://sites.example.org",
                 "--client-metadata-urls", "off"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "is off" not in out["note"] and out["settings"]["client_metadata_urls"] is False


def test_storage_keeps_no_usable_secret(world):
    world.site("corner")
    client, tokens = world.connect("corner")
    request, _ = world.ask(client, "corner")
    code = parse_qs(urlsplit(world.decide(request).headers["location"]).query)["code"][0]
    raw = (world.folder / "platform.db").read_bytes()
    wal = world.folder / "platform.db-wal"
    raw += wal.read_bytes() if wal.exists() else b""
    for secret in (tokens["access_token"], tokens["refresh_token"], code):
        assert secret.encode() not in raw, "tokens and codes are stored only as hashes"


def test_connections_object_direct(world):
    c = world.platform.connections
    assert isinstance(c, Connections) and c.available()
    assert c.site_of(HOST + "/s/nothing/mcp") is None and c.site_of("") is None
    with pytest.raises(SiteError):
        c.pending("req_nope")
    with pytest.raises(SiteError):
        c.disconnect("acct_x", "con_x")
    assert c.principal_for("ais_at_unknown", "corner") is None and c.principal_for(None, "corner") is None
    assert isinstance(Site, type)
