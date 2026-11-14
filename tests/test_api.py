import pytest

from tests.util import auth, client


@pytest.fixture
def web(made):
    site, key = made
    site.define("genres", type="tree")
    site["genres"].add({"id": "action", "label": "Action"})
    site.define("games", kind="Product", fields=["cost:decimal:private"], refs={"category": "genres"})
    for i, price in enumerate(["59.99", "39.99", "19.99", "9.99"], start=1):
        site["games"].add(sku=f"G{i}", name=f"Game {i}", price=price, stock=i, cost="1", category="action")
    site.define("calendar", kind="Event", access="private")
    site["calendar"].add(id="e1", title="Dentist", start="2026-11-20T09:00")
    site.define("hours", kind="Hours")
    site["hours"].add(id="shop", weekly={"sun": [["10:00", "16:00"]]})
    return site, key, client(site)


def test_manifest_is_the_entry_point(web):
    site, key, c = web
    r = c.get("/.well-known/aisite.json")
    m = r.json()
    assert r.headers["link"] == '</.well-known/aisite.json>; rel="describedby"'
    assert c.get("/").json()["structures"] == m["structures"]
    by_name = {s["name"]: s for s in m["structures"]}
    assert set(by_name) == {"genres", "games", "hours"}, "private structures are not listed"
    assert by_name["games"] == {"name": "games", "type": "collection", "access": "public",
                                "href": "/collections/games", "kind": "Product", "semantic": "schema:Product",
                                "description": by_name["games"]["description"],
                                "schema": "/collections/games/schema"}
    assert m["timezone"] == "America/Los_Angeles" and m["currency"] == "USD"
    assert m["first_published"] and m["public_key"] and "open_on_date" in m["answers"]
    assert "calendar" in {s["name"] for s in c.get("/", headers=auth(key)).json()["structures"]}


def test_filters_sorting_paging_and_projection(web):
    _, _, c = web
    assert [i["key"] for i in c.get("/collections/games?price.lt=40").json()["items"]] == ["G2", "G3", "G4"]
    assert [i["key"] for i in c.get("/collections/games?price.gte=19.99&price.lte=39.99").json()["items"]] == \
        ["G2", "G3"]
    assert [i["key"] for i in c.get("/collections/games?sku=G3").json()["items"]] == ["G3"]
    assert [i["key"] for i in c.get("/collections/games?sku.ne=G3&sort=-price").json()["items"]] == \
        ["G1", "G2", "G4"]
    assert [i["key"] for i in c.get("/collections/games?stock.gt=2").json()["items"]] == ["G3", "G4"]

    page1 = c.get("/collections/games?limit=3&sort=price").json()
    assert [i["key"] for i in page1["items"]] == ["G4", "G3", "G2"] and page1["total"] == 4
    page2 = c.get(f"/collections/games?limit=3&sort=price&cursor={page1['next']}").json()
    assert [i["key"] for i in page2["items"]] == ["G1"] and "next" not in page2

    slim = c.get("/collections/games?fields=name,price&limit=1").json()["items"][0]
    assert slim["data"] == {"name": "Game 1", "price": "59.99"}

    table = c.get("/collections/games?format=table&fields=sku,price&price.lt=20").json()
    assert table["columns"] == ["sku", "price"] and table["rows"] == [["G3", "19.99"], ["G4", "9.99"]]


def test_bad_requests_explain_themselves(web):
    _, _, c = web
    r = c.get("/collections/games?colour=red")
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_filter"
    assert "price" in r.json()["error"]["hint"]
    assert c.get("/collections/games?price.near=5").json()["error"]["code"] == "bad_filter"
    assert c.get("/collections/games?price.lt=cheap").json()["error"]["code"] == "bad_filter"
    assert c.get("/collections/games?cost.gt=0").json()["error"]["code"] == "bad_filter"
    r = c.get("/collections/nothing")
    assert r.status_code == 404 and "aisite.json" in r.json()["error"]["hint"]
    assert c.get("/collections/games/NOPE").status_code == 404
    r = c.get("/no/such/route")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert c.get("/history/abc").status_code == 400
    assert c.get("/trees/games").json()["error"]["code"] == "wrong_structure"


