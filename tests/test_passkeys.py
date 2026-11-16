"""Passkeys on the hosted service: a faster way in, and the thing that guards handing out control."""

import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from aisites import passkeys as passkeys_module
from aisites.errors import SiteError
from aisites.host import Host
from aisites.mail import MemoryMailer
from aisites.platform import Platform
from tests.passkey_device import Device
from tests.test_oauth import World
from tests.util import auth

ANA, BO = "ana@example.org", "bo@example.org"
HOST = "https://host.example"


def csrf_of(http):
    return re.search(r"name='csrf' value='([^']+)'", http.get("/account").text).group(1)


def add_passkey(world, device, name="Ana's laptop", http=None, confirm_with=None):
    """Add a passkey through the same requests the page's script makes."""
    http = http or world.browser
    token = csrf_of(http)
    if confirm_with is not None:
        confirm(http, confirm_with, "add_passkey")
    start = http.post("/account/passkeys/options", json={"csrf": token, "name": name})
    assert start.status_code == 200, start.text
    done = http.post("/account/passkeys", follow_redirects=False, data={
        "csrf": token, "challenge": start.json()["challenge_id"],
        "credential": json.dumps(device.create(start.json()["options"]))})
    assert done.status_code == 303, done.text
    return start.json()


def confirm(http, device, action, **changes):
    """Present the passkey for one action, as the page's script does before sending a guarded form."""
    token = csrf_of(http)
    start = http.post("/account/passkey/confirm/options", json={"csrf": token, "action": action})
    assert start.status_code == 200, start.text
    return http.post("/account/passkey/confirm", json={
        "csrf": token, "challenge": start.json()["challenge_id"],
        "credential": device.get(start.json()["options"], **changes)})


def sign_in_with_passkey(http, device, **form):
    start = http.post("/account/passkey/options", json={})
    assert start.status_code == 200, start.text
    return http.post("/account/passkey/session", follow_redirects=False, data={
        "challenge": start.json()["challenge_id"], "credential": json.dumps(device.get(start.json()["options"])),
        **form})


@pytest.fixture
def world(tmp_path, clock):
    w = World(tmp_path, clock)
    w.sign_in()
    return w


@pytest.fixture
def device():
    return Device(HOST)


# ---------------------------------------------------------------- adding and signing in

def test_add_a_passkey_and_sign_in_with_it(world, device):
    assert world.platform.passkeys.unavailable() is None
    page = world.browser.get("/account")
    assert "<h2>Passkeys</h2>" in page.text and "data-passkey='add'" in page.text
    assert "script-src 'nonce-" in page.headers["content-security-policy"] and "<script nonce='" in page.text

    options = add_passkey(world, device)["options"]
    assert options["rp"]["id"] == "host.example" and options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    account = world.platform.accounts()[0]["account"]
    listed = world.platform.passkeys.list(account)
    assert [p["name"] for p in listed] == ["Ana's laptop"] and listed[0]["last_used"] is None
    assert "Ana&#x27;s laptop" in world.browser.get("/account").text

    stranger = TestClient(world.host, base_url=HOST)
    assert "Sign in with a passkey" in stranger.get("/account").text
    r = sign_in_with_passkey(stranger, device)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/account" in cookie
    assert "Your sites" in stranger.get("/account").text, "signed in with no email typed and no code"
    assert world.platform.passkeys.list(account)[0]["last_used"] is not None
    assert len(world.platform.accounts()) == 1, "the passkey led to the same account"


def test_a_passkey_sign_in_returns_to_a_pending_approval(world, device):
    add_passkey(world, device)
    world.site("corner")
    request, _ = world.ask(world.register(), "corner")
    fresh = TestClient(world.host, base_url=HOST)
    page = fresh.get(f"/account/authorize?request={request}").text
    assert "data-passkey='signin'" in page and f"name='next' value='{request}'" in page
    r = sign_in_with_passkey(fresh, device, next=request)
    assert r.headers["location"] == f"/account/authorize?request={request}"
    assert sign_in_with_passkey(fresh, device, next="https://evil.example").headers["location"] == "/account"


