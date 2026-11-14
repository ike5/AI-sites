"""How the parts of Milestone 3 behave together, and with what came before."""

import pytest

from aisites import SiteError, ops
from tests.util import auth, client


@pytest.fixture
def cafe(made, clock):
    """Tables at three sittings, open to anyone, two active bookings per caller."""
    site, owner = made
    site.define("tables", kind="Slot", settings={"booking": {
        "open": True, "max_active_per_caller": 2, "details": ["name!:string"]}})
    for key, day in (("mon", "16"), ("tue", "17"), ("wed", "18")):
        site["tables"].add(id=key, name=f"Dinner {key}", start=f"2026-11-{day}T19:00",
                           end=f"2026-11-{day}T21:00", capacity=4, remaining=4)
    return site, owner, client(site)


def test_past_bookings_stop_counting_against_a_callers_limit(cafe, clock):
    site, _, _ = cafe
    regular = site.anonymous("203.0.113.5")
    for key in ("mon", "tue"):
        hold = site.reserve("tables", key, caller=regular)
        site.book("tables", hold["booking"], {"name": "Ana"}, caller=regular)
    with pytest.raises(SiteError) as e:
        site.reserve("tables", "wed", caller=regular)
    assert e.value.code == "too_many_bookings", "two upcoming bookings is this caller's limit"

    clock.set(2026, 11, 17, 6, 0)   # Monday's dinner is over; Tuesday's is still to come
    assert site.reserve("tables", "wed", caller=regular)["status"] == "held"
    with pytest.raises(SiteError):
        site.reserve("tables", "wed", caller=regular)


def test_booked_capacity_cannot_also_be_scheduled(cafe):
    site, _, _ = cafe
    with pytest.raises(SiteError) as e:
        site["tables"].schedule("mon", remaining=0, on="2026-11-16")
    assert "bookings draw on" in e.value.message and "change" in e.value.hint
    site["tables"].schedule("mon", name="Dinner, live music", on="2026-11-16")

    site.define("stock", kind="Product")
    site["stock"].add(sku="s1", name="Thing", stock=5)
    site["stock"].schedule("s1", stock=0, on="2026-12-01")
    with pytest.raises(SiteError) as e:
        site.alter("stock", settings={"booking": {}})
    assert "scheduled change" in e.value.message and "unschedule" in e.value.hint


def test_first_read_after_a_hold_expires_shows_the_place_again(cafe, clock):
    site, _, http = cafe
    site.reserve("tables", "mon", quantity=3, caller=site.anonymous("203.0.113.6"))
    assert http.get("/collections/tables/mon").json()["data"]["remaining"] == 1
    clock.advance(minutes=16)
    assert http.get("/collections/tables/mon").json()["data"]["remaining"] == 4
    listed = {i["key"]: i["data"]["remaining"] for i in http.get("/collections/tables").json()["items"]}
    assert listed == {"mon": 4, "tue": 4, "wed": 4}


def test_staff_roles_and_guest_roles_do_not_overlap(cafe):
    site, owner, http = cafe
    site.alter("tables", settings={"booking": {"open": False, "details": ["name!:string"]}})
    site.set_role("pricing", [{"target": "tables", "ops": ["get", "change"], "fields": ["name"]}])
    staff = site.issue_key("pricing")["secret"]
    site.set_role("diners", [{"target": "tables", "ops": ["reserve", "book", "release", "booking"]}])
    diner = site.issue_key("diners")["secret"]

    def act(key, op, body, idem):
        return http.post(f"/actions/tables/{op}", json=body, headers={**auth(key), "Idempotency-Key": idem})

    assert act(staff, "reserve", {"key": "mon"}, "s1").status_code == 403, "editing rights are not booking rights"
    held = act(diner, "reserve", {"key": "mon"}, "d1")
    assert held.status_code == 200
    r = http.post("/admin/tables/change", json={"key": "mon", "set": {"name": "x"}}, headers=auth(diner))
    assert r.status_code == 403, "booking rights are not editing rights"
    assert http.post("/admin/tables/bookings", headers=auth(diner)).status_code == 403
    assert http.post("/admin/tables/bookings", headers=auth(owner)).json()["total"] == 1
    assert act(staff, "booking", {"booking": held.json()["booking"]}, "s2").status_code in (403, 404)