def test_private_data_needs_the_owner_key(web):
    _, key, c = web
    assert c.get("/collections/calendar").status_code == 404, "a private structure looks absent"
    assert c.get("/collections/calendar/e1").status_code == 404
    assert c.get("/collections/calendar", headers=auth(key)).json()["items"][0]["data"]["title"] == "Dentist"
    r = c.get("/collections/games", headers=auth("ais_owner_wrong"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_key"

    public = c.get("/collections/games/G1").json()
    assert "cost" not in public["data"] and public["history"] == "/collections/games/G1/history"
    assert c.get("/collections/games/G1", headers=auth(key)).json()["data"]["cost"] == "1"
    assert "cost" not in c.get("/collections/games/schema").json()["properties"]
    assert "cost" in c.get("/collections/games/schema", headers=auth(key)).json()["properties"]


def test_schema_carries_meaning(web):
    _, _, c = web
    s = c.get("/collections/games/schema").json()
    assert s["x-type"] == "schema:Product" and s["x-kind"] == "Product" and s["x-key"] == "sku"
    assert s["properties"]["price"] == {"type": "string", "format": "decimal", "x-history": "logged",
                                        "x-property": "schema:price"}
    assert s["properties"]["stock"]["x-history"] == "live" and s["properties"]["stock"]["x-capacity"] is True
    assert s["properties"]["category"]["x-choices"] == "/trees/genres"
    assert s["required"] == ["name"]


def test_etags_revalidate_cheaply_and_follow_time(web, clock):
    site, _, c = web
    r = c.get("/collections/games/G1")
    tag = r.headers["etag"]
    assert r.headers["cache-control"] == "no-cache"
    assert c.get("/collections/games/G1", headers={"If-None-Match": tag}).status_code == 304

    head = c.head("/collections/games/G1")
    assert head.status_code == 200 and head.headers["etag"] == tag and head.content == b""

    site["games"].change("G1", stock=99)
    assert c.get("/collections/games/G1", headers={"If-None-Match": tag}).status_code == 200

    site["games"].schedule("G1", price="1.00", on="2026-12-15")
    tag = c.get("/collections/games/G1").headers["etag"]
    clock.set(2026, 12, 15, 12, 0)
    r = c.get("/collections/games/G1", headers={"If-None-Match": tag})
    assert r.status_code == 200 and r.json()["data"]["price"] == "1.00", "a change starting moves the ETag"

    tag = c.get("/hours?date=today").headers["etag"]
    clock.advance(days=1)
    assert c.get("/hours?date=today", headers={"If-None-Match": tag}).status_code == 200, "a new day, a new answer"


def test_html_only_when_asked(web):
    _, _, c = web
    assert c.get("/collections/games").headers["content-type"] == "application/json"
    page = c.get("/collections/games", headers={"Accept": "text/html,*/*"})
    assert page.headers["content-type"].startswith("text/html") and "<table>" in page.text and "Game 1" in page.text
    assert c.get("/collections/games?format=json", headers={"Accept": "text/html"}).headers["content-type"] == \
        "application/json"
    assert "<ul>" in c.get("/trees/genres?format=html").text


def test_overview_and_openapi(web):
    _, key, c = web
    text = c.get("/llms.txt").text
    assert text.startswith("# Test site") and "[games](/collections/games)" in text and "calendar" not in text
    doc = c.get("/openapi.json").json()
    assert "/collections/games" in doc["paths"] and "/trees/genres" in doc["paths"] and "/hours" in doc["paths"]
    assert "/collections/calendar" not in doc["paths"]
    assert doc["components"]["schemas"]["games"]["x-type"] == "schema:Product"
    assert "/collections/calendar" in c.get("/openapi.json", headers=auth(key)).json()["paths"]


def test_tree_hours_changes_history_routes(web):
    _, _, c = web
    assert c.get("/trees/genres").json()["nodes"] == [{"id": "action", "label": "Action"}]
    assert c.get("/hours?date=tomorrow").json()["results"][0]["open"] is True
    assert c.get("/hours/shop?date=2026-11-16").json()["results"][0]["open"] is False
    assert c.get("/hours/nope").status_code == 404

    feed = c.get("/changes").json()
    assert feed["changes"][0]["path"] == "schemas/genres"
    assert c.get(f"/changes?since={feed['cursor']}").json()["changes"] == []
    assert all("calendar" not in ch["path"] for ch in feed["changes"])

    h = c.get("/history?limit=2").json()
    assert [e["seq"] for e in h["entries"]] == [1, 2] and h["next_after"] == 2 and h["head"]["seq"] > 2
    assert c.get("/history/1").json()["op"] == "genesis"
    rec = c.get("/collections/games/G2/history").json()["entries"]
    assert [e["values"]["price"] for e in rec] == ["39.99"]
    assert c.get("/history?path=collections/games&since=2030-01-01").json()["entries"] == []


def test_admin_door(web):
    site, key, c = web
    assert {o["op"] for o in c.get("/admin/ops").json()["operations"]} >= {"define", "add", "schedule"}
    r = c.post("/admin/games/change", json={"key": "G1", "set": {"price": "1"}})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert c.post("/admin/games/change", json={"key": "G1", "set": {"price": "1"}},
                  headers=auth("nope")).status_code == 401
    r = c.post("/admin/games/change", json={"key": "G1", "set": {"price": "55.00"}}, headers=auth(key))
    assert r.status_code == 200 and r.json()["data"]["price"] == "55.00"
    assert c.post("/admin/describe", headers=auth(key)).json()["site"]["name"] == "Test site"
    assert c.post("/admin/configure", json={"description": "Hello"}, headers=auth(key)).json()["description"] == \
        "Hello"
    r = c.post("/admin/games/add", json={"data": {"sku": "G1", "name": "dup"}}, headers=auth(key))
    assert r.status_code == 409 and r.json()["error"]["code"] == "exists"
    assert c.post("/admin/games/explode", headers=auth(key)).status_code == 404
    assert c.post("/admin/games/change", content="{not json", headers=auth(key)).status_code == 400
    assert c.post("/mcp/", json={}).status_code == 401
