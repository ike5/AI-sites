"""The visitor foundation: the /actions door, retry safety, preview, quotas, guest reads, custom actions."""

import json

import pytest
from pydantic import BaseModel

from aisites import PaymentHandoff, SiteError, hooks, ops
from tests.util import auth, client, log


@pytest.fixture
def stamped(made):
    """A site with a throwaway visitor operation, `stamp`, switched on by a `stampable` setting."""
    site, owner = made

    def check(site, sdef, value):
        return {"open": bool(value.get("open", False))}

    def stamp(site, caller, target, key, note=""):
        with site.store.tx():
            count = int(site.store.meta("stamps", 0)) + 1
            site.store.set_meta("stamps", count)
        return {"stamps": count, "caller": caller.id, "key": key}

    hooks.SETTINGS["stampable"] = check
    ops.register(ops.Op("stamp", "Stamp a record.", stamp,
                        {"target": {"type": "string"}, "key": {"type": "string"}, "note": {"type": "string"}},
                        ("target", "key"), visitor=True, setting="stampable", positional=("target", "key")))
    site.define("cards", fields=["title:string", "secret:string:private"], settings={"stampable": {"open": False}})
    site["cards"].add(id="c1", title="Card", secret="s")
    yield site, owner, client(site)
    ops.OPS.pop("stamp")
    hooks.SETTINGS.pop("stampable")


def guest(site, grants):
    site.set_role("guests", grants)
    return site.issue_key("guests")["secret"]


def stamps(site):
    return int(site.store.meta("stamps", 0))


def call(http, key=None, idem="k1", body=None, path="/actions/cards/stamp", **params):
    headers = {**(auth(key) if key else {}), **({"Idempotency-Key": idem} if idem else {})}
    return http.post(path, json={"key": "c1"} if body is None else body, headers=headers, params=params)


def test_actions_are_discoverable(stamped):
    site, _, http = stamped
    entry = next(s for s in http.get("/map").json()["structures"] if s["name"] == "cards")
    assert entry["actions"] == ["stamp"] and http.get("/map").json()["links"]["actions"] == "/actions"
    listing = http.get("/actions").json()
    action = listing["actions"][0]
    assert action["href"] == "/actions/cards/stamp" and action["needs_key"] is True and action["changes_site"]
    assert list(action["arguments"]["properties"]) == ["key", "note"] and action["arguments"]["required"] == ["key"]
    assert any("Idempotency-Key" in line for line in listing["calling"])


def test_who_may_act(stamped):
    site, owner, http = stamped
    r = call(http)
    assert r.status_code == 401 and r.json()["error"]["code"] == "needs_key"
    assert call(http, path="/actions/nothing/stamp").status_code == 401, "an unknown structure reads the same"

    outsider = guest(site, [{"target": "cards", "ops": ["read"]}])
    r = call(http, outsider)
    assert r.status_code == 403 and "may not `stamp`" in r.json()["error"]["message"]

    site.set_role("guests", [{"target": "cards", "ops": ["stamp"]}])
    r = call(http, outsider)
    assert r.status_code == 200 and r.json()["stamps"] == 1 and r.json()["caller"].startswith("key_")
    assert call(http, owner, idem="o1").json()["caller"] == "owner"

    site.alter("cards", settings={"stampable": {"open": True}})
    r = call(http, idem="a1")
    assert r.status_code == 200 and r.json()["caller"].startswith("anon:")
    assert http.get("/actions").json()["actions"][0]["needs_key"] is False

    r = call(http, owner, path="/actions/cards/change", body={"key": "c1", "set": {"title": "x"}})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_action", "editing is not a visitor action"
    assert call(http, owner, body={"key": "c1", "colour": "red"}, idem="o2").status_code == 400


