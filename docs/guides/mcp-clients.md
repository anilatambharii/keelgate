# MCP server for Claude Desktop and Cursor

Give an MCP client tools that are governed by Keelgate. The client sees ordinary tools; behind each
one is the gate: a signed grant, a policy, human approvals and a hash-chained audit log.
[`examples/mcp_server.py`][srv] is a complete, runnable server.

[srv]: https://github.com/anilatambharii/keelgate/blob/main/examples/mcp_server.py

```bash
pip install "keelgate[mcp]"
python examples/mcp_server.py --print-config
```

## Claude Desktop

`--print-config` prints the entry to merge into `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "keelgate-demo": {
      "command": "/path/to/python",
      "args": ["/path/to/keelgate/examples/mcp_server.py", "--state-dir", "/home/you/.keelgate-demo"]
    }
  }
}
```

Restart Claude Desktop. The server lists one tool, `market_quote`.

## Cursor

The same entry goes in `.cursor/mcp.json` (per project) or `~/.cursor/mcp.json` (global). Nothing
about it is specific to either client: any MCP client that can launch a stdio server works.

## Read-only by default

The server starts **read-only**. Add `--enable-paper-orders` to the `args` to also expose
`place_paper_order`. Even then:

- nothing real is ever executed: Keelgate v1 has no live path;
- the policy still denies restricted symbols and oversized orders, and an error result tells the
  model *why*, in fixed wording;
- a large order is **parked** (`APPROVAL_REQUIRED`) until a person approves it from a terminal:

```bash
keelgate-approvals --db ~/.keelgate-demo/approvals.sqlite --tenant demo list
keelgate-approvals --db ~/.keelgate-demo/approvals.sqlite --tenant demo approve <id> --approver you
```

The model cannot approve its own request, and an approval is good for those exact arguments, once.

## Check what it did

Every call lands in a hash chain in `--state-dir`. To verify it later:

```python
from keelgate.audit import AuditLog, SqliteAuditStore, verify_chain

store = SqliteAuditStore("~/.keelgate-demo/audit.sqlite")        # expand the path first
print(verify_chain(AuditLog(store).records("demo"), tenant_id="demo"))
```

## Before you point this at anything real

- **One grant, shared.** The example serves every caller with one grant. That is right for a local
  stdio server that only your client can reach. For HTTP, give each caller its own authority with
  `GovernedMCPServer(toolset, toolset_for_request=...)`; without it, anyone who can reach the port
  has the server's authority (the confused-deputy problem; see the
  [security model](../security-model.md), row A3).
- **Tool results are untrusted.** They arrive under `untrusted_tool_output`, and a model that
  decides to obey text inside them is exactly the case the gate exists for.
- **The grant is minted at start-up** with a throwaway key, which is fine for a demo. In production
  load the signing key from a secret store and issue grants from a service that is not the agent.
- `--as-of` pins the clock, for reproducible demos and tests. Otherwise "now" is used, and the
  policy's trading-hours rule means orders are denied outside US market hours.

## Governing tools you did not write

The same adapter works the other way round: `GovernedMCPClient` connects to an *external* MCP server
and exposes its tools through the gateway, with an allowlist, a capability mapping, optional schema
pinning (to catch a server that changes its tools after you reviewed them) and untrusted output.
See the API reference for `keelgate.adapters.mcp`.
