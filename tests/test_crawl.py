"""Crawling: how a search engine finds a site, reads all of it and keeps its copy fresh, with no web pages."""

import json

import pytest
from fastapi.testclient import TestClient

from aisites import Site, crawl, ops
from aisites.cli import main
from aisites.errors import SiteError
from aisites.host import Host
from examples import game_catalogue, pharmacy, school
from tests.test_oauth import World
from tests.util import auth, client, mcp_call

BASE = "http://testserver"
HOST = "https://host.example"


@pytest.fixture
def chemist(tmp_path, clock):
    site, key = pharmacy.build(tmp_path / "pharmacy", clock)
    yield site, key, client(site)
    site.close()


def read_all(http, start="/crawl"):
    """Read a feed the way a crawler would: follow `next` until there is none. Returns (records, pages)."""
    records, pages, url = [], [], start
    while url:
        page = http.get(url).json()
        pages.append(page)
        records += page["records"]
        url = page.get("next")
    return records, pages


# ---------------------------------------------------------------- finding a site

def test_the_first_response_says_what_the_site_may_be_used_for(chemist):
    site, _, http = chemist
    front = http.get("/").json()
    assert front["use"] == {"search": True, "training": True} and front["more"]["crawl"] == f"{BASE}/crawl"
    assert "search" not in {k for k in front if k != "use"} and "training" not in front


def test_robots_and_sitemap_for_todays_crawlers(chemist):
    site, _, http = chemist
    r = http.get("/robots.txt")
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert f"# Start at {BASE}/.well-known/aisite.json" in text and "/crawl" in text
    assert text.rstrip().endswith(f"Sitemap: {BASE}/sitemap.xml")
    assert "User-agent: *\nAllow: /" in text and "Disallow" not in text, "nothing is closed by default"

    s = http.get("/sitemap.xml")
    assert s.headers["content-type"] == "application/xml" and s.text.startswith("<?xml")
    assert [line.split("<loc>")[1].split("</loc>")[0] for line in s.text.splitlines() if "<loc>" in line] == [
        f"{BASE}/.well-known/aisite.json", f"{BASE}/llms.txt", f"{BASE}/map", f"{BASE}/crawl"]
    assert s.text.count(f"<lastmod>{site.now_z()}</lastmod>") == 4


def test_the_owner_states_what_is_allowed(chemist):
    site, key, http = chemist
    assert site.configure(training="false")["training"] is False
    text = http.get("/robots.txt").text
    block = text.split("User-agent: *")[0]
    for name in crawl.TRAINING_CRAWLERS:
        assert f"User-agent: {name}" in block
    assert "Disallow: /" in block and "OAI-SearchBot" not in block, "search crawlers are still welcome"
    assert "User-agent: *\nAllow: /" in text, "and so is an assistant fetching for a person"
    assert http.get("/").json()["use"] == {"search": True, "training": False}
    assert http.get("/crawl").json()["use"]["training"] is False, "the feed says so too"
    assert "x-robots-tag" not in http.get("/crawl").headers

    site.configure(search="false", training="true")
    text = http.get("/robots.txt").text
    assert "User-agent: Googlebot" in text.split("User-agent: *")[0] and "User-agent: GPTBot" not in text
    assert http.get("/crawl").status_code == 200, "training alone still reads the feed"
    for path in ("/", "/crawl", "/collections/products", "/collections/products/ibuprofen-200", "/map"):
        assert http.get(path).headers["x-robots-tag"] == "noindex", "search off: nothing asks to be indexed"

    site.configure(training="false")
    refused = http.get("/crawl")
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "crawl_off"
    assert "crawl" not in http.get("/").json()["more"]
    assert http.get("/ask/stock?q=ibuprofen").status_code == 200, "a person's assistant is never turned away"
    with pytest.raises(SiteError):
        site.configure(search="sometimes")
    assert main(["configure", "search=true", "-s", str(site.store.path).rsplit("/", 1)[0]]) == 0


def test_variants_ask_not_to_be_indexed_and_stay_open(chemist):
    _, _, http = chemist
    for path in ("/ask/stock", "/ask/stock?q=ibuprofen", "/find?q=ibuprofen", "/changes", "/history",
                 "/collections/products?q=ibuprofen", "/crawl?since=0", "/hours?date=tomorrow"):
        r = http.get(path)
        assert r.status_code == 200 and r.headers["x-robots-tag"] == "noindex", path
    for path in ("/", "/.well-known/aisite.json", "/crawl", "/map", "/collections/products",
                 "/collections/products/ibuprofen-200", "/llms.txt", "/hours"):
        assert "x-robots-tag" not in http.get(path).headers, path
    assert "/ask" not in http.get("/robots.txt").text, "never closed in robots.txt, or assistants would be shut out"


# ---------------------------------------------------------------- reading all of a site

