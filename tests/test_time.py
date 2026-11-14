import pytest

from aisites import Site, SiteError
from tests.util import assert_chain_ok, code, log


@pytest.fixture
def games(site):
    site.define("games", kind="Product")
    site["games"].add(sku="G1", name="Starfall", price="59.99", stock=10)
    return site


def paths(site):
    return [(c["path"], c["what"]) for c in site.changes()["changes"]]


def test_hidden_change_is_invisible_until_it_starts_then_reverts(games, clock):
    # Site zone is Los Angeles, so the 15th runs from 08:00Z on the 15th to 08:00Z on the 16th.
    planned = games["games"].schedule("G1", price="47.99", on="2026-12-15")
    assert planned["schedule"]["start"] == "2026-12-15T08:00:00Z"
    assert planned["schedule"]["end"] == "2026-12-16T08:00:00Z"

    before = games.read_record("games", "G1")
    assert before["data"]["price"] == "59.99"
    assert "upcoming_changes" not in before and "next_change" not in before
    sealed = [e for e in log(games) if e["op"] == "sealed"]
    assert len(sealed) == 1 and "revealed" not in sealed[0] and sealed[0]["path"] == "sealed"
    assert all(e["op"] != "sealed" for e in games.history.entries(path="collections/games/G1"))
    assert ("collections/games/G1", "scheduled change started") not in paths(games)
    assert games["games"].get("G1")["upcoming_changes"][0]["set"] == {"price": "47.99"}, "the owner sees it"

    clock.set(2026, 12, 15, 20, 0)
    during = games.read_record("games", "G1")
    assert during["data"]["price"] == "47.99"
    assert during["active_changes"] == [{"fields": ["price"], "since": "2026-12-15T08:00:00Z",
                                         "until": "2026-12-16T08:00:00Z"}]
    assert during["next_change"] == "2026-12-16T08:00:00Z"
    assert during["updated"] == "2026-12-15T08:00:00Z"
    assert games.list("games", {"price.lt": 50}, owner=False)["total"] == 1
    revealed = [e for e in games.history.entries(path="collections/games/G1") if e["op"] == "sealed"]
    assert revealed[0]["revealed"]["values"] == {"price": "47.99"}
    assert ("collections/games/G1", "scheduled change started") in paths(games)
    assert_chain_ok(games)

    clock.set(2026, 12, 16, 9, 0)
    after = games.read_record("games", "G1")
    assert after["data"]["price"] == "59.99" and "active_changes" not in after
    assert after["updated"] == "2026-12-16T08:00:00Z"
    assert ("collections/games/G1", "scheduled change ended") in paths(games)


def test_announced_change_is_visible_ahead(games):
    games["games"].schedule("G1", price="47.99", start="2026-12-15", end="2026-12-16", announce=True)
    view = games.read_record("games", "G1")
    assert view["data"]["price"] == "59.99"
    assert view["upcoming_changes"] == [{"set": {"price": "47.99"}, "start": "2026-12-15T08:00:00Z",
                                         "end": "2026-12-16T08:00:00Z"}]
    assert view["next_change"] == "2026-12-15T08:00:00Z"
    entry = games.history.entries(path="collections/games/G1")[-1]
    assert entry["op"] == "schedule" and entry["values"] == {"price": "47.99"}
    assert ("collections/games/G1", "change announced") in paths(games)


def test_permanent_change_then_edit(games, clock):
    games["games"].schedule("G1", price="49.99", start="2026-12-01")
    clock.set(2026, 12, 2, 0, 0)
    assert games.read_record("games", "G1")["data"]["price"] == "49.99"
    games["games"].change("G1", price="44.99")
    assert games.read_record("games", "G1")["data"]["price"] == "44.99", "a later edit wins"
    assert "schedules" not in games["games"].get("G1")


