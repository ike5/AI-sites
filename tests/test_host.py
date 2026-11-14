import json

from fastapi.testclient import TestClient

from aisites import Site
from aisites.host import Host
from tests.util import auth


def make(tmp_path, clock, name):
    site, key = Site.create(tmp_path / name, name=name.title(), clock=clock)
    site.define("products", kind="Product")
    site["products"].add(sku="ONLY", name=f"Sold by {name}", price="1.00")
    site.define("diary", kind="Event", access="private")
    site["diary"].add(id="d", title=f"{name}'s secret", start="2026-11-20T09:00")
    return key


def test_sites_are_isolated_and_links_carry_their_prefix(tmp_path, clock):
    keys = {name: make(tmp_path, clock, name) for name in ("alpha", "beta")}
    c = TestClient(Host(tmp_path, clock))

    m = c.get("/s/alpha/").json()
    assert m["name"] == "Alpha" and m["structures"][0]["href"] == "/s/alpha/collections/products"
    assert c.get("/s/alpha").json()["name"] == "Alpha", "the trailing slash is optional"
    r = c.get("/s/alpha/collections/products/ONLY")
    assert r.json()["data"]["name"] == "Sold by alpha"
    assert r.headers["link"] == '</s/alpha/.well-known/aisite.json>; rel="describedby"'
    assert r.json()["history"] == "/s/alpha/collections/products/ONLY/history"
    assert c.get("/s/beta/collections/products/ONLY").json()["data"]["name"] == "Sold by beta"

    assert c.get("/s/alpha/collections/diary", headers=auth(keys["alpha"])).status_code == 200
    assert c.get("/s/beta/collections/diary", headers=auth(keys["alpha"])).status_code == 401, \
        "one site's key is not valid on another"
    r = c.post("/s/beta/admin/products/remove", json={"key": "ONLY"}, headers=auth(keys["alpha"]))
    assert r.status_code == 401
    assert c.get("/s/beta/collections/products/ONLY").status_code == 200


def test_unknown_sites_and_bad_names(tmp_path, clock):
    make(tmp_path, clock, "alpha")
    c = TestClient(Host(tmp_path, clock))
    for path in ("/", "/s/missing/", "/s/..%2Falpha/", "/collections/products"):
        r = c.get(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", path
    assert "alpha" not in c.get("/").text, "the host does not list the sites it holds"


def test_site_created_while_running_is_picked_up(tmp_path, clock):
    host = Host(tmp_path, clock)
    c = TestClient(host)
    assert c.get("/s/late/").status_code == 404
    make(tmp_path, clock, "late")
    assert c.get("/s/late/").json()["name"] == "Late"
    assert host.names() == ["late"]


def test_hostname_routing(tmp_path, clock):
    make(tmp_path, clock, "alpha")
    (tmp_path / "hosts.json").write_text(json.dumps({"alpha.example.org": "alpha"}))
    c = TestClient(Host(tmp_path, clock))
    r = c.get("/collections/products/ONLY", headers={"Host": "alpha.example.org"})
    assert r.json()["data"]["name"] == "Sold by alpha" and r.json()["history"] == "/collections/products/ONLY/history"
    assert c.get("/collections/products/ONLY", headers={"Host": "other.example.org"}).status_code == 404
