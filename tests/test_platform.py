"""The hosted service: sign up by email, create a site from a template, connect your own AI."""

import io
import json
import re

import pytest
from fastapi.testclient import TestClient

from aisites import Site, templates
from aisites.cli import main
from aisites.host import Host
from aisites.mail import MemoryMailer
from aisites.platform import Platform
from tests.util import assert_chain_ok, auth

ANA, BO = "ana@example.org", "bo@example.org"


@pytest.fixture
def hosted(tmp_path, clock):
    """(platform, mailer, client) for a host with nobody signed up yet."""
    mailer = MemoryMailer()
    folder = tmp_path / "sites"
    Platform.init(folder, clock, name="Test Host", public_url="https://host.example", sites_per_account=3).close()
    host = Host(folder, clock, mailer)
    http = TestClient(host, base_url="https://host.example")
    return host.platform, mailer, http


def code_in(mailer):
    return re.search(r"\b(\d{6})\b", mailer.sent[-1]["text"]).group(1)


def register(http, mailer, email=ANA):
    """Play both the AI and the person: returns an account key."""
    started = http.post("/register", json={"email": email}).json()
    assert http.post("/account/confirm", data={"ticket": started["ticket"], "code": code_in(mailer)}).status_code == 200
    return http.get(f"/register/{started['ticket']}", headers=auth(started["claim"])).json()["account_key"]


def sign_in(http, mailer, email=ANA):
    """Sign in through the pages. Returns the hidden form token of the session."""
    http.post("/account/code", data={"email": email})
    r = http.post("/account/session", data={"email": email, "code": code_in(mailer)}, follow_redirects=False)
    assert r.status_code == 303
    return re.search(r"name='csrf' value='([^']+)'", http.get("/account").text).group(1), r


# ---------------------------------------------------------------- people