def test_the_feed_is_everything_a_visitor_could_read(chemist):
    site, key, http = chemist
    feed = http.get("/crawl").json()
    assert feed["site"] == {"name": "Greenleaf Pharmacy", "description": "Two branches in Madrid.",
                            "timezone": "Europe/Madrid", "language": "es", "currency": "EUR", "url": BASE,
                            "public_key": site.public_key}
    assert feed["format"] == 1 and feed["how"].startswith("Read this feed once") and "next" not in feed

    by_name = {s["name"]: s for s in feed["structures"]}
    assert set(by_name) == {"places", "hours", "products", "stock"}
    stock = by_name["stock"]
    assert stock["kind"] == "Offer" and stock["means"] == "schema:Offer"
    assert stock["fields"]["price"] == "schema:price" and stock["fields"]["stock"] == "schema:inventoryLevel"
    assert stock["refs"] == {"product": "products", "place": "places"}, "meanings and references, stated once"

    records = feed["records"]
    assert len(records) == 2 + 2 + 2 + 4
    assert [r["in"] for r in records] == sorted(r["in"] for r in records), "in a stable order"
    one = next(r for r in records if r["key"] == "ibuprofen-200@norte")
    assert one["data"]["stock"] == 32 and one["live_as_of"] and one["updated"], "with the times that say how fresh it is"
    assert feed["record_address"] == BASE + "/collections/{in}/{key}" and "url" not in one, "said once, not per record"
    address = feed["record_address"].replace("{in}", one["in"]).replace("{key}", one["key"])
    assert http.get(address).json()["data"] == one["data"], "each record has an address of its own to cite"
    hours = next(r for r in records if r["in"] == "hours" and r["key"] == "norte")
    assert hours["data"]["weekly"]["mon"] == [["09:30", "14:00"], ["17:00", "20:30"]] and "open_now" not in str(hours), \
        "rules, not the answer for one moment"


def test_the_feed_holds_nothing_a_visitor_cannot_see(tmp_path, clock):
    site, key = Site.create(tmp_path / "s", name="Shop", clock=clock)
    site.define("products", kind="Product", fields=["cost:decimal:private"])
    site.define("suppliers", access="private", fields=["name"])
    site.define("members", access="invite", fields=["name"])
    site.define("contact", type="form")
    site.define("notices", kind="Notice")
    site["products"].add(sku="tea", name="Tea", price="3.00", cost="1.10")
    site["suppliers"].add(id="acme", name="Acme Wholesale")
    site["members"].add(id="m1", name="Ana")
    site["notices"].add(id="old", title="Last month", valid_until="2026-10-31T00:00")
    site["notices"].add(id="soon", title="Not yet announced", valid_from="2026-12-01T00:00")
    site["notices"].add(id="now", title="Open late this week")
    site["products"].schedule("tea", price="2.00", on="2026-12-15")   # a price change not announced
    http = client(site)
    text = http.get("/crawl").text
    feed = json.loads(text)
    assert {s["name"] for s in feed["structures"]} == {"products", "notices"}
    assert {(r["in"], r["key"]) for r in feed["records"]} == {("products", "tea"), ("notices", "now")}
    for hidden in ("cost", "1.10", "Acme", "suppliers", "members", "Ana", "Last month", "Not yet announced",
                   "2.00", "contact"):
        assert hidden not in text, hidden
    assert http.get("/crawl", headers=auth(key)).json()["records"] == feed["records"], \
        "the feed is what the public sees, whoever asks"


def test_a_large_site_is_read_in_bounded_pages(tmp_path, clock):
    site, _ = Site.create(tmp_path / "s", name="Big", clock=clock)
    site.define("items", fields=["name", "text:text"])
    site.define("zebras", fields=["name"])
    for n in range(300):
        site["items"].add(id=f"i{n:03}", name=f"Item {n}", text="x" * 200)
    site["zebras"].add(id="z1", name="Last of all")
    http = client(site)
    records, pages = read_all(http, "/crawl?max=20000")
    assert len(records) == 301 and len({(r["in"], r["key"]) for r in records}) == 301, "every record exactly once"
    assert len(pages) > 3 and all(len(json.dumps(p)) <= 21000 for p in pages)
    assert "structures" in pages[0] and "structures" not in pages[1], "what the fields mean is said once"
    assert {p["cursor"] for p in pages} == {pages[0]["cursor"]}, "one cursor for the whole read"
    assert len(read_all(http)[1]) == 1, "the usual page holds far more"
    assert http.get("/crawl?max=lots").status_code == 400 and http.get("/crawl?since=x").status_code == 400


