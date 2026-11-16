"""One question across every site a host serves."""

import pytest
from fastapi.testclient import TestClient

from aisites import Site, host_ask
from aisites.host import Host
from aisites.platform import Platform
from examples import game_catalogue, pharmacy, school
from tests.test_questions import LinkFollower
from tests.test_oauth import World

HOST = "https://host.example"


@pytest.fixture
def street(tmp_path, clock):
    """A host with a pharmacy, a game shop and a school, on Monday 16 November at 11:00 in Madrid."""
    clock.set(2026, 11, 16, 10, 0)
    world = World(tmp_path, clock)
    for name, module in (("farmacia", pharmacy), ("games", game_catalogue), ("school", school)):
        module.build(world.folder / name, clock)[0].close()
    return world, TestClient(world.host, base_url=HOST)


def test_the_host_says_what_can_be_asked_across_it(street):
    world, http = street
    front = http.get("/").json()
    by_id = {q["id"]: q for q in front["ask"]}
    assert by_id["Offer"] == {"id": "Offer", "url": f"{HOST}/ask/Offer", "sites": 2,
                              "does": by_id["Offer"]["does"]} and "every site here" in by_id["Offer"]["does"]
    assert by_id["Hours"]["sites"] == 2 and by_id["Product"]["sites"] == 2 and "Closure" not in by_id
    assert "/ask/Offer?q=" in front["asking"]
    assert http.get("/ask").json()["ask"] == front["ask"]


def test_one_request_asks_every_site(street):
    world, http = street
    out = http.get("/ask/Offer?q=ibuprofen&stock.gt=0").json()
    assert out["question"] == "Offer" and out["asked"] == 2
    assert [s["site"] for s in out["sites"]] == ["farmacia"], "a site with nothing matching is left out"
    found = out["sites"][0]
    assert found["name"] == "Greenleaf Pharmacy" and found["url"] == f"{HOST}/s/farmacia/"
    item = found["answer"]["items"][0]
    assert item["key"] == "ibuprofen-200@norte" and item["data"]["stock"] == 32
    assert item["with"]["place"]["with"]["hours"][0]["open_now"] is True, "each site answers in full, as itself"
    assert "tag" not in found["answer"] and out["tag"] and out["as_of"]

    one = http.get("/ask/Product?key=GAME-1").json()
    assert {s["site"] for s in one["sites"]} == {"games"} and one["could_not_answer"] == 1
    hours = http.get("/ask/Hours?date=tomorrow").json()
    assert {s["site"] for s in hours["sites"]} == {"farmacia", "school"}
    assert hours["sites"][0]["answer"]["results"][0]["date"] == "2026-11-17"

    # A filter one site's records do not have is that site's to skip, not a failure of the whole question.
    regional = http.get("/ask/Offer?region=MX&product=GAME-1").json()
    assert [s["site"] for s in regional["sites"]] == ["games"] and "could_not_answer" not in regional
    odd = http.get("/ask/Offer?place.city=Madrid").json()
    assert [s["site"] for s in odd["sites"]] == ["farmacia"] and odd["could_not_answer"] == 1
    assert http.get("/ask/Slot?q=x").status_code == 404 and http.get("/ask/stock?q=x").status_code == 404
    assert http.get("/ask/Closure?q=x").status_code == 404


def test_level_1_walks_from_the_host_to_an_answer(street):
    world, http = street
    agent = LinkFollower(http, f"{HOST}/")
    front = agent.open(f"{HOST}/")
    offers = next(q for q in front["ask"] if q["id"] == "Offer")
    sites = agent.open(offers["url"])
    assert sites["choose"] == "site" and sites["total"] == 2
    assert [o["label"] for o in sites["options"]] == ["Greenleaf Pharmacy", "Game catalogue"] or len(sites["options"]) == 2
    pharmacy_link = next(o["url"] for o in sites["options"] if "farmacia" in o["url"])
    assert pharmacy_link == f"{HOST}/s/farmacia/ask/Offer"
    products = agent.open(pharmacy_link)
    answer = agent.open(products["options"][0]["url"])
    assert answer["items"][0]["with"]["place"]["data"]["name"].startswith("Greenleaf")
    assert agent.requests == 4, "the host, the sites, a site's options, the answer"


