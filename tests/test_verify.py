import base64
import io
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aisites import Site, signing
from aisites.anchors import Anchor, FileAnchor
from aisites.cli import main
from aisites.verify import local_fetch, verify_site
from examples import game_catalogue
from tests.util import auth, client, mcp_call


def fetcher(site):
    """The verifier reading a site through its real web door."""
    c = client(site)

    def fetch(path, params):
        r = c.get(path, params=params)
        assert r.status_code == 200, r.text
        return r.json()
    return fetch


def small_site(directory, clock, items=3):
    site, _ = Site.create(directory, name="Shop", timezone="America/Los_Angeles", clock=clock)
    site.define("products", kind="Product")
    for i in range(items):
        clock.advance(minutes=1)
        site["products"].add(sku=f"P{i}", name=f"Product {i}", price=f"{10 + i}.00")
    return site


# ------------------------------------------------------------ signing

def test_signatures_verify_and_fail_safely(site):
    key = signing.load_key(site.dir)
    body = {"seq": 1, "hash": "sha256:abc", "time": "2026-11-14T18:00:00Z"}
    sig = signing.sign(body, key)
    assert signing.public_key(key) == site.public_key
    assert signing.verify_signature(body, sig, site.public_key)
    assert not signing.verify_signature({**body, "seq": 2}, sig, site.public_key)
    other = signing.public_key(Ed25519PrivateKey.generate())
    assert not signing.verify_signature(body, sig, other)
    for bad_sig, bad_key in [("not base64!", site.public_key), ("", site.public_key), (None, site.public_key),
                             (sig, "short"), (sig, None), (base64.b64encode(b"x" * 64).decode(), site.public_key)]:
        assert signing.verify_signature(body, bad_sig, bad_key) is False


def test_signed_head_matches_the_log_head(site, clock):
    site.define("products", kind="Product")
    clock.advance(hours=1)
    head = site.signed_head()
    seq, h, _ = site.history.head()
    assert head["checkpoint"] == {"seq": seq, "hash": h, "time": "2026-11-14T19:00:00Z", "key": site.public_key}
    assert head["algorithm"] == "ed25519"
    assert signing.verify_signature(head["checkpoint"], head["signature"], site.public_key)


# ------------------------------------------------------------ checkpoints and anchors

def test_checkpoint_stores_a_row_and_writes_to_a_file_anchor(made, clock, tmp_path):
    site, _ = made
    outside = tmp_path / "elsewhere" / "anchor.jsonl"
    site = Site(site.dir, clock, anchors=[FileAnchor(outside)])
    assert isinstance(site.anchors[0], Anchor)

    first = site.checkpoint()
    assert first["proofs"] == [{"anchor": "file", "path": str(outside), "line": 1}]
    lines = outside.read_text().splitlines()
    assert json.loads(lines[0]) == {k: first[k] for k in ("checkpoint", "signature", "algorithm")}

    clock.advance(minutes=5)
    again = site.checkpoint()   # unchanged head: the row is replaced, the anchor still gets a line
    assert again["proofs"][0]["line"] == 2
    stored = site.checkpoints()["checkpoints"]
    assert len(stored) == 1 and stored[0] == again

    site.define("products", kind="Product")
    third = site.checkpoint()
    assert [c["checkpoint"]["seq"] for c in site.checkpoints()["checkpoints"]] == [1, 2]
    assert third["checkpoint"]["seq"] == 2
    page = site.checkpoints(limit=1)
    assert len(page["checkpoints"]) == 1 and page["next_after"] == 1
    assert [c["checkpoint"]["seq"] for c in site.checkpoints(after=1)["checkpoints"]] == [2]


def test_failing_anchor_does_not_lose_the_checkpoint(site):
    class Broken:
        name = "broken"

        def anchor(self, checkpoint):
            raise RuntimeError("unreachable")

    class Silent:
        name = "silent"

        def anchor(self, checkpoint):
            return None

    s = Site(site.dir, site.clock, anchors=[Broken(), Silent()])
    out = s.checkpoint()
    assert out["proofs"] == [{"anchor": "broken", "error": "unreachable"}]
    assert s.checkpoints()["checkpoints"][0]["proofs"] == out["proofs"]


