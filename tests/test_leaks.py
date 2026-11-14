"""Things a caller could once infer without being shown them. Each test pins one leak shut."""

import json

import pytest

from aisites import SiteError, ops
from tests.util import auth, client, log


@pytest.fixture
def games(site):
    site.define("games", kind="Product", fields=["cost:decimal:private"])
    site["games"].add(sku="G1", name="Starfall", price="59.99", stock=5, cost="20")
    return site


def public_state(site, http):
    """Everything a visitor without a key can observe about one record and the site."""
    record = http.get("/collections/games/G1")
    return {"record": {k: v for k, v in record.json().items() if k != "as_of"},
            "etags": [record.headers["etag"], http.get("/").headers["etag"], http.get("/changes").headers["etag"]],
            "changes": http.get("/changes").json()["changes"], "log": len(log(site))}


def test_private_edits_are_invisible_to_visitors(games, clock):
    http = client(games)
    before = public_state(games, http)
    clock.advance(hours=3)
    games["games"].change("G1", cost="25")
    assert public_state(games, http) == before, "no updated time, feed row, log entry or ETag moved"
    assert games["games"].get("G1")["data"]["cost"] == "25"


def test_private_scheduled_change_is_invisible_to_visitors(games, clock):
    http = client(games)
    before = public_state(games, http)
    games["games"].schedule("G1", cost="30", start="2026-12-01", end="2026-12-10")
    assert public_state(games, http) == before, "planning it shows nothing"
    clock.set(2026, 12, 5, 0, 0)
    assert public_state(games, http) == before, "nor does its starting"
    clock.set(2026, 12, 20, 0, 0)
    assert public_state(games, http) == before, "nor its ending"
    owner = games["games"].get("G1")
    assert owner["data"]["cost"] == "20" and owner["updated"] == "2026-12-10T08:00:00Z", "the owner sees it all"


def test_private_structure_activity_does_not_move_public_etags(site, made, clock):
    _, key = made
    site.define("calendar", kind="Event", access="private")
    http = client(site)
    public = [http.get(p).headers["etag"] for p in ("/", "/changes", "/history", "/llms.txt")]
    owner = http.get("/collections/calendar", headers=auth(key)).headers["etag"]
    clock.advance(hours=1)
    site["calendar"].add(title="Dentist", start="2026-11-20T09:00")
    assert [http.get(p).headers["etag"] for p in ("/", "/changes", "/history", "/llms.txt")] == public
    assert http.get("/collections/calendar", headers=auth(key)).headers["etag"] != owner, \
        "the owner's own view still revalidates"


def role_key(site, grants):
    site.set_role("staff", grants)
    return ops.Principal("role", site.issue_key("staff")["id"], "staff")


def test_role_cannot_see_timing_of_changes_to_fields_hidden_from_it(games, clock):
    pm = role_key(games, [{"target": "games", "ops": ["get", "list", "change"], "fields": ["price"]}])
    before = ops.run(games, pm, "get", {"target": "games", "key": "G1"})
    games["games"].schedule("G1", cost="30", start="2026-12-01", end="2026-12-10")
    for when in ((2026, 11, 20), (2026, 12, 5), (2026, 12, 20)):
        clock.set(*when, 0, 0)
        seen = ops.run(games, pm, "get", {"target": "games", "key": "G1"})
        assert seen == before, f"nothing about the hidden change on {when}"
        listed = ops.run(games, pm, "list", {"target": "games"})["items"][0]
        assert listed == before

    clock.set(2026, 11, 20, 0, 0)
    games["games"].schedule("G1", price="49.99", cost="31", start="2026-11-25")
    mixed = ops.run(games, pm, "get", {"target": "games", "key": "G1"})
    assert mixed["upcoming_changes"] == [{"set": {"price": "49.99"}, "start": "2026-11-25T08:00:00Z", "end": None}]
    assert "cost" not in json.dumps(mixed)


