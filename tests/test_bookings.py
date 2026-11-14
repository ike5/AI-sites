"""Bookings: holds on capacity, confirmation, release, expiry, isolation and privacy."""

import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from aisites import SiteError, ops
from aisites.cli import main
from examples import hangout, restaurant
from tests.util import assert_chain_ok, auth, client, log, mcp_call

TABLE = "2026-11-14-1800-2"  # four tables for two at 18:00 tonight


@pytest.fixture
def rest(tmp_path, clock):
    site, owner = restaurant.build(tmp_path / "restaurant", clock=clock)
    yield site, owner
    site.close()


@pytest.fixture
def hang(tmp_path, clock):
    site, owner = hangout.build(tmp_path / "hangout", clock=clock)
    yield site, owner
    site.close()


def visitor(n="a"):
    return ops.Principal("anonymous", f"anon:{n}")


def remaining(site, key=TABLE, target="tables"):
    return site.get(target, key)["data"]["remaining"]


def reserve(site, who, key=TABLE, quantity=1, idem=None):
    result, _ = site.act(who, "reserve", "tables", {"key": key, "quantity": quantity},
                         idem or f"{who.id}-{key}-{quantity}-{remaining(site, key)}")
    return result


def raises(code, fn, *args, **kw):
    with pytest.raises(SiteError) as e:
        fn(*args, **kw)
    assert e.value.code == code, e.value.message
    return e.value


# ---------------------------------------------------------------- the setting

def test_setting_validation(site):
    site.define("slots", kind="Slot")
    site.define("plain", fields=["title:string"])
    site.define("two", fields=["a:integer:capacity", "b:integer:capacity", "price:decimal", "cur:string"])
    site.define("menu", type="tree")

    def bad(name, value):
        return raises("invalid", site.alter, name, settings={"booking": value})

    assert "only a collection" in bad("menu", {}).message
    assert "capacity" in bad("plain", {}).hint
    assert "capacity: a, b" in bad("two", {}).hint, "two capacity fields: name one"
    assert "not a capacity field" in bad("slots", {"field": "capacity"}).message
    assert "hold_minutes" in bad("slots", {"hold_minutes": 31}).message
    assert "hold_minutes" in bad("slots", {"hold_minutes": 0}).message
    assert "max_active_per_caller" in bad("slots", {"max_active_per_caller": 0}).message
    assert "open" in bad("slots", {"open": "yes"}).message
    assert "hold_mins" in bad("slots", {"hold_mins": 5}).message
    assert "listed twice" in bad("slots", {"details": ["name", "name"]}).message
    assert raises("bad_field", site.alter, "slots", settings={"booking": {"details": ["x:colour"]}})
    assert "amount_field" in bad("two", {"field": "a", "payment": {"url": "https://p.example/{booking}",
                                                                   "amount_field": "cur"}}).message
    assert "url" in bad("two", {"field": "a", "payment": {"url": "pay.example", "amount_field": "price"}}).message
    assert "currency_field" in bad("two", {"field": "a", "payment": {
        "url": "https://p.example", "amount_field": "price", "currency_field": "a"}}).message

    out = site.alter("slots", settings={"booking": {"details": ["name!:string"]}})["settings"]["booking"]
    assert out["field"] == "remaining" and out["open"] is False and out["hold_minutes"] == 15
    assert out["max_active_per_caller"] == 3 and out["details"][0]["name"] == "name"
    with pytest.raises(SiteError):
        site.define("other", kind="Slot", settings={"booking": {"hold_minutes": 45}})
    assert "other" not in site._defs(), "a bad setting leaves nothing behind"


# ---------------------------------------------------------------- the restaurant, as a visiting agent