def test_edit_during_a_temporary_change_shows_after_it_ends(games, clock):
    games["games"].schedule("G1", price="47.99", on="2026-12-15")
    clock.set(2026, 12, 15, 12, 0)
    games["games"].change("G1", price="54.99")
    assert games.read_record("games", "G1")["data"]["price"] == "47.99"
    clock.set(2026, 12, 16, 12, 0)
    assert games.read_record("games", "G1")["data"]["price"] == "54.99"


def test_cancelled_hidden_change_leaves_no_public_trace(games, clock):
    sid = games["games"].schedule("G1", price="47.99", on="2026-12-15")["schedule"]["id"]
    games["games"].unschedule("G1", sid)
    clock.set(2026, 12, 15, 12, 0)
    assert games.read_record("games", "G1")["data"]["price"] == "59.99"
    assert all("revealed" not in e for e in log(games))
    assert all(what != "scheduled change started" for _, what in paths(games))
    with pytest.raises(SiteError) as e:
        games["games"].unschedule("G1", sid)
    assert code(e) == "not_found"
    assert_chain_ok(games)


def test_schedule_argument_errors(games):
    with pytest.raises(SiteError) as e:
        games["games"].schedule("G1", price="1")
    assert "needs `start`" in e.value.message
    with pytest.raises(SiteError) as e:
        games["games"].schedule("G1", price="1", start="2026-12-02", end="2026-12-01")
    assert "after `start`" in e.value.message
    with pytest.raises(SiteError) as e:
        games["games"].schedule("G1", price="1", on="2026-01-01")
    assert "already over" in e.value.message
    with pytest.raises(SiteError) as e:
        games["games"].schedule("G1", colour="red", on="2026-12-15")
    assert code(e) == "invalid"


def test_notices_expire_and_future_ones_stay_sealed(site, clock):
    site.define("notices", kind="Notice")
    site["notices"].add(id="now", title="Here today", valid_until="2026-11-15T22:00")
    site["notices"].add(id="later", title="Opening soon", valid_from="2026-11-20T09:00")

    def visible():
        return [i["key"] for i in site.list("notices", owner=False)["items"]]

    assert visible() == ["now"]
    assert site.read_record("notices", "now")["next_change"] == "2026-11-16T06:00:00Z"
    with pytest.raises(SiteError):
        site.read_record("notices", "later")
    assert all("later" not in e["path"] for e in log(site)), "a future record is not named in the log"
    assert all("later" not in p for p, _ in paths(site))
    assert [i["visible"] for i in site.list("notices")["items"]] == [False, True], "owner sees both"

    clock.set(2026, 11, 21, 0, 0)
    assert visible() == ["later"]
    assert ("collections/notices/now", "expired") in paths(site)
    assert ("collections/notices/later", "became visible") in paths(site)
    assert any(e.get("revealed", {}).get("path") == "collections/notices/later" for e in log(site))
    assert_chain_ok(site)


