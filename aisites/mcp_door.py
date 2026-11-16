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
    "Tools for editing one AI-sites site. Call `describe` first: it lists the structures in short form. "
    "Create structures with `define` (a built-in kind where one fits), then use `add`, `change`, `remove` on "
    "records, `schedule` for changes that start or revert on a date, and `confirm` when information is still "
    "true. `ask` shows what a visitor is told; `question` adds a question of your own. Every tool returns the "
    "new state. Errors carry a `hint` saying what to do instead. Tools for bookings, message forms, trees, "
    "opening hours and staff roles are listed once the site uses them; before that, reach them with `other`."
)


# Tools that only make sense once a site uses a feature. Until then they are left out of the owner's list,
# which is read in full by an assistant before it does anything, and reached through `other` instead.
FEATURES = {
    "booking": ("reserve", "book", "release", "booking", "bookings"),
    "form": ("submit", "ticket", "inbox", "read_message", "reply"),
    "tree": ("move",),
    "hours": ("hours",),
    "roles": ("drop_role", "issue_key", "revoke_key", "list_keys"),
    "contacts": ("set_contacts", "contacts", "contact", "invite"),
}
# Rarely wanted while editing, so never listed; `other` still runs it.
RARE = ("crawl",)
OTHER = types.Tool(
    name="other",
    description="Run an operation that is not listed as a tool because this site does not use its feature yet "
                "(bookings, message forms, trees, opening hours, staff roles, contacts). With no name, lists them with "
                "their arguments.",
    input_schema={"type": "object", "additionalProperties": False, "properties": {
        "name": {"type": "string", "description": "The operation to run. Omit to list what is available."},
        "arguments": {"type": "object", "description": "Its arguments."}}})


def features_in_use(site: Site) -> set:
    defs = list(site._defs().values())
    used = {name for name in ("booking",) if any(name in d.settings for d in defs)}
    if any(d.type == "form" for d in defs):
        used.add("form")
    if any(d.type == "tree" for d in defs):
        used.add("tree")
    if any(d.kind == "Hours" for d in defs):
        used.add("hours")
    if site.store.one("SELECT 1 FROM roles LIMIT 1"):
        used.add("roles")
    if site.contact_settings["requests"] != "off" or site.store.one("SELECT 1 FROM contacts LIMIT 1"):
        used.add("contacts")
    return used


def hidden_for(site: Site, principal: ops.Principal) -> list:
    """Operations the owner can use but is not shown as tools, because the site has no use for them yet."""
    if principal.kind == "role":
        return []
    used = features_in_use(site)
    return [ops.OPS[name] for feature, names in FEATURES.items() if feature not in used
            for name in names if name in ops.OPS] + [ops.OPS[name] for name in RARE if name in ops.OPS]


def tools_for(site: Site, principal: ops.Principal) -> list:
    """The operations shown to this caller as tools.

    A role is shown the ones its grants name. The owner is shown everything the site has a use for. This
    is about how much an assistant must read, not about permission: every call is checked either way.
    """
    if principal.kind == "role":
        granted = {name for g in site.grants_of(principal) for name in g["ops"]} | {"describe"}
        return [op for op in ops.OPS.values() if op.name in granted]
    hidden = {op.name for op in hidden_for(site, principal)}
    return [op for op in ops.OPS.values() if op.name not in hidden]


def _call(site: Site, principal: ops.Principal, name: str, arguments: dict) -> Any:
    if name == OTHER.name and name not in ops.OPS:
        wanted = arguments.get("name")
        if not wanted:
            return {"operations": [{"name": op.name, "summary": op.summary, "arguments": op.schema()}
                                   for op in hidden_for(site, principal)],
                    "use": "Call `other` with name and arguments. Once the site uses a feature, its tools are "
                           "listed the next time you connect."}
        name, arguments = str(wanted), dict(arguments.get("arguments") or {})
    before = features_in_use(site) if principal.kind != "role" else set()
    result = ops.run(site, principal, name, arguments)
    if principal.kind != "role" and isinstance(result, dict):
        added = [n for feature in sorted(features_in_use(site) - before) for n in FEATURES[feature]]
        if added:  # said here because a connected assistant does not see its tool list change
            result = {**result, "tools_added": added,
                      "tools_note": "Listed as tools the next time you connect. Until then, run them with `other`."}
    return result


def build_server(site: Site, principal: ops.Principal = ops.LOCAL) -> Server:
    """A caller is shown only the tools it can use, and each call is still checked against `principal`."""

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        allowed = await anyio.to_thread.run_sync(tools_for, site, principal)
        more = [OTHER] if len(allowed) < len(ops.OPS) and principal.kind != "role" else []
        return types.ListToolsResult(tools=[
            types.Tool(name=op.name, description=op.summary, input_schema=op.schema()) for op in allowed] + more)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            result = await anyio.to_thread.run_sync(
                _call, site, principal, params.name, dict(params.arguments or {}))
        except SiteError as e:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(e.as_dict()))], is_error=True)
        # The same result twice over: as data for clients that read structured results, as text for the rest.
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))],
                                    structured_content=result if isinstance(result, dict) else None)

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
