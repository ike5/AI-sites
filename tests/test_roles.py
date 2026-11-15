"""Roles limited to structures, fields and operations; keys per role; the private audit trail.

The main scenario: a game publisher's product manager may change and schedule prices on the games
collection and nothing else.
"""

import asyncio
import io
import json

import pytest
from mcp import Client

from aisites import SiteError, ops
from aisites.cli import main
from aisites.mcp_door import build_server
from examples import game_catalogue
from tests.util import auth, client, code, log

PRICING = {"target": "games", "ops": ["get", "list", "change", "schedule", "unschedule"], "fields": ["price"]}


@pytest.fixture
def games(tmp_path, clock):
    """(site, owner key, pricing key id, pricing secret, web client)."""
    site, owner_key = game_catalogue.build(tmp_path / "games", clock)
    ops.run(site, ops.OWNER, "set_role", {"role": "pricing", "grants": [PRICING],
                                          "description": "Product manager: prices only"})
    issued = ops.run(site, ops.OWNER, "issue_key", {"role": "pricing", "label": "Dana, product manager"})
    yield site, owner_key, issued["id"], issued["secret"], client(site)
    site.close()


def post(c, key, url, body=None):
    return c.post(url, json=body or {}, headers=auth(key))


def error(r, status=403):
    assert r.status_code == status, r.text
    return r.json()["error"]


# ---------------------------------------------------------------- the product manager

def test_product_manager_changes_and_schedules_prices_only(games):
    site, owner_key, key_id, secret, c = games

    r = post(c, secret, "/admin/games/change", {"key": "GAME-1", "set": {"price": "54.99"}})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["price"] == "54.99"
    assert "wholesale_cost" not in r.json()["data"], "the reply to a change hides private fields too"
    assert site.get("games", "GAME-1")["data"]["price"] == "54.99", "a role's change takes effect at once"

    r = post(c, secret, "/admin/games/schedule", {"key": "GAME-2", "set": {"price": "39.99"}, "on": "2026-11-20"})
    assert r.status_code == 200, r.text
    sid = r.json()["schedule"]["id"]
    assert "wholesale_cost" not in r.json()["record"]["data"]
    r = post(c, secret, "/admin/games/unschedule", {"key": "GAME-2", "schedule": sid})
    assert r.status_code == 200 and r.json()["unscheduled"] == sid

    e = error(post(c, secret, "/admin/games/change", {"key": "GAME-1", "set": {"name": "Starfall II"}}))
    assert e["code"] == "forbidden"
    assert e["message"] == "Role 'pricing' may not change field name in 'games'."
    assert e["hint"] == "In 'games' it may change only: price."
    e = error(post(c, secret, "/admin/games/change", {"key": "GAME-1", "set": {"price": "1", "stock": 3}}))
    assert "stock" in e["message"] and "price" not in e["message"]

    e = error(post(c, secret, "/admin/games/add", {"data": {"sku": "GAME-11", "name": "New", "price": "9.99"}}))
    assert e["message"] == "Role 'pricing' may not `add` in 'games'."
    assert "get, list, change, schedule, unschedule on 'games' (fields: price)" in e["hint"]
    assert error(post(c, secret, "/admin/games/remove", {"key": "GAME-1"}))["code"] == "forbidden"

    e = error(post(c, secret, "/admin/offers/change", {"key": "GAME-1-MX", "set": {"price": "1"}}))
    assert e["message"] == "Role 'pricing' has no access to 'offers'."
    assert error(post(c, secret, "/admin/offers/list"))["code"] == "forbidden"

    for url, body in [("/admin/reviews/define", {}), ("/admin/games/alter", {"add": ["rating:integer"]}),
                      ("/admin/games/drop", {}), ("/admin/configure", {"name": "Mine now"}),
                      ("/admin/issue_key", {"role": "pricing"}), ("/admin/audit", {}),
                      ("/admin/list_keys", {}), ("/admin/set_role", {"role": "pricing", "grants": [PRICING]}),
                      ("/admin/hours", {})]:
        e = error(post(c, secret, url, body))
        assert e["code"] == "forbidden" and "only the site owner can" in e["message"], url
    names = [s["name"] for s in site.describe()["structures"]]
    assert names == ["games", "genres", "offers"] and site.config["name"] == "Northwind Games"
    assert "rating" not in [f["name"] for f in site.describe("games")["fields"]]

    # Private fields stay private: not in get, not in list, not usable as a filter, sort or column.
    got = post(c, secret, "/admin/games/get", {"key": "GAME-3"}).json()
    assert "wholesale_cost" not in got["data"] and got["data"]["price"] == "59.99"
    assert got["schedules"][0]["set"] == {"price": "47.99"}, "the role sees the scheduled discount it may manage"
    listed = post(c, secret, "/admin/games/list").json()
    assert listed["total"] == 10 and all("wholesale_cost" not in i["data"] for i in listed["items"])
    for body in ({"where": {"wholesale_cost.lt": 100}}, {"sort": "-wholesale_cost"},
                 {"fields": ["name", "wholesale_cost"]}):
        e = error(post(c, secret, "/admin/games/list", body))
        assert "cannot see field 'wholesale_cost'" in e["message"] and "wholesale_cost" not in e["hint"]
    assert post(c, secret, "/admin/games/list", {"where": {"price.gt": 50}}).json()["total"] == 5
    e = error(post(c, secret, "/admin/games/list", {"where": {"colour": "red"}}), 400)
    assert e["code"] == "bad_filter" and "wholesale_cost" not in e["hint"]
    described = post(c, secret, "/admin/games/describe").json()
    assert "wholesale_cost" not in [f["name"] for f in described["fields"]]
    assert error(post(c, secret, "/admin/describe"))["code"] == "forbidden"

    # The owner still sees everything.
    assert "wholesale_cost" in post(c, owner_key, "/admin/games/get", {"key": "GAME-1"}).json()["data"]