# ---------------------------------------------------------------- all three features together

import json  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from aisites.host import Host  # noqa: E402
from aisites.verify import verify_site  # noqa: E402
from examples import contact_form, hangout, restaurant  # noqa: E402
from tests.util import assert_chain_ok, log  # noqa: E402

PRIVATE_TEXT = "Please-call-me-about-order-R-1042-Matilde-Ferreira"


def fetcher(http, prefix=""):
    def fetch(path, params=None):
        r = http.get(prefix + path, params=params or {})
        assert r.status_code == 200, r.text
        return r.json()
    return fetch


def test_nothing_from_bookings_or_messages_reaches_public_views(tmp_path, clock):
    site, owner = restaurant.build(tmp_path / "r", clock)
    site.define("contact", type="form", fields=["order_number:string", "message!:text"],
                settings={"form": {"purposes": ["Question"], "text_field": "message"}})
    site.set_role("customers", [{"target": "contact", "ops": ["submit", "ticket"]}])
    customer = site.issue_key("customers")["secret"]
    http = client(site)
    fetch = fetcher(http)
    history_before = len(log(site))
    pin = verify_site(fetch)["pin"]

    table = fetch("/collections/tables", {"limit": 1})["items"][0]["key"]
    held = http.post(f"/actions/tables/reserve", json={"key": table}, headers={"Idempotency-Key": "h"}).json()
    booked = http.post("/actions/tables/book", headers={"Idempotency-Key": "b"},
                       json={"booking": held["booking"], "details": {"name": "Matilde Ferreira", "party_size": 2}})
    assert booked.status_code == 200 and booked.json()["payment"]["url"].endswith(held["booking"])

    etag_before = http.get("/").headers["etag"]
    sent = http.post("/actions/contact/submit", headers={**auth(customer), "Idempotency-Key": "m"},
                     json={"data": {"purpose": "Question", "order_number": "R-1042", "message": PRIVATE_TEXT}})
    assert sent.status_code == 200
    site.reply("contact", sent.json()["ticket"], "We will call you today.")
    assert http.get("/").headers["etag"] == etag_before, "a message and its reply are invisible from outside"

    public = json.dumps([fetch("/"), fetch("/history", {"limit": 1000}), fetch("/changes"), fetch("/actions"),
                         fetch("/collections/tables"), fetch("/bookable/tables"), fetch("/forms/contact"),
                         fetch("/openapi.json"), http.get("/llms.txt").text])
    for secret in ("Matilde", PRIVATE_TEXT, held["booking"], sent.json()["ticket"], "We will call you"):
        assert secret not in public, secret
    assert len(log(site)) == history_before, "bookings and messages add nothing to the public history"
    assert {c["what"] for c in fetch("/changes", {"since": 0, "limit": 1000})["changes"]} <= {
        "defined", "added", "live value changed"}
    assert_chain_ok(site)
    report = verify_site(fetch, pin)
    assert report["ok"], report["problems"]

    trail = json.dumps(site.audit(limit=1000))
    assert "Matilde" not in trail and PRIVATE_TEXT not in trail, "the audit trail records who and what, never values"