def test_person_signs_up_signs_out_and_back_in(hosted):
    platform, mailer, http = hosted
    assert "Send me a code" in http.get("/account").text
    page = http.post("/account/code", data={"email": ANA})
    assert page.status_code == 200 and "Enter your code" in page.text
    assert mailer.sent[-1]["to"] == ANA and "Test Host" in mailer.sent[-1]["subject"]

    csrf, r = sign_in(http, mailer)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    assert "Path=/account" in cookie, "the cookie is never sent to the sites this host serves"
    assert "Your sites" in http.get("/account").text
    first = platform.accounts()
    assert len(first) == 1 and first[0]["sites"] == []

    assert http.post("/account/signout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert "Send me a code" in http.get("/account").text
    sign_in(http, mailer)
    assert platform.accounts() == first, "the same address is the same account"


def test_codes_are_single_use_short_lived_and_limited_in_attempts(hosted, clock):
    platform, mailer, http = hosted

    def try_code(code):
        return http.post("/account/session", data={"email": ANA, "code": code}, follow_redirects=False)

    http.post("/account/code", data={"email": ANA})
    good = code_in(mailer)
    wrong = "000000" if good != "000000" else "111111"
    r = try_code(wrong)
    assert r.status_code == 401 and "not valid" in r.text
    assert try_code(good).status_code == 303
    assert try_code(good).status_code == 401, "a code works once"

    http.post("/account/code", data={"email": ANA})
    first = code_in(mailer)
    http.post("/account/code", data={"email": ANA})
    assert try_code(first).status_code == 401 or first == code_in(mailer), "a newer code replaces the older one"

    http.post("/account/code", data={"email": ANA})
    late = code_in(mailer)
    clock.advance(minutes=11)
    assert try_code(late).status_code == 401, "codes last ten minutes"

    clock.advance(hours=2)
    http.post("/account/code", data={"email": ANA})
    target = code_in(mailer)
    wrong = "000000" if target != "000000" else "111111"
    for _ in range(5):
        assert try_code(wrong).status_code == 401
    assert try_code(target).status_code == 401, "after five wrong guesses even the right code is void"
    assert http.post("/account/session", data={"email": "nobody@example.org", "code": "123456"},
                     follow_redirects=False).status_code == 401
    assert http.post("/account/code", data={"email": "not an address"}).status_code == 400


def test_code_requests_are_limited(hosted, clock):
    _, mailer, http = hosted
    for _ in range(5):
        assert http.post("/register", json={"email": ANA}).status_code == 200
    r = http.post("/register", json={"email": ANA})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_codes" and r.headers["retry-after"] == "3600"
    assert len(mailer.sent) == 5, "a refused request sends nothing"
    for i in range(15):
        assert http.post("/register", json={"email": f"p{i}@example.org"}).status_code == 200
    assert http.post("/register", json={"email": "one-more@example.org"}).status_code == 429, \
        "one network address cannot make the host mail the world"
    clock.advance(hours=1, minutes=1)
    assert http.post("/register", json={"email": ANA}).status_code == 200


# ---------------------------------------------------------------- registration started by an AI

def test_ai_starts_registration_and_person_confirms(hosted):
    platform, mailer, http = hosted
    described = http.get("/").json()
    assert described["register"]["by_an_ai"][0].startswith("POST https://host.example/register")

    started = http.post("/register", json={"email": ANA}).json()
    assert started["confirm_at"] == f"https://host.example/account/confirm?ticket={started['ticket']}"
    assert re.fullmatch(r"[a-z]+-[a-z]+-\d\d", started["pairing_phrase"])
    assert started["pairing_phrase"] in started["tell_the_person"]
    email = mailer.sent[-1]
    assert started["pairing_phrase"] in email["text"] and started["confirm_at"] in email["text"]
    assert started["claim"] not in email["text"], "the claim secret stays with the AI"

    collect = f"/register/{started['ticket']}"
    assert http.get(collect).status_code == 401
    assert http.get(collect, headers=auth("guess")).json()["error"]["code"] == "bad_claim"
    pending = http.get(collect, headers=auth(started["claim"]))
    assert pending.status_code == 202 and pending.json()["status"] == "pending"

    page = http.get("/account/confirm", params={"ticket": started["ticket"]})
    assert started["pairing_phrase"] in page.text and "Only continue if your own assistant" in page.text
    assert http.post("/account/confirm", data={"ticket": started["ticket"], "code": "000000"}).status_code == 401
    assert http.get(collect, headers=auth(started["claim"])).status_code == 202, "a wrong code confirms nothing"
    assert http.post("/account/confirm", data={"ticket": started["ticket"], "code": code_in(mailer)}).status_code == 200
    assert http.post("/account/confirm", data={"ticket": started["ticket"], "code": code_in(mailer)}).status_code == 409

    done = http.get(collect, headers=auth(started["claim"]))
    assert done.status_code == 200 and done.json()["account_key"].startswith("ais_acct_")
    assert http.get(collect, headers=auth(started["claim"])).status_code == 409, "the key is handed over once"
    assert http.get("/sites", headers=auth(done.json()["account_key"])).json() == {"sites": []}


def test_codes_cannot_cross_between_a_ticket_and_a_browser_sign_in(hosted, clock):
    _, mailer, http = hosted
    started = http.post("/register", json={"email": ANA}).json()
    ticket_code = code_in(mailer)
    r = http.post("/account/session", data={"email": ANA, "code": ticket_code}, follow_redirects=False)
    assert r.status_code == 401, "a code mailed for an assistant does not sign a browser in"

    other = http.post("/register", json={"email": BO}).json()
    http.post("/account/code", data={"email": BO})
    browser_code = code_in(mailer)
    assert http.post("/account/confirm", data={"ticket": other["ticket"], "code": browser_code}).status_code == 401, \
        "nor does a browser sign-in code confirm an assistant"

    clock.advance(minutes=16)
    assert http.get(f"/register/{started['ticket']}", headers=auth(started["claim"])).status_code == 404
    assert http.get("/account/confirm", params={"ticket": started["ticket"]}).status_code == 404
    assert http.get("/account/confirm", params={"ticket": "reg_nothing"}).status_code == 404


# ---------------------------------------------------------------- sites

def test_every_template_makes_a_working_site(hosted):
    platform, mailer, http = hosted
    platform.configure(sites_per_account=20)
    key = register(http, mailer)
    listed = [t["name"] for t in http.get("/templates").json()["templates"]]
    assert listed == list(templates.TEMPLATES) and len(listed) == 7
    for name in listed:
        site_name = f"my-{name.replace('_', '-')}"
        r = http.post("/sites", headers=auth(key), json={"name": site_name, "template": name, "title": f"My {name}",
                                                          "timezone": "Europe/Madrid"})
        assert r.status_code == 201, r.text
        made = r.json()
        assert made["site"] == f"https://host.example/s/{site_name}/" and made["mcp"].endswith(f"/s/{site_name}/mcp/")
        manifest = http.get(f"/s/{site_name}/").json()
        assert manifest["name"] == f"My {name}" and manifest["timezone"] == "Europe/Madrid"
        described = http.post(f"/s/{site_name}/admin/describe", headers=auth(made["owner_key"])).json()
        assert [s["name"] for s in described["structures"]] == sorted(made["structures"])
        site = Site(platform.dir / site_name)
        assert_chain_ok(site)
        site.close()
    assert len(http.get("/sites", headers=auth(key)).json()["sites"]) == 7


def test_site_names_limits_and_bad_requests(hosted):
    platform, mailer, http = hosted
    key = register(http, mailer)

    def create(**body):
        return http.post("/sites", headers=auth(key), json=body)

    for bad in ("ab", "Has Space", "../x", "account", "s", "-lead"):
        r = create(name=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_name", bad
    assert create(name="shop-one", template="teleport").json()["error"]["code"] == "bad_template"
    assert create(name="shop-one", timezone="PST").json()["error"]["code"] == "bad_timezone"
    assert create(name="shop-one", colour="red").json()["error"]["code"] == "invalid"
    assert not (platform.dir / "shop-one").exists(), "a refused request leaves nothing behind"
    assert http.post("/sites", json={"name": "shop-one"}).status_code == 401

    assert create(name="shop-one", template="shop").status_code == 201
    assert create(name="shop-one").json()["error"]["code"] == "name_taken"
    assert create(name="shop-two").status_code == 201 and create(name="shop-three").status_code == 201
    r = create(name="shop-four")
    assert r.status_code == 409 and r.json()["error"]["code"] == "site_limit" and "3" in r.json()["error"]["message"]


def test_accounts_cannot_see_or_take_each_others_sites(hosted):
    platform, mailer, http = hosted
    ana, bo = register(http, mailer, ANA), register(http, mailer, BO)
    made = http.post("/sites", headers=auth(ana), json={"name": "anas-shop", "template": "shop"}).json()

    assert http.get("/sites", headers=auth(bo)).json() == {"sites": []}
    theirs = http.post("/sites/anas-shop/key", headers=auth(bo))
    missing = http.post("/sites/no-such-site/key", headers=auth(bo))
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["error"]["message"].replace("anas-shop", "X") == \
        missing.json()["error"]["message"].replace("no-such-site", "X"), "someone else's site looks absent"
    assert http.post("/s/anas-shop/admin/describe", headers=auth(bo)).status_code == 401, \
        "an account key is not a site key"

    rotated = http.post("/sites/anas-shop/key", headers=auth(ana)).json()
    assert rotated["owner_key"] != made["owner_key"]
    assert http.post("/s/anas-shop/admin/describe", headers=auth(made["owner_key"])).status_code == 401
    assert http.post("/s/anas-shop/admin/describe", headers=auth(rotated["owner_key"])).status_code == 200

    key_id = platform.account_keys(platform.account_for_key(ana))[0]["id"]
    platform.revoke_account_key(platform.account_for_key(ana), key_id)
    assert http.get("/sites", headers=auth(ana)).status_code == 401, "a revoked key stops at once"


def test_site_created_here_has_the_whole_engine(hosted):
    _, mailer, http = hosted
    key = register(http, mailer)
    made = http.post("/sites", headers=auth(key), json={"name": "lucia", "template": "restaurant"}).json()
    base = "/s/lucia"
    assert any(a["action"] == "reserve" and not a["needs_key"] for a in http.get(f"{base}/actions").json()["actions"])
    table = http.get(f"{base}/collections/tables", params={"limit": 1}).json()["items"][0]["key"]
    held = http.post(f"{base}/actions/tables/reserve", json={"key": table}, headers={"Idempotency-Key": "a"})
    assert held.status_code == 200 and held.json()["status"] == "held"
    added = http.post(f"{base}/admin/places/add", headers=auth(made["owner_key"]),
                      json={"data": {"id": "terrace", "name": "Terrace"}})
    assert added.status_code == 200
    assert http.get(f"{base}/collections/places/terrace").json()["data"]["name"] == "Terrace"
    assert http.get(f"{base}/history").json()["head"]["seq"] > 1
    assert http.post(f"{base}/mcp/", json={}).status_code == 401


def test_operator_can_suspend_a_site(hosted, capsys):
    platform, mailer, http = hosted
    key = register(http, mailer)
    made = http.post("/sites", headers=auth(key), json={"name": "spammy", "template": "shop"}).json()
    assert main(["host", "suspend", str(platform.dir), "spammy"]) == 0
    capsys.readouterr()
    for path in ("/s/spammy/", "/s/spammy/collections/products", "/s/spammy/actions"):
        r = http.get(path)
        assert r.status_code == 403 and r.json()["error"]["code"] == "suspended", path
    assert http.post("/s/spammy/admin/describe", headers=auth(made["owner_key"])).status_code == 403
    assert http.get("/sites", headers=auth(key)).json()["sites"][0]["suspended"] is True
    assert main(["host", "unsuspend", str(platform.dir), "spammy"]) == 0
    assert http.get("/s/spammy/").status_code == 200
    assert main(["host", "suspend", str(platform.dir), "no-such-site"]) == 1


# ---------------------------------------------------------------- privacy and form safety

def test_email_addresses_are_not_kept(hosted):
    platform, mailer, http = hosted
    key = register(http, mailer)
    http.post("/sites", headers=auth(key), json={"name": "anas-site"})
    sign_in(http, mailer, BO)
    platform.store.scrub()
    for path in platform.dir.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for address in (ANA, BO, "ana@", "bo@"):
                assert address.encode() not in data, f"{address} found in {path.name}"
    listed = json.dumps(platform.accounts())
    assert "example.org" not in listed and len(platform.accounts()) == 2
    assert platform.fingerprint(" ANA@Example.org ") == platform.fingerprint(ANA), "case and spaces do not matter"


def test_forms_need_a_session_and_its_token(hosted):
    platform, mailer, http = hosted
    r = http.post("/account/sites", data={"name": "sneaky"})
    assert r.status_code == 401 and "Sign in first" in r.text
    csrf, _ = sign_in(http, mailer)
    r = http.post("/account/sites", data={"name": "sneaky", "csrf": "forged"})
    assert r.status_code == 403 and "did not come from this page" in r.text
    assert not (platform.dir / "sneaky").exists()
    for path in ("/account/keys", "/account/signout", "/account/sites/x/key", "/account/keys/ak_1/revoke"):
        assert http.post(path, data={}).status_code == 403, path
    assert http.get("/sites").status_code == 401, "a browser session is not an API key"


def test_account_page_creates_sites_and_manages_keys(hosted):
    platform, mailer, http = hosted
    csrf, _ = sign_in(http, mailer)
    page = http.post("/account/sites", data={"csrf": csrf, "name": "cafe", "title": "Café <b>Sol</b>",
                                              "template": "notice_board", "timezone": "Europe/Madrid"})
    assert page.status_code == 200 and "cafe is ready" in page.text and "Copy it now" in page.text
    owner_key = re.search(r"ais_owner_[\w-]+", page.text).group(0)
    assert http.post("/s/cafe/admin/describe", headers=auth(owner_key)).json()["site"]["name"] == "Café <b>Sol</b>"
    listing = http.get("/account").text
    assert "/s/cafe/" in listing and "<b>Sol</b>" not in listing, "nothing typed in is echoed back as markup"

    page = http.post("/account/sites/cafe/key", data={"csrf": csrf})
    new_key = re.search(r"ais_owner_[\w-]+", page.text).group(0)
    assert new_key != owner_key and http.post("/s/cafe/admin/describe", headers=auth(owner_key)).status_code == 401

    page = http.post("/account/keys", data={"csrf": csrf, "label": "My assistant"})
    account_key = re.search(r"ais_acct_[\w-]+", page.text).group(0)
    assert [s["name"] for s in http.get("/sites", headers=auth(account_key)).json()["sites"]] == ["cafe"]
    assert "My assistant" in http.get("/account").text and account_key not in http.get("/account").text
    key_id = platform.account_keys(platform.account_for_key(account_key))[0]["id"]
    assert http.post(f"/account/keys/{key_id}/revoke", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert http.get("/sites", headers=auth(account_key)).status_code == 401
    bad = http.post("/account/sites", data={"csrf": csrf, "name": "account"})
    assert bad.status_code == 400 and "Back to the account page" in bad.text


# ---------------------------------------------------------------- discovery, operator commands, no platform

def test_host_describes_itself_to_ais(hosted):
    _, _, http = hosted
    described = http.get("/").json()
    assert described["name"] == "Test Host" and described["sites_per_account"] == 3
    assert len(described["register"]["by_an_ai"]) == 4 and described["templates"] == "https://host.example/templates"
    text = http.get("/llms.txt").text
    assert text.startswith("# Test Host") and "`restaurant`" in text and "POST https://host.example/register" in text
    assert "<dl>" in http.get("/", headers={"Accept": "text/html"}).text
    missing = http.get("/no/such/page")
    assert missing.status_code == 404 and "/s/<name>/" in missing.json()["error"]["hint"]


def test_operator_commands(tmp_path, clock, capsys):
    folder = str(tmp_path / "sites")
    assert main(["host", "init", folder, "--name", "Ops Host", "--mail-file", str(tmp_path / "out.jsonl")],
                clock=clock) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["settings"]["name"] == "Ops Host" and shown["settings"]["mail"]["type"] == "file"
    assert main(["host", "init", folder, "--smtp-host", "smtp.resend.com", "--smtp-user", "resend",
                 "--smtp-sender", "hello@sites.example", "--sites-per-account", "5"], clock=clock) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["settings"]["name"] == "Ops Host", "settings not given are kept"
    assert shown["settings"]["mail"] == {"type": "smtp", "host": "smtp.resend.com", "port": 587,
                                         "username": "resend", "sender": "hello@sites.example", "tls": "starttls"}
    assert "AISITES_SMTP_PASSWORD" in shown["note"] and shown["settings"]["sites_per_account"] == 5
    assert main(["host", "init", folder, "--smtp-host", "smtp.resend.com"], clock=clock) == 1, "a sender is required"
    capsys.readouterr()
    assert main(["host", "accounts", folder]) == 0
    assert json.loads(capsys.readouterr().out) == {"accounts": []}
    assert main(["host", "accounts", str(tmp_path / "elsewhere")]) == 1


def test_templates_work_when_self_hosting(tmp_path, clock, capsys):
    out = io.StringIO()
    assert main(["site", "create", str(tmp_path / "school"), "--name", "Lincoln", "--template", "school"],
                clock=clock, out=out) == 0
    made = json.loads(out.getvalue())
    assert made["template"] == "school" and "hours" in made["structures"]
    assert main(["site", "create", str(tmp_path / "x"), "--name", "X", "--template", "teleport"], clock=clock) == 1
    assert not (tmp_path / "x").exists()
    capsys.readouterr()
    assert main(["templates"]) == 0
    assert len(json.loads(capsys.readouterr().out)["templates"]) == 7


def test_a_plain_sites_folder_is_not_a_hosted_service(tmp_path, clock):
    Site.create(tmp_path / "alpha", name="Alpha", clock=clock)[0].close()
    http = TestClient(Host(tmp_path, clock))
    for path in ("/", "/account", "/templates", "/sites"):
        assert http.get(path).status_code == 404, path
    assert http.post("/register", json={"email": ANA}).status_code == 404
    assert http.get("/s/alpha/").json()["name"] == "Alpha"
