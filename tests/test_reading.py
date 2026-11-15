"""Reading across records: answers that stay fresh, word matching, and following references."""

import pytest

from aisites import Site, ops, reading
from aisites.errors import SiteError
from examples import pharmacy
from tests.util import auth, client, mcp_call


@pytest.fixture
def chemist(tmp_path, clock):
    """The pharmacy example on Monday 16 November at 11:00 in Madrid, with both branches open."""
    clock.set(2026, 11, 16, 10, 0)
    site, key = pharmacy.build(tmp_path / "pharmacy", clock)
    yield site, key, client(site)
    site.close()


# ---------------------------------------------------------------- answers that follow the clock

def test_open_now_is_never_revalidated_as_unchanged(chemist, clock):
    site, _, http = chemist
    clock.set(2026, 11, 16, 7, 59)   # Monday 08:59 in Madrid; Centro opens at 09:00
    before = http.get("/hours/centro")
    assert before.json()["results"][0]["open_now"] is False
    assert before.json()["results"][0]["next_change"] == "2026-11-16T08:00:00Z", "it opens at 09:00 local"
    tag = before.headers["etag"]
    assert http.get("/hours/centro", headers={"If-None-Match": tag}).status_code == 304

    clock.set(2026, 11, 16, 8, 1)    # 09:01 local: nothing was written, but the answer changed
    after = http.get("/hours/centro", headers={"If-None-Match": tag})
    assert after.status_code == 200 and after.json()["results"][0]["open_now"] is True
    assert after.json()["results"][0]["next_change"] == "2026-11-16T20:00:00Z", "it closes at 21:00 local"

    clock.set(2026, 11, 16, 20, 30)  # 21:30 local, closed for the day: the answer holds until midnight there
    assert http.get("/hours/centro").json()["results"][0]["next_change"] == "2026-11-16T23:00:00Z"