def test_retry_safety(stamped):
    site, owner, http = stamped
    r = call(http, owner, idem=None)
    assert r.status_code == 400 and r.json()["error"]["code"] == "needs_idempotency_key"
    first = call(http, owner, idem="same")
    again = call(http, owner, idem="same")
    assert first.json() == again.json() and "idempotent-replay" not in first.headers
    assert again.headers["idempotent-replay"] == "true" and stamps(site) == 1, "the retry did not act twice"
    r = call(http, owner, idem="same", body={"key": "c1", "note": "different"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_mismatch"
    assert call(http, owner, idem="other").json()["stamps"] == 2

    failing = call(http, owner, idem="bad", body={"note": "no key"})
    assert failing.status_code == 400
    assert call(http, owner, idem="bad", body={"key": "c1"}).status_code == 200, "a failed call leaves the key free"


def test_preview_leaves_no_trace(stamped):
    site, owner, http = stamped
    audit_before = len(site.audit()["entries"])
    r = call(http, owner, idem=None, dry_run="true")
    assert r.status_code == 200 and r.json() == {"dry_run": True, "result": {"stamps": 1, "caller": "owner", "key": "c1"}}
    assert stamps(site) == 0 and len(site.audit()["entries"]) == audit_before
    assert call(http, owner).json()["stamps"] == 1, "and the real call then starts from the same state"


def test_quotas_limit_changes_not_reads(stamped, clock):
    site, owner, http = stamped
    site.alter("cards", settings={"stampable": {"open": True}})
    site.set_limits(anonymous_per_day=2, key_per_day=3)
    assert [call(http, idem=f"a{i}").status_code for i in range(3)] == [200, 200, 429]
    refused = call(http, idem="a9")
    assert refused.json()["error"]["code"] == "quota_exceeded" and int(refused.headers["retry-after"]) == 6 * 3600
    assert call(http, idem="a0").status_code == 200, "replaying an earlier call is not charged"
    assert call(http, idem=None, dry_run="true").status_code == 200, "previews are not charged"
    assert http.get("/collections/cards").status_code == 200

    key = guest(site, [{"target": "cards", "ops": ["stamp"]}])
    assert [call(http, key, idem=f"g{i}").status_code for i in range(4)] == [200, 200, 200, 429]
    assert call(http, owner, idem="o").status_code == 200, "the owner is not limited"
    clock.advance(days=1)
    assert call(http, idem="tomorrow").status_code == 200

    with pytest.raises(SiteError):
        site.set_limits(per_minute=5)
    with pytest.raises(SiteError):
        site.set_limits(concurrent="many")
    assert ops.run(site, ops.OWNER, "set_limits", {"concurrent": 1})["concurrent"] == 1
    visitor = site.anonymous("203.0.113.9")
    site._enter(visitor)
    with pytest.raises(SiteError) as e:
        site._enter(visitor)
    assert e.value.status == 429 and e.value.retry_after == 1
    site._leave(visitor)
    site._enter(visitor)


def test_visitor_calls_are_audited_without_addresses(stamped):
    site, _, http = stamped
    site.alter("cards", settings={"stampable": {"open": True}})
    call(http)
    row = site.audit()["entries"][0]
    assert row["op"] == "stamp" and row["target"] == "cards" and row["key"] == "c1" and row["ok"]
    assert row["who"].startswith("anon:") and "testclient" not in json.dumps(site.audit())
    assert site.anonymous("198.51.100.7").id != site.anonymous("198.51.100.8").id
    assert all("stamp" not in json.dumps(e) for e in log(site) if e["path"] != "schemas/cards")


def test_guest_key_reads_invite_only_data(stamped):
    site, owner, http = stamped
    site.alter("cards", access="invite")
    assert http.get("/collections/cards").status_code == 401
    entry = next(s for s in http.get("/map").json()["structures"] if s["name"] == "cards")
    assert entry["note"] == "Needs a key from the site owner."

    stranger = guest(site, [{"target": "cards", "ops": ["stamp"]}])
    assert http.get("/collections/cards", headers=auth(stranger)).status_code == 401, "acting is not reading"
    site.set_role("guests", [{"target": "cards", "ops": ["read"]}])
    r = http.get("/collections/cards", headers=auth(stranger))
    assert r.status_code == 200 and r.json()["items"][0]["data"] == {"id": "c1", "title": "Card"}, \
        "a guest gets the visitor view: no private fields"
    assert http.get("/collections/cards/c1", headers=auth(stranger)).status_code == 200
    assert http.get("/collections/cards/schema", headers=auth(stranger)).status_code == 200
    entry = next(s for s in http.get("/map", headers=auth(stranger)).json()["structures"] if s["name"] == "cards")
    assert "note" not in entry

    tag = r.headers["etag"]
    site["cards"].change("c1", title="Changed")
    fresh = http.get("/collections/cards", headers={**auth(stranger), "If-None-Match": tag})
    assert fresh.status_code == 200 and fresh.json()["items"][0]["data"]["title"] == "Changed", \
        "invite-only changes are not in the public feed, so a keyed reader's ETag must follow every write"
    assert http.get("/collections/cards", headers=auth(owner)).json()["items"][0]["data"]["secret"] == "s"


def test_custom_actions(made):
    site, owner = made

    class Signup(BaseModel):
        email: str
        seats: int = 1

    @site.action("join_waitlist", summary="Join the waiting list.", input=Signup, open=True)
    def join(caller, form):
        with site.store.tx():
            site.store.set_meta("waiting", int(site.store.meta("waiting", 0)) + form.seats)
        return {"position": int(site.store.meta("waiting")),
                "payment": PaymentHandoff(amount="5.00", currency="USD", url="https://pay.example/w1")
                .model_dump(mode="json")}

    @site.action("members_only", mutates=False)
    def members(caller):
        return {"hello": caller.id}

    http = client(site)
    listed = {a["action"]: a for a in http.get("/actions").json()["actions"]}
    assert listed["join_waitlist"]["href"] == "/actions/join_waitlist" and not listed["join_waitlist"]["needs_key"]
    assert "email" in listed["join_waitlist"]["arguments"]["properties"] and listed["members_only"]["needs_key"]

    r = http.post("/actions/join_waitlist", json={"email": "a@example.org", "seats": 2},
                  headers={"Idempotency-Key": "w"})
    assert r.json()["position"] == 2 and r.json()["payment"] == {
        "amount": "5.00", "currency": "USD", "url": "https://pay.example/w1", "expires": None, "note": ""}
    again = http.post("/actions/join_waitlist", json={"email": "a@example.org", "seats": 2},
                      headers={"Idempotency-Key": "w"})
    assert again.json()["position"] == 2 and again.headers["idempotent-replay"] == "true"
    preview = http.post("/actions/join_waitlist?dry_run=true", json={"email": "b@example.org"})
    assert preview.json()["dry_run"] is True and int(site.store.meta("waiting")) == 2
    bad = http.post("/actions/join_waitlist", json={"seats": "many"}, headers={"Idempotency-Key": "x"})
    assert bad.status_code == 400 and "email" in bad.json()["error"]["message"]
    assert site.audit()["entries"][0]["op"] == "join_waitlist"

    assert http.post("/actions/members_only").status_code == 401
    assert http.post("/actions/members_only", headers=auth(owner)).json() == {"hello": "owner"}
    assert http.post("/actions/nope").status_code == 404
    with pytest.raises(SiteError):
        site.action("change")


def test_settings_and_grants(stamped):
    site, _, _ = stamped
    assert site.describe("cards")["settings"] == {"stampable": {"open": False}}
    assert log(site)[-2]["values"]["definition"]["settings"] == {"stampable": {"open": False}}, \
        "what visitors may do on a structure is part of its public definition"
    with pytest.raises(SiteError) as e:
        site.alter("cards", settings={"teleport": {}})
    assert "stampable" in e.value.hint
    site.alter("cards", settings={"stampable": None})
    assert "settings" not in site.describe("cards")

    assert {"read", "stamp", "change"} <= set(ops.grantable())
    with pytest.raises(SiteError) as e:
        site.set_role("guests", [{"target": "cards", "ops": ["teleport"]}])
    assert "stamp" in e.value.hint and "read" in e.value.hint


def test_forms_are_not_record_collections(site):
    site.define("contact", type="form", access="private", fields=["topic:string", "text:text"])
    described = site.describe("contact")
    assert described["type"] == "form" and [f["name"] for f in described["fields"]] == ["topic", "text"]
    for attempt in (lambda: site["contact"].add(topic="x"), lambda: site["contact"].list(),
                    lambda: ops.run(site, ops.OWNER, "get", {"target": "contact", "key": "1"})):
        with pytest.raises(SiteError) as e:
            attempt()
        assert e.value.code == "wrong_structure" and "inbox" in e.value.hint
    site.alter("contact", access="public")
    entry = next(s for s in site.manifest()["structures"] if s["name"] == "contact")
    assert entry["type"] == "form" and entry["href"] == "/forms/contact"


def test_a_feature_can_offer_staff_operations_to_roles(site):
    ops.register(ops.Op("peek", "Look at a structure's count.", lambda site, target: site.describe(target)["count"],
                        {"target": {"type": "string"}}, ("target",), mutates=False, grantable=True))
    try:
        site.define("things")
        site.set_role("lookers", [{"target": "things", "ops": ["peek"]}])
        looker = ops.Principal("role", site.issue_key("lookers")["id"], "lookers")
        assert ops.run(site, looker, "peek", {"target": "things"}) == 0
        with pytest.raises(SiteError) as e:
            ops.run(site, looker, "add", {"target": "things", "data": {}})
        assert e.value.code == "forbidden", "being granted one operation grants nothing else"
        with pytest.raises(SiteError):
            ops.run(site, ops.ANONYMOUS, "peek", {"target": "things"})
    finally:
        ops.OPS.pop("peek")