def test_unschedule_needs_every_field_of_the_change(games):
    site, owner_key, key_id, secret, c = games
    sid = site.schedule("games", "GAME-1", {"price": "1.00", "name": "Promo"}, on="2026-11-20")["schedule"]["id"]
    e = error(post(c, secret, "/admin/games/unschedule", {"key": "GAME-1", "schedule": sid}))
    assert e["message"] == "Role 'pricing' may not cancel a scheduled change to field name in 'games'."
    hidden = site.schedule("games", "GAME-1", {"wholesale_cost": "1.00"}, on="2026-11-21")["schedule"]["id"]
    got = post(c, secret, "/admin/games/get", {"key": "GAME-1"}).json()
    assert hidden not in [s["id"] for s in got.get("schedules", [])], "a change of private fields only is hidden"
    assert all("wholesale_cost" not in s["set"] for s in got["schedules"])
    assert "wholesale_cost" not in json.dumps(got)


def test_product_manager_through_mcp(games):
    site, owner_key, key_id, secret, c = games
    principal = site.resolve(secret)
    assert principal == ops.Principal("role", key_id, "pricing")

    async def go():
        async with Client(build_server(site, principal)) as m:
            tools = {t.name for t in (await m.list_tools()).tools}
            out = {}
            for name, args in [
                ("change", {"target": "games", "key": "GAME-4", "set": {"price": "44.99"}}),
                ("change", {"target": "games", "key": "GAME-4", "set": {"name": "x"}}),
                ("get", {"target": "games", "key": "GAME-4"}),
                ("define", {"target": "reviews"}),
                ("remove", {"target": "games", "key": "GAME-4"}),
            ]:
                res = await m.call_tool(name, args)
                out.setdefault(name, []).append((bool(res.is_error), json.loads(res.content[0].text)))
            return tools, out

    tools, out = asyncio.run(go())
    granted = {name for g in site.role_grants("pricing") for name in g["ops"]}
    assert tools == (granted & set(ops.OPS)) | {"describe"}, "a role is shown only the tools it may use"
    assert "change" in tools and "define" not in tools and len(tools) < len(ops.OPS) / 2
    assert out["change"][0][0] is False and out["change"][0][1]["data"]["price"] == "44.99"
    assert out["change"][1][0] is True and out["change"][1][1]["error"]["code"] == "forbidden"
    assert "wholesale_cost" not in out["get"][0][1]["data"]
    assert out["define"][0][1]["error"]["code"] == "forbidden"
    assert out["remove"][0][1]["error"]["code"] == "forbidden"