def test_a_filter_on_today_follows_midnight(tmp_path, clock):
    site, _ = Site.create(tmp_path / "s", name="Diary", timezone="Europe/Madrid", clock=clock)
    site.define("events", kind="Event", access="public")
    site["events"].add(id="a", title="Saturday market", start="2026-11-14T20:00")
    site["events"].add(id="b", title="Sunday walk", start="2026-11-15T10:00")
    http = client(site)
    first = http.get("/collections/events?start.gte=today")
    assert first.json()["total"] == 2
    clock.advance(days=1)
    again = http.get("/collections/events?start.gte=today", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 200 and again.json()["total"] == 1, "a new day, with no write, is a new answer"


def test_a_branch_in_another_time_zone_follows_its_own_clock(tmp_path, clock):
    site, _ = Site.create(tmp_path / "s", name="Two cities", timezone="America/Los_Angeles", clock=clock)
    site.define("hours", kind="Hours")
    site["hours"].add(id="tokyo", timezone="Asia/Tokyo", weekly={"sun": [["09:00", "17:00"]]})
    http = client(site)
    clock.set(2026, 11, 14, 23, 59)   # Sunday 08:59 in Tokyo, still Saturday afternoon in Los Angeles
    before = http.get("/hours/tokyo")
    assert before.json()["results"][0]["open_now"] is False
    clock.set(2026, 11, 15, 0, 1)
    after = http.get("/hours/tokyo", headers={"If-None-Match": before.headers["etag"]})
    assert after.status_code == 200 and after.json()["results"][0]["open_now"] is True


def test_private_changes_still_leave_public_tags_alone(chemist):
    site, key, http = chemist
    site.alter("products", add=["cost:decimal:private"])
    paths = ["/", "/map", "/ask/products?key=ibuprofen-200", "/collections/products", "/find?q=ibuprofen"]
    before = [http.get(p).headers["etag"] for p in paths]
    site["products"].change("ibuprofen-200", cost="1.10")
    assert [http.get(p).headers["etag"] for p in paths] == before
    assert "cost" not in http.get("/collections/products/ibuprofen-200").text


# ---------------------------------------------------------------- words

def test_words_ignore_case_and_accents_and_accept_a_beginning():
    assert reading.words("Ibuprofeno 200 mg, 20 COMPRIMIDOS") == ["ibuprofeno", "200", "mg", "20", "comprimidos"]
    assert reading.words("Café Ñandú_niño") == ["cafe", "nandu", "nino"]
    assert reading.has(reading.words("Ibuprofeno 200 mg"), reading.words("ibuprofen"))
    assert reading.has(reading.words("Jarabe para la tos"), reading.words("TOS jarab"))
    assert not reading.has(reading.words("Ibuprofeno"), reading.words("profeno")), "a beginning, not a middle"
    assert not reading.has(reading.words("Ibuprofeno"), reading.words("ibuprofeno forte"))
    assert not reading.has(reading.words("anything"), [])


def test_name_lookup_finds_the_product_the_exact_filter_missed(chemist):
    _, _, http = chemist
    assert http.get("/collections/products?name=ibuprofen").json()["total"] == 0, "the exact filter still misses"
    found = http.get("/collections/products?name.has=ibuprofen").json()
    assert [i["key"] for i in found["items"]] == ["ibuprofen-200"]
    assert http.get("/collections/products?name.has=IBUPROFENO+comprim").json()["total"] == 1
    assert http.get("/collections/products?q=nasal").json()["items"][0]["key"] == "saline-spray"

    # q also sees the names of what a record refers to: stock rows have no name of their own.
    assert http.get("/collections/stock?q=nasal+norte").json()["items"][0]["key"] == "saline-spray@norte"
    assert http.get("/collections/stock?q=aspirin").json()["total"] == 0

    wrong = http.get("/collections/products?price.has=3")
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "bad_filter"
    assert http.get("/collections/products?name.has=").status_code == 400


def test_aliases_are_searched(chemist):
    site, _, http = chemist
    site["products"].change("ibuprofen-200", aliases=["Advil", "painkiller"])
    assert http.get("/collections/products?q=advil").json()["total"] == 1
    assert http.get("/find?q=painkiller").json()["matches"][0]["key"] == "ibuprofen-200"


def test_find_looks_across_the_site(chemist):
    site, key, http = chemist
    found = http.get("/find?q=ibuprofen").json()
    assert found["matches"][0] == {"collection": "products", "key": "ibuprofen-200",
                                   "label": "Ibuprofeno 200 mg, 20 comprimidos", "kind": "Product"}, \
        "a record named for the words comes before ones that only mention them"
    assert found["total"] == 3 and http.get("/find?q=ibuprofen&limit=1").json()["matches"].__len__() == 1
    assert http.get("/find?q=norte").json()["matches"][0]["collection"] == "places"
    assert http.get("/find").status_code == 400 and http.get("/find?q=zzz").json()["matches"] == []

    site.define("suppliers", access="private", fields=["name"])
    site["suppliers"].add(id="s1", name="Ibuprofen wholesale")
    assert http.get("/find?q=wholesale").json()["total"] == 0
    assert http.get("/find?q=wholesale", headers=auth(key)).json()["total"] == 1


# ---------------------------------------------------------------- following references

def test_the_pharmacy_question_in_one_read(chemist):
    _, _, http = chemist
    r = http.get("/collections/stock?q=ibuprofen&stock.gt=0&with=place.hours"
                 "&fields=stock,place.name,place.address")
    out = r.json()
    assert out["total"] == 1
    item = out["items"][0]
    assert item["data"] == {"stock": 32} and item["live_as_of"], "the stock count says when it was read"
    place = item["with"]["place"]
    assert place["key"] == "norte" and place["data"] == {"name": "Greenleaf Norte", "address": "Av. de Burgos 80"}
    hours = place["with"]["hours"][0]
    assert hours["open"] is True and hours["intervals"] == [["09:30", "14:00"], ["17:00", "20:30"]]
    assert "weekly" not in str(hours), "the answer for today, not the weekly pattern to work out"


def test_references_forward_back_and_through(chemist):
    _, _, http = chemist
    record = http.get("/collections/places/norte?with=stock.product,hours").json()
    assert {s["key"] for s in record["with"]["stock"]} == {"ibuprofen-200@norte", "saline-spray@norte"}
    assert record["with"]["stock"][0]["with"]["product"]["data"]["name"].startswith("Ibuprofeno")
    assert record["with"]["hours"][0]["key"] == "norte"

    through = http.get("/collections/stock?place.city=Madrid&product.name.has=spray").json()
    assert {i["key"] for i in through["items"]} == {"saline-spray@centro", "saline-spray@norte"}
    assert http.get("/collections/stock?place.city=Lisbon").json()["total"] == 0


def test_bad_references_are_refused_with_what_is_possible(chemist, monkeypatch):
    _, _, http = chemist
    r = http.get("/collections/stock?with=nothing")
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_with"
    assert "place, product" in r.json()["error"]["hint"]
    assert http.get("/collections/stock?with=place.hours.place.stock").json()["error"]["code"] == "bad_with"
    many = ",".join(["place.hours", "place.stock.product", "place.stock.place", "product.stock.place",
                     "product.stock.product"])
    assert http.get("/collections/stock?limit=1&with=" + many).status_code == 200, "five chains are allowed"
    monkeypatch.setattr(reading, "MAX_PATHS", 4)
    assert http.get("/collections/stock?limit=1&with=" + many).json()["error"]["code"] == "bad_with"
    assert http.get("/collections/stock?fields=place.nothing").json()["error"]["code"] == "bad_fields"
    assert http.get("/collections/stock?stock.city=1").json()["error"]["code"] == "bad_filter"
    assert http.get("/collections/stock?price.foo=1").json()["error"]["message"] == "'.foo' is not a filter operator."


def test_records_attached_from_a_link_back_are_capped(tmp_path, clock):
    site, _ = Site.create(tmp_path / "s", name="Depot", clock=clock)
    site.define("places", kind="Place")
    site.define("stock", kind="Offer", refs={"place": "places"})
    site["places"].add(id="depot", name="Depot")
    for n in range(25):
        site["stock"].add(id=f"o{n:02}", product=f"p{n}", place="depot", stock=n)
    out = client(site).get("/collections/places/depot?with=stock").json()
    assert len(out["with"]["stock"]) == reading.MAX_BACK and out["with"]["stock_more"] == 5


# ---------------------------------------------------------------- nothing more is shown than a direct read

@pytest.fixture
def guarded(tmp_path, clock):
    site, key = Site.create(tmp_path / "s", name="Shop", clock=clock)
    site.define("places", kind="Place", fields=["alarm_code::private"])
    site.define("suppliers", access="private", fields=["name", "place"], refs={"place": "places"})
    site.define("members", access="invite", fields=["name", "place"], refs={"place": "places"})
    site.define("stock", kind="Offer", refs={"place": "places", "supplier": "suppliers"},
                fields=["supplier", "margin:decimal:private"])
    site["places"].add(id="shop", name="The Shop", alarm_code="4321")
    site["suppliers"].add(id="acme", name="Acme Wholesale", place="shop")
    site["members"].add(id="m1", name="Ana", place="shop")
    site["stock"].add(id="o1", product="tea", place="shop", supplier="acme", stock=3, margin="0.40")
    yield site, key, client(site)
    site.close()


def test_a_visitor_cannot_reach_private_things_through_links(guarded):
    site, key, http = guarded
    for path in ("/collections/stock?with=supplier", "/collections/places/shop?with=suppliers",
                 "/collections/stock?with=nonexistent"):
        r = http.get(path)
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_with"
        assert "supplier" not in r.json()["error"]["hint"], "a private structure is not even named as a choice"
    assert http.get("/collections/places/shop?with=members").status_code == 400, "invite-only, and no key"

    seen = http.get("/collections/stock?with=place").text
    assert "4321" not in seen and "alarm_code" not in seen and "margin" not in seen
    around = http.get("/collections/places/shop?with=stock").text
    assert "margin" not in around and "0.40" not in around

    owner = http.get("/collections/stock?with=place,supplier", headers=auth(key)).json()["items"][0]
    assert owner["with"]["supplier"]["data"]["name"] == "Acme Wholesale"
    assert owner["with"]["place"]["data"]["alarm_code"] == "4321"


def test_private_values_cannot_be_probed_by_matching(guarded):
    site, key, http = guarded
    same = http.get("/collections/stock?nonexistent=1").json()["error"]
    for path in ("/collections/stock?margin=0.40", "/collections/stock?margin.gt=0", "/collections/stock?margin.foo=1",
                 "/collections/stock?place.alarm_code=4321", "/collections/stock?place.alarm_code.has=4321",
                 "/collections/stock?supplier.name=Acme+Wholesale", "/collections/stock?supplier.name.has=acme"):
        r = http.get(path)
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_filter", path
    hidden = http.get("/collections/stock?margin=0.40").json()["error"]
    assert hidden["message"].replace("margin", "nonexistent") == same["message"] and hidden["hint"] == same["hint"], \
        "a private field is answered exactly like one that does not exist"
    assert http.get("/collections/stock?q=wholesale").json()["total"] == 0, "a private supplier's name does not match"
    assert http.get("/collections/places?q=4321").json()["total"] == 0
    assert http.get("/find?q=wholesale").json()["total"] == 0 and http.get("/find?q=4321").json()["total"] == 0
    assert http.get("/collections/stock?q=wholesale", headers=auth(key)).json()["total"] == 1


def test_a_role_sees_linked_records_as_a_visitor_would(guarded):
    site, key, http = guarded
    site.set_role("stockist", [{"target": "stock", "ops": ["list", "get", "change"]}])
    secret = site.issue_key("stockist")["secret"]
    listed = http.post("/admin/stock/list", json={"with": ["place"]}, headers=auth(secret)).json()
    place = listed["items"][0]["with"]["place"]
    assert place["data"]["name"] == "The Shop" and "alarm_code" not in place["data"]
    refused = http.post("/admin/stock/list", json={"with": ["supplier"]}, headers=auth(secret))
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "bad_with"
    full = http.post("/admin/stock/list", json={"with": ["supplier"]}, headers=auth(key)).json()
    assert full["items"][0]["with"]["supplier"]["key"] == "acme"


# ---------------------------------------------------------------- compact rows keep their evidence

def test_table_rows_keep_keys_and_times(chemist, clock):
    site, _, http = chemist
    t = http.get("/collections/stock?format=table&fields=stock,place.name").json()
    assert t["columns"] == ["key", "stock", "place.name"]
    assert t["rows"][0] == ["ibuprofen-200@centro", 0, "Greenleaf Centro"]
    assert t["live_as_of"] and t["updated"], "a time every row shares is stated once"

    clock.advance(minutes=5)
    site["stock"].change("ibuprofen-200@norte", stock=31)
    t = http.get("/collections/stock?format=table&fields=stock").json()
    assert t["columns"] == ["key", "stock", "live_as_of"], "a time that differs goes back into the rows"
    assert t["rows"][1][2] != t["rows"][0][2] and "live_as_of" not in t and t["updated"]

    assert http.get("/collections/products?format=table").json()["columns"][0] == "sku", "the key field is the key"
    bad = http.get("/collections/places?format=table&fields=name,stock.stock")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_format"


# ---------------------------------------------------------------- the same through every door

def test_the_other_doors_read_the_same_way(chemist, capsys):
    site, key, http = chemist
    from aisites.cli import main
    web = http.get("/collections/stock?q=ibuprofen&stock.gt=0&with=place").json()["items"]
    python = site.list("stock", {"stock.gt": 0}, q="ibuprofen", follow=["place"])["items"]
    admin = http.post("/admin/stock/list", json={"where": {"stock.gt": 0}, "q": "ibuprofen", "with": ["place"]},
                      headers=auth(key)).json()["items"]
    _, tool = mcp_call(site, "list", {"target": "stock", "where": {"stock.gt": 0}, "q": "ibuprofen", "with": ["place"]})
    strip = lambda items: [(i["key"], i["with"]["place"]["data"]["name"]) for i in items]   # noqa: E731
    assert strip(web) == strip(python) == strip(admin) == strip(tool["items"]) == [("ibuprofen-200@norte", "Greenleaf Norte")]

    assert main(["list", "stock", "-s", str(site.store.path).rsplit("/", 1)[0],
                 "stock.gt=0", "--q", "ibuprofen", "--with", "place"]) == 0
    assert "Greenleaf Norte" in capsys.readouterr().out
    assert ops.run(site, ops.LOCAL, "find", {"q": "nasal"})["matches"][0]["key"] == "saline-spray"
    assert ops.run(site, ops.LOCAL, "get", {"target": "places", "key": "norte", "with": ["hours"]})["with"]["hours"]
    with pytest.raises(SiteError):
        site.list("stock", follow=["nope"])