def test_lean_forms_work_across_a_host(street):
    world, http = street
    full = http.get("/ask/Offer?q=ibuprofen")
    lean = http.get("/ask/Offer?q=ibuprofen&format=table&links=0")
    assert len(lean.content) < len(full.content) * 0.6 and "http" not in lean.text
    assert lean.json()["sites"][0]["answer"]["rows"][0][0] == "ibuprofen-200@centro"
    assert http.get("/ask/Offer?links=0").json()["options"][0] == {"label": "Greenleaf Pharmacy", "site": "farmacia"}

    tag = full.json()["tag"]
    same = http.get(f"/ask/Offer?q=ibuprofen&unless={tag}").json()
    assert same == {"question": "Offer", "unchanged": True, "tag": tag, "as_of": same["as_of"]}
    assert http.get("/ask/Offer?q=ibuprofen", headers={"If-None-Match": full.headers["etag"]}).status_code in (200, 304)

    cut = http.get("/ask/Hours?date=tomorrow&max=400").json()
    assert cut["cut"] is True and len(cut["sites"]) == 1 and "cursor=1" in cut["next"]
    rest = http.get(cut["next"]).json()
    assert [s["site"] for s in rest["sites"]] == ["school"]
    assert http.get("/ask/Offer?q=x&max=lots").status_code == 400
    assert http.get("/ask/Offer?q=x&cursor=x").status_code == 400


def test_sites_are_asked_a_page_at_a_time(tmp_path, clock, monkeypatch):
    world = World(tmp_path, clock)
    for n in range(5):
        site, _ = Site.create(world.folder / f"shop{n}", name=f"Shop {n}", clock=clock)
        site.define("offers", kind="Offer")
        site["offers"].add(id="tea", product="tea", price=str(n + 1))
        site.close()
    monkeypatch.setattr(host_ask, "MAX_SITES", 2)
    http = TestClient(world.host, base_url=HOST)
    seen, url = [], "/ask/Offer?product=tea"
    while url:
        page = http.get(url).json()
        assert page["asked"] <= 2
        seen += [s["site"] for s in page["sites"]]
        url = page.get("next")
    assert seen == [f"shop{n}" for n in range(5)]
    assert http.get("/ask/Offer").json()["next"] == f"{HOST}/ask/Offer?cursor=2"


def test_what_is_left_out(street, clock):
    world, http = street
    # A site suspended by the host.
    world.sign_in()
    world.site("corner")                      # a notice board with a menu of products
    assert "corner" in {s["site"] for s in http.get("/ask/Product?q=sample").json()["sites"]}
    world.platform.suspend("corner")
    assert http.get("/ask/Product?q=sample").json()["sites"] == []

    # A site whose owner left it out.
    games = Site(world.folder / "games", clock)
    assert games.configure(listed="false")["listed"] is False
    games.close()
    fresh = TestClient(Host(world.folder, clock), base_url=HOST)
    assert fresh.get("/ask/Offer?product=GAME-1").json()["sites"] == []
    assert {q["id"]: q["sites"] for q in fresh.get("/ask").json()["ask"]}["Offer"] == 1
    assert "listed" not in fresh.get("/s/games/").json(), "not something a visitor needs to read"
    assert fresh.get("/s/games/ask/Offer?product=GAME-1").status_code == 200, "the site itself still answers"

    # Private and invite-only structures, and private fields.
    secret, key = Site.create(world.folder / "secret", name="Secret shop", clock=clock)
    secret.define("offers", kind="Offer", access="private")
    secret["offers"].add(id="x", product="ibuprofen", price="1")
    secret.define("deals", kind="Offer", access="invite")
    secret["deals"].add(id="y", product="ibuprofen", price="2")
    secret.define("products", kind="Product", fields=["cost:decimal:private"])
    secret["products"].add(sku="p1", name="Ibuprofen special", cost="0.10")
    secret.close()
    fresh = TestClient(Host(world.folder, clock), base_url=HOST)
    text = fresh.get("/ask/Offer?q=ibuprofen").text + fresh.get("/ask/Product?q=ibuprofen").text \
        + fresh.get("/find?q=ibuprofen").text + fresh.get("/ask/Offer").text
    assert "Secret shop" in text and "Ibuprofen special" in text, "its public products are found"
    assert "0.10" not in text and "cost" not in text and '"x"' not in text and '"y"' not in text
    assert fresh.get("/ask/Product?cost=0.10").json()["sites"] == []


def test_search_across_a_host(street):
    world, http = street
    found = http.get("/find?q=ibuprofen").json()
    assert [s["site"] for s in found["sites"]] == ["farmacia"] and found["total"] == 3
    assert found["sites"][0]["matches"][0]["collection"] == "products" and found["sites"][0]["url"] == f"{HOST}/s/farmacia/"
    assert http.get("/find?q=zzzz").json()["sites"] == [] and http.get("/find").status_code == 400


def test_a_host_can_switch_it_off(street):
    world, http = street
    world.platform.configure(ask_across_sites=False)
    fresh = TestClient(Host(world.folder, world.clock), base_url=HOST)
    assert "ask" not in fresh.get("/").json() and fresh.get("/ask").json()["ask"] == []
    assert fresh.get("/ask/Offer?q=x").status_code == 404 and fresh.get("/find?q=x").status_code == 404
    assert fresh.get("/s/farmacia/ask/Offer?q=ibuprofen").status_code == 200
    assert Platform