def test_anchors_configured_without_python(site, tmp_path, clock):
    path = tmp_path / "outside" / "heads.jsonl"
    out = io.StringIO()
    assert main(["anchors", "-s", str(site.dir), "--add-file", str(path)], clock=clock, out=out) == 0
    assert json.loads(out.getvalue()) == {"anchors": [{"type": "file", "path": str(path)}]}
    assert "anchors" not in site.config and "anchors" not in site.manifest()

    out = io.StringIO()
    assert main(["checkpoint", "-s", str(site.dir)], clock=clock, out=out) == 0
    assert json.loads(out.getvalue())["proofs"] == [{"anchor": "file", "path": str(path), "line": 1}]
    assert len(path.read_text().splitlines()) == 1

    assert main(["anchors", "-s", str(site.dir), "--clear"], clock=clock, out=io.StringIO()) == 0
    assert site.checkpoint()["proofs"] == []


def test_checkpoint_through_every_door(made, clock):
    site, key = made
    py = site.checkpoint()
    clock.advance(minutes=1)
    r = client(site).post("/admin/checkpoint", headers=auth(key))
    assert r.status_code == 200, r.text
    admin = r.json()
    clock.advance(minutes=1)
    is_error, mcp = mcp_call(site, "checkpoint", {})
    assert not is_error, mcp
    clock.advance(minutes=1)
    out = io.StringIO()
    assert main(["checkpoint", "-s", str(site.dir)], clock=clock, out=out) == 0
    cli = json.loads(out.getvalue())
    for result in (py, admin, mcp, cli):
        assert set(result) == {"checkpoint", "signature", "algorithm", "proofs"}
        assert result["checkpoint"]["seq"] == 1
        assert signing.verify_signature(result["checkpoint"], result["signature"], site.public_key)
    assert [r["checkpoint"]["time"][11:16] for r in (py, admin, mcp, cli)] == ["18:00", "18:01", "18:02", "18:03"]
    assert client(site).post("/admin/checkpoint").status_code == 401


# ------------------------------------------------------------ routes

def test_head_and_checkpoint_routes(made, clock):
    site, key = made
    c = client(site)
    assert site.manifest()["links"]["head"] == "/history/head"

    head = c.get("/history/head").json()
    assert head["checkpoint"]["seq"] == 1 and "proofs" not in head
    assert head["first_published"] == site.first_published()
    assert c.get("/history/checkpoints").json()["checkpoints"] == []
    assert c.get("/history/1").json()["op"] == "genesis", "/history/{seq} still works"
    assert c.get("/history/head", headers={"Accept": "text/html"}).headers["content-type"].startswith("text/html")

    etag = c.get("/history/head").headers["etag"]
    stored = site.checkpoint()
    r = c.get("/history/head", headers={"If-None-Match": etag})
    assert r.status_code == 200, "a new checkpoint changes the ETag"
    assert r.json()["proofs"] == [] and r.json()["checkpointed"] == stored["checkpoint"]["time"]
    assert c.get("/history/head", headers={"If-None-Match": r.headers["etag"]}).status_code == 304

    site.define("products", kind="Product")
    head = c.get("/history/head").json()
    assert head["checkpoint"]["seq"] == 2 and "proofs" not in head, "proofs only for a checkpointed head"
    site.checkpoint()
    page = c.get("/history/checkpoints", params={"limit": 1}).json()
    assert [x["checkpoint"]["seq"] for x in page["checkpoints"]] == [1] and page["next_after"] == 1
    page = c.get("/history/checkpoints", params={"after": 1}).json()
    assert [x["checkpoint"]["seq"] for x in page["checkpoints"]] == [2] and "next_after" not in page
    assert c.get("/history/checkpoints", params={"after": "x"}).json()["error"]["code"] == "bad_page"


# ------------------------------------------------------------ verifier

def test_verifier_passes_on_a_realistic_site_before_and_after_reveal(tmp_path, clock):
    site, _ = game_catalogue.build(tmp_path / "games", clock)
    report = verify_site(fetcher(site))
    assert report["ok"], report["problems"]
    n = len(site.history.entries(limit=1000))
    assert report["entries"] == n and report["head"]["seq"] == n
    assert report["sealed"] == 2 and report["redacted"] == 0
    assert report["first_published"] == site.first_published()
    assert report["pin"] == {"seq": n, "hash": site.history.head()[1]}

    clock.set(2026, 11, 19, 15, 0)   # sale day in New York: the hidden discounts are revealed
    later = verify_site(fetcher(site), pin=report["pin"])
    assert later["ok"], later["problems"]
    assert later["sealed"] == 0
    assert verify_site(local_fetch(site), pin=report["pin"])["ok"]


