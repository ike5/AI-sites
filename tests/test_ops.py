import pytest

from aisites import SiteError, ops
from tests.util import code, log


@pytest.fixture
def shop(site):
    site.define("categories", type="tree")
    site.define("products", kind="Product", refs={"category": "categories"},
                fields=["cost:decimal:private", "tags:string[]"])
    site["categories"].add({"id": "bikes", "label": "Bikes"})
    site["products"].add(sku="B1", name="Road bike", price="499.00", stock=3, category="bikes", cost="250")
    return site


def test_add_get_change_remove_rules(shop):
    with pytest.raises(SiteError) as e:
        shop["products"].add(sku="B1", name="Again")
    assert code(e) == "exists" and e.value.status == 409
    with pytest.raises(SiteError) as e:
        shop["products"].change("NOPE", price="1")
    assert code(e) == "not_found"
    with pytest.raises(SiteError) as e:
        shop["products"].remove("NOPE")
    assert code(e) == "not_found"
    with pytest.raises(SiteError) as e:
        shop["products"].change("B1", sku="B2")
    assert "key cannot be changed" in e.value.message

    changed = shop["products"].change("B1", price="549.00")
    assert changed["data"]["price"] == "549.00"
    assert shop["products"].remove("B1") == {"removed": "B1"}
    with pytest.raises(SiteError):
        shop["products"].get("B1")


def test_missing_key_is_generated(shop):
    made = shop["products"].add(name="No sku given")
    assert made["key"] and made["data"]["sku"] == made["key"]


def test_validation_errors_say_what_to_do(shop):
    with pytest.raises(SiteError) as e:
        shop["products"].add(sku="B2", name="Bad", price="not a number")
    assert code(e) == "invalid" and "price" in e.value.message and "price (decimal)" in e.value.hint
    with pytest.raises(SiteError) as e:
        shop["products"].add(sku="B3", name="Bad", colour="red")
    assert "colour" in e.value.message
    with pytest.raises(SiteError) as e:
        shop["products"].add(sku="B4")
    assert "name" in e.value.message
    with pytest.raises(SiteError) as e:
        shop["products"].add(sku="has space", name="x")
    assert code(e) == "bad_key"


def test_values_are_coerced_the_same_from_strings(shop):
    made = shop["products"].add(sku="B5", name="From strings", price="10", stock="7", tags=["a", "b"])
    assert made["data"]["stock"] == 7 and made["data"]["price"] == "10"


def test_choices_are_enforced(shop):
    with pytest.raises(SiteError) as e:
        shop["products"].add(sku="B6", name="x", category="boats")
    assert code(e) == "bad_choice" and "categories" in e.value.hint
    with pytest.raises(SiteError) as e:
        shop["categories"].remove("bikes")
    assert code(e) == "in_use"
    with pytest.raises(SiteError) as e:
        shop.drop("categories")
    assert code(e) == "in_use"


def test_private_and_live_fields(shop):
    owner_view = shop["products"].get("B1")
    assert owner_view["data"]["cost"] == "250"
    public = shop.read_record("products", "B1")
    assert "cost" not in public["data"] and public["live_as_of"]
    with pytest.raises(SiteError) as e:
        shop.list("products", {"cost.gt": 1}, owner=False)
    assert code(e) == "bad_filter"

    before = len(log(shop))
    updated = public["updated"]
    shop.clock.advance(hours=1)
    after = shop["products"].change("B1", stock=2)
    assert after["live_as_of"] > public["live_as_of"]
    assert after["updated"] == updated, "a live value changing is not a content update"
    assert len(log(shop)) == before, "live values are not logged"

    logged_fields = {f for entry in log(shop) for f in entry.get("values", {})}
    assert "cost" not in logged_fields and "stock" not in logged_fields