def test_what_is_refused_when_adding_or_signing_in(world, device, clock):
    token = csrf_of(world.browser)
    start = world.browser.post("/account/passkeys/options", json={"csrf": token, "name": "x"}).json()
    credential = json.dumps(Device(HOST).create(start["options"]))   # a device whose passkey never gets added
    finish = lambda **changes: world.browser.post("/account/passkeys", follow_redirects=False, data={   # noqa: E731
        "csrf": token, "challenge": start["challenge_id"], "credential": credential, **changes})
    assert finish(csrf="forged").status_code == 403
    assert finish(credential="not json").status_code == 400, "and the challenge is now spent"
    assert finish().status_code == 400, "a challenge is good for one answer"
    assert world.browser.post("/account/passkeys/options", json={"name": "x"}).status_code == 403, "needs the form token"
    assert TestClient(world.host, base_url=HOST).post("/account/passkeys/options", json={}).status_code == 401

    add_passkey(world, device)
    fresh = TestClient(world.host, base_url=HOST)

    def attempt(make):
        start = fresh.post("/account/passkey/options", json={}).json()
        return fresh.post("/account/passkey/session", follow_redirects=False, data={
            "challenge": start["challenge_id"], "credential": json.dumps(make(start["options"]))})

    assert attempt(lambda o: device.get(o, origin="https://evil.example")).status_code == 403, "another site's page"
    assert attempt(lambda o: device.get({**o, "challenge": "AAAA"})).status_code == 403, "a different challenge"
    unknown = Device(HOST)
    unknown.keys["bm90LWtub3du"] = [next(iter(device.keys.values()))[0], b"x", 0]
    assert attempt(lambda o: unknown.get(o)).status_code == 403, "a passkey this host never registered"

    lazy = Device(HOST, verified=False)
    lazy.keys = {k: list(v) for k, v in device.keys.items()}
    assert attempt(lambda o: lazy.get(o)).status_code == 403, "the device must have checked who is holding it"

    start = fresh.post("/account/passkey/options", json={}).json()
    answer = json.dumps(device.get(start["options"]))
    clock.advance(minutes=6)
    late = fresh.post("/account/passkey/session", follow_redirects=False,
                      data={"challenge": start["challenge_id"], "credential": answer})
    assert late.status_code == 400, "a challenge lasts five minutes"
    assert attempt(lambda o: device.get(o)).status_code == 303, "and the real thing still works"