def test_verifier_pages_through_long_histories(tmp_path, clock, monkeypatch):
    from aisites import verify
    monkeypatch.setattr(verify, "PAGE", 2)
    site = small_site(tmp_path / "s", clock, items=4)
    report = verify_site(fetcher(site))
    assert report["ok"], report["problems"]
    assert report["entries"] == 6


def test_verifier_detects_an_altered_value(tmp_path, clock):
    site = small_site(tmp_path / "s", clock)
    site.store.x("UPDATE log_values SET value_json=? WHERE seq=3 AND field='price'", (json.dumps("1.00"),))
    report = verify_site(fetcher(site))
    assert not report["ok"]
    assert any("entry 3" in p and "commitment" in p for p in report["problems"])


def test_verifier_detects_a_deleted_entry(tmp_path, clock):
    site = small_site(tmp_path / "s", clock)
    site.store.x("DELETE FROM log WHERE seq=3")
    report = verify_site(fetcher(site))
    assert not report["ok"]
    assert any("entry 4" in p for p in report["problems"])


def test_verifier_detects_a_wrong_signing_key(tmp_path, clock):
    site = small_site(tmp_path / "s", clock)
    raw = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                    serialization.NoEncryption())
    (site.dir / "site.key").write_text(base64.b64encode(raw).decode())
    report = verify_site(fetcher(site))
    assert not report["ok"]
    assert any("different key" in p for p in report["problems"])


def test_verifier_detects_a_forged_signature(tmp_path, clock):
    site = small_site(tmp_path / "s", clock)
    real = fetcher(site)

    def forged(path, params):
        out = real(path, params)
        if path == "/history/head":
            out["checkpoint"]["time"] = "2020-01-01T00:00:00Z"
        return out
    report = verify_site(forged)
    assert not report["ok"] and any("signature" in p for p in report["problems"])


def test_verifier_detects_a_rewrite_since_the_pin(tmp_path, clock):
    first = small_site(tmp_path / "first", clock)
    pin = verify_site(fetcher(first))["pin"]
    clock.set(2026, 11, 14, 18, 0)
    second, _ = game_catalogue.build(tmp_path / "second", clock)
    report = verify_site(fetcher(second), pin=pin)
    assert not report["ok"]
    assert any("rewritten since the pin" in p for p in report["problems"])
    assert verify_site(fetcher(second))["ok"], "without a pin a consistent rewrite passes: the documented limit"


def test_verifier_detects_truncation(tmp_path, clock):
    site = small_site(tmp_path / "s", clock)
    pin = verify_site(fetcher(site))["pin"]
    site.store.x("DELETE FROM log_values WHERE seq >= 4")
    site.store.x("DELETE FROM log WHERE seq >= 4")
    report = verify_site(fetcher(site), pin=pin)
    assert not report["ok"]
    assert any("truncated" in p for p in report["problems"])


def test_verifier_accepts_withheld_values(tmp_path, clock):
    """A committed field with no served value (redaction) is counted, never treated as tampering."""
    site = small_site(tmp_path / "s", clock)
    real = fetcher(site)

    def redacting(path, params):
        out = real(path, params)
        for e in out.get("entries", []):
            if e["seq"] == 3:
                del e["values"]["price"], e["salts"]["price"]
                e["redacted"] = {"price": {"at": "2026-11-15T00:00:00Z"}}
        return out
    report = verify_site(redacting)
    assert report["ok"], report["problems"]
    assert report["redacted"] == 1


def test_verify_command_with_a_pin_file(tmp_path, clock):
    site = small_site(tmp_path / "s", clock)
    pin = tmp_path / "pin.json"
    out = io.StringIO()
    assert main(["verify", str(site.dir), "--pin", str(pin)], clock=clock, out=out) == 0
    report = json.loads(out.getvalue())
    assert report["ok"] and json.loads(pin.read_text()) == report["pin"]

    site["products"].add(sku="P9", name="New")
    assert main(["verify", str(site.dir), "--pin", str(pin)], clock=clock, out=io.StringIO()) == 0
    assert json.loads(pin.read_text())["seq"] == report["pin"]["seq"] + 1

    saved = pin.read_text()
    site.store.x("UPDATE log_values SET value_json=? WHERE seq=3 AND field='price'", (json.dumps("1.00"),))
    out = io.StringIO()
    assert main(["verify", str(site.dir), "--pin", str(pin)], clock=clock, out=out) == 1
    assert not json.loads(out.getvalue())["ok"]
    assert pin.read_text() == saved, "a failed run leaves the pin alone"