def test_alter_and_drop(shop):
    shop.alter("products", add=["colour:string"])
    shop["products"].change("B1", colour="red")
    shop.alter("products", remove=["colour"])
    assert "colour" not in shop["products"].get("B1")["data"]
    shop["products"].change("B1", name="Still valid after the field went away")
    with pytest.raises(SiteError) as e:
        shop.alter("products", add=["weight!:number"])
    assert "required" in e.value.message
    with pytest.raises(SiteError) as e:
        shop.alter("products", remove=["sku"])
    assert "key field" in e.value.message
    assert shop.drop("products") == {"dropped": "products"}
    with pytest.raises(SiteError):
        shop.describe("products")


def test_define_errors(site):
    with pytest.raises(SiteError) as e:
        site.define("things", kind="Gadget")
    assert code(e) == "bad_kind" and "Product" in e.value.hint
    with pytest.raises(SiteError) as e:
        site.define("Bad Name")
    assert code(e) == "bad_name"
    site.define("things")
    with pytest.raises(SiteError) as e:
        site.define("things")
    assert code(e) == "exists"
    with pytest.raises(SiteError) as e:
        site.define("offers", kind="Offer", refs={"product": "missing"})
    assert code(e) == "not_found"
    with pytest.raises(SiteError) as e:
        site.define("odd", fields=["size:colour"])
    assert code(e) == "bad_field"


def test_tree_operations(site):
    site.define("menu", type="tree")
    t = site["menu"]
    t.add({"id": "food", "label": "Food"})
    t.add({"id": "drinks", "label": "Drinks"})
    t.add({"label": "Hot dogs"}, parent="food")
    t.add({"id": "tacos", "label": "Tacos", "spicy": True}, parent="food", position=0)
    nodes = site.read_tree("menu")["nodes"]
    assert [n["id"] for n in nodes] == ["food", "drinks"]
    assert [n["id"] for n in nodes[0]["children"]] == ["tacos", "hot-dogs"]
    assert nodes[0]["children"][0]["spicy"] is True

    with pytest.raises(SiteError) as e:
        t.move("food", parent="tacos")
    assert code(e) == "cycle"
    t.move("tacos", parent="drinks")
    assert t.get("tacos")["parent"] == "drinks"
    t.move("drinks", position=0)
    assert [n["id"] for n in site.read_tree("menu")["nodes"]] == ["drinks", "food"]

    with pytest.raises(SiteError) as e:
        t.remove("drinks")
    assert code(e) == "has_children"
    assert t.remove("drinks", recursive=True) == {"removed": ["drinks", "tacos"]}

    shallow = site.read_tree("menu", depth=1)["nodes"]
    assert shallow[0]["children_count"] == 1 and "children" not in shallow[0]
    assert t.change("food", label="Comida")["label"] == "Comida"


def test_run_checks_arguments_and_caller(shop):
    assert ops.run(shop, ops.OWNER, "get", {"target": "products", "key": "B1"})["key"] == "B1"
    with pytest.raises(SiteError) as e:
        ops.run(shop, ops.ANONYMOUS, "get", {"target": "products", "key": "B1"})
    assert code(e) == "forbidden" and e.value.status == 403
    with pytest.raises(SiteError) as e:
        ops.run(shop, ops.OWNER, "frobnicate", {})
    assert code(e) == "unknown_operation" and "define" in e.value.hint
    with pytest.raises(SiteError) as e:
        ops.run(shop, ops.OWNER, "get", {"target": "products"})
    assert "needs: key" in e.value.message
    with pytest.raises(SiteError) as e:
        ops.run(shop, ops.OWNER, "get", {"target": "products", "key": "B1", "colour": "red"})
    assert "does not take: colour" in e.value.message
    with pytest.raises(SiteError) as e:
        ops.run(shop, ops.OWNER, "change", {"target": "products", "key": "B1", "set": "price=1"})
    assert "must be object" in e.value.message


def test_every_operation_is_described():
    described = {d["op"]: d for d in ops.describe_ops()}
    assert set(described) == set(ops.OPS)
    assert described["schedule"]["arguments"]["required"] == ["target", "key", "set"]