def test_role_key_over_remote_mcp(games):
    site, owner_key, key_id, secret, c = games
    head = {"Accept": "application/json, text/event-stream", **auth(secret)}
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "change", "arguments": {"target": "games", "key": "GAME-5", "set": {"price": "1.00"}}}}
    r = c.post("/mcp/", json=call, headers=head)
    assert r.status_code == 200, r.text
    assert site.get("games", "GAME-5")["data"]["price"] == "1.00"
    call["params"]["arguments"]["set"] = {"stock": 1}
    assert "forbidden" in c.post("/mcp/", json=call, headers=head).text
    assert c.post("/mcp/", json=call, headers={**head, **auth("ais_key_nope")}).status_code == 401
    assert site.audit(who=key_id)["entries"][0]["ok"] is False


# ---------------------------------------------------------------- other grants

def test_wildcard_role_without_field_limit(games):
    site, owner_key, key_id, secret, c = games
    ops.run(site, ops.OWNER, "set_role", {"role": "editor", "grants": [
        {"target": "*", "ops": ["get", "list", "add", "change", "remove", "move"]}]})
    editor = ops.run(site, ops.OWNER, "issue_key", {"role": "editor"})["secret"]
    assert post(c, editor, "/admin/offers/change", {"key": "GAME-1-MX", "set": {"price": "999.00"}}).status_code == 200
    got = post(c, editor, "/admin/games/get", {"key": "GAME-1"}).json()
    assert got["data"]["wholesale_cost"] == "29.995", "no field limit: private fields are visible"
    assert post(c, editor, "/admin/games/list", {"sort": "wholesale_cost"}).status_code == 200
    assert post(c, editor, "/admin/genres/move", {"key": "racing", "parent": "strategy"}).status_code == 200
    r = post(c, editor, "/admin/games/add", {"data": {"sku": "GAME-11", "name": "New one"}})
    assert r.status_code == 200
    assert post(c, editor, "/admin/games/remove", {"key": "GAME-11"}).status_code == 200
    e = error(post(c, editor, "/admin/games/schedule", {"key": "GAME-1", "set": {"price": "1"}, "on": "tomorrow"}))
    assert e["message"] == "Role 'editor' may not `schedule` in 'games'."
    assert error(post(c, editor, "/admin/genres/drop"))["code"] == "forbidden"
    assert error(post(c, editor, "/admin/describe"))["code"] == "forbidden"


def test_field_limit_denies_remove_and_move(games):
    site, owner_key, key_id, secret, c = games
    ops.run(site, ops.OWNER, "set_role", {"role": "clerk", "grants": [
        {"target": "games", "ops": ["add", "remove", "confirm"], "fields": ["price"]},
        {"target": "genres", "ops": ["move"]}]})
    clerk = ops.run(site, ops.OWNER, "issue_key", {"role": "clerk"})["secret"]
    e = error(post(c, clerk, "/admin/games/remove", {"key": "GAME-1"}))
    assert "covers only some fields (price)" in e["message"] and "affects the whole record" in e["message"]
    e = error(post(c, clerk, "/admin/games/add", {"data": {"sku": "GAME-12", "name": "x", "price": "1"}}))
    assert e["message"] == "Role 'clerk' may not set fields name, sku in 'games'."
    assert post(c, clerk, "/admin/games/confirm", {"key": "GAME-1"}).status_code == 200
    assert post(c, clerk, "/admin/genres/move", {"key": "racing", "parent": None}).status_code == 200
    assert site.get("games", "GAME-1")["data"]["name"] == "Starfall"


