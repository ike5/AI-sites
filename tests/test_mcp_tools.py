"""The MCP tool list: an assistant reads all of it before doing anything, so it holds only what is of use."""

import asyncio
import json

import pytest
from mcp import Client

from aisites import Site, ops
from aisites.mcp_door import FEATURES, build_server, features_in_use, tools_for
from tests.util import mcp_call, mcp_tools


def names(site, principal=ops.LOCAL):
    return {op.name for op in tools_for(site, principal)}


def size(site):
    return len(json.dumps([{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                           for t in mcp_tools(site)]))


def test_a_plain_site_is_shown_only_what_it_can_use(site):
    hidden = {n for group in FEATURES.values() for n in group}
    assert names(site) == set(ops.OPS) - hidden and features_in_use(site) == set()
    listed = {t.name for t in mcp_tools(site)}
    assert listed == (set(ops.OPS) - hidden) | {"other"}
    assert {"define", "add", "change", "list", "get", "schedule", "ask", "question", "set_role"} <= listed
    assert not {"reserve", "inbox", "move", "hours", "issue_key"} & listed
    assert len(listed) <= 23 and size(site) < 13500, "about a third smaller than listing every operation"


@pytest.mark.parametrize("feature, turn_on", [
    ("booking", lambda s: s.define("tables", kind="Slot", settings={"booking": {}})),
    ("form", lambda s: s.define("contact", type="form")),
    ("tree", lambda s: s.define("menu", type="tree")),
    ("hours", lambda s: s.define("hours", kind="Hours")),
    ("roles", lambda s: s.set_role("staff", [{"target": "*", "ops": ["list"]}])),
    ("contacts", lambda s: s.set_contacts(requests="invite")),
])
def test_each_feature_brings_its_own_tools(site, feature, turn_on):
    before = names(site)
    turn_on(site)
    assert features_in_use(site) == {feature}
    assert names(site) - before == set(FEATURES[feature]), "exactly that feature's tools, and no others"


def test_a_site_using_everything_lists_everything_and_needs_no_other(site):
    site.define("tables", kind="Slot", settings={"booking": {}})
    site.define("contact", type="form")
    site.define("menu", type="tree")
    site.define("hours", kind="Hours")
    site.set_role("staff", [{"target": "*", "ops": ["list"]}])
    site.set_contacts(requests="open")
    assert {t.name for t in mcp_tools(site)} == set(ops.OPS)


def test_other_lists_and_runs_what_is_hidden(site):
    is_error, listed = mcp_call(site, "other", {})
    assert not is_error
    by_name = {o["name"]: o for o in listed["operations"]}
    assert set(by_name) == {n for group in FEATURES.values() for n in group}
    assert by_name["hours"]["arguments"]["properties"]["date"] and "summary" in by_name["hours"]

    is_error, answer = mcp_call(site, "other", {"name": "hours", "arguments": {"date": "today"}})
    assert not is_error and answer["results"] == []
    is_error, answer = mcp_call(site, "other", {"name": "list_keys"})
    assert not is_error and answer["keys"] == []
    is_error, answer = mcp_call(site, "other", {"name": "nothing"})
    assert is_error and answer["error"]["code"] == "unknown_operation"
    is_error, answer = mcp_call(site, "hours", {}), None
    assert is_error[0] is False, "a hidden tool called by name still works: hiding is not refusing"


def test_turning_a_feature_on_says_which_tools_will_appear(site):
    is_error, made = mcp_call(site, "define", {"target": "menu", "type": "tree"})
    assert not is_error and made["tools_added"] == ["move"] and "other" in made["tools_note"]
    is_error, again = mcp_call(site, "define", {"target": "second", "type": "tree"})
    assert "tools_added" not in again, "said once, when the feature first appears"
    is_error, role = mcp_call(site, "set_role", {"role": "staff", "grants": [{"target": "*", "ops": ["list"]}]})
    assert role["tools_added"] == list(FEATURES["roles"])
    assert "tools_added" not in site.define("more", type="tree"), "other doors' answers are unchanged"


def test_hiding_never_changes_what_a_caller_may_do(site):
    site.define("notes", fields=["text"])
    site.set_role("reader", [{"target": "notes", "ops": ["list", "get"]}])
    key = site.resolve(site.issue_key("reader")["secret"])
    assert names(site, key) == {"list", "get", "describe"}

    async def as_role():
        async with Client(build_server(site, key)) as m:
            listed = {t.name for t in (await m.list_tools()).tools}
            hidden = await m.call_tool("other", {})
            run = await m.call_tool("other", {"name": "define", "arguments": {"target": "x"}})
            direct = await m.call_tool("issue_key", {"role": "reader"})
            return listed, hidden, run, direct

    listed, hidden, run, direct = asyncio.run(as_role())
    assert "other" not in listed, "a role's list is its grants; there is nothing more to reach"
    assert json.loads(hidden.content[0].text)["operations"] == []
    for refused in (run, direct):
        assert refused.is_error and json.loads(refused.content[0].text)["error"]["code"] == "forbidden"
