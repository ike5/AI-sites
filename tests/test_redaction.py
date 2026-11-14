import io
import json

import pytest

from aisites.cli import main
from aisites.errors import SiteError
from tests.util import assert_chain_ok, auth, client, code, log, mcp_call

TOMORROW = "2026-11-15"


@pytest.fixture
def shop(site, clock):
    """A product whose first price (entry 3) has since been replaced."""
    site.define("products", kind="Product")
    site["products"].add(sku="A", name="Thing", price="10.00")
    clock.advance(hours=1)
    site["products"].change("A", price="12.00")
    return site


def seq_of(site, op, path):
    return next(e["seq"] for e in log(site) if e["op"] == op and e["path"] == path)


def stored(site, seq):
    return [r["field"] for r in site.store.q("SELECT field FROM log_values WHERE seq=?", (seq,))]


# ------------------------------------------------------------ the basics

def test_redacting_one_field_keeps_the_rest_and_the_chain(shop):
    first = seq_of(shop, "set", "collections/products/A")
    result = shop.redact(first, ["price"], "legal takedown")
    entry = result["entry"]
    assert entry["values"] == {"sku": "A", "name": "Thing"}
    assert set(entry["salts"]) == {"sku", "name"}
    assert set(entry["commit"]) == {"sku", "name", "price"}, "the commitment stays"
    assert entry["redacted"]["price"] == {"at": shop.now_z(), "by": result["redact_seq"]}
    assert_chain_ok(shop)


def test_value_is_gone_from_every_history_view_and_the_table(shop):
    first = seq_of(shop, "set", "collections/products/A")
    assert "10.00" in client(shop).get("/history").text
    shop.redact(first, ["price"])
    http = client(shop)
    for url in ("/history", f"/history/{first}", "/collections/products/A/history"):
        body = http.get(url)
        assert body.status_code == 200
        assert "10.00" not in body.text, url
    assert "price" in http.get(f"/history/{first}").json()["redacted"]
    assert sorted(stored(shop, first)) == ["name", "sku"]
    assert shop.store.one("SELECT 1 FROM log_values WHERE seq=? AND field='price'", (first,)) is None
    assert "10.00" not in json.dumps(log(shop))


def test_redacting_every_field_by_default(shop):
    shop["products"].change("A", name="Other")
    shop["products"].remove("A")
    first = seq_of(shop, "set", "collections/products/A")
    entry = shop.redact(first)["entry"]
    assert set(entry["redacted"]) == {"sku", "name", "price"}
    assert "values" not in entry
    assert_chain_ok(shop)


def test_redact_entry_shape_and_reason(shop):
    first = seq_of(shop, "set", "collections/products/A")
    result = shop.redact(first, ["price"], "  Takedown\n   request  " + "x" * 300)
    entry = log(shop)[-1]
    assert entry["seq"] == result["redact_seq"]
    assert (entry["op"], entry["path"], entry["commit"]) == ("redact", "collections/products/A", {})
    assert entry["detail"]["target"] == first and entry["detail"]["fields"] == ["price"]
    reason = entry["detail"]["reason"]
    assert reason.startswith("Takedown request x") and "\n" not in reason and len(reason) == 200

    shop["products"].change("A", price="13.00")
    shop.redact(first + 1, ["price"])
    assert "reason" not in log(shop)[-1]["detail"]
    shop["products"].change("A", name="Renamed")
    shop.redact(first, ["name"], "   ")
    assert "reason" not in log(shop)[-1]["detail"]
    assert any(c["path"] == "collections/products/A" and c["what"] == "history redacted"
               for c in shop.changes()["changes"])
    assert_chain_ok(shop)


def test_redaction_moves_the_etag(shop):
    before = shop.etag()
    shop.redact(seq_of(shop, "set", "collections/products/A"), ["price"])
    assert shop.etag() != before


# ------------------------------------------------------------ refusing current values

def test_refused_while_a_record_field_is_current(shop):
    first = seq_of(shop, "set", "collections/products/A")
    with pytest.raises(SiteError) as e:
        shop.redact(first)
    assert code(e) == "still_current" and e.value.status == 409
    assert "sku" in e.value.message and "price" not in e.value.message and e.value.hint
    assert set(stored(shop, first)) == {"sku", "name", "price"}
    assert len(log(shop)) == 4, "a refusal writes nothing"
    shop["products"].change("A", name="Renamed")
    with pytest.raises(SiteError):
        shop.redact(first, ["sku"])
    shop.redact(first, ["name"])
    shop["products"].remove("A")
    shop.redact(first, ["sku"])


