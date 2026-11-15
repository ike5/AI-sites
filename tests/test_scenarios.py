"""The acceptance scenarios for Milestone 1.

Each visiting agent starts with nothing but the site's address: it reads the manifest and follows what it
finds there. Owners make changes through a door, never by touching the database.
"""

import pytest
from fastapi.testclient import TestClient

from aisites.host import Host
from examples import game_catalogue, hotdog_cart, personal_calendar, pharmacy, school
from tests.util import assert_chain_ok, auth, client, log, mcp_call


class Agent:
    """A visiting AI. It knows a base address and discovers the rest."""

    def __init__(self, http, base=""):
        self.http, self.base = http, base
        self.manifest = self.get(f"{base}/.well-known/aisite.json")
        self.map = self.get(f"{base}/map")   # for an agent that writes its own reads

    def get(self, path, **params):
        r = self.http.get(path, params=params)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"
        return r.json()

    def of_kind(self, kind):
        """Find a structure by what it means, not by what the site happened to call it."""
        return next(s for s in self.map["structures"] if s.get("kind") == kind)

    def records(self, kind, **params):
        return self.get(self.of_kind(kind)["href"], **params)["items"]


# ---------------------------------------------------------------- school

def test_school_is_it_open_tomorrow(tmp_path, clock):
    site, key = school.build(tmp_path / "school", clock)   # Saturday 14 November, 10:00 local
    http = client(site)
    agent = Agent(http)
    assert any(q.get("kind") == "Hours" for q in agent.manifest["ask"]), "the direct answer is advertised"

    def school_day(date):
        return next(r for r in agent.get("/hours", date=date)["results"] if r["key"] == "school")

    tomorrow = school_day("tomorrow")
    assert tomorrow["date"] == "2026-11-15" and tomorrow["weekday"] == "sun" and tomorrow["open"] is False
    monday = school_day("2026-11-16")
    assert monday["open"] is True and monday["intervals"] == [["08:00", "15:00"]]
    training = school_day("2026-11-17")
    assert training["open"] is False and training["reason"] == "Staff training day, no classes"
    office = next(r for r in agent.get("/hours", date="2026-11-17")["results"] if r["key"] == "office")
    assert office["open"] is True, "the closure applies to the school day, not the front office"

    # Sunday evening: snow. The office closes Monday through the admin door; agents see it at once.
    clock.set(2026, 11, 16, 3, 0)
    cursor = agent.get("/changes")["cursor"]
    r = http.post("/admin/closures/add", headers=auth(key),
                  json={"data": {"id": "snow", "start_date": "2026-11-16", "reason": "Snow day"}})
    assert r.status_code == 200
    assert school_day("tomorrow")["open"] is False and school_day("tomorrow")["reason"] == "Snow day"
    assert [c["path"] for c in agent.get("/changes", since=cursor)["changes"]] == ["collections/closures/snow"]
    assert_chain_ok(site)


# ---------------------------------------------------------------- hotdog cart

def test_hotdog_cart_notices_expire_and_owner_ai_updates(tmp_path, clock):
    site, key = hotdog_cart.build(tmp_path / "cart", clock)
    agent = Agent(client(site))
    assert agent.manifest["language"] == "es" and agent.manifest["currency"] == "MXN"
    assert agent.manifest["timezone"] == "America/Tijuana"

    notices = {n["key"]: n for n in agent.records("Notice")}
    assert set(notices) == {"ubicacion", "dos-por-uno"}
    assert notices["ubicacion"]["data"]["place"] == "quinta-cabrillo"
    assert notices["ubicacion"]["next_change"] == "2026-11-17T07:00:00Z", "the agent is told when this expires"
    place = agent.get(f"{agent.of_kind('Place')['href']}/quinta-cabrillo")
    assert place["data"]["name"] == "Esquina de Quinta y Cabrillo"
    assert notices["dos-por-uno"]["data"]["starts"] == "2026-11-15T18:00:00"
    assert notices["dos-por-uno"]["instants"]["starts"] == "2026-11-16T02:00:00Z"

    clock.advance(days=1, hours=14)   # after the promotion day ends
    assert [n["key"] for n in agent.records("Notice")] == ["ubicacion"]
    clock.advance(days=2)
    assert agent.records("Notice") == [], "old information removed itself"
    assert len(agent.records("Product")) == 2, "the menu is not dated, so it stays"

    # The owner tells their AI where the cart is next; the AI uses the MCP door.
    is_error, added = mcp_call(site, "add", {"target": "notices", "data": {
        "title": "Ahora en Plaza Río", "text": "Viernes y sábado.", "valid_until": "2026-11-22T23:00"}})
    assert not is_error and added["visible"] is True
    assert [n["data"]["title"] for n in agent.records("Notice")] == ["Ahora en Plaza Río"]
    is_error, problem = mcp_call(site, "add", {"target": "notices", "data": {"text": "no title"}})
    assert is_error and "title" in problem["error"]["message"], "mistakes come back with what to fix"