def test_hours_weekly_pattern_and_closures(site, clock):
    site.define("hours", kind="Hours")
    site.define("closures", kind="Closure", refs={"hours": "hours"})
    week = {d: [["08:00", "15:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
    site["hours"].add(id="school", name="School", weekly=week)

    def on(date):
        return site.hours(date)["results"][0]

    assert on("today")["weekday"] == "sat" and on("today")["open"] is False
    assert on("tomorrow")["date"] == "2026-11-15" and on("tomorrow")["open"] is False
    monday = on("2026-11-16")
    assert monday["open"] is True and monday["intervals"] == [["08:00", "15:00"]] and monday["source"] == "weekly"

    site["closures"].add(id="snow", hours="school", start_date="2026-11-16", end_date="2026-11-17", reason="Snow")
    assert on("2026-11-16")["open"] is False and on("2026-11-16")["reason"] == "Snow"
    assert on("2026-11-17")["open"] is False and on("2026-11-18")["open"] is True

    clock.advance(minutes=1)
    site["closures"].add(id="half", hours="school", start_date="2026-11-17", closed=False,
                         intervals=[["10:00", "12:00"]], reason="Late start")
    late = on("2026-11-17")
    assert late["open"] is True and late["intervals"] == [["10:00", "12:00"]], "the later closure wins"

    clock.set(2026, 11, 18, 17, 30)  # Wednesday 09:30 in Los Angeles
    assert on("today")["open_now"] is True
    clock.set(2026, 11, 19, 0, 30)   # Wednesday 16:30
    assert on("today")["open_now"] is False


def test_hours_input_is_checked(site):
    site.define("hours", kind="Hours")
    with pytest.raises(SiteError) as e:
        site["hours"].add(weekly={"monday": [["08:00", "15:00"]]})
    assert code(e) == "bad_hours" and "mon" in e.value.hint
    with pytest.raises(SiteError) as e:
        site["hours"].add(weekly={"mon": [["15:00", "08:00"]]})
    assert code(e) == "bad_hours"


def test_today_depends_on_the_sites_zone(tmp_path, clock):
    clock.set(2026, 11, 15, 6, 30)  # 22:30 on the 14th in Los Angeles, 07:30 on the 15th in Madrid
    answers = {}
    for name, zone in (("la", "America/Los_Angeles"), ("madrid", "Europe/Madrid")):
        s, _ = Site.create(tmp_path / name, name=name, timezone=zone, clock=clock)
        s.define("hours", kind="Hours")
        s["hours"].add(id="h", weekly={})
        answers[name] = s.hours("today")["results"][0]["date"]
    assert answers == {"la": "2026-11-14", "madrid": "2026-11-15"}


def test_datetimes_keep_local_time_and_zone(site):
    site.define("calendar", kind="Event", access="private")
    cal = site["calendar"]
    bridget = cal.add(title="Bridget", start="2026-11-23T14:00", timezone="America/Los_Angeles")
    assert bridget["data"]["start"] == "2026-11-23T14:00:00"
    assert bridget["instants"]["start"] == "2026-11-23T22:00:00Z"
    call = cal.add(title="Call", start="2026-11-23T18:00:00-05:00", timezone="America/New_York")
    assert call["data"]["start"] == "2026-11-23T18:00:00", "an offset input is stored as local time"
    summer = cal.add(title="Picnic", start="2026-07-04T12:00", timezone="America/Los_Angeles")
    assert summer["instants"]["start"] == "2026-07-04T19:00:00Z", "daylight time, which 'PST' would get wrong"

    week = cal.list({"start.gte": "2026-11-23", "start.lt": "2026-11-24"}, sort="start")["items"]
    assert [i["data"]["title"] for i in week] == ["Bridget", "Call"]
    assert [i["data"]["title"] for i in cal.list(sort="-start")["items"]][0] == "Call"

    with pytest.raises(SiteError) as e:
        cal.add(title="x", start="2026-11-23T14:00", timezone="PST")
    assert code(e) == "bad_timezone" and "America/Los_Angeles" in e.value.hint
    with pytest.raises(SiteError) as e:
        cal.add(title="x", start="2026-11-23T14:00", end="2026-11-23T13:00")
    assert "after start" in e.value.message


def test_confirm_records_still_true(games, clock):
    clock.advance(days=30)
    view = games["games"].confirm("G1")
    assert view["confirmed"] == "2026-12-14T18:00:00Z" and view["updated"] == "2026-11-14T18:00:00Z"
    assert log(games)[-1]["op"] == "confirm"
    assert games.read_record("games", "G1")["confirmed"] == "2026-12-14T18:00:00Z"


def test_changes_feed_cursor(games):
    first = games.changes()
    assert [c["what"] for c in first["changes"]] == ["defined", "added"]
    assert games.changes(since=first["cursor"])["changes"] == []
    games["games"].change("G1", stock=9)
    more = games.changes(since=first["cursor"])
    assert [(c["path"], c["what"]) for c in more["changes"]] == [("collections/games/G1", "live value changed")]
    page = games.changes(limit=1)
    assert page["more"] is True and len(page["changes"]) == 1
