"""Site templates: each applies cleanly, resolves dates at creation time, and behaves as a visitor expects."""

import json
import re

import pytest

from aisites import Site, SiteError, templates
from aisites.templates import TEMPLATES, apply, describe
from tests.util import assert_chain_ok, auth, client

NAMES = ["blank", "school", "notice_board", "shop", "restaurant", "contact", "calendar"]


def raises(code, fn, *args):
    with pytest.raises(SiteError) as e:
        fn(*args)
    assert e.value.code == code, e.value.message
    return e.value


# ---------------------------------------------------------------- the catalogue

def test_describe_lists_every_template_blank_first():
    listing = describe()
    assert [t["name"] for t in listing] == NAMES
    assert listing[0]["name"] == "blank" and listing[0]["structures"] == []
    for t in listing:
        assert set(t) == {"name", "title", "description", "structures", "good_for"}
        assert all(isinstance(t[k], str) and t[k] for k in ("title", "description", "good_for"))
        assert len(t["title"].split()) <= 6


@pytest.mark.parametrize("name", NAMES)
def test_applies_cleanly_and_reports_what_it_created(name, made):
    site, owner = made
    result = apply(site, name)
    created = sorted(site._defs())
    entry = next(t for t in describe() if t["name"] == name)
    assert result == {"template": name, "structures": entry["structures"]}
    assert sorted(result["structures"]) == created
    assert_chain_ok(site)

    http = client(site)
    public = {s["name"] for s in http.get("/map").json()["structures"]}
    mine = {s["name"] for s in http.get("/map", headers=auth(owner)).json()["structures"]}
    assert mine == set(created)
    assert public == {n for n in created if site.describe(n)["access"] != "private"}
    assert site.list_keys()["keys"] == [], "a template issues no keys"
    assert site.config["name"] == "Test site", "a template never changes site settings"


def test_templates_are_plain_json():
    assert json.loads(json.dumps(TEMPLATES)) == TEMPLATES


def test_no_fixed_dates_in_templates():
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}", json.dumps(TEMPLATES))
    assert not re.search(r"\b20\d{2}\b", json.dumps(TEMPLATES)), "years belong to placeholders"


# ---------------------------------------------------------------- placeholders

@pytest.fixture
def probe():
    """A throwaway template that records resolved dates."""
    TEMPLATES["probe"] = {"name": "probe", "title": "Probe", "description": "x", "good_for": "x", "steps": [
        ["define", {"target": "notices", "kind": "Notice"}],
        ["add", {"target": "notices", "data": {"id": "a", "title": "t", "valid_until": "{date+1}T19:00",
                                              "text": "{date} and {date+10}"}}],
    ]}
    yield "probe"
    del TEMPLATES["probe"]


def test_placeholders_resolve_against_the_clock(made, probe):
    site, _ = made
    apply(site, probe)
    data = site.get("notices", "a")["data"]
    assert data["valid_until"].startswith("2026-11-15T19:00") and data["text"] == "2026-11-14 and 2026-11-24"


def test_placeholders_use_the_sites_time_zone(tmp_path, clock, probe):
    # 18:00 UTC on the 14th is already the 15th in Auckland.
    site, _ = Site.create(tmp_path / "nz", name="NZ", timezone="Pacific/Auckland", clock=clock)
    try:
        apply(site, probe)
        data = site.get("notices", "a")["data"]
        assert data["valid_until"].startswith("2026-11-16T19:00") and data["text"] == "2026-11-15 and 2026-11-25"
    finally:
        site.close()


def test_unknown_placeholder_is_an_error_and_leaves_nothing(made):
    site, _ = made
    TEMPLATES["typo"] = {"name": "typo", "title": "T", "description": "x", "good_for": "x", "steps": [
        ["define", {"target": "notices", "kind": "Notice"}],
        ["add", {"target": "notices", "data": {"id": "a", "title": "{tomorrow}"}}]]}
    try:
        err = raises("bad_placeholder", apply, site, "typo")
        assert "{tomorrow}" in err.message and "{date+N}" in err.hint
        assert site._defs() == {}
    finally:
        del TEMPLATES["typo"]
    assert templates._resolve({"a": ["{date+2}"]}, __import__("datetime").date(2026, 1, 30)) == {"a": ["2026-02-01"]}
    raises("bad_placeholder", templates._resolve, "{date-1}", __import__("datetime").date(2026, 1, 1))


# ---------------------------------------------------------------- refusals

def test_refuses_non_empty_site_and_unknown_name(site):
    site.define("things")
    err = raises("not_empty", apply, site, "school")
    assert err.status == 409 and "new" in err.hint
    assert list(site._defs()) == ["things"]
    err = raises("bad_template", apply, site, "spaceship")
    assert all(n in err.hint for n in NAMES)