def test_refused_while_a_tree_node_value_is_current(site):
    site.define("genres", type="tree")
    site["genres"].add(id="action", label="Action")
    site["genres"].add(id="racing", label="Racing", parent="action")
    first = seq_of(site, "set", "trees/genres/racing")
    with pytest.raises(SiteError) as e:
        site.redact(first, ["label"])
    assert code(e) == "still_current"
    with pytest.raises(SiteError) as e:
        site.redact(first, ["parent"])
    assert code(e) == "still_current", "the parent is part of what the tree serves"
    site["genres"].change("racing", label="Motor racing")
    site.redact(first, ["label"])
    site["genres"].remove("racing")
    entry = site.redact(first, ["parent"])["entry"]
    assert set(entry["redacted"]) == {"label", "parent"}
    assert_chain_ok(site)


def test_refused_while_a_scheduled_change_still_exists(shop):
    shop["products"].schedule("A", {"price": "8.00"}, start=TOMORROW, announce=True)
    entry = next(e for e in log(shop) if e["op"] == "schedule")
    with pytest.raises(SiteError) as e:
        shop.redact(entry["seq"], ["price"])
    assert code(e) == "still_current"
    shop["products"].unschedule("A", 1)
    shop.redact(entry["seq"], ["price"])
    assert_chain_ok(shop)


def test_refused_while_a_schema_definition_or_site_setting_is_current(site):
    site.define("faq", fields=["answer:string"])
    definition = seq_of(site, "set", "schemas/faq")
    with pytest.raises(SiteError) as e:
        site.redact(definition, ["definition"])
    assert code(e) == "still_current"
    site.alter("faq", add=["extra:string"])
    site.redact(definition, ["definition"])

    with pytest.raises(SiteError) as e:
        site.redact(1, ["name"])
    assert code(e) == "still_current"
    site.configure(name="New name")
    entry = site.redact(1, ["name"])["entry"]
    assert "name" not in entry["values"] and entry["detail"]["public_key"] == site.public_key
    site.configure(description="x")
    config_entry = log(site)[-1]["seq"]
    site.configure(description="y")
    site.redact(config_entry, ["description"])
    assert_chain_ok(site)


# ------------------------------------------------------------ sealed entries

def hidden_schedule(site):
    site.define("products", kind="Product")
    site["products"].add(sku="A", name="Thing", price="10.00")
    site["products"].schedule("A", {"price": "7.77"}, start=TOMORROW)
    return next(e["seq"] for e in log(site) if e["op"] == "sealed")


def test_sealed_entry_redacted_before_it_would_be_revealed(site, clock):
    seq = hidden_schedule(site)
    with pytest.raises(SiteError) as e:
        site.redact(seq)
    assert code(e) == "still_current", "the schedule is still planned"
    site["products"].unschedule("A", 1)
    result = site.redact(seq, reason="cancelled plan")
    assert result["entry"]["redacted"]["sealed"]["by"] == result["redact_seq"]
    clock.advance(days=3)
    for entry in (site.history.entry(seq), *[e for e in log(site) if e["seq"] == seq]):
        assert "revealed" not in entry and "salt" not in entry
    assert "7.77" not in json.dumps(log(site)) and "7.77" not in client(site).get("/history").text
    row = site.store.one("SELECT * FROM log_sealed WHERE seq=?", (seq,))
    assert row["reveal_at"] is None and "7.77" not in row["inner_json"] and row["salt"] == "" and row["path"] == ""
    assert_chain_ok(site)


def test_sealed_entry_redacted_after_it_was_revealed(site, clock):
    seq = hidden_schedule(site)
    clock.advance(days=2)
    assert site.history.entry(seq)["revealed"]["values"] == {"price": "7.77"}
    assert "7.77" in client(site).get("/collections/products/A/history").text
    site["products"].unschedule("A", 1)
    site.redact(seq)
    assert "revealed" not in site.history.entry(seq)
    assert "7.77" not in client(site).get("/collections/products/A/history").text
    assert_chain_ok(site)


def test_sealed_entry_rules(site):
    seq = hidden_schedule(site)
    site["products"].unschedule("A", 1)
    with pytest.raises(SiteError) as e:
        site.redact(seq, ["sealed"])
    assert code(e) == "invalid"
    site.redact(seq)
    with pytest.raises(SiteError) as e:
        site.redact(seq)
    assert code(e) == "already_redacted"