def test_role_with_private_field_listed_sees_it(games):
    site, owner_key, key_id, secret, c = games
    ops.run(site, ops.OWNER, "set_role", {"role": "pricing", "grants": [
        {**PRICING, "fields": ["price", "wholesale_cost"]}]})
    got = post(c, secret, "/admin/games/get", {"key": "GAME-1"}).json()
    assert "wholesale_cost" in got["data"], "the changed role applies to the existing key at once"
    assert post(c, secret, "/admin/games/list", {"where": {"wholesale_cost.lt": 25}}).json()["total"] == 5


# ---------------------------------------------------------------- keys

def test_key_lifecycle(games):
    site, owner_key, key_id, secret, c = games
    assert secret.startswith("ais_key_")
    keys = post(c, owner_key, "/admin/list_keys").json()
    assert keys["roles"] == [{"role": "pricing", "description": "Product manager: prices only",
                              "grants": [PRICING]}]
    assert keys["keys"] == [{"id": key_id, "label": "Dana, product manager", "role": "pricing",
                             "created": "2026-11-14T18:00:00Z", "revoked": None}]
    assert secret not in json.dumps(keys) and "hash" not in json.dumps(keys)
    assert secret not in (site.dir / "site.db").read_bytes().decode("latin-1"), "only a hash is stored"

    e = error(post(c, owner_key, "/admin/drop_role", {"role": "pricing"}), 409)
    assert key_id in e["message"] and "revoke_key" in e["hint"]

    assert post(c, secret, "/admin/games/get", {"key": "GAME-1"}).status_code == 200
    principal = site.resolve(secret)
    assert post(c, owner_key, "/admin/revoke_key", {"id": key_id}).json()["revoked"] == "2026-11-14T18:00:00Z"
    assert error(post(c, secret, "/admin/games/get", {"key": "GAME-1"}), 401)["code"] == "bad_key"
    assert c.get("/collections/games", headers=auth(secret)).status_code == 401
    assert site.resolve(secret) is None
    with pytest.raises(SiteError) as e:
        ops.run(site, principal, "get", {"target": "games", "key": "GAME-1"})
    assert e.value.status == 401, "a principal held from before the revocation stops working too"

    assert post(c, owner_key, "/admin/drop_role", {"role": "pricing"}).json() == {"dropped_role": "pricing"}
    assert error(post(c, owner_key, "/admin/issue_key", {"role": "pricing"}), 404)["code"] == "not_found"
    assert error(post(c, owner_key, "/admin/revoke_key", {"id": "key_nope"}), 404)["code"] == "not_found"
    assert site.resolve(owner_key) == ops.OWNER and site.resolve("") is None and site.resolve("x") is None


def test_grant_validation(games):
    site, owner_key, key_id, secret, c = games

    def refused(grants, role="r"):
        with pytest.raises(SiteError) as e:
            ops.run(site, ops.OWNER, "set_role", {"role": role, "grants": grants})
        return e.value

    e = refused([{"target": "games", "ops": ["change", "define"]}])
    assert "define cannot be given to a role" in e.message
    assert "add, get, list, change, remove, move, schedule, unschedule, confirm, describe" in e.hint
    e = refused([{"target": "reviews", "ops": ["get"]}])
    assert "'reviews' is not a structure" in e.message and "games" in e.hint and '"*"' in e.hint
    e = refused([{"target": "games", "ops": ["get"], "fields": ["prize"]}])
    assert "prize is not a field of 'games'" in e.message and "price" in e.hint
    assert "cannot be limited to fields" in refused([{"target": "*", "ops": ["get"], "fields": ["price"]}]).message
    assert "tree" in refused([{"target": "genres", "ops": ["get"], "fields": ["label"]}]).message
    assert "unknown entries: field" in refused([{"target": "games", "ops": ["get"], "field": ["price"]}]).message
    assert "non-empty list" in refused([]).message
    assert "non-empty list" in refused([{"target": "games", "ops": []}]).message
    assert refused([{"target": "games", "ops": ["get"]}], role="Bad Name").code == "bad_name"
    with pytest.raises(SiteError) as ex:
        ops.run(site, ops.OWNER, "set_role", {"role": "r", "grants": "games"})
    assert "must be array" in ex.value.message