# ---------------------------------------------------------------- game catalogue

def test_game_catalogue_regional_prices_and_one_day_discount(tmp_path, clock):
    site, key = game_catalogue.build(tmp_path / "games", clock)   # sale day is 19 November, New York time
    agent = Agent(client(site))

    mx = {o["data"]["product"]: o["data"] for o in agent.records("Offer", region="MX")}
    assert len(mx) == 10 and mx["GAME-1"]["price"] == "1019.83" and mx["GAME-1"]["price_currency"] == "MXN"
    table = agent.get(agent.of_kind("Offer")["href"], region="JP", format="table", fields="product,price")
    assert table["columns"] == ["key", "product", "price"] and table["rows"][0][1:] == ["GAME-1", "8998"]

    def price(sku):
        return agent.get(f"{agent.of_kind('Product')['href']}/{sku}")

    # Before the day: nothing about the discount is visible anywhere.
    assert price("GAME-3")["data"]["price"] == "59.99" and "next_change" not in price("GAME-3")
    everything = str([agent.get("/history", limit=1000), agent.get("/changes"), agent.records("Product")])
    assert "47.99" not in everything and "31.99" not in everything
    assert "wholesale_cost" not in everything, "private fields never leave the site"

    # On the day: exactly two of ten titles are cheaper, and the record says until when.
    clock.set(2026, 11, 19, 15, 0)
    on_sale = [p for p in agent.records("Product") if "active_changes" in p]
    assert sorted(p["key"] for p in on_sale) == ["GAME-3", "GAME-7"]
    assert price("GAME-3")["data"]["price"] == "47.99" and price("GAME-7")["data"]["price"] == "31.99"
    assert price("GAME-3")["next_change"] == "2026-11-20T05:00:00Z"
    assert [p["key"] for p in agent.records("Product", **{"price.lt": "35"})] == ["GAME-7"]
    history = agent.get(price("GAME-3")["history"])["entries"]
    assert history[-1]["revealed"]["values"] == {"price": "47.99"}, "now provable that it was planned in advance"
    assert history[-1]["time"] == "2026-11-14T18:00:00Z"

    # The day after: back to normal, with no one having had to remember to undo it.
    clock.set(2026, 11, 20, 15, 0)
    assert price("GAME-3")["data"]["price"] == "59.99" and len(agent.records("Product")) == 10
    assert_chain_ok(site)


def test_agent_compares_prices_across_two_sites_on_one_server(tmp_path, clock):
    a, key_a = game_catalogue.build(tmp_path / "northwind", clock)
    b, key_b = game_catalogue.build(tmp_path / "southwind", clock)
    b["games"].change("GAME-2", price="54.99")
    b.close(), a.close()
    http = TestClient(Host(tmp_path, clock))

    quotes = {}
    for name in ("northwind", "southwind"):
        agent = Agent(http, f"/s/{name}")
        product = agent.of_kind("Product")
        schema = agent.get(product["schema"])
        assert schema["properties"]["price"]["x-property"] == "schema:price", "same meaning on both sites"
        quotes[name] = agent.get(f"{product['href']}/GAME-2")["data"]["price"]
    assert quotes == {"northwind": "59.99", "southwind": "54.99"}
    assert min(quotes, key=lambda n: float(quotes[n])) == "southwind"


