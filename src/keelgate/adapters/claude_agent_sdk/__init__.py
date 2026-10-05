"""Claude Agent SDK adapter: governed tools as an in-process MCP server, and locked-down options.

Needs ``keelgate[anthropic]``. The SDK drives the Claude Code CLI, which has its own powerful
built-in tools (shell, file edits, web). The point of this adapter is that an agent built with
it can only act through governed tools:

* the governed tools are served from an **in-process** MCP server, and every call goes through the
  gateway (grant, policy, approval, audit);
* the CLI's built-in tools are switched off, ``strict_mcp_config`` stops it loading other MCP
  servers from the machine's settings, and ``setting_sources`` is emptied so project or user
  settings cannot add tools or hooks;
* a ``can_use_tool`` callback refuses anything that is not one of the governed tools, as a final
  check even if one of the settings above is overridden.

``can_use_tool`` requires the SDK's streaming input mode (pass ``prompt`` as an async iterable).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

try:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
        create_sdk_mcp_server,
        tool,
    )
except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
        "keelgate.adapters.claude_agent_sdk needs the Claude Agent SDK: "
        "pip install 'keelgate[anthropic]'"
    ) from exc

if TYPE_CHECKING:
    from claude_agent_sdk import McpSdkServerConfig, ToolPermissionContext

    from keelgate.adapters.governed import GovernedToolset

# Claude Code's built-in tools. Named explicitly (as well as disabled wholesale) so a change to
# the SDK's defaults cannot quietly re-enable one.
BUILTIN_TOOLS: Final = (
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "ExitPlanMode", "SlashCommand",
)  # fmt: skip


def governed_sdk_mcp_server(
    toolset: GovernedToolset, *, name: str = "keelgate", version: str = "1.0.0"
) -> McpSdkServerConfig:
    """An in-process MCP server exposing the governed tools to the Claude Agent SDK."""
    sdk_tools = []
    for governed in toolset.tools():
        tool_name = governed.spec.name

        @tool(
            tool_name,
            governed.spec.description or tool_name,
            governed.spec.input_schema or governed.spec.input_model.model_json_schema(),
        )
        async def handler(arguments: dict[str, Any], _name: str = tool_name) -> dict[str, Any]:
            outcome = await toolset.invoke(_name, arguments)
            return {
                "content": [{"type": "text", "text": toolset.render(outcome)}],
                "is_error": toolset.is_error(outcome),
            }

        sdk_tools.append(handler)
    return create_sdk_mcp_server(name, version, sdk_tools)


def governed_tool_names(toolset: GovernedToolset, *, server_name: str = "keelgate") -> list[str]:
    """The SDK's fully qualified names for the governed tools: ``mcp__<server>__<tool>``."""
    return [f"mcp__{server_name}__{t.spec.name}" for t in toolset.tools()]


def governed_claude_options(
    toolset: GovernedToolset, *, server_name: str = "keelgate", **overrides: Any
) -> ClaudeAgentOptions:
    """``ClaudeAgentOptions`` that confine an agent to the governed tools.

    Extra keyword arguments (``model``, ``system_prompt``, ``max_turns``, ``max_budget_usd``...)
    are passed through. Overriding the security-relevant ones (``tools``, ``allowed_tools``,
    ``mcp_servers``, ``can_use_tool``, ``strict_mcp_config``) is refused, because that would
    defeat the adapter.
    """
    locked = {"tools", "allowed_tools", "disallowed_tools", "mcp_servers", "can_use_tool",
              "strict_mcp_config", "setting_sources"}  # fmt: skip
    clash = locked & overrides.keys()
    if clash:
        raise ValueError(
            f"these options define the governance boundary and cannot be "
            f"overridden: {sorted(clash)}"
        )

    allowed = governed_tool_names(toolset, server_name=server_name)
    allowed_set = frozenset(allowed)

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],  # noqa: ARG001
        context: ToolPermissionContext,  # noqa: ARG001
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name in allowed_set:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message="Only the governed tools are permitted. This tool is not one of them.",
            interrupt=False,
        )

    return ClaudeAgentOptions(
        mcp_servers={server_name: governed_sdk_mcp_server(toolset, name=server_name)},
        allowed_tools=allowed,
        disallowed_tools=list(BUILTIN_TOOLS),
        tools=[],
        strict_mcp_config=True,
        setting_sources=[],
        can_use_tool=can_use_tool,
        permission_mode="default",
        **overrides,
    )


__all__ = [
    "BUILTIN_TOOLS",
    "governed_claude_options",
    "governed_sdk_mcp_server",
    "governed_tool_names",
]