def test_hidden_record_is_refused_while_it_exists(site):
    site.define("notices", kind="Notice")
    site["notices"].add(id="n1", title="Later", valid_from="2026-12-01T00:00")
    seq = next(e["seq"] for e in log(site) if e["op"] == "sealed")
    with pytest.raises(SiteError) as e:
        site.redact(seq)
    assert code(e) == "still_current"
    site["notices"].remove("n1")
    site.redact(seq)
    assert_chain_ok(site)


# ------------------------------------------------------------ errors

def test_nothing_to_redact_unknown_and_double(shop):
    shop["products"].confirm("A")
    shop["products"].remove("A")
    for op in ("confirm", "delete"):
        with pytest.raises(SiteError) as e:
            shop.redact(seq_of(shop, op, "collections/products/A"))
        assert code(e) == "nothing_to_redact"
    first = seq_of(shop, "set", "collections/products/A")
    shop.redact(first, ["price"])
    redaction = log(shop)[-1]["seq"]
    with pytest.raises(SiteError) as e:
        shop.redact(redaction)
    assert code(e) == "nothing_to_redact"

    with pytest.raises(SiteError) as e:
        shop.redact(first, ["colour"])
    assert code(e) == "unknown_field" and "sku" in e.value.hint
    with pytest.raises(SiteError) as e:
        shop.redact(first, ["price"])
    assert code(e) == "already_redacted" and "sku" in e.value.hint
    shop.redact(first)
    with pytest.raises(SiteError) as e:
        shop.redact(first)
    assert code(e) == "nothing_to_redact"
    with pytest.raises(SiteError) as e:
        shop.redact(999)
    assert code(e) == "not_found"
    with pytest.raises(SiteError) as e:
        shop.redact(first, [])
    assert code(e) == "invalid"
    assert_chain_ok(shop)


def test_private_structures_leave_nothing_to_redact(site):
    site.define("calendar", kind="Event", access="private")
    site["calendar"].add(title="Dentist", start="2026-11-20T09:00")
    assert [e["path"] for e in log(site)] == ["site"]
    with pytest.raises(SiteError) as e:
        site.redact(2)
    assert code(e) == "not_found"


def test_verify_flags_a_marker_for_an_uncommitted_field(shop):
    from aisites.history import verify_entries
    shop.redact(seq_of(shop, "set", "collections/products/A"), ["price"])
    entries = log(shop)
    entries[2]["redacted"]["colour"] = {"at": "x", "by": 5}
    assert any("colour" in p for p in verify_entries(entries))


# ------------------------------------------------------------ the four doors

def test_through_the_command_line(shop, clock):
    first = seq_of(shop, "set", "collections/products/A")
    out = io.StringIO()
    argv = ["redact", str(first), "--fields", "price", "--reason", "takedown", "-s", str(shop.dir)]
    assert main(argv, clock=clock, out=out) == 0
    result = json.loads(out.getvalue())
    assert result["entry"]["redacted"]["price"]["by"] == result["redact_seq"]
    assert log(shop)[-1]["detail"] == {"target": first, "fields": ["price"], "reason": "takedown"}
    assert_chain_ok(shop)


def test_through_the_admin_api(made):
    site, key = made
    site.define("products", kind="Product")
    site["products"].add(sku="A", name="Thing", price="10.00")
    site["products"].change("A", price="12.00")
    first = seq_of(site, "set", "collections/products/A")
    http = client(site)
    assert http.post("/admin/redact", json={"seq": first, "fields": ["price"]}).status_code == 401
    r = http.post("/admin/redact", json={"seq": first, "fields": ["price"], "reason": "legal"}, headers=auth(key))
    assert r.status_code == 200, r.text
    assert "price" in r.json()["entry"]["redacted"]
    r = http.post("/admin/redact", json={"seq": first}, headers=auth(key))
    assert r.status_code == 409 and r.json()["error"]["code"] == "still_current"
    assert_chain_ok(site)


def test_through_mcp(shop):
    first = seq_of(shop, "set", "collections/products/A")
    is_error, result = mcp_call(shop, "redact", {"seq": first, "fields": ["price"], "reason": "legal"})
    assert not is_error and "price" in result["entry"]["redacted"]
    is_error, result = mcp_call(shop, "redact", {"seq": first})
    assert is_error and result["error"]["code"] == "still_current"
    assert_chain_ok(shop)
