import copy

from aisites import canon
from aisites.history import verify_entries
from tests.util import assert_chain_ok, log


def build(site):
    site.define("products", kind="Product", fields=["cost:decimal:private"])
    site["products"].add(sku="A", name="Thing", price="10.00", stock=4, cost="3")
    site["products"].change("A", price="12.00")
    site["products"].change("A", stock=3)
    site["products"].remove("A")
    return log(site)


def test_log_records_only_what_policy_allows(site):
    entries = build(site)
    assert [(e["op"], e["path"]) for e in entries] == [
        ("genesis", "site"), ("set", "schemas/products"), ("set", "collections/products/A"),
        ("set", "collections/products/A"), ("delete", "collections/products/A")]
    assert entries[2]["values"] == {"sku": "A", "name": "Thing", "price": "10.00"}
    assert entries[3]["values"] == {"price": "12.00"}, "entries are deltas"
    assert entries[0]["detail"]["public_key"] == site.public_key
    assert site.first_published() == entries[0]["time"]
    assert_chain_ok(site)


def test_tampering_is_detected(site):
    entries = build(site)

    edited = copy.deepcopy(entries)
    edited[3]["values"]["price"] = "1.00"
    assert any("does not match its commitment" in p for p in verify_entries(edited))

    dropped = entries[:2] + entries[3:]
    assert verify_entries(dropped)

    rewritten = copy.deepcopy(entries)
    rewritten[2]["time"] = "2020-01-01T00:00:00Z"
    assert any("hash does not match" in p for p in verify_entries(rewritten))

    rehashed = copy.deepcopy(entries)
    rehashed[2]["time"] = "2020-01-01T00:00:00Z"
    rehashed[2]["hash"] = canon.entry_hash(rehashed[2])
    assert any("does not link" in p for p in verify_entries(rehashed)), "the next entry exposes the rewrite"


def test_private_and_invite_structures_stay_out_of_the_log(site):
    site.define("calendar", kind="Event", access="private")
    site["calendar"].add(title="Dentist", start="2026-11-20T09:00")
    site.define("members", access="invite", fields=["note:string"])
    site["members"].add(id="m1", note="hello")
    site["calendar"].confirm(site["calendar"].list()["items"][0]["key"])
    assert [e["path"] for e in log(site)] == ["site"]
    assert site.changes()["changes"] == []


def test_making_a_structure_private_is_recorded_without_its_content(site):
    site.define("notes", fields=["text:string"])
    site.alter("notes", access="private")
    site["notes"].add(text="secret")
    entries = log(site)
    assert [(e["op"], e["path"]) for e in entries][-2:] == [("set", "schemas/notes"), ("delete", "schemas/notes")]
    assert "secret" not in str(entries)


def test_history_paging_and_filters(site, clock):
    site.define("products", kind="Product")
    for i in range(5):
        clock.advance(hours=1)
        site["products"].add(sku=f"P{i}", name=f"Product {i}")
    h = site.history
    assert [e["seq"] for e in h.entries(after=2, limit=2)] == [3, 4]
    assert [e["path"] for e in h.entries(path="collections/products/P3")] == ["collections/products/P3"]
    assert len(h.entries(path="collections/products")) == 5
    assert len(h.entries(path="collections/prod")) == 0, "a path filter matches whole segments"
    assert [e["seq"] for e in h.entries(since="2026-11-14T22:00:00Z")] == [6, 7]
    assert h.entry(1)["op"] == "genesis" and h.entry(99) is None


def test_private_field_names_are_not_published(site):
    site.define("products", kind="Product", fields=["cost:decimal:private"])
    before = len(log(site))
    site.alter("products", add=["supplier:string:private"])
    assert len(log(site)) == before, "adding a private field changes nothing the public can see"
    site.alter("products", add=["colour:string"])
    assert len(log(site)) == before + 1
    text = str(log(site)) + str(site.changes())
    assert "cost" not in text and "supplier" not in text and "colour" in text
