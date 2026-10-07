"""A governed MCP server over stdio, for the real-transport test.

    python -m tests.mcp_stdio_server

It serves the shared test tools through a real gateway with a real grant, so a client that
spawns it exercises the same governed path as everything else, over genuine stdin/stdout.
"""

from __future__ import annotations

import asyncio

from keelgate.adapters._governed import GovernedToolset
from keelgate.adapters.mcp import GovernedMCPServer
from keelgate.policy import RegoEngine
from tests.conftest import build_harness


def main() -> None:
    h = build_harness(engine=RegoEngine())
    toolset = GovernedToolset(
        gateway=h.gateway,
        registry=h.registry,
        grant_token=h.grant(),
        context_factory=h.ctx,
    )
    asyncio.run(GovernedMCPServer(toolset, name="keelgate-test").serve_stdio())


if __name__ == "__main__":
    main()