def test_sign_in_must_come_from_this_hosts_own_page(world, device):
    add_passkey(world, device)
    fresh = TestClient(world.host, base_url=HOST)
    start = fresh.post("/account/passkey/options", json={}).json()
    form = {"challenge": start["challenge_id"], "credential": json.dumps(device.get(start["options"]))}
    elsewhere = fresh.post("/account/passkey/session", data=form, headers={"Origin": "https://evil.example"},
                           follow_redirects=False)
    assert elsewhere.status_code == 403 and "set-cookie" not in elsewhere.headers
    assert fresh.post("/account/passkey/session", data=form, headers={"Origin": HOST},
                      follow_redirects=False).status_code == 303
    fresh.post("/account/code", data={"email": ANA})
    code = re.search(r"\b(\d{6})\b", world.mailer.sent[-1]["text"]).group(1)
    by_code = fresh.post("/account/session", data={"email": ANA, "code": code},
                         headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert by_code.status_code == 403 and "set-cookie" not in by_code.headers


def test_sign_in_attempts_are_limited_per_address(world, device):
    fresh = TestClient(world.host, base_url=HOST)
    for _ in range(passkeys_module.CHALLENGES_PER_ADDRESS_PER_HOUR):
        assert fresh.post("/account/passkey/options", json={}).status_code == 200
    refused = fresh.post("/account/passkey/options", json={})
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "too_many"


# ---------------------------------------------------------------- what a passkey guards

def test_without_a_passkey_nothing_changes(world):
    world.site("corner")
    assert "data-passkey='confirm'" not in world.browser.get("/account").text
    assert world.browser.post("/account/keys", data={"csrf": world.csrf(), "label": "x"}).status_code == 200
    client, tokens = world.connect("corner")
    assert world.call("corner", tokens["access_token"]).status_code == 200


def test_with_a_passkey_handing_out_control_needs_it(world, device):
    world.site("corner")
    add_passkey(world, device)
    page = world.browser.get("/account").text
    for action in ("account_key", "owner_key:corner", "add_passkey"):
        assert f"data-action='{action}'" in page

    # Signed in, with a valid form token, and still refused: the emailed code is not enough any more.
    token = world.csrf()
    refused = [world.browser.post("/account/keys", data={"csrf": token, "label": "x"}),
               world.browser.post("/account/sites/corner/key", data={"csrf": token}),
               world.browser.post("/account/passkeys/options", json={"csrf": token, "name": "second"})]
    request, verifier = world.ask(world.register(), "corner")
    refused.append(world.decide(request))
    passkey_id = world.platform.passkeys.list(world.platform.accounts()[0]["account"])[0]["id"]
    refused.append(world.browser.post(f"/account/passkeys/{passkey_id}/remove", data={"csrf": token}))
    for r in refused:
        assert r.status_code == 403 and "passkey" in r.text.lower(), r.text[:200]
    assert world.platform.connections.pending(request), "the request is still waiting"
    assert "Allowing will ask for your passkey" in world.browser.get(f"/account/authorize?request={request}").text

    # With the passkey presented for that exact action, each goes through, once.
    assert confirm(world.browser, device, "account_key").json() == {"confirmed": "account_key"}
    assert world.browser.post("/account/keys", data={"csrf": token, "label": "x"}).status_code == 200
    assert world.browser.post("/account/keys", data={"csrf": token, "label": "y"}).status_code == 403, "once"

    confirm(world.browser, device, "owner_key:corner")
    assert world.browser.post("/account/sites/corner/key", data={"csrf": token}).status_code == 200

    confirm(world.browser, device, f"approve:{request}")
    back = world.decide(request)
    assert back.status_code == 303 and "code=" in back.headers["location"]

    second = Device(HOST)
    add_passkey(world, second, "Phone", confirm_with=device)
    assert len(world.platform.passkeys.list(world.platform.accounts()[0]["account"])) == 2
    confirm(world.browser, second, f"remove_passkey:{passkey_id}")
    assert world.browser.post(f"/account/passkeys/{passkey_id}/remove", data={"csrf": token},
                              follow_redirects=False).status_code == 303
    assert [p["name"] for p in world.platform.passkeys.list(world.platform.accounts()[0]["account"])] == ["Phone"]


def test_a_confirmation_is_for_one_action_one_session_and_two_minutes(world, device, clock):
    world.site("corner")
    add_passkey(world, device)
    token = world.csrf()

    confirm(world.browser, device, "owner_key:corner")
    assert world.browser.post("/account/keys", data={"csrf": token}).status_code == 403, "confirmed something else"

    confirm(world.browser, device, "account_key")
    other = TestClient(world.host, base_url=HOST)
    world.sign_in(ANA, other)
    assert other.post("/account/keys", data={"csrf": csrf_of(other)}).status_code == 403, "another session"

    clock.advance(seconds=passkeys_module.PROOF_SECONDS + 1)
    assert world.browser.post("/account/keys", data={"csrf": token}).status_code == 403, "too long ago"

    assert world.decide("req_whatever", "deny").status_code == 404, "denying never asks for a passkey"
    request, _ = world.ask(world.register(), "corner")
    assert world.decide(request, "deny").status_code == 303


def test_another_accounts_passkey_confirms_nothing(world, device):
    add_passkey(world, device)
    bo = TestClient(world.host, base_url=HOST)
    world.sign_in(BO, bo)
    bos = Device(HOST)
    add_passkey(world, bos, "Bo's", http=bo)

    token = csrf_of(bo)
    start = bo.post("/account/passkey/confirm/options", json={"csrf": token, "action": "account_key"}).json()
    assert [c["id"] for c in start["options"]["allowCredentials"]] == list(bos.keys), "only Bo's own are offered"
    stolen = bo.post("/account/passkey/confirm", json={
        "csrf": token, "challenge": start["challenge_id"],
        "credential": device.get({**start["options"], "allowCredentials": []})})
    assert stolen.status_code == 403
    assert bo.post("/account/keys", data={"csrf": token}).status_code == 403


def test_an_assistants_sign_up_needs_the_passkey_too(world, device):
    """Confirming an AI-started sign-up hands that assistant an account key."""
    add_passkey(world, device)
    ai = TestClient(world.host, base_url=HOST)
    person = TestClient(world.host, base_url=HOST)
    started = ai.post("/signup", json={"email": ANA}).json()
    code = re.search(r"\b(\d{6})\b", world.mailer.sent[-1]["text"]).group(1)

    page = person.get(f"/account/confirm?ticket={started['ticket']}")
    assert "data-passkey='ticket'" in page.text and "will ask for it as well as the code" in page.text
    refused = person.post("/account/confirm", data={"ticket": started["ticket"], "code": code})
    assert refused.status_code == 403 and "passkey" in refused.text
    assert ai.get(f"/signup/{started['ticket']}", headers=auth(started["claim"])).json()["status"] == "pending"

    start = person.post("/account/confirm/options", json={"ticket": started["ticket"]}).json()
    assert not start["options"].get("allowCredentials"), "the page learns nothing about which passkeys exist"
    done = person.post("/account/confirm", data={
        "ticket": started["ticket"], "code": code, "challenge": start["challenge_id"],
        "credential": json.dumps(device.get(start["options"]))})
    assert done.status_code == 200 and "Confirmed" in done.text
    assert ai.get(f"/signup/{started['ticket']}", headers=auth(started["claim"])).json()["account_key"]

    # An address with no account yet, or one without a passkey, confirms by code alone as before.
    other = ai.post("/signup", json={"email": BO}).json()
    plain = person.get(f"/account/confirm?ticket={other['ticket']}")
    assert "data-passkey" not in plain.text and "script-src 'none'" in plain.headers["content-security-policy"]


# ---------------------------------------------------------------- a lost device

def test_lost_device_removal_waits_a_day_and_says_so_by_email(world, device, clock):
    add_passkey(world, device)
    account = world.platform.accounts()[0]["account"]
    token = world.csrf()
    wrong = world.browser.post("/account/passkeys/reset", data={"csrf": token, "email": BO})
    assert wrong.status_code == 400 and world.platform.passkeys.pending_reset(account) is None

    asked = world.browser.post("/account/passkeys/reset", data={"csrf": token, "email": ANA})
    assert asked.status_code == 200 and "will be removed" in asked.text
    notice = world.mailer.sent[-1]
    assert notice["to"] == ANA and "will be removed" in notice["subject"] and "cancel" in notice["text"]
    assert "Cancel the removal" in world.browser.get("/account").text

    clock.advance(hours=23)
    assert world.platform.passkeys.has(account)
    assert world.browser.post("/account/keys", data={"csrf": token}).status_code == 403, "still guarded while waiting"
    clock.advance(hours=2)
    assert not world.platform.passkeys.has(account) and world.platform.passkeys.pending_reset(account) is None
    assert world.browser.post("/account/keys", data={"csrf": world.csrf()}).status_code == 200


def test_using_a_passkey_or_cancelling_calls_the_removal_off(world, device, clock):
    add_passkey(world, device)
    account = world.platform.accounts()[0]["account"]
    token = world.csrf()
    world.browser.post("/account/passkeys/reset", data={"csrf": token, "email": ANA})
    assert sign_in_with_passkey(TestClient(world.host, base_url=HOST), device).status_code == 303
    assert world.platform.passkeys.pending_reset(account) is None, "the device turned up"
    clock.advance(days=2)
    assert world.platform.passkeys.has(account)

    world.browser.post("/account/passkeys/reset", data={"csrf": token, "email": ANA})
    assert world.browser.post("/account/passkeys/reset/cancel", data={"csrf": token},
                              follow_redirects=False).status_code == 303
    clock.advance(days=2)
    assert world.platform.passkeys.has(account)


# ---------------------------------------------------------------- where passkeys are off

def test_off_without_https_or_without_the_library(tmp_path, clock, monkeypatch):
    plain = World(tmp_path / "a", clock, public_url="http://sites.example.org")
    plain.sign_in()
    page = plain.browser.get("/account")
    assert "Not available on this host" in page.text and "data-passkey" not in page.text
    assert plain.browser.post("/account/passkey/options", json={}).status_code == 404
    with pytest.raises(SiteError):
        plain.platform.passkeys.start_using("sign_in")

    monkeypatch.setattr(passkeys_module, "library", lambda: None)
    hosted = World(tmp_path / "b", clock)
    assert "not installed" in hosted.platform.passkeys.unavailable()
    hosted.sign_in()
    assert "pip install" in hosted.browser.get("/account").text
    assert "Sign in with a passkey" not in TestClient(hosted.host, base_url=HOST).get("/account").text


def test_storage_and_limits(world, device):
    add_passkey(world, device)
    account = world.platform.accounts()[0]["account"]
    raw = (world.folder / "platform.db").read_bytes()
    private = next(iter(device.keys.values()))[0].private_numbers().private_value.to_bytes(32, "big")
    assert private not in raw, "only the public half of a passkey is ever stored"
    for n in range(passkeys_module.MAX_PER_ACCOUNT - 1):
        add_passkey(world, Device(HOST), f"Device {n}", confirm_with=device)
    confirm(world.browser, device, "add_passkey")
    full = world.browser.post("/account/passkeys/options", json={"csrf": world.csrf(), "name": "one too many"})
    assert full.status_code == 409 and len(world.platform.passkeys.list(account)) == passkeys_module.MAX_PER_ACCOUNT
    assert Host and MemoryMailer and Platform and parse_qs and urlsplit