def test_restaurant_flow_through_the_web(rest, clock):
    site, owner = rest
    http = client(site)
    entry = next(s for s in http.get("/.well-known/aisite.json").json()["structures"] if s["name"] == "tables")
    assert set(entry["actions"]) == {"reserve", "book", "release", "booking"}
    actions = {a["action"]: a for a in http.get("/actions").json()["actions"] if a.get("target") == "tables"}
    assert actions["reserve"]["needs_key"] is False and actions["reserve"]["arguments"]["required"] == ["key"]
    assert actions["booking"]["changes_site"] is False

    how = http.get("/bookable/tables").json()
    assert how["hold_minutes"] == 10 and how["details"]["required"] == ["name", "party_size"]
    assert how["actions"]["book"] == "/actions/tables/book" and how["payment"] is True
    assert http.get("/bookable/places").status_code == 404

    href = actions["reserve"]["href"]
    preview = http.post(href + "?dry_run=true", json={"key": TABLE, "quantity": 2})
    assert preview.status_code == 200 and preview.json()["dry_run"] is True
    assert preview.json()["result"]["status"] == "held" and remaining(site) == 4
    assert site.bookings("tables")["total"] == 0, "a preview leaves no hold"

    first = http.post(href, json={"key": TABLE, "quantity": 2}, headers={"Idempotency-Key": "r1"})
    again = http.post(href, json={"key": TABLE, "quantity": 2}, headers={"Idempotency-Key": "r1"})
    assert first.status_code == 200 and first.json() == again.json()
    assert again.headers["idempotent-replay"] == "true"
    held = first.json()
    assert held["status"] == "held" and held["quantity"] == 2 and held["remaining"] == 2
    assert held["expires"] == "2026-11-14T18:10:00Z" and held["book"]["details"]["required"] == ["name", "party_size"]
    assert remaining(site) == 2 and site.bookings("tables")["total"] == 1, "one hold, not two"

    record = http.get(f"/collections/tables/{TABLE}").json()
    assert record["data"]["remaining"] == 2 and record["live_as_of"] == "2026-11-14T18:00:00Z"
    assert "booking" not in json.dumps(record)

    bad = http.post("/actions/tables/book", json={"booking": held["booking"], "details": {"name": "Ada"}},
                    headers={"Idempotency-Key": "b0"})
    assert bad.status_code == 400 and "party_size" in bad.json()["error"]["message"]
    booked = http.post("/actions/tables/book", json={"booking": held["booking"],
                                                     "details": {"name": "Ada Lovelace", "party_size": 2}},
                       headers={"Idempotency-Key": "b1"}).json()
    assert booked["status"] == "booked" and booked["details"]["name"] == "Ada Lovelace" and "expires" not in booked
    assert booked["payment"] == {"amount": "20.00", "currency": "USD", "expires": None,
                                 "url": f"https://pay.example/trattoria-lucia/{held['booking']}",
                                 "note": booked["payment"]["note"]}
    status = http.post("/actions/tables/booking", json={"booking": held["booking"]}).json()
    assert status["status"] == "booked"

    clock.advance(minutes=5)
    released = http.post("/actions/tables/release", json={"booking": held["booking"]},
                         headers={"Idempotency-Key": "x1"})
    assert released.json()["status"] == "released" and remaining(site) == 4
    twice = http.post("/actions/tables/release", json={"booking": held["booking"]}, headers={"Idempotency-Key": "x2"})
    assert twice.status_code == 409 and "already released" in twice.json()["error"]["message"]
    assert http.get(f"/collections/tables/{TABLE}").json()["live_as_of"] == "2026-11-14T18:05:00Z"


# ---------------------------------------------------------------- the hangout, with guest keys

