"""The same script of operations through each of the four doors must leave identical sites."""

import io
import json

import pytest

from aisites import Site
from aisites.cli import main
from tests.util import auth, client, log, mcp_call, mcp_tools

SCRIPT = [
    ("define", {"target": "genres", "type": "tree"}),
    ("add", {"target": "genres", "data": {"id": "action", "label": "Action"}}),
    ("add", {"target": "genres", "data": {"id": "racing", "label": "Racing"}, "parent": "action"}),
    ("add", {"target": "genres", "data": {"id": "puzzle", "label": "Puzzle"}}),
    ("move", {"target": "genres", "key": "racing", "parent": "puzzle"}),
    ("define", {"target": "games", "kind": "Product", "fields": ["cost:decimal:private", "tags:string[]"],
                "refs": {"category": "genres"}}),
    ("add", {"target": "games", "data": {"sku": "G1", "name": "Starfall", "price": "59.99", "stock": 10,
                                         "category": "racing", "cost": "20", "tags": ["new", "hot"]}}),
    ("add", {"target": "games", "data": {"sku": "G2", "name": "Sundial", "price": "39.99"}}),
    ("change", {"target": "games", "key": "G2", "set": {"price": "34.99", "stock": 5}}),
    ("schedule", {"target": "games", "key": "G1", "set": {"price": "47.99"}, "on": "2026-12-15"}),
    ("schedule", {"target": "games", "key": "G2", "set": {"price": "29.99"}, "start": "2026-12-01",
                  "end": "2026-12-08", "announce": True}),
    ("unschedule", {"target": "games", "key": "G2", "schedule": 2}),
    ("confirm", {"target": "games", "key": "G1"}),
    ("alter", {"target": "games", "add": ["platform:string"], "description": "Our games"}),
    ("change", {"target": "games", "key": "G1", "set": {"platform": "PC"}}),
    ("define", {"target": "notices", "kind": "Notice"}),
    ("add", {"target": "notices", "data": {"id": "n1", "title": "Sale soon", "valid_until": "2026-12-01T00:00"}}),
    ("remove", {"target": "games", "key": "G2"}),
    ("configure", {"description": "A games site"}),
]


def via_python(site, key, op, args):
    args = dict(args)
    if op == "configure":
        return site.configure(**args)
    return getattr(site, op)(args.pop("target"), **args)


def via_admin(site, key, op, args):
    args = dict(args)
    target = args.pop("target", None)
    url = f"/admin/{target}/{op}" if target else f"/admin/{op}"
    r = client(site).post(url, json=args, headers=auth(key))
    assert r.status_code == 200, r.text
    return r.json()


def via_mcp(site, key, op, args):
    is_error, result = mcp_call(site, op, args)
    assert not is_error, result
    return result


def cli_argv(op, args):
    """Turn operation arguments into a command line, using name=value pairs where the CLI offers them."""
    args = dict(args)
    argv = [op]
    for name in ("target", "key"):
        if name in args:
            argv.append(args.pop(name))
    if op == "unschedule":
        argv.append(str(args.pop("schedule")))
    for name in ("data", "set"):
        if name in args:
            for field, value in args.pop(name).items():
                argv.append(f"{field}={value}" if isinstance(value, str) else f"{field}:={json.dumps(value)}")
    if op == "configure":
        argv += [f"{k}={v}" for k, v in args.items()]
        return argv
    for name, value in args.items():
        flag = {"fields": "--field", "refs": "--ref"}.get(name, f"--{name}")
        if value is True:
            argv.append(flag)
        elif isinstance(value, dict):
            for k, v in value.items():
                argv += [flag, f"{k}={v}"]
        elif isinstance(value, list):
            for item in value:
                argv += [flag, item]
        else:
            argv += [flag, str(value)]
    return argv


def via_cli(site, key, op, args):
    out = io.StringIO()
    status = main(cli_argv(op, args) + ["-s", str(site.dir)], clock=site.clock, out=out)
    assert status == 0, out.getvalue()
    return json.loads(out.getvalue())


DOORS = {"python": via_python, "cli": via_cli, "admin": via_admin, "mcp": via_mcp}


def snapshot(site):
    """Everything observable about a site, minus the random salts and the hashes that depend on them."""
    entries = [{k: e[k] for k in ("seq", "time", "op", "path") if k in e}
               | {"values": e.get("values"), "fields": sorted(e["commit"]), "revealed": e.get("revealed"),
                  "detail": {k: v for k, v in e["detail"].items() if k != "public_key"}}  # random per site
               for e in log(site)]
    described = site.describe()
    state = {}
    for s in described["structures"]:
        state[s["name"]] = site.read_tree(s["name"])["nodes"] if s["type"] == "tree" \
            else site.list(s["name"])["items"]
    return {"site": described["site"], "structures": described["structures"], "state": state,
            "log": entries, "changes": site.changes()["changes"]}


def test_all_four_doors_leave_identical_sites(tmp_path, clock):
    results, snapshots = {}, {}
    for door, call in DOORS.items():
        clock.set(2026, 11, 14, 18, 0)
        site, key = Site.create(tmp_path / door, name="Parity", timezone="America/Los_Angeles", clock=clock)
        results[door] = []
        for op, args in SCRIPT:
            clock.advance(minutes=1)
            results[door].append(call(site, key, op, args))
        clock.set(2026, 12, 15, 20, 0)  # the hidden discount has started and been revealed
        snapshots[door] = snapshot(site)

    for door in ("cli", "admin", "mcp"):
        assert results[door] == results["python"], f"{door} returned something different from python"
        assert snapshots[door] == snapshots["python"], f"{door} left a different site from python"
    final = snapshots["python"]
    assert [i["key"] for i in final["state"]["games"]] == ["G1"]
    assert final["state"]["games"][0]["data"]["price"] == "47.99"
    assert any(e["op"] == "sealed" and e["revealed"] for e in final["log"])


def test_mcp_tools_mirror_the_operation_table(site):
    from aisites import ops
    tools = {t.name: t for t in mcp_tools(site)}
    assert set(tools) == set(ops.OPS)
    assert tools["schedule"].input_schema["required"] == ["target", "key", "set"]
    is_error, result = mcp_call(site, "get", {"target": "nothing", "key": "x"})
    assert is_error and result["error"]["code"] == "not_found" and "define" in result["error"]["hint"]


def test_cli_reports_errors_as_json(site, capsys):
    assert main(["get", "nothing", "x", "-s", str(site.dir)]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"
    assert main(["add", "nothing", "not-a-pair", "-s", str(site.dir)]) == 1
    assert "name=value" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert main(["guide"]) == 0
    assert "# Authoring an AI-sites site" in capsys.readouterr().out


def test_cli_creates_sites_and_rotates_keys(tmp_path, capsys):
    target = str(tmp_path / "new")
    assert main(["site", "create", target, "--name", "Fresh", "--timezone", "Europe/Madrid"]) == 0
    created = json.loads(capsys.readouterr().out)
    site = Site(target)
    assert site.check_owner(created["owner_key"]) and site.config["timezone"] == "Europe/Madrid"
    assert main(["site", "key", target]) == 0
    rotated = json.loads(capsys.readouterr().out)["owner_key"]
    assert site.check_owner(rotated) and not site.check_owner(created["owner_key"])
    assert main(["site", "create", target, "--name", "Again"]) == 1
    assert main(["site", "create", str(tmp_path / "bad"), "--name", "x", "--timezone", "PST"]) == 1
