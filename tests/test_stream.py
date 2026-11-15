import json

import pytest
from fastapi.testclient import TestClient

from aisites import Site, stream
from aisites.host import Host
from tests.util import client


class Waiter:
    """Stands in for stream.wait: returns at once, counts calls, and runs hooks so a test can act mid-connection."""

    def __init__(self):
        self.waits = []
        self.hooks = []

    async def __call__(self, seconds):
        self.waits.append(seconds)
        for hook in list(self.hooks):
            hook()

    def once(self, fn):
        done = []
        self.hooks.append(lambda: done or (done.append(1), fn()))


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    """Whole connections run instantly: a stream lives 10 virtual seconds, with a heartbeat after 3 quiet ones."""
    waiter = Waiter()
    monkeypatch.setattr(stream, "wait", waiter)
    monkeypatch.setattr(stream, "SETTINGS", {**stream.SETTINGS, "poll": 1.0, "heartbeat": 3.0,
                                              "max_seconds": 10.0, "retry_ms": 2500})
    return waiter


@pytest.fixture
def shop(site):
    site.define("products", kind="Product")
    site["products"].add(sku="A", name="Alpha", price="5.00")
    site.define("diary", kind="Event", access="private")
    return site


def read(c, url="/changes/stream", **kw):
    """(status, headers, raw text) of a whole stream."""
    with c.stream("GET", url, **kw) as r:
        return r.status_code, r.headers, "".join(line + "\n" for line in r.iter_lines())


def events(text):
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":") and ": " in line)
        if fields.get("event") == "change":
            out.append({"id": fields["id"], "data": json.loads(fields["data"])})
    return out


def test_backlog_from_since_matches_changes_route(shop):
    c = client(shop)
    feed = c.get("/changes").json()["changes"]
    status, head, text = read(c, "/changes/stream?since=0")
    assert status == 200 and head["content-type"].startswith("text/event-stream")
    got = events(text)
    assert [e["data"] for e in got] == feed and [e["id"] for e in got] == [str(f["id"]) for f in feed]
    assert "event: change" in text
    after = feed[1]["id"]
    assert [e["data"] for e in events(read(c, f"/changes/stream?since={after}")[2])] == feed[2:]