def test_validation_hints_do_not_name_fields_hidden_from_a_role(games):
    clerk = role_key(games, [{"target": "games", "ops": ["add", "change"], "fields": ["sku", "name", "price"]}])
    with pytest.raises(SiteError) as e:
        ops.run(games, clerk, "add", {"target": "games", "data": {"sku": "G2", "name": "x", "price": "cheap"}})
    assert "price" in e.value.message and "cost" not in e.value.message + e.value.hint
    with pytest.raises(SiteError) as e:
        games["games"].add(sku="G3", name="x", price="cheap")
    assert "cost" in e.value.hint, "the owner still gets the full list"

    games.alter("games", add=[{"name": "margin", "type": "decimal", "policy": "private", "default": "1"}])
    games.alter("games", remove=["margin"])
    games.define("strict", fields=[{"name": "secret", "type": "string", "policy": "private", "required": True},
                                   "title:string"])
    writer = role_key(games, [{"target": "strict", "ops": ["add"], "fields": ["id", "title"]}])
    with pytest.raises(SiteError) as e:
        ops.run(games, writer, "add", {"target": "strict", "data": {"title": "t"}})
    assert "secret" not in e.value.message + e.value.hint and "may not set" in e.value.message


def test_role_cannot_probe_a_structure_it_cannot_read_through_a_choice_field(site):
    site.define("shelves", access="private", fields=["label:string"])
    site["shelves"].add(id="back-room", label="Back room")
    site.define("items", fields=["name:string", "shelf:string"], refs={"shelf": "shelves"})
    site["items"].add(id="i1", name="Thing")
    stocker = role_key(site, [{"target": "items", "ops": ["change"]}])

    for guess in ("back-room", "no-such-shelf"):
        with pytest.raises(SiteError) as e:
            ops.run(site, stocker, "change", {"target": "items", "key": "i1", "set": {"shelf": guess}})
        assert e.value.code == "forbidden" and "cannot read" in e.value.message, "same answer either way"

    site.set_role("staff", [{"target": "items", "ops": ["change"]}, {"target": "shelves", "ops": ["list"]}])
    assert ops.run(site, stocker, "change", {"target": "items", "key": "i1", "set": {"shelf": "back-room"}})
    with pytest.raises(SiteError) as e:
        ops.run(site, stocker, "change", {"target": "items", "key": "i1", "set": {"shelf": "no-such-shelf"}})
    assert e.value.code == "bad_choice"

    site.define("colours", type="tree")
    site.alter("items", add=["colour:string"], refs={"colour": "colours"})
    site["colours"].add({"id": "red", "label": "Red"})
    site.set_role("staff", [{"target": "items", "ops": ["change"]}])
    assert ops.run(site, stocker, "change", {"target": "items", "key": "i1", "set": {"colour": "red"}}), \
        "a public source is readable by anyone, so setting from it is fine"


def test_grants_do_not_outlive_what_they_name(games):
    games.define("notes", fields=["text:string", "tag:string"])
    games.set_role("staff", [{"target": "notes", "ops": ["add", "list"]},
                             {"target": "games", "ops": ["change"], "fields": ["price", "name"]}])
    key = games.issue_key("staff")["id"]
    staff = ops.Principal("role", key, "staff")

    games.drop("notes")
    games.define("notes", fields=["text:string"])
    with pytest.raises(SiteError) as e:
        ops.run(games, staff, "list", {"target": "notes"})
    assert e.value.code == "forbidden", "a new structure with an old name starts with no grants"

    games.alter("games", add=["blurb:string"])
    games.set_role("staff", [{"target": "games", "ops": ["change"], "fields": ["blurb"]}])
    games.alter("games", remove=["blurb"])
    assert games.role_grants("staff") == [], "a grant left with no fields is dropped, not widened to all fields"
    with pytest.raises(SiteError):
        ops.run(games, staff, "change", {"target": "games", "key": "G1", "set": {"price": "1"}})