def test_trees_and_the_signed_position_come_with_the_feed(tmp_path, clock):
    site, _ = game_catalogue.build(tmp_path / "games", clock)
    http = client(site)
    feed = http.get("/crawl").json()
    assert feed["trees"][0]["name"] == "genres" and feed["trees"][0]["nodes"]
    assert next(s for s in feed["structures"] if s["name"] == "genres")["type"] == "tree"
    signed = http.get(feed["head"]["signed"]).json()["checkpoint"]
    assert (feed["head"]["seq"], feed["head"]["hash"]) == (signed["seq"], signed["hash"]), \
        "the copy is tied to a signed point in the site's history"
    assert next(s for s in feed["structures"] if s["name"] == "games")["refs"] == {"category": "genres"}


# ---------------------------------------------------------------- staying fresh

def test_since_gives_only_what_changed(chemist, clock):
    site, _, http = chemist
    cursor = http.get("/crawl").json()["cursor"]
    quiet = http.get(f"/crawl?since={cursor}")
    assert quiet.json()["records"] == [] and quiet.json()["cursor"] == cursor and "removed" not in quiet.json()
    assert len(quiet.content) < 400, "nothing changed is a short answer"

    clock.advance(minutes=5)
    site["stock"].change("ibuprofen-200@norte", stock=30)
    site["products"].add(sku="plasters", name="Tiritas", price="2.10", price_currency="EUR")
    site["stock"].remove("saline-spray@centro")
    delta = http.get(f"/crawl?since={cursor}").json()
    assert {(r["in"], r["key"]): r["data"].get("stock") for r in delta["records"]} == {
        ("stock", "ibuprofen-200@norte"): 30, ("products", "plasters"): None}
    assert delta["removed"] == [{"in": "stock", "key": "saline-spray@centro"}]
    assert delta["cursor"] > cursor and "structures" not in delta
    assert http.get(f"/crawl?since={delta['cursor']}").json()["records"] == []

    # A copy kept this way ends up equal to a fresh read.
    copy = {(r["in"], r["key"]): r for r in http.get("/crawl?since=0").json()["records"]}
    assert copy == {(r["in"], r["key"]): r for r in http.get("/crawl").json()["records"]}


def test_time_and_new_definitions_reach_a_crawler_too(tmp_path, clock):
    site, _ = school.build(tmp_path / "school", clock)
    http = client(site)
    site["notices"].add(id="fair", title="Book fair on Friday", valid_until="2026-11-21T00:00")
    site["notices"].add(id="trip", title="Museum trip", valid_from="2026-11-18T00:00")
    cursor = http.get("/crawl").json()["cursor"]
    assert "fair" in http.get("/crawl").text and "Museum trip" not in http.get("/crawl").text

    clock.set(2026, 11, 22, 12, 0)   # nothing was written; one notice expired and one began
    delta = http.get(f"/crawl?since={cursor}").json()
    assert {"in": "notices", "key": "fair"} in delta["removed"]
    assert any(r["key"] == "trip" for r in delta["records"])

    cursor = delta["cursor"]
    site.define("clubs", fields=["name"])
    site["clubs"].add(id="chess", name="Chess club")
    site.drop("pages")
    delta = http.get(f"/crawl?since={cursor}").json()
    names = {s["name"] for s in delta["structures"]}
    assert "clubs" in names and "pages" not in names, "a structure no longer listed is gone, with its records"
    assert any(r["in"] == "clubs" for r in delta["records"])

    site.configure(description="A primary school")
    assert http.get(f"/crawl?since={delta['cursor']}").json()["site"]["description"] == "A primary school"


def test_changes_during_a_paged_read_are_caught(tmp_path, clock):
    site, _ = Site.create(tmp_path / "s", name="Busy", clock=clock)
    site.define("items", fields=["name", "text:text"])
    for n in range(60):
        site["items"].add(id=f"i{n:02}", name=f"Item {n}", text="x" * 300)
    http = client(site)
    first = http.get("/crawl?max=8000").json()
    site["items"].change("i00", name="Changed after it was read")
    site["items"].add(id="i99", name="Added mid-read")
    records = first["records"]
    url = first.get("next")
    while url:
        page = http.get(url).json()
        records += page["records"]
        url = page.get("next")
    copy = {r["key"]: r["data"]["name"] for r in records}
    assert copy["i00"] == "Item 0", "the first page was read before the change"
    for r in http.get(f"/crawl?since={first['cursor']}").json()["records"]:
        copy[r["key"]] = r["data"]["name"]
    assert copy["i00"] == "Changed after it was read" and copy["i99"] == "Added mid-read"
    assert copy == {r["key"]: r["data"]["name"] for r in read_all(http)[0]}

    big = http.get("/crawl?since=0&max=5000").json()
    assert "next" in big and big["cursor"] < first["cursor"] + 2, "a long list of changes is paged too"
    seen, url = {r["key"] for r in big["records"]}, big["next"]
    while url:
        page = http.get(url).json()
        seen |= {r["key"] for r in page["records"]}
        url = page.get("next")
    assert len(seen) == 61


