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