def test_resume_from_last_event_id_has_no_gaps_or_repeats(shop):
    c = client(shop)
    feed = c.get("/changes").json()["changes"]
    mid = feed[len(feed) // 2]["id"]
    _, _, text = read(c, headers={"Last-Event-ID": str(mid)})
    assert [e["data"] for e in events(text)] == [f for f in feed if f["id"] > mid]
    _, _, text = read(c, f"/changes/stream?since={feed[-1]['id']}", headers={"Last-Event-ID": "0"})
    assert events(text) == [], "?since= wins over the header"


def test_default_start_is_end_of_feed(shop):
    _, _, text = read(client(shop))
    assert events(text) == []


def test_change_made_while_open_is_delivered(shop, instant):
    c = client(shop)
    instant.once(lambda: shop["products"].add(sku="B", name="Bravo", price="1.00"))
    _, _, text = read(c)
    got = events(text)
    assert [e["data"]["path"] for e in got] == ["collections/products/B"]
    assert got[0]["data"] in c.get("/changes").json()["changes"]


def test_scheduled_start_is_delivered_when_clock_passes_it(shop, clock, instant):
    shop["products"].schedule("A", {"price": "3.00"}, start="2026-11-14T19:00:00Z")
    instant.once(lambda: clock.set(2026, 11, 14, 19, 30))
    _, _, text = read(client(shop))
    assert [(e["data"]["path"], e["data"]["what"]) for e in events(text)] == [
        ("collections/products/A", "scheduled change started")]


def test_heartbeat_and_closing_retry(shop, instant):
    _, _, text = read(client(shop))
    assert text.count(": keep-alive\n\n") == 3, "one per 3 quiet seconds over 10"
    assert len(instant.waits) == 10 and text.endswith("retry: 2500\n\n")


def test_no_heartbeat_right_after_an_event(shop, instant):
    instant.once(lambda: shop["products"].add(sku="B", name="Bravo", price="1.00"))
    _, _, text = read(client(shop))
    assert text.count(": keep-alive") == 3 and len(events(text)) == 1


def test_private_structure_never_appears(shop, instant):
    instant.once(lambda: shop["diary"].add(id="d", title="secret diary", start="2026-11-20T09:00"))
    c = client(shop)
    _, _, text = read(c, "/changes/stream?since=0")
    assert "diary" not in text and "secret" not in text
    assert shop.changes(0)["cursor"] >= 1


def test_path_filter(shop):
    got = events(read(client(shop), "/changes/stream?since=0&path=collections/products")[2])
    assert got and all(e["data"]["path"].startswith("collections/products") for e in got)
    assert len(got) < len(events(read(client(shop), "/changes/stream?since=0")[2]))


def test_bad_cursor_is_a_normal_400_and_frees_its_slot(shop, monkeypatch):
    monkeypatch.setitem(stream.SETTINGS, "per_address", 1)
    c = client(shop)
    assert read(c, "/changes/stream?since=abc")[0] == 400
    assert read(c)[0] == 200


def test_headers(shop):
    _, head, _ = read(client(shop))
    assert head["cache-control"] == "no-cache" and head["x-accel-buffering"] == "no"
    assert head["link"] == '</.well-known/aisite.json>; rel="describedby"'


def test_per_address_limit_and_release(shop, instant, monkeypatch):
    monkeypatch.setitem(stream.SETTINGS, "per_address", 1)
    c = client(shop)
    seen = []
    instant.once(lambda: seen.append(c.get("/changes/stream")))  # opened while the first is still held
    assert read(c)[0] == 200
    assert seen[0].status_code == 429 and seen[0].headers["retry-after"]
    assert seen[0].json()["error"]["code"] == "too_many_streams"
    assert [read(c)[0] for _ in range(3)] == [200, 200, 200], "every slot was released"


def test_total_limit(shop, instant, monkeypatch):
    monkeypatch.setitem(stream.SETTINGS, "total", 1)
    c = client(shop)
    seen = []
    instant.once(lambda: seen.append(c.get("/changes/stream")))
    assert read(c)[0] == 200
    assert seen[0].status_code == 429 and "retry-after" in seen[0].headers
    assert read(c)[0] == 200


def test_forwarded_address_counts_only_when_trusted(shop, instant, monkeypatch):
    monkeypatch.setitem(stream.SETTINGS, "per_address", 1)
    c = client(shop)
    seen = []
    instant.once(lambda: seen.append(c.get("/changes/stream", headers={"X-Forwarded-For": "2.2.2.2"})))
    read(c, headers={"X-Forwarded-For": "1.1.1.1"})
    assert seen[0].status_code == 429, "untrusted header: both calls are the same direct address"

    shop.set_limits(trust_proxy=True)
    seen.clear()
    instant.hooks.clear()
    instant.once(lambda: seen.append(c.get("/changes/stream", headers={"X-Forwarded-For": "2.2.2.2"})))
    read(c, headers={"X-Forwarded-For": "1.1.1.1"})
    assert seen[0].status_code == 200, "trusted: a different forwarded address has its own slot"


def test_manifest_link_and_prefixed_host(tmp_path, clock):
    site, _ = Site.create(tmp_path / "alpha", name="Alpha", clock=clock)
    site.define("products", kind="Product")
    site["products"].add(sku="A", name="Alpha", price="1.00")
    site.close()
    c = TestClient(Host(tmp_path, clock))
    assert c.get("/s/alpha/map").json()["links"]["stream"] == "/s/alpha/changes/stream"
    status, head, text = read(c, "/s/alpha/changes/stream?since=0")
    assert status == 200 and events(text)
    assert head["link"] == '</s/alpha/.well-known/aisite.json>; rel="describedby"'
