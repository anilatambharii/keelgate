"""Expose governed tools as an MCP server (``pip install 'keelgate[mcp]'``).

One ``tools/call`` handler routes every request through :class:`GovernedToolset`, so the grant,
policy, approval and audit checks apply to MCP callers exactly as they do to any other
framework. There is no second code path to forget to secure.

Two things to understand before exposing this:

* **The server acts with its own grant.** An MCP client does not present a Keelgate grant; the
  grant configured on the toolset is the authority every caller borrows. That is the right
  shape for a stdio server run by the operator for one agent, and a *confused deputy* if the
  same server is put on a network for many callers. Supply a ``grant_resolver`` that maps each
  request to the caller's own grant before doing that.
* **A handler must never raise.** In MCP 2.x an exception inside a handler becomes an opaque
  "Internal server error". This handler turns every failure into an ``is_error`` result with a
  fixed, safe body.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from keelgate import __version__
from keelgate.tools._spec import SideEffect

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.applications import Starlette

    from keelgate.adapters._governed import GovernedToolset

_INTERNAL = json.dumps(
    {
        "status": "ERROR",
        "error": {"code": "internal_error", "message": "The call could not be processed."},
    }
)


class GovernedMCPServer:
    """Serve governed tools as an MCP server over stdio or streamable HTTP.

    Every call goes through the gateway, handlers never raise, and output is labelled untrusted.
    """

    def __init__(
        self,
        toolset: GovernedToolset,
        *,
        name: str = "keelgate",
        instructions: str | None = None,
        toolset_for_request: Callable[[types.CallToolRequestParams], GovernedToolset] | None = None,
    ) -> None:
        """``toolset_for_request`` lets a deployment pick a per-caller toolset (and so a
        per-caller grant). Without it, every caller shares ``toolset``'s grant."""
        self._toolset = toolset
        self._per_request = toolset_for_request
        self.server: Server[Any] = Server(
            name,
            version=__version__,
            instructions=instructions,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    async def _list_tools(self, _ctx: Any, _params: Any) -> types.ListToolsResult:
        tools = []
        for tool in self._toolset.tools():
            read_only = tool.spec.side_effect is SideEffect.READ
            tools.append(
                types.Tool(
                    name=tool.spec.name,
                    description=tool.spec.description,
                    input_schema=tool.spec.input_schema
                    or tool.spec.input_model.model_json_schema(),
                    annotations=types.ToolAnnotations(
                        read_only_hint=read_only,
                        destructive_hint=tool.spec.side_effect is SideEffect.WRITE,
                        idempotent_hint=tool.spec.idempotency_key is not None,
                        open_world_hint=False,
                    ),
                )
            )
        return types.ListToolsResult(tools=tools)

    async def _call_tool(
        self, _ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        try:
            toolset = self._per_request(params) if self._per_request is not None else self._toolset
            outcome = await toolset.invoke(params.name, params.arguments or {})
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=toolset.render(outcome))],
                is_error=toolset.is_error(outcome),
            )
        except Exception:  # an MCP handler that raises becomes an opaque transport error
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_INTERNAL)], is_error=True
            )

    async def serve_stdio(self) -> None:
        """Serve one client over stdin/stdout until it disconnects."""
        async with stdio_server() as (read, write):
            await self.server.run(read, write, self.server.create_initialization_options())

    def http_app(self, *, path: str = "/mcp", **kwargs: Any) -> Starlette:
        """An ASGI app serving streamable HTTP. Read the confused-deputy note before using it."""
        return self.server.streamable_http_app(streamable_http_path=path, **kwargs)