def test_hangout_needs_a_friends_key(hang):
    site, owner = hang
    http = client(site)
    day = site["playtimes"].list()["items"][0]["key"]
    body = {"key": day}
    r = http.post("/actions/playtimes/reserve", json=body, headers={"Idempotency-Key": "n"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "needs_key"
    assert http.get("/bookable/playtimes").status_code == 401
    assert http.get("/collections/playtimes").status_code == 401

    friend = hangout.friend_key(site, "Sam's family")
    assert http.get("/collections/playtimes", headers=auth(friend)).json()["total"] == 3
    assert http.get("/bookable/playtimes", headers=auth(friend)).json()["needs_key"] is True
    held = http.post("/actions/playtimes/reserve", json=body,
                     headers={**auth(friend), "Idempotency-Key": "f1"}).json()
    assert held["status"] == "held" and held["expires"] == "2026-11-14T18:30:00Z"
    booked = http.post("/actions/playtimes/book", headers={**auth(friend), "Idempotency-Key": "f2"},
                       json={"booking": held["booking"], "details": {"child": "Sam", "grown_up": "Jo"}}).json()
    assert booked["status"] == "booked" and remaining(site, day, "playtimes") == 2
    assert held["booking"] not in json.dumps(http.get("/changes").json()), "invite-only changes stay off the feed"

    site.set_role("lookers", [{"target": "playtimes", "ops": ["read"]}])
    looker = site.issue_key("lookers")["secret"]
    r = http.post("/actions/playtimes/reserve", json=body, headers={**auth(looker), "Idempotency-Key": "l1"})
    assert r.status_code == 403 and "may not `reserve`" in r.json()["error"]["message"]
    assert http.post("/actions/playtimes/booking", json={"booking": held["booking"]},
                     headers=auth(looker)).status_code == 403


# ---------------------------------------------------------------- limits

def test_concurrent_reserves_never_oversell(rest):
    site, _ = rest

    def attempt(i):
        try:
            site.act(visitor(f"t{i}"), "reserve", "tables", {"key": TABLE}, f"k{i}")
            return "ok"
        except SiteError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(40)))
    assert results.count("ok") == 4 and set(results) == {"ok", "no_capacity"}
    assert remaining(site) == 0 and site.bookings("tables", status="held")["total"] == 4


def test_hoarding_capacity_and_visibility(rest, site):
    rsite, _ = rest
    a = visitor("a")
    first = reserve(rsite, a)
    reserve(rsite, a, key="2026-11-14-1930-2")
    e = raises("too_many_bookings", reserve, rsite, a, key="2026-11-14-2100-2")
    assert "limit is 2" in e.message and e.status == 409 and "release" in e.hint
    rsite.act(a, "release", "tables", {"booking": first["booking"]}, "rel")
    reserve(rsite, a, key="2026-11-14-2100-2")
    for i in range(3):
        reserve(rsite, ops.OWNER, key="2026-11-15-1800-2", idem=f"o{i}")  # the owner is not limited

    e = raises("no_capacity", reserve, rsite, visitor("b"), quantity=5)
    assert "Only 4 left" in e.message
    e = raises("no_capacity", reserve, rsite, visitor("b"), key="2026-11-15-1800-4", quantity=4)
    assert "Only 3 left" in e.message and "3 or fewer" in e.hint
    raises("invalid", reserve, rsite, visitor("b"), quantity=0)

    site.define("later", kind="Slot", fields=["valid_from:datetime"], settings={"booking": {"open": True}})
    site["later"].add(id="soon", name="Soon", start="2026-12-01T10:00", remaining=5, valid_from="2026-11-20T00:00")
    missing = raises("not_found", site.act, visitor(), "reserve", "later", {"key": "nope"}, "x1")
    hidden = raises("not_found", site.act, visitor(), "reserve", "later", {"key": "soon"}, "x2")
    assert hidden.hint == missing.hint and hidden.message.replace("soon", "nope") == missing.message
    assert site.act(ops.OWNER, "reserve", "later", {"key": "soon"}, "x3")[0]["status"] == "held"


# ---------------------------------------------------------------- expiry

def test_holds_expire_by_themselves(rest, clock):
    site, _ = rest
    a, b, c = visitor("a"), visitor("b"), visitor("c")
    early = reserve(site, a, quantity=2)
    clock.advance(minutes=3)
    late = reserve(site, b)
    kept = reserve(site, c)
    site.act(c, "book", "tables", {"booking": kept["booking"], "details": {"name": "C", "party_size": 2}}, "bk")
    assert remaining(site) == 0

    clock.advance(minutes=8)  # past the first hold's 10 minutes, not the second's
    site.tick()
    assert remaining(site) == 2 and site.booking("tables", early["booking"])["status"] == "expired"

    clock.advance(hours=6)  # nobody reads for hours; every overdue hold is settled in one pass
    assert site.changes()["changes"][-1]["what"] == "live value changed"
    assert remaining(site) == 3, "the booked place stays taken"
    assert {i["booking"]: i["status"] for i in site.bookings("tables")["items"]} == {
        early["booking"]: "expired", late["booking"]: "expired", kept["booking"]: "booked"}

    e = raises("hold_expired", site.act, b, "book", "tables",
               {"booking": late["booking"], "details": {"name": "B", "party_size": 2}}, "late")
    assert "reserve" in e.hint.lower()
    assert raises("hold_expired", site.act, b, "release", "tables", {"booking": late["booking"]}, "rl")