def test_operations_outside_the_grantable_set_are_owner_only(games):
    """Even a role whose stored grants name `define` (or an operation added later) cannot use it."""
    site, owner_key, key_id, secret, c = games
    site.store.x("UPDATE roles SET grants=? WHERE name='pricing'", (json.dumps(
        [{"target": "*", "ops": ["define", "drop", "configure", "issue_key", "audit", "hours"]}]),))
    principal = site.resolve(secret)
    for op, args in [("define", {"target": "reviews"}), ("drop", {"target": "games"}),
                     ("configure", {"name": "x"}), ("issue_key", {"role": "pricing"}), ("audit", {}),
                     ("hours", {})]:
        with pytest.raises(SiteError) as e:
            ops.run(site, principal, op, args)
        assert code(e) == "forbidden" and "only the site owner can" in e.value.message, op
    try:
        ops.OPS["later"] = ops.Op("later", "An operation added after roles.", lambda site: {"ran": True})
        with pytest.raises(SiteError) as e:
            ops.run(site, principal, "later", {})
        assert code(e) == "forbidden"
        assert ops.run(site, ops.OWNER, "later", {}) == {"ran": True}
    finally:
        del ops.OPS["later"]
    with pytest.raises(SiteError) as e:
        ops.run(site, ops.ANONYMOUS, "set_role", {"role": "x", "grants": [PRICING]})
    assert e.value.message == "This operation needs the site owner's key."


# ---------------------------------------------------------------- audit

def test_audit_trail(games):
    site, owner_key, key_id, secret, c = games
    post(c, secret, "/admin/games/change", {"key": "GAME-1", "set": {"price": "50.00"}})
    post(c, secret, "/admin/games/change", {"key": "GAME-1", "set": {"wholesale_cost": "1.23"}})
    post(c, secret, "/admin/games/get", {"key": "GAME-1"})                         # reads are not audited
    post(c, owner_key, "/admin/games/change", {"key": "GAME-2", "set": {"wholesale_cost": "9.87"}})
    post(c, owner_key, "/admin/games/change", {"key": "NOPE", "set": {"price": "1"}})
    out = io.StringIO()
    assert main(["change", "games", "GAME-3", "price=11.00", "-s", str(site.dir)], clock=site.clock, out=out) == 0

    trail = post(c, owner_key, "/admin/audit").json()["entries"]
    rows = [(e["who"], e["role"], e["op"], e["target"], e["key"], e["detail"], e["ok"]) for e in trail]
    assert rows[:5] == [
        ("local", "owner", "change", "games", "GAME-3", {"fields": ["price"]}, True),
        ("owner", "owner", "change", "games", "NOPE", {"fields": ["price"], "error": "not_found"}, False),
        ("owner", "owner", "change", "games", "GAME-2", {"fields": ["wholesale_cost"]}, True),
        (key_id, "pricing", "change", "games", "GAME-1", {"fields": ["wholesale_cost"], "error": "forbidden"},
         False),
        (key_id, "pricing", "change", "games", "GAME-1", {"fields": ["price"]}, True),
    ]
    assert rows[5][2] == "issue_key" and rows[5][5]["id"] == key_id and rows[6][2] == "set_role"
    assert len(trail) == 7
    text = json.dumps(trail)
    assert "1.23" not in text and "9.87" not in text and "50.00" not in text, "no values, private or not"
    assert secret not in text

    mine = site.audit(who=key_id)["entries"]
    assert [e["ok"] for e in mine] == [False, True]
    assert len(site.audit(target="games")["entries"]) == 5
    page = site.audit(limit=2)
    assert len(page["entries"]) == 2 and site.audit(cursor=page["next"])["entries"][0]["id"] == trail[2]["id"]

    # Revoking and dropping are audited too, and nothing of this reaches the public record.
    post(c, owner_key, "/admin/revoke_key", {"id": key_id})
    post(c, owner_key, "/admin/drop_role", {"role": "pricing"})
    assert [e["op"] for e in site.audit(limit=2)["entries"]] == ["drop_role", "revoke_key"]
    public = json.dumps([c.get("/history?limit=1000").json(), c.get("/changes?limit=1000").json(),
                         c.get("/.well-known/aisite.json").json(), c.get("/llms.txt").text,
                         c.get("/history?limit=1000", headers=auth(owner_key)).json(),
                         c.get("/changes", headers=auth(owner_key)).json(),
                         c.get("/.well-known/aisite.json", headers=auth(owner_key)).json()])
    for word in ("pricing", key_id, "audit", "set_role", "issue_key", "revoke", "drop_role", "ais_key_"):
        assert word not in public, word
    assert all(e["path"].split("/")[0] in ("site", "schemas", "collections", "trees", "sealed")
               for e in log(site))