def test_roles_from_different_features_stay_in_their_lanes(tmp_path, clock):
    site, owner = contact_form.build(tmp_path / "bakery", clock)
    site.define("orders", kind="Slot", settings={"booking": {"open": False}})
    site["orders"].add(id="sat", name="Saturday pickup", start="2026-11-21T09:00", capacity=5, remaining=5)
    site.set_role("pickers", [{"target": "orders", "ops": ["reserve", "book", "release", "booking"]}])
    inbox = site.issue_key("inbox")["secret"]
    customer = site.issue_key("customers")["secret"]
    picker = site.issue_key("pickers")["secret"]
    http = client(site)

    def act(key, target, op, body):
        return http.post(f"/actions/{target}/{op}", json=body, headers={**auth(key), "Idempotency-Key": f"{op}-{key[-6:]}"})

    message = {"data": {"purpose": "Other", "message": "Hello"}}
    assert act(customer, "contact", "submit", message).status_code == 200
    assert act(picker, "contact", "submit", message).status_code == 403
    assert act(inbox, "contact", "submit", message).status_code == 403, "reading the inbox is not sending to it"
    assert act(picker, "orders", "reserve", {"key": "sat"}).status_code == 200
    assert act(customer, "orders", "reserve", {"key": "sat"}).status_code == 403
    assert act(inbox, "orders", "reserve", {"key": "sat"}).status_code == 403

    for key in (customer, picker):
        assert http.post("/admin/contact/inbox", headers=auth(key)).status_code == 403
        assert http.post("/admin/orders/bookings", headers=auth(key)).status_code == 403
    assert http.post("/admin/contact/inbox", headers=auth(inbox)).status_code == 200
    assert http.post("/admin/orders/bookings", headers=auth(inbox)).status_code == 403
    assert http.post("/admin/orders/change", json={"key": "sat", "set": {"name": "x"}},
                     headers=auth(inbox)).status_code == 403


def test_preview_of_a_message_or_booking_stores_nothing(tmp_path, clock):
    site, owner = restaurant.build(tmp_path / "r", clock)
    http = client(site)
    table = http.get("/collections/tables", params={"limit": 1}).json()["items"][0]
    before = table["data"]["remaining"]
    r = http.post("/actions/tables/reserve?dry_run=true", json={"key": table["key"], "quantity": 2})
    assert r.status_code == 200 and r.json()["dry_run"] is True and r.json()["result"]["remaining"] == before - 2
    assert http.get(f"/collections/tables/{table['key']}").json()["data"]["remaining"] == before
    assert site.bookings("tables")["total"] == 0 and site.audit()["entries"] == []


def test_visitor_features_work_under_a_host_prefix(tmp_path, clock):
    restaurant.build(tmp_path / "lucia", clock)[0].close()
    mia, _ = hangout.build(tmp_path / "mia", clock)
    friend = hangout.friend_key(mia, "Noor's family")
    mia.close()
    http = TestClient(Host(tmp_path, clock))

    manifest = http.get("/s/lucia/").json()
    assert manifest["links"]["actions"] == "/s/lucia/actions" and manifest["links"]["stream"] == "/s/lucia/changes/stream"
    actions = {a["action"]: a for a in http.get("/s/lucia/actions").json()["actions"]}
    assert actions["reserve"]["href"] == "/s/lucia/actions/tables/reserve" and actions["reserve"]["needs_key"] is False
    table = http.get("/s/lucia/collections/tables", params={"limit": 1}).json()["items"][0]["key"]
    held = http.post(actions["reserve"]["href"], json={"key": table}, headers={"Idempotency-Key": "p"})
    assert held.status_code == 200 and "href" not in held.json()["book"], "no link that ignores the prefix"
    assert http.get("/s/lucia/bookable/tables").json()["actions"]["book"] == "/s/lucia/actions/tables/book"

    assert http.get("/s/mia/collections/playtimes").status_code == 401
    times = http.get("/s/mia/collections/playtimes", headers=auth(friend)).json()["items"]
    assert len(times) == 3
    joined = http.post("/s/mia/actions/playtimes/reserve", json={"key": times[0]["key"]},
                       headers={**auth(friend), "Idempotency-Key": "j"})
    assert joined.status_code == 200
    assert http.post("/s/lucia/actions/tables/reserve", json={"key": table},
                     headers={**auth(friend), "Idempotency-Key": "x"}).status_code == 401, \
        "a key for one site is not a key for another"