def test_expiry_without_any_tick(rest, clock):
    site, _ = rest
    holds = [reserve(site, visitor(f"v{i}")) for i in range(4)]
    assert remaining(site) == 0
    clock.advance(days=2)
    # The next booking call settles overdue holds itself, before checking capacity.
    assert reserve(site, visitor("new"), quantity=4)["remaining"] == 0
    assert all(site.booking("tables", h["booking"])["status"] == "expired" for h in holds)


# ---------------------------------------------------------------- isolation and privacy

def test_one_caller_cannot_touch_anothers_booking(rest):
    site, _ = rest
    mine = reserve(site, visitor("a"))["booking"]
    other = visitor("b")
    http = client(site)
    for op, args in (("booking", {}), ("release", {}), ("book", {"details": {"name": "X", "party_size": 1}})):
        theirs = raises("not_found", site.act, other, op, "tables", {"booking": mine, **args}, f"{op}1")
        bogus = raises("not_found", site.act, other, op, "tables", {"booking": "bk_0000000000000000", **args},
                       f"{op}2")
        assert (theirs.message.replace(mine, "ID"), theirs.hint, theirs.status) == \
               (bogus.message.replace("bk_0000000000000000", "ID"), bogus.hint, bogus.status)
    web = http.post("/actions/tables/booking", json={"booking": mine}).json()
    fake = http.post("/actions/tables/booking", json={"booking": "bk_0000000000000000"}).json()
    assert json.dumps(web).replace(mine, "ID") == json.dumps(fake).replace("bk_0000000000000000", "ID")
    assert site.booking("tables", mine, visitor("a"))["status"] == "held"


def test_bookings_stay_private(rest):
    site, owner = rest
    http = client(site)
    cursor = site.changes()["cursor"]
    seq = len(log(site))
    a = visitor("a")
    held = reserve(site, a, quantity=2)
    site.act(a, "book", "tables", {"booking": held["booking"],
                                   "details": {"name": "Grace Hopper", "party_size": 2, "phone": "555-0199"}}, "b")
    second = reserve(site, a, key="2026-11-15-1800-4")
    site.act(a, "release", "tables", {"booking": second["booking"]}, "r")

    secrets = (held["booking"], second["booking"], "Grace Hopper", "555-0199", "anon:a")
    public = [json.dumps(log(site)), json.dumps(http.get("/").json()),
              json.dumps(http.get("/collections/tables").json()),
              json.dumps(http.get(f"/collections/tables/{TABLE}").json()), json.dumps(http.get("/history").json()),
              json.dumps(http.get("/bookable/tables").json()), json.dumps(http.get("/actions").json())]
    assert not [s for s in secrets for page in public if s in page]
    assert len(log(site)) == seq, "a capacity value is live: no history entries at all"
    changes = site.changes(since=cursor)["changes"]
    assert changes and {c["what"] for c in changes} == {"live value changed"}
    assert {c["path"] for c in changes} == {f"collections/tables/{TABLE}", "collections/tables/2026-11-15-1800-4"}
    assert "Grace" not in json.dumps(site.audit()), "the audit trail names fields, never values"
    assert "bookings" not in site.get("tables", TABLE) and site["tables"].list()["total"] == 12
    assert_chain_ok(site)


# ---------------------------------------------------------------- the record changing underneath

