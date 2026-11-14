import asyncio
import json

from fastapi.testclient import TestClient
from mcp import Client

from aisites.api import create_app
from aisites.history import verify_entries
from aisites.mcp_door import build_server


def client(site):
    return TestClient(create_app(site))


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def mcp_call(site, tool, args):
    """Call one MCP tool in-process. Returns (is_error, parsed result)."""
    async def go():
        async with Client(build_server(site)) as c:
            res = await c.call_tool(tool, args)
            return bool(res.is_error), json.loads(res.content[0].text)
    return asyncio.run(go())


def mcp_tools(site):
    async def go():
        async with Client(build_server(site)) as c:
            return (await c.list_tools()).tools
    return asyncio.run(go())


def log(site):
    return site.history.entries(limit=1000)


def assert_chain_ok(site):
    assert verify_entries(log(site)) == []


def code(excinfo):
    return excinfo.value.code
