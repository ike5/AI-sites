"""The MCP door: the operation table exposed as a small, fixed set of tools.

Tools are generated from `ops.OPS`, one per operation rather than one per structure, so the tool list
stays the same size however large the site grows. Run locally over stdio with `aisites mcp <site>`
(no sign-in), or reach a served site at /mcp with the owner key or a role key as a bearer token.
"""

import json
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from . import ops
from .errors import SiteError
from .site import Site, VERSION

INSTRUCTIONS = (
    "Tools for editing one AI-sites site. Call `describe` first to see its structures and the built-in "
    "kinds. Create structures with `define`, then use `add`, `change`, `remove` on records, `schedule` for "
    "changes that start or revert on a date, and `confirm` when information is still true. Every tool "
    "returns the new state. Errors carry a `hint` saying what to do instead."
)


def build_server(site: Site, principal: ops.Principal = ops.LOCAL) -> Server:
    """Every caller sees the same tools; each call is checked against `principal`."""

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[
            types.Tool(name=op.name, description=op.summary, input_schema=op.schema())
            for op in ops.OPS.values()])

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            result = await anyio.to_thread.run_sync(
                ops.run, site, principal, params.name, dict(params.arguments or {}))
        except SiteError as e:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(e.as_dict()))], is_error=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))])

    return Server("aisites", version=VERSION, title=site.config.get("name"), instructions=INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def run_stdio(site: Site) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(site)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


class McpHttp:
    """ASGI app for remote MCP. The owner key or a role key gets in; each request is handled statelessly."""

    def __init__(self, site: Site):
        self.site = site

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
        principal = await anyio.to_thread.run_sync(self.site.resolve, token)
        if principal is None:
            body = json.dumps(SiteError(
                "needs_key", "The MCP door needs the site owner's key or a role key.",
                "Send it as: Authorization: Bearer <key>").as_dict()).encode()
            # A host that offers a sign-in flow for assistants says where this door's discovery document is.
            found = scope.get("aisites.resource_metadata")
            challenge = f'Bearer resource_metadata="{found}"' if found else "Bearer"
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", challenge.encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        manager = StreamableHTTPSessionManager(
            app=build_server(self.site, principal), stateless=True, json_response=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        async with manager.run():
            await manager.handle_request(scope, receive, send)