def test_owner_changes_and_removals(rest):
    site, _ = rest
    a = visitor("a")
    held = reserve(site, a, quantity=2)
    site["tables"].change(TABLE, remaining=10)  # the owner's number is simply the new capacity
    site.act(a, "release", "tables", {"booking": held["booking"]}, "r1")
    assert remaining(site) == 12

    one = reserve(site, a)
    two = reserve(site, a, key="2026-11-14-1930-2")
    site.act(a, "book", "tables", {"booking": two["booking"], "details": {"name": "A", "party_size": 2}}, "b")
    site["tables"].remove(TABLE)
    gone = site.booking("tables", one["booking"], a)
    assert gone["status"] == "released" and gone["reason"] == "record_removed"
    e = raises("already_released", site.act, a, "book", "tables",
               {"booking": one["booking"], "details": {"name": "A", "party_size": 2}}, "b2")
    assert "record was removed" in e.message
    assert reserve(site, a, key="2026-11-14-2100-2")["status"] == "held", "the removed hold no longer counts"

    site["tables"].add(id=TABLE, name="Again", start="2026-11-14T18:00", remaining=4)
    assert site.booking("tables", one["booking"], a)["status"] == "released", "a new record is not the old one"


# ---------------------------------------------------------------- owner operations through every door

def test_owner_operations_through_every_door(rest, tmp_path, clock):
    site, owner = rest
    a, b = visitor("a"), visitor("b")
    held = reserve(site, a, quantity=2)
    site.act(a, "book", "tables", {"booking": held["booking"], "details": {"name": "Ada", "party_size": 2}}, "b")
    other = reserve(site, b, key="2026-11-15-1800-4")
    third = reserve(site, b, key="2026-11-14-1930-2")

    listing = site.bookings("tables")
    assert listing["total"] == 3 and listing["items"][0]["caller"].startswith("anon:")
    assert site.bookings("tables", status="booked")["items"][0]["details"] == {"name": "Ada", "party_size": 2}
    assert site.bookings("tables", key=TABLE)["total"] == 1
    page = site.bookings("tables", limit=2)
    assert len(page["items"]) == 2 and page["next"] == "2"
    assert len(site.bookings("tables", limit=2, cursor=page["next"])["items"]) == 1
    raises("invalid", site.bookings, "tables", status="lost")
    raises("not_bookable", site.bookings, "places")

    out = io.StringIO()
    assert main(["bookings", "tables", "--status", "held", "-s", str(tmp_path / "restaurant")],
                clock=clock, out=out) == 0
    assert json.loads(out.getvalue())["total"] == 2
    out = io.StringIO()
    assert main(["release", "tables", other["booking"], "-s", str(tmp_path / "restaurant")], clock=clock, out=out) == 0
    assert json.loads(out.getvalue())["status"] == "released" and remaining(site, "2026-11-15-1800-4") == 3

    http = client(site)
    r = http.post("/admin/tables/bookings", json={"status": "booked"}, headers=auth(owner))
    assert r.status_code == 200 and r.json()["items"][0]["booking"] == held["booking"]
    assert http.post("/admin/tables/bookings", json={}).status_code == 401
    r = http.post("/admin/tables/release", json={"booking": held["booking"]}, headers=auth(owner))
    assert r.json()["status"] == "released" and r.json()["reason"] == "released by owner"

    error, result = mcp_call(site, "bookings", {"target": "tables", "status": "held"})
    assert not error and [i["booking"] for i in result["items"]] == [third["booking"]]
    error, result = mcp_call(site, "release", {"target": "tables", "booking": third["booking"]})
    assert not error and result["status"] == "released"
    error, result = mcp_call(site, "release", {"target": "tables", "booking": third["booking"]})
    assert error and result["error"]["code"] == "already_released"
    assert remaining(site) == 4

    raises("forbidden", ops.run, site, a, "bookings", {"target": "tables"})
    site.set_role("hosts", [{"target": "tables", "ops": ["bookings"]}])
    host = ops.Principal("role", site.issue_key("hosts")["id"], "hosts")
    assert ops.run(site, host, "bookings", {"target": "tables"})["total"] == 3, "staff can be granted the list"