def test_a_failing_step_leaves_the_site_untouched(site):
    TEMPLATES["broken"] = {"name": "broken", "title": "B", "description": "x", "good_for": "x", "steps": [
        ["define", {"target": "notices", "kind": "Notice"}],
        ["add", {"target": "notices", "data": {"id": "a", "title": "ok"}}],
        ["add", {"target": "nowhere", "data": {"id": "b"}}]]}
    try:
        with pytest.raises(SiteError):
            apply(site, "broken")
    finally:
        del TEMPLATES["broken"]
    assert site._defs() == {}
    apply(site, "blank")


def test_blank_creates_nothing(site):
    assert apply(site, "blank") == {"template": "blank", "structures": []}
    assert site._defs() == {}


# ---------------------------------------------------------------- behaviour, as a visitor

def test_school(made):
    site, _ = made
    apply(site, "school")
    http = client(site)
    today = http.get("/hours", params={"date": "2026-11-16"}).json()["results"][0]  # a Monday
    assert today["open"] is True and today["intervals"] == [["08:00", "15:00"]]
    assert http.get("/hours", params={"date": "2026-11-15"}).json()["results"][0]["open"] is False  # Sunday
    closed = http.get("/hours", params={"date": "2026-11-17"}).json()["results"][0]  # three days ahead
    assert closed["open"] is False and "closure" in closed["reason"]
    assert http.get("/collections/closures").json()["total"] == 1
    assert http.get("/collections/pages/about").status_code == 200


def test_notice_board_notices_expire(made, clock):
    site, _ = made
    apply(site, "notice_board")
    http = client(site)

    def ids():
        return {r["key"] for r in http.get("/collections/notices").json()["items"]}

    assert ids() == {"location", "special"}
    clock.advance(days=2)  # 18:00 UTC on the 16th: the first expired at 23:00 local on the 15th
    assert ids() == {"special"}
    clock.advance(days=3)
    assert ids() == set()
    assert http.get("/collections/menu").json()["total"] == 2


def test_shop_filters_by_category(made):
    site, _ = made
    apply(site, "shop")
    http = client(site)
    tree = http.get("/trees/categories").json()
    assert "featured" in json.dumps(tree) and "gifts" in json.dumps(tree)
    found = http.get("/collections/products", params={"category": "gifts"}).json()
    assert [r["key"] for r in found["items"]] == ["SAMPLE-3"]
    assert found["items"][0]["data"]["stock"] == 8
    assert http.get("/collections/products").json()["total"] == 3
    assert http.get("/collections/offers").status_code == 404


def test_restaurant_books_a_table_anonymously(made):
    site, _ = made
    apply(site, "restaurant")
    http = client(site)
    table = "2026-11-14-1800-2"
    assert http.get(f"/collections/tables/{table}").json()["data"]["remaining"] == 4
    assert http.get("/collections/tables").json()["total"] == 8
    held = http.post("/actions/tables/reserve", json={"key": table}, headers={"Idempotency-Key": "r1"})
    assert held.status_code == 200, held.text
    held = held.json()
    assert held["status"] == "held" and held["expires"] == "2026-11-14T18:15:00Z"
    booked = http.post("/actions/tables/book", headers={"Idempotency-Key": "b1"},
                       json={"booking": held["booking"], "details": {"name": "Ada", "party_size": 2}})
    assert booked.json()["status"] == "booked"
    assert http.get(f"/collections/tables/{table}").json()["data"]["remaining"] == 3
    assert http.get("/hours", params={"date": "2026-11-17"}).json()["results"][0]["open"] is True


def test_contact_round_trip(made):
    site, owner = made
    apply(site, "contact")
    assert site.list_keys()["keys"] == [], "the template issues no keys"
    http = client(site)
    customer = site.issue_key("customers")["secret"]
    staff = site.issue_key("inbox")["secret"]

    assert http.post("/actions/contact/submit", json={"data": {"purpose": "Question", "message": "hi"}},
                     headers={"Idempotency-Key": "a"}).status_code == 401
    sent = http.post("/actions/contact/submit", json={"data": {"purpose": "Question", "message": "Open Sunday?"}},
                     headers={**auth(customer), "Idempotency-Key": "a"})
    assert sent.status_code == 200, sent.text
    ticket = sent.json()["ticket"]
    inbox = http.post("/admin/contact/inbox", json={}, headers=auth(staff))
    assert inbox.status_code == 200 and inbox.json()["messages"][0]["id"] == ticket
    assert http.post("/admin/contact/inbox", json={}, headers=auth(customer)).status_code == 403
    read = http.post("/admin/contact/read_message", json={"ticket": ticket}, headers=auth(staff))
    assert "Open Sunday?" in json.dumps(read.json())


def test_calendar_is_private(made):
    site, owner = made
    apply(site, "calendar")
    http = client(site)
    assert http.get("/map").json()["structures"] == []
    assert http.get("/collections/calendar").status_code == 404
    assert http.get("/collections/calendar", headers=auth(owner)).json()["total"] == 0