# ---------------------------------------------------------------- pharmacy

def test_pharmacy_stock_per_branch(tmp_path, clock):
    site, key = pharmacy.build(tmp_path / "pharmacy", clock)
    http = client(site)
    agent = Agent(http)

    in_stock = agent.records("Offer", product="ibuprofen-200", **{"stock.gt": "0"})
    assert [o["data"]["place"] for o in in_stock] == ["norte"] and in_stock[0]["data"]["stock"] == 32
    assert in_stock[0]["live_as_of"] == "2026-11-14T18:00:00Z", "the agent knows how fresh the count is"
    branch = agent.get(f"{agent.of_kind('Place')['href']}/norte")["data"]
    assert branch["address"] == "Av. de Burgos 80"
    monday = next(r for r in agent.get("/hours", date="2026-11-16")["results"] if r["place"] == "norte")
    assert monday["intervals"] == [["09:30", "14:00"], ["17:00", "20:30"]] and monday["timezone"] == "Europe/Madrid"

    before = len(log(site))
    clock.advance(minutes=20)
    r = http.post("/admin/stock/change", headers=auth(key),
                  json={"key": "ibuprofen-200@centro", "set": {"stock": 6}})
    assert r.status_code == 200
    both = agent.records("Offer", product="ibuprofen-200", **{"stock.gt": "0"})
    assert sorted(o["data"]["place"] for o in both) == ["centro", "norte"]
    assert len(log(site)) == before, "stock levels are live values and stay out of the public history"

    r = http.post("/admin/stock/add", headers=auth(key),
                  json={"data": {"product": "ibuprofen-200", "place": "sur", "stock": 1}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_choice"


# ---------------------------------------------------------------- personal calendar

def test_private_calendar_through_the_owners_ai(tmp_path, clock):
    site, key = personal_calendar.build(tmp_path / "me", clock)   # the site's zone is New York
    http = client(site)

    stranger = Agent(http)
    assert stranger.manifest["ask"] == [] and stranger.map["structures"] == []
    assert http.get("/collections/calendar").status_code == 404
    assert http.post("/mcp/", json={}).status_code == 401

    # "I have an appointment with Bridget on the 23rd at 2 PM Pacific."
    _, bridget = mcp_call(site, "add", {"target": "calendar", "data": {
        "title": "Bridget", "start": "2026-11-23T14:00", "timezone": "America/Los_Angeles", "with": ["Bridget"]}})
    assert bridget["instants"]["start"] == "2026-11-23T22:00:00Z"
    mcp_call(site, "add", {"target": "calendar", "data": {"title": "Dentist", "start": "2026-11-18T09:30"}})
    mcp_call(site, "add", {"target": "calendar", "data": {"title": "Next month", "start": "2026-12-20T10:00"}})
    is_error, wrong = mcp_call(site, "add", {"target": "calendar", "data": {
        "title": "Lunch", "start": "2026-11-19T12:00", "timezone": "PST"}})
    assert is_error and "America/Los_Angeles" in wrong["error"]["hint"]

    # "What do I have in the next week or so?"
    _, week = mcp_call(site, "list", {"target": "calendar", "sort": "start",
                                      "where": {"start.gte": "today", "start.lt": "2026-11-24"}})
    assert [(e["data"]["title"], e["data"]["start"]) for e in week["items"]] == [
        ("Dentist", "2026-11-18T09:30:00"), ("Bridget", "2026-11-23T14:00:00")]

    owner = http.get("/collections/calendar", params={"start.gte": "2026-11-23", "sort": "start"}, headers=auth(key)).json()
    assert [e["data"]["title"] for e in owner["items"]] == ["Bridget", "Next month"]

    assert [e["path"] for e in log(site)] == ["site"], "nothing private reaches the public history"
    assert http.get("/changes").json()["changes"] == []
