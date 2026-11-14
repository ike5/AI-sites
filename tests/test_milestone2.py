"""How the three parts of Milestone 2 behave together: roles, redaction, and signing with verification."""

import io
import json

import pytest

from aisites import ops
from aisites.cli import POSITIONAL, main
from aisites.verify import verify_site
from examples import game_catalogue
from tests.util import auth, client, log, mcp_call

SECRET = "Unfounded-allegation-about-a-named-reviewer-4419"


@pytest.fixture
def games(tmp_path, clock):
    site, owner = game_catalogue.build(tmp_path / "games", clock)
    site.set_role("pricing", [{"target": "games", "ops": ["get", "list", "change", "schedule", "unschedule"],
                               "fields": ["price"]}])
    pm = site.issue_key("pricing", "Product manager")["secret"]
    return site, owner, pm, client(site)


def fetcher(http):
    def fetch(path, params=None):
        r = http.get(path, params=params or {})
        assert r.status_code == 200, r.text
        return r.json()
    return fetch


def on_disk(site, text):
    return any(text.encode() in f.read_bytes() for f in site.dir.iterdir() if f.name.startswith("site.db"))


def test_every_operation_has_a_command_line_form():
    assert set(POSITIONAL) == set(ops.OPS)


def test_a_role_cannot_redact_checkpoint_or_manage_access(games):
    site, owner, pm, http = games
    for url, body in [("/admin/redact", {"seq": 3}), ("/admin/checkpoint", {}),
                      ("/admin/set_role", {"role": "pricing", "grants": [{"target": "*", "ops": ["change"]}]}),
                      ("/admin/issue_key", {"role": "pricing"}), ("/admin/audit", {}), ("/admin/list_keys", {})]:
        r = http.post(url, json=body, headers=auth(pm))
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", url
        assert "only the site owner can" in r.json()["error"]["message"]
    is_error, result = mcp_call(site, "audit", {})
    assert not is_error, "the local owner still can"
    refused = [row for row in result["entries"] if not row["ok"]]
    assert {row["op"] for row in refused} == {"redact", "checkpoint", "set_role", "issue_key"}, \
        "refused changes are on the record; refused reads are not changes"
    assert all(row["role"] == "pricing" for row in refused)


def test_takedown_end_to_end_keeps_history_verifiable(games, tmp_path):
    site, owner, pm, http = games
    fetch = fetcher(http)

    # The product manager works normally; an earlier description turns out to need removing.
    assert http.post("/admin/games/change", json={"key": "GAME-1", "set": {"price": "49.99"}},
                     headers=auth(pm)).status_code == 200
    assert http.post("/admin/games/change", json={"key": "GAME-4", "set": {"description": SECRET}},
                     headers=auth(owner)).status_code == 200

    # A visitor verifies the site and saves a pin before the takedown.
    http.post("/admin/checkpoint", headers=auth(owner))
    before = verify_site(fetch)
    assert before["ok"] and before["redacted"] == 0
    pin = before["pin"]

    seq = next(e["seq"] for e in log(site) if e.get("values", {}).get("description") == SECRET)
    r = http.post("/admin/redact", json={"seq": seq}, headers=auth(owner))
    assert r.status_code == 409 and r.json()["error"]["code"] == "still_current"
    http.post("/admin/games/change", json={"key": "GAME-4", "set": {"description": "A calm puzzle game."}},
              headers=auth(owner))
    assert on_disk(site, SECRET)
    r = http.post("/admin/redact", json={"seq": seq, "fields": ["description"], "reason": "Court order 12/2026"},
                  headers=auth(owner))
    assert r.status_code == 200

    # Gone from every read, and from the files on disk, even though it went through the audited door.
    everything = json.dumps([fetch("/history", {"limit": 1000}), fetch(f"/history/{seq}"),
                             fetch("/collections/games/GAME-4/history"), fetch("/changes")])
    assert SECRET not in everything and not on_disk(site, SECRET)
    marker = fetch(f"/history/{seq}")["redacted"]["description"]
    notice = fetch(f"/history/{marker['by']}")
    assert notice["op"] == "redact" and notice["detail"]["reason"] == "Court order 12/2026"

    # A redaction is not a rewrite: the earlier pin still holds and the chain still verifies.
    after = verify_site(fetch, pin)
    assert after["ok"], after["problems"]
    assert after["redacted"] == 1 and after["entries"] == before["entries"] + 2

    # The public sees that something was removed and why, never who did it; the owner's audit shows who.
    assert "owner" not in json.dumps(notice) and pm[:12] not in everything
    _, trail = mcp_call(site, "audit", {})
    who = {(row["op"], row["who"]) for row in trail["entries"] if row["ok"]}
    assert ("redact", "owner") in who and ("checkpoint", "owner") in who
    redaction = next(row for row in trail["entries"] if row["op"] == "redact" and row["ok"])
    assert redaction["detail"] == {"seq": seq, "fields": ["description"]}, "the audit says what was removed"
    assert any(op == "change" and w not in ("owner", "local") for op, w in who), "the role key's change is attributed"
    assert SECRET not in json.dumps(trail), "the audit trail records field names, not values"


def test_redaction_through_the_command_line_also_scrubs_the_disk(games):
    site, _, _, _ = games
    site["games"].change("GAME-5", description=SECRET)
    site["games"].change("GAME-5", description="Replaced")
    seq = next(e["seq"] for e in log(site) if e.get("values", {}).get("description") == SECRET)
    assert on_disk(site, SECRET)
    out = io.StringIO()
    assert main(["redact", str(seq), "-s", str(site.dir)], clock=site.clock, out=out) == 0
    assert not on_disk(site, SECRET)


def test_rewriting_history_instead_of_redacting_is_caught(games, tmp_path, clock):
    site, owner, _, http = games
    pin = verify_site(fetcher(http))["pin"]
    # Someone edits a past value in place and recomputes nothing: the commitment no longer matches.
    seq = next(e["seq"] for e in log(site) if e.get("values", {}).get("name") == "Starfall")
    changed = site.store.x("UPDATE log_values SET value_json=? WHERE seq=? AND field='name'",
                           (json.dumps("Edited"), seq))
    assert changed.rowcount == 1
    report = verify_site(fetcher(http), pin)
    assert not report["ok"] and any("commitment" in p for p in report["problems"])


def test_role_key_changes_are_logged_publicly_like_any_other(games):
    site, owner, pm, http = games
    http.post("/admin/games/change", json={"key": "GAME-2", "set": {"price": "44.99"}}, headers=auth(pm))
    entry = http.get("/collections/games/GAME-2/history").json()["entries"][-1]
    assert entry["values"] == {"price": "44.99"}
    assert "wholesale_cost" not in http.get("/collections/games/GAME-2", headers=auth(pm)).text