def test_revalidating_and_the_other_doors(chemist, capsys):
    site, key, http = chemist
    r = http.get("/crawl")
    assert http.get("/crawl", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    by_ops = ops.run(site, ops.LOCAL, "crawl", {})
    by_admin = http.post("/admin/crawl", json={}, headers=auth(key)).json()
    _, by_mcp = mcp_call(site, "other", {"name": "crawl"})
    assert main(["crawl", "-s", str(site.store.path).rsplit("/", 1)[0]]) == 0
    by_cli = json.loads(capsys.readouterr().out)
    strip = lambda f: [(x["in"], x["key"], x["data"]) for x in f["records"]]   # noqa: E731
    assert strip(r.json()) == strip(by_ops) == strip(by_admin) == strip(by_mcp) == strip(by_cli)
    assert ops.run(site, ops.LOCAL, "crawl", {"since": by_ops["cursor"]})["records"] == []


# ---------------------------------------------------------------- a host

@pytest.fixture
def street(tmp_path, clock):
    world = World(tmp_path, clock)
    for name, module in (("farmacia", pharmacy), ("games", game_catalogue), ("school", school)):
        module.build(world.folder / name, clock)[0].close()
    return world, TestClient(world.host, base_url=HOST)


def test_a_host_lists_its_sites_for_crawlers(street):
    world, http = street
    assert http.get("/").json()["sites_directory"] == f"{HOST}/.well-known/aisites.json"
    listed = http.get("/.well-known/aisites.json").json()
    assert listed["total"] == 3 and [s["site"] for s in listed["sites"]] == ["farmacia", "games", "school"]
    farmacia = listed["sites"][0]
    assert farmacia == {"site": "farmacia", "name": "Greenleaf Pharmacy", "description": "Two branches in Madrid.",
                        "language": "es", "url": f"{HOST}/s/farmacia/", "crawl": f"{HOST}/s/farmacia/crawl",
                        "kinds": ["Hours", "Offer", "Place", "Product"], "records": 10,
                        "updated": farmacia["updated"], "use": {"search": True, "training": True}}
    feed = http.get(farmacia["crawl"]).json()
    assert len(feed["records"]) == 10 and feed["site"]["url"] == f"{HOST}/s/farmacia"
    assert feed["record_address"] == HOST + "/s/farmacia/collections/{in}/{key}"

    robots = http.get("/robots.txt").text
    assert f"# Start at {HOST}/.well-known/aisites.json" in robots and "Disallow" not in robots
    sitemap = http.get("/sitemap.xml").text
    assert sitemap.count("<url>") == 12 and f"<loc>{HOST}/s/games/crawl</loc>" in sitemap


def test_a_host_speaks_for_each_sites_wishes_and_leaves_some_out(street, clock):
    world, http = street
    for name, changes in (("games", {"training": "false"}), ("school", {"search": "false"}),
                          ("farmacia", {"listed": "false"})):
        site = Site(world.folder / name, clock)
        site.configure(**changes)
        site.close()
    world.sign_in()
    world.site("corner")
    world.platform.suspend("corner")
    http = TestClient(Host(world.folder, clock), base_url=HOST)

    robots = http.get("/robots.txt").text
    training, search = robots.split("\n\n")[1], robots.split("\n\n")[2]
    assert "User-agent: GPTBot" in training and "Disallow: /s/games/" in training and "/s/school/" not in training
    assert "User-agent: Googlebot" in search and "Disallow: /s/school/" in search and "/s/games/" not in search
    assert "corner" not in robots and "User-agent: *\nAllow: /" in robots

    listed = http.get("/.well-known/aisites.json").json()
    assert [s["site"] for s in listed["sites"]] == ["games"], "unlisted, search-off and suspended sites are left out"
    assert listed["sites"][0]["use"] == {"search": True, "training": False}
    sitemap = http.get("/sitemap.xml").text
    assert sitemap.count("<url>") == 4 and "farmacia" not in sitemap and "school" not in sitemap
    assert http.get("/s/farmacia/crawl").status_code == 200, "left out of the list, still readable itself"


def test_the_directory_is_paged(tmp_path, clock, monkeypatch):
    from aisites import host_ask
    world = World(tmp_path, clock)
    for n in range(5):
        Site.create(world.folder / f"shop{n}", name=f"Shop {n}", clock=clock)[0].close()
    monkeypatch.setattr(host_ask, "DIRECTORY_PAGE", 2)
    http = TestClient(world.host, base_url=HOST)
    seen, url = [], "/.well-known/aisites.json"
    while url:
        page = http.get(url).json()
        seen += [s["site"] for s in page["sites"]]
        url = page.get("next")
    assert seen == [f"shop{n}" for n in range(5)] and page["total"] == 5
    assert http.get("/.well-known/aisites.json?cursor=x").status_code == 400
