"""Model Context Protocol adapter. MCP tool output is untrusted data.

Serve governed tools to MCP clients with :class:`GovernedMCPServer`, and admit external MCP tools
into a registry, under an allowlist, with :class:`GovernedMCPClient`.
"""

try:
    from keelgate.adapters.mcp._client import (
        Discovery,
        ExternalArgs,
        ExternalOutput,
        ExternalToolError,
        ExternalToolMapping,
        GovernedMCPClient,
        schema_digest,
    )
    from keelgate.adapters.mcp._server import GovernedMCPServer
except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
        "keelgate.adapters.mcp needs the MCP SDK: pip install 'keelgate[mcp]'"
    ) from exc

__all__ = [
    "Discovery",
    "ExternalArgs",
    "ExternalOutput",
    "ExternalToolError",
    "ExternalToolMapping",
    "GovernedMCPClient",
    "GovernedMCPServer",
    "schema_digest",
]
