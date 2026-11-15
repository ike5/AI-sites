"""Questions with options: what a site can be asked, read the same way by three levels of assistant."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from aisites import Site, ops, questions
from aisites.cli import main
from aisites.errors import SiteError
from aisites.host import Host
from aisites.mcp_door import build_server
from aisites.platform import Platform
from examples import game_catalogue, pharmacy, school
from tests.util import auth, client, mcp_call

BASE = "http://testserver"


@pytest.fixture
def chemist(tmp_path, clock):
    """The pharmacy example on Monday 16 November at 11:00 in Madrid, with both branches open."""
    clock.set(2026, 11, 16, 10, 0)
    site, key = pharmacy.build(tmp_path / "pharmacy", clock)
    yield site, key, client(site)
    site.close()


class LinkFollower:
    """A level 1 assistant: it may open only an address it was given or has already been shown."""

    def __init__(self, http, start):
        self.http, self.seen, self.requests = http, {start}, 0

    def open(self, url):
        assert url in self.seen, f"{url} was never shown to this assistant"
        r = self.http.get(url)
        self.requests += 1
        assert r.status_code == 200, r.text
        self.seen |= set(_urls(r.json()))
        return r.json()


def _urls(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _urls(v)
    elif isinstance(node, list):
        for v in node:
            yield from _urls(v)
    elif isinstance(node, str) and node.startswith("http"):
        yield node


def in_stock(answer):
    """(branch, stock, open now) for each offer in an answer that has stock."""
    return [(i["with"]["place"]["data"]["name"], i["data"]["stock"], i["with"]["place"]["with"]["hours"][0]["open_now"])
            for i in answer["items"] if i["data"]["stock"] > 0]


# ---------------------------------------------------------------- the first response

def test_the_first_response_is_a_short_list_of_questions(chemist):
    _, _, http = chemist
    r = http.get("/")
    front = r.json()
    assert front == {**http.get("/.well-known/aisite.json").json(), "as_of": front["as_of"]}
    assert front["format"] == questions.FORMAT and front["name"] == "Greenleaf Pharmacy"
    assert front["timezone"] == "Europe/Madrid" and front["language"] == "es" and front["currency"] == "EUR"
    assert [(q["id"], q.get("kind")) for q in front["ask"]] == [
        ("hours", "Hours"), ("stock", "Offer"), ("products", "Product"), ("places", "Place")]
    stock = front["ask"][1]
    assert stock == {"id": "stock", "kind": "Offer", "does": "Stock of each product at each branch.",
                     "url": f"{BASE}/ask/stock", "takes": ["q", "product", "place", "region"]}, \
        "the owner's own description is used where they wrote one"
    assert front["ask"][0]["does"].startswith("Open or closed on a date")
    assert front["ask"][0]["try"] == [f"{BASE}/ask/hours?date=tomorrow"]
    assert len(front["how"]) == 3 and front["more"]["map"] == f"{BASE}/map"
    assert "structures" not in front and "fields" not in r.text, "fields and references live in the map"
    assert "do" not in front, "nothing can be done here, so no actions are offered"
    assert http.get("/ask").json()["ask"] == front["ask"]


def test_the_first_response_stays_small(tmp_path, clock):
    """A budget: what every visiting assistant must load before it can ask anything."""
    for name, module, most in (("school", school, 1500), ("pharmacy", pharmacy, 1500), ("games", game_catalogue, 1300)):
        site, _ = module.build(tmp_path / name, clock)
        size = len(client(site).get("/").content)
        assert size <= most, f"{name}: the first response is {size} bytes, over its budget of {most}"
        site.close()


def test_many_structures_do_not_grow_the_first_response_without_limit(tmp_path, clock):
    site, _ = Site.create(tmp_path / "s", name="Big", clock=clock)
    for n in range(20):
        site.define(f"list{n:02}", fields=["name"])
    front = client(site).get("/").json()
    assert len(front["ask"]) == questions.MAX_INLINE and front["more_questions"] == f"{BASE}/ask"
    assert len(client(site).get("/ask").json()["ask"]) == 20


def test_private_and_invite_only_structures(tmp_path, clock):
    site, key = Site.create(tmp_path / "s", name="Club", clock=clock)
    site.define("diary", access="private", fields=["title"])
    site.define("members", access="invite", fields=["name"])
    site["members"].add(id="m1", name="Ana")
    http = client(site)
    asks = http.get("/").json()["ask"]
    assert [q["id"] for q in asks] == ["members"] and asks[0]["needs"] == "a key from the site owner"
    assert http.get("/ask/members").status_code == 401
    assert http.get("/ask/diary").status_code == 404 and http.get("/ask/nothing").status_code == 404
    assert http.get("/ask/diary").json()["error"]["message"] == \
        http.get("/ask/nothing").json()["error"]["message"].replace("nothing", "diary")
    assert [q["id"] for q in http.get("/", headers=auth(key)).json()["ask"]] == ["diary", "members"]

    site.set_role("friend", [{"target": "members", "ops": ["read"]}])
    friend = auth(site.issue_key("friend")["secret"])
    assert "needs" not in http.get("/", headers=friend).json()["ask"][0]
    assert http.get("/ask/members", headers=friend).json()["data"]["name"] == "Ana"


# ---------------------------------------------------------------- level 1: following links

def test_level_1_reaches_the_pharmacy_answer_in_three_requests(chemist):
    _, _, http = chemist
    agent = LinkFollower(http, f"{BASE}/")
    front = agent.open(f"{BASE}/")
    stock = next(q for q in front["ask"] if q.get("kind") == "Offer")
    options = agent.open(stock["url"])
    assert options["choose"] == "product" and options["total"] == 2
    assert [o["label"] for o in options["options"]] == ["Ibuprofeno 200 mg, 20 comprimidos",
                                                         "Spray nasal salino 100 ml"]
    answer = agent.open(options["options"][0]["url"])
    assert agent.requests == 3
    assert in_stock(answer) == [("Greenleaf Norte", 32, True)]
    norte = next(i for i in answer["items"] if i["key"] == "ibuprofen-200@norte")
    hours = norte["with"]["place"]["with"]["hours"][0]
    assert hours["intervals"] == [["09:30", "14:00"], ["17:00", "20:30"]] and hours["next_change"]
    assert answer["live_as_of"], "the stock counts say when they were read"
    assert norte["with"]["product"]["data"] == {"name": "Ibuprofeno 200 mg, 20 comprimidos", "price": "3.45",
                                                "price_currency": "EUR"}


def test_level_1_school_hours_take_two_requests(tmp_path, clock):
    site, _ = school.build(tmp_path / "school", clock)
    agent = LinkFollower(client(site), f"{BASE}/")
    front = agent.open(f"{BASE}/")
    hours = next(q for q in front["ask"] if q.get("kind") == "Hours")
    today = agent.open(hours["url"])
    assert today["results"][0]["date"] == "2026-11-14" and today["results"][0]["open"] is False
    tomorrow = agent.open(today["options"][0]["url"])
    assert tomorrow["results"][0]["date"] == "2026-11-15" and "options" not in tomorrow
    assert agent.open(hours["try"][0])["results"] == tomorrow["results"], "tomorrow is also one link from the start"


def test_level_1_walks_records_pages_and_trees(tmp_path, clock):
    site, _ = game_catalogue.build(tmp_path / "games", clock)
    agent = LinkFollower(client(site), f"{BASE}/")
    front = agent.open(f"{BASE}/")
    by_id = {q["id"]: q for q in front["ask"]}

    games = agent.open(by_id["games"]["url"])
    assert games["choose"] == "key" and games["total"] == 10
    one = agent.open(games["options"][0]["url"])
    assert one["key"] == "GAME-1" and len(one["with"]["offers"]) > 1, "a product comes with its offers"

    tree = agent.open(by_id["genres"]["url"])
    assert tree["choose"] == "node" and tree["options"]
    branch = agent.open(tree["options"][0]["url"])
    assert branch["nodes"][0]["id"]

    site2, _ = Site.create(tmp_path / "many", name="Many", clock=clock)
    site2.define("items", fields=["name"])
    for n in range(120):
        site2["items"].add(id=f"i{n:03}", name=f"Item {n:03}")
    walker = LinkFollower(client(site2), f"{BASE}/ask/items")
    seen, page = [], walker.open(f"{BASE}/ask/items")
    while True:
        seen += [o["label"] for o in page["options"]]
        if "next" not in page:
            break
        page = walker.open(page["next"])
    assert len(seen) == 120 and walker.requests == 3, "50 options a page, each page reached by a link"


# ---------------------------------------------------------------- level 2: filling in

def test_level_2_takes_two_requests(chemist):
    _, _, http = chemist
    stock = next(q for q in http.get("/").json()["ask"] if q.get("kind") == "Offer")
    assert "q" in stock["takes"]
    answer = http.get(stock["url"], params={"q": "ibuprofen", "stock.gt": 0}).json()
    assert in_stock(answer) == [("Greenleaf Norte", 32, True)] and answer["total"] == 1

    by_field = http.get("/ask/stock?place=centro").json()
    assert {i["key"] for i in by_field["items"]} == {"ibuprofen-200@centro", "saline-spray@centro"}
    through = http.get("/ask/stock?place.city=Madrid&product.name.has=spray").json()
    assert through["total"] == 2
    assert http.get("/ask/stock?nonsense=1").status_code == 400


def test_one_match_is_answered_in_full_and_several_are_listed_briefly(chemist):
    site, _, http = chemist
    one = http.get("/ask/products?q=ibuprofen").json()
    assert one["key"] == "ibuprofen-200" and {s["key"] for s in one["with"]["stock"]} == {
        "ibuprofen-200@centro", "ibuprofen-200@norte"}
    assert one["with"]["stock"][0]["with"]["place"]["data"]["name"] == "Greenleaf Centro"

    site.alter("products", add=["leaflet:text"])
    site["products"].change("ibuprofen-200", leaflet="Long text " * 50)
    both = http.get("/ask/products?price.gt=1").json()
    assert both["total"] == 2 and all("leaflet" not in i["data"] for i in both["items"]), "long prose is left out"
    assert both["items"][0]["url"] == f"{BASE}/ask/products?key=ibuprofen-200"
    assert "Long text" in http.get(both["items"][0]["url"]).json()["data"]["leaflet"]

    place = http.get("/ask/places?key=norte").json()
    assert place["with"]["hours"][0]["open_now"] is True and len(place["with"]["stock"]) == 2
    assert http.get("/ask/places?key=nowhere").status_code == 404


def test_kinds_answered_as_lists(tmp_path, clock):
    site, _ = school.build(tmp_path / "school", clock)
    http = client(site)
    site["notices"].add(id="fair", title="Book fair on Friday", valid_until="2026-11-21T00:00")
    site["notices"].add(id="old", title="Last month's trip", valid_until="2026-10-31T00:00")
    notices = http.get("/ask/notices").json()
    assert "fair" in [i["key"] for i in notices["items"]] and "old" not in [i["key"] for i in notices["items"]]
    assert "choose" not in notices, "notices in force are simply listed"
    site.define("events", kind="Event", access="public")
    site["events"].add(id="past", title="Last week", start="2026-11-07T10:00")
    site["events"].add(id="next", title="Fair", start="2026-11-21T10:00")
    assert [i["key"] for i in http.get("/ask/events").json()["items"]] == ["next"], "from today onwards"
    assert http.get("/ask/closures").status_code == 404, "closures are part of the hours answer, not a question"


# ---------------------------------------------------------------- level 3: combining

def test_several_questions_in_one_request(chemist):
    _, _, http = chemist
    both = http.get("/ask/hours,stock?date=tomorrow&q=ibuprofen").json()
    hours, stock = both["answers"]
    assert hours["question"] == "hours" and hours["results"][0]["date"] == "2026-11-17"
    assert stock["question"] == "stock" and stock["total"] == 2, "each question used the arguments it takes"
    alone = http.get("/ask/stock?q=ibuprofen").json()
    assert stock["items"] == alone["items"] and stock["tag"] == alone["tag"], "the same facts as asking alone"

    posted = http.post("/ask", json={"ask": [{"id": "stock", "q": "spray", "stock.gt": 10},
                                              {"id": "hours", "key": "norte"}, {"id": "nothing"}]}).json()
    assert posted["answers"][0]["items"][0]["key"] == "saline-spray@centro"
    assert posted["answers"][1]["results"][0]["key"] == "norte"
    assert posted["answers"][2]["error"]["code"] == "not_found", "one bad question does not spoil the others"

    too_many = ",".join(["hours"] * (questions.MAX_AT_ONCE + 1))
    assert http.get(f"/ask/{too_many}").json()["error"]["code"] == "too_many"
    assert http.post("/ask", json={"ask": [{"id": "hours"}] * 9}).status_code == 400
    assert http.post("/ask", content=b'{"ask": [{"id": "hours", "pad": "' + b"x" * 5000 + b'"}]}').status_code == 413
    assert http.post("/ask", json={"ask": "hours"}).status_code == 400
    assert http.post("/ask", content=b"not json").status_code == 400


def test_asking_by_kind_needs_no_list(chemist, tmp_path, clock):
    _, _, http = chemist
    by_kind = http.get("/ask/Offer?q=ibuprofen&stock.gt=0").json()
    assert by_kind["question"] == "Offer" and by_kind["collection"] == "stock"
    assert in_stock(by_kind) == [("Greenleaf Norte", 32, True)]
    assert http.get("/ask/Hours?date=tomorrow").json()["results"][0]["date"] == "2026-11-17"
    assert http.get("/ask/Slot").status_code == 404, "no structure of that kind here"

    # The same address works on another site that named its structure differently.
    games, _ = game_catalogue.build(tmp_path / "games", clock)
    other = client(games).get("/ask/Offer?product=GAME-1&region=MX").json()
    assert other["collection"] == "offers" and other["items"][0]["data"]["price"] == "1019.83"


def test_unless_answers_only_when_something_changed(chemist, clock):
    site, _, http = chemist
    first = http.get("/ask/stock?q=ibuprofen").json()
    same = http.get(f"/ask/stock?q=ibuprofen&unless={first['tag']}")
    assert same.json() == {"question": "stock", "unchanged": True, "tag": first["tag"], "as_of": same.json()["as_of"]}
    assert len(same.content) < 120

    site["stock"].change("ibuprofen-200@norte", stock=31)
    changed = http.get(f"/ask/stock?q=ibuprofen&unless={first['tag']}").json()
    assert "unchanged" not in changed and changed["tag"] != first["tag"]

    # The clock alone changes an answer too: Norte closes for lunch at 14:00.
    before = http.get("/ask/hours?key=norte").json()
    clock.set(2026, 11, 16, 13, 30)   # 14:30 in Madrid
    after = http.get(f"/ask/hours?key=norte&unless={before['tag']}").json()
    assert after["results"][0]["open_now"] is False and "unchanged" not in after


def test_lean_output_and_a_size_limit(chemist):
    _, _, http = chemist
    full = http.get("/ask/stock?q=ibuprofen")
    lean = http.get("/ask/stock?q=ibuprofen&format=table&links=0")
    assert len(lean.content) < len(full.content) / 2
    table = lean.json()
    assert table["columns"] == ["key", "stock", "product.name", "product.price", "product.price_currency",
                                "place.name", "place.address", "place.city"], "empty columns are dropped"
    assert table["rows"][1][:2] == ["ibuprofen-200@norte", 32] and table["live_as_of"]

    plain = http.get("/ask/stock?links=0").json()
    assert plain["options"][0] == {"label": "Ibuprofeno 200 mg, 20 comprimidos", "product": "ibuprofen-200"}
    assert "http" not in json.dumps(plain)

    assert len(http.get("/ask/stock?q=spray").content) > 900
    cut = http.get("/ask/stock?q=spray&max=900")
    assert len(cut.content) <= 900 and cut.json()["cut"] is True and len(cut.json()["items"]) == 1
    rest = http.get(cut.json()["next"]).json()
    assert [i["key"] for i in rest["items"]] == ["saline-spray@norte"] and "cut" not in rest
    assert http.get("/ask/stock?q=spray&max=lots").status_code == 400


def test_no_level_sees_more_than_another(tmp_path, clock):
    site, key = Site.create(tmp_path / "s", name="Shop", clock=clock)
    site.define("places", kind="Place", fields=["alarm_code::private"])
    site.define("suppliers", access="private", fields=["name"])
    site.define("stock", kind="Offer", refs={"place": "places"}, fields=["margin:decimal:private"])
    site["places"].add(id="shop", name="The Shop", alarm_code="4321")
    site["suppliers"].add(id="acme", name="Acme Wholesale")
    site["stock"].add(id="o1", product="tea", place="shop", stock=3, margin="0.40")
    http = client(site)
    seen = [http.get("/").text, http.get("/ask").text, http.get("/ask/stock").text,
            http.get("/ask/stock?product=tea").text, http.get("/ask/places").text,
            http.get("/ask/stock,places,suppliers?q=tea").text, http.get("/ask/Offer?q=tea&format=table&links=0").text,
            http.post("/ask", json={"ask": [{"id": "stock", "q": "tea"}, {"id": "suppliers"}]}).text,
            http.get("/ask/Place").text, http.get("/map").text, http.get("/find?q=shop").text]
    for text in seen:
        assert "4321" not in text and "alarm_code" not in text and "margin" not in text and "Acme" not in text
        assert "suppliers" not in text or '"not_found"' in text
    for path in ("/ask/stock?margin=0.40", "/ask/stock?place.alarm_code=4321", "/ask/Offer?margin.gt=0"):
        assert http.get(path).json()["error"]["code"] == "bad_filter"
    assert "4321" in http.get("/ask/places", headers=auth(key)).text


# ---------------------------------------------------------------- questions the owner defines

def test_owner_defines_a_question(chemist):
    site, key, http = chemist
    made = site.question("in_stock", does="Where a product is in stock right now", read="stock",
                         where={"stock.gt": 0}, takes=["q", "product", "place.city"], follow=["place", "place.hours"],
                         fields=["stock", "place.name"], options="product")
    assert made["id"] == "in_stock" and made["with"] == ["place", "place.hours"]

    first = http.get("/").json()["ask"][0]
    assert first == {"id": "in_stock", "does": "Where a product is in stock right now",
                     "url": f"{BASE}/ask/in_stock", "takes": ["q", "product", "place.city"]}, "the owner's come first"

    options = http.get("/ask/in_stock").json()
    assert options["choose"] == "product" and len(options["options"]) == 2
    answer = http.get(options["options"][0]["url"]).json()
    assert [i["key"] for i in answer["items"]] == ["ibuprofen-200@norte"], "the fixed filter always applies"
    assert answer["items"][0]["data"] == {"stock": 32}
    assert answer["items"][0]["with"]["place"]["data"] == {"name": "Greenleaf Norte"}
    assert http.get("/ask/in_stock?q=spray&place.city=Madrid").json()["total"] == 2
    refused = http.get("/ask/in_stock?stock.gt=-5")
    assert refused.status_code == 400 and "It takes: q, product, place.city" in refused.json()["error"]["hint"]

    site.question("in_stock", remove=True)
    assert http.get("/ask/in_stock").status_code == 404
    with pytest.raises(SiteError):
        site.question("in_stock", remove=True)


def test_a_bad_question_is_refused_when_defined(chemist):
    site, _, http = chemist
    site.alter("stock", add=["margin:decimal:private"])
    site.define("suppliers", access="private", fields=["name"])
    bad = [dict(id="stock", does="x", read="stock"),                              # a structure already has that name
           dict(id="Bad Name", does="x", read="stock"),
           dict(id="q1", does="", read="stock"),
           dict(id="q1", does="x", read="nothing"),
           dict(id="q1", does="x", read="stock", takes=["margin"]),               # a private field
           dict(id="q1", does="x", read="stock", where={"margin.gt": 0}),
           dict(id="q1", does="x", read="stock", follow=["supplier"]),
           dict(id="q1", does="x", read="stock", fields=["place.nothing"]),
           dict(id="q1", does="x", read="stock", sort="nothing"),
           dict(id="q1", does="x", read="stock", takes=["q"], options="product")]  # options must be in takes
    for attempt in bad:
        with pytest.raises(SiteError):
            site.question(**attempt)
    assert site.defined_questions() == {} and http.get("/ask/q1").status_code == 404


def test_questions_through_every_door(chemist, capsys):
    site, key, http = chemist
    folder = str(site.store.path).rsplit("/", 1)[0]
    args = {"id": "cheap", "does": "Products under a price", "read": "products", "takes": ["price.lte"],
            "fields": ["name", "price"], "sort": "price"}
    assert ops.run(site, ops.LOCAL, "question", args)["id"] == "cheap"
    by_python = ops.run(site, ops.LOCAL, "ask", {"id": "cheap", "args": {"price.lte": "5"}})
    assert [i["data"]["name"] for i in by_python["items"]] == ["Ibuprofeno 200 mg, 20 comprimidos"]

    by_admin = http.post("/admin/ask", json={"id": "cheap", "args": {"price.lte": "5"}}, headers=auth(key)).json()
    _, by_mcp = mcp_call(site, "ask", {"id": "cheap", "args": {"price.lte": "5"}})
    by_web = http.get("/ask/cheap?price.lte=5&links=0").json()
    assert main(["ask", "cheap", "price.lte=5", "-s", folder]) == 0
    by_cli = json.loads(capsys.readouterr().out)
    assert by_python["items"] == by_admin["items"] == by_mcp["items"] == by_web["items"] == by_cli["items"]

    assert [q["id"] for q in ops.run(site, ops.LOCAL, "ask", {})["ask"]][0] == "cheap"
    assert main(["question", "cheap", "--remove", "-s", folder]) == 0
    capsys.readouterr()
    assert http.post("/admin/question", json={"id": "x", "does": "y", "read": "stock"}).status_code == 401
    trail = site.audit()["entries"]
    assert [e["op"] for e in trail[:2]] == ["question", "question"], "defining and removing are in the audit trail"


def test_fields_may_be_called_anything(tmp_path, clock):
    """Field names end up in addresses; none of them may collide with how answers are built."""
    site, _ = Site.create(tmp_path / "s", name="Odd", clock=clock)
    site.define("things", fields=["name", "qid", "out", "label", "params", "self", "cursor_note"])
    for key in ("a", "b"):
        site["things"].add({"id": key, "name": key.upper(), "qid": "1", "out": "x", "label": "l", "params": "p",
                            "self": "s"})
    http = client(site)
    for path in ("/ask/things?qid=1", "/ask/things?out=x", "/ask/things?label=l&params=p&self=s&limit=1",
                 "/ask/things", "/ask/things?qid=1&max=250", "/ask/things?qid=1&format=table&links=0"):
        assert http.get(path).status_code == 200, path
    page = http.get("/ask/things?qid=1&limit=1").json()
    assert "qid=1" in page["next"] and http.get(page["next"]).json()["items"][0]["key"] == "b"
    site.question("odd", does="Odd", read="things", takes=["label"], options="label")
    assert http.get("/ask/odd").json()["options"] == [{"label": "l", "url": f"{BASE}/ask/odd?label=l"}]


# ---------------------------------------------------------------- full addresses

def test_links_are_full_addresses_on_a_host(tmp_path, clock):
    Platform.init(tmp_path, clock, public_url="https://host.example").close()
    site, _ = pharmacy.build(tmp_path / "farmacia", clock)
    site.close()
    http = TestClient(Host(tmp_path, clock), base_url="http://internal:8000")
    front = http.get("/s/farmacia/").json()
    assert front["ask"][0]["url"] == "https://host.example/s/farmacia/ask/hours", "the public address, not the inner one"
    options = http.get("/s/farmacia/ask/stock").json()["options"]
    assert options[0]["url"] == "https://host.example/s/farmacia/ask/stock?product=ibuprofen-200"


def test_a_site_can_state_its_own_address(chemist):
    site, _, http = chemist
    site.configure(url="https://farmacia.example/")
    assert http.get("/").json()["ask"][0]["url"] == "https://farmacia.example/ask/hours"
    assert http.get("/ask/places").json()["options"][0]["url"] == "https://farmacia.example/ask/places?key=centro"


# ---------------------------------------------------------------- the authoring door stays small

def test_describe_is_short_unless_asked(chemist):
    site, _, _ = chemist
    short, full = site.describe(), site.describe(full=True)
    assert len(json.dumps(short)) < len(json.dumps(full)) / 3
    stock = next(s for s in short["structures"] if s["name"] == "stock")
    assert stock["fields"]["place"] == "string -> places" and stock["fields"]["stock"] == "integer capacity"
    assert stock["count"] == 4 and "Offer" in short["kinds"] and isinstance(full["kinds"][0], dict)
    assert ops.run(site, ops.LOCAL, "describe", {"full": True}) == full


def test_mcp_results_are_structured(chemist):
    site, _, _ = chemist

    async def go():
        async with Client(build_server(site)) as m:
            return await m.call_tool("ask", {"id": "stock", "args": {"q": "ibuprofen"}})

    res = asyncio.run(go())
    assert res.structured_content["total"] == 2
    assert json.loads(res.content[0].text) == res.structured_content, "the same result as data and as text"