def test_audit_through_cli_and_local_mcp(games, capsys):
    site, owner_key, key_id, secret, c = games
    grant = json.dumps({"target": "offers", "ops": ["get", "change"], "fields": ["price"]})
    assert main(["set_role", "regional", "--grant", grant, "--description", "Regional prices",
                 "-s", str(site.dir)], clock=site.clock) == 0
    assert json.loads(capsys.readouterr().out)["grants"] == [json.loads(grant)]
    assert main(["set_role", "both", "--grant", json.dumps([PRICING, json.loads(grant)]),
                 "-s", str(site.dir)], clock=site.clock) == 0
    assert len(json.loads(capsys.readouterr().out)["grants"]) == 2
    assert main(["issue_key", "regional", "--label", "MX team", "-s", str(site.dir)], clock=site.clock) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["secret"].startswith("ais_key_") and "cannot be shown again" in issued["note"]
    assert main(["revoke_key", issued["id"], "-s", str(site.dir)], clock=site.clock) == 0
    capsys.readouterr()
    assert main(["drop_role", "regional", "-s", str(site.dir)], clock=site.clock) == 0
    capsys.readouterr()
    assert main(["list_keys", "-s", str(site.dir)], clock=site.clock) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["role"] for r in listed["roles"]] == ["both", "pricing"]
    assert issued["secret"] not in json.dumps(listed)
    assert main(["set_role", "x", "--grant", "{nope", "-s", str(site.dir)], clock=site.clock) == 1
    capsys.readouterr()

    async def go():
        async with Client(build_server(site)) as m:
            await m.call_tool("confirm", {"target": "games", "key": "GAME-1"})
    asyncio.run(go())

    assert main(["audit", "--who", "local", "--limit", "3", "-s", str(site.dir)], clock=site.clock) == 0
    entries = json.loads(capsys.readouterr().out)["entries"]
    assert [(e["who"], e["op"]) for e in entries] == [("local", "confirm"), ("local", "drop_role"),
                                                      ("local", "revoke_key")]


# ---------------------------------------------------------------- public reads

def test_public_reads_with_a_role_key_get_the_public_view(games):
    site, owner_key, key_id, secret, c = games
    for url in ("/collections/games", "/collections/games/GAME-1", "/collections/games/schema",
                "/.well-known/aisite.json", "/history", "/changes"):
        with_role = c.get(url, headers=auth(secret))
        assert with_role.status_code == 200, url
        assert with_role.json() == c.get(url).json(), url
    record = c.get("/collections/games/GAME-3", headers=auth(secret)).json()
    assert "wholesale_cost" not in record["data"] and "schedules" not in record
    assert "wholesale_cost" in c.get("/collections/games/GAME-1", headers=auth(owner_key)).json()["data"]
    assert c.get("/collections/games", headers=auth("ais_key_wrong")).json()["error"]["code"] == "bad_key"
    r = c.post("/admin/games/get", json={"key": "GAME-1"}, headers=auth("ais_key_wrong"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_key"
    r = c.post("/admin/games/get", json={"key": "GAME-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "needs_key"
