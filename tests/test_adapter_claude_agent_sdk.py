"""Claude Agent SDK adapter, tested at the tool and permission boundary.

The SDK drives the Claude Code CLI, which cannot run offline, so these tests exercise what the
adapter actually controls: the in-process MCP server's handlers, the permission callback, and the
options that confine the agent. They do not claim to have run a live Claude agent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

from mcp import Client

from keelgate.adapters.claude_agent_sdk import (
    BUILTIN_TOOLS,
    governed_claude_options,
    governed_sdk_mcp_server,
    governed_tool_names,
)
from keelgate.adapters.governed import UNTRUSTED_KEY, GovernedToolset
from keelgate.tools import SideEffect
from tests.conftest import Harness, run

TRADE = {"symbol": "AAPL", "notional": 5000, "client_order_id": "cl-1"}


def toolset(h: Harness, **kw: Any) -> GovernedToolset:
    return GovernedToolset(
        gateway=h.gateway, registry=h.registry, grant_token=h.grant(), context_factory=h.ctx, **kw
    )


async def call_via_sdk_server(
    h: Harness, name: str, args: dict[str, Any], **kw: Any
) -> tuple[bool, dict[str, Any]]:
    config = governed_sdk_mcp_server(toolset(h, **kw))
    async with Client(config["instance"]) as client:
        result = await client.call_tool(name, args)
    block = result.content[0]
    return bool(result.is_error), json.loads(block.text)  # type: ignore[union-attr]


def test_the_sdk_server_exposes_exactly_the_governed_tools(h: Harness) -> None:
    async def listed() -> list[str]:
        config = governed_sdk_mcp_server(toolset(h), name="gov")
        assert config["type"] == "sdk" and config["name"] == "gov"
        async with Client(config["instance"]) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert sorted(run(listed())) == sorted(h.registry.names())


def test_a_governed_read_through_the_sdk_server_is_labelled_untrusted(h: Harness) -> None:
    is_error, body = run(call_via_sdk_server(h, "market_quote", {"symbol": "AAPL"}))
    assert not is_error and body[UNTRUSTED_KEY]["price"] == 101.5


def test_a_governed_write_through_the_sdk_server_is_gated_like_any_other(h: Harness) -> None:
    ok, body = run(call_via_sdk_server(h, "trade_paper_execute", TRADE))
    assert not ok and body["status"] == "OK" and len(h.executed) == 1

    denied, body = run(
        call_via_sdk_server(
            h, "trade_paper_execute", {**TRADE, "symbol": "TSLA", "client_order_id": "cl-d"}
        )
    )
    assert denied and body["error"]["code"] == "policy_denied"

    gated, body = run(
        call_via_sdk_server(
            h, "trade_paper_execute", {**TRADE, "notional": 30_000, "client_order_id": "cl-b"}
        )
    )
    assert gated and body["status"] == "APPROVAL_REQUIRED"
    assert len(h.executed) == 1 and h.audit.verify_chain("tenant-1").ok


def test_the_sdk_server_honours_a_read_only_toolset(h: Harness) -> None:
    async def scenario() -> tuple[list[str], bool]:
        ts = toolset(h, allowed_side_effects=frozenset({SideEffect.READ}))
        config = governed_sdk_mcp_server(ts)
        async with Client(config["instance"]) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            result = await client.call_tool("trade_paper_execute", TRADE)  # named anyway
        return names, bool(result.is_error)

    names, errored = run(scenario())
    assert names == ["market_quote"] and errored and h.executed == []


def test_qualified_tool_names_follow_the_sdk_convention(h: Harness) -> None:
    names = governed_tool_names(toolset(h), server_name="gov")
    assert names == [f"mcp__gov__{n}" for n in h.registry.names()]


# --------------------------------------------------------------------------- options


def test_the_options_confine_the_agent_to_the_governed_tools(h: Harness) -> None:
    options = governed_claude_options(toolset(h), model="claude-sonnet-5-5", max_turns=5)
    assert options.tools == []  # every built-in tool is switched off
    assert set(options.disallowed_tools) == set(BUILTIN_TOOLS)
    assert options.allowed_tools == governed_tool_names(toolset(h))
    assert options.strict_mcp_config is True and options.setting_sources == []
    assert list(options.mcp_servers) == ["keelgate"]
    assert (
        options.model == "claude-sonnet-5-5" and options.max_turns == 5
    )  # harmless options pass through


@pytest.mark.parametrize(
    "override",
    [
        "tools",
        "allowed_tools",
        "disallowed_tools",
        "mcp_servers",
        "can_use_tool",
        "strict_mcp_config",
        "setting_sources",
    ],
)
def test_the_security_relevant_options_cannot_be_overridden(h: Harness, override: str) -> None:
    with pytest.raises(ValueError, match="governance boundary"):
        governed_claude_options(toolset(h), **{override: []})


def test_the_permission_callback_allows_governed_tools_and_denies_everything_else(
    h: Harness,
) -> None:
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    can_use = governed_claude_options(toolset(h)).can_use_tool
    assert can_use is not None

    async def decide(name: str) -> Any:
        return await can_use(name, {}, None)  # type: ignore[arg-type]

    assert isinstance(run(decide("mcp__keelgate__market_quote")), PermissionResultAllow)
    for forbidden in (
        "Bash",
        "Write",
        "WebFetch",
        "mcp__other__market_quote",
        "mcp__keelgate__nope",
        "",
    ):
        verdict = run(decide(forbidden))
        assert isinstance(verdict, PermissionResultDeny), forbidden
        assert "governed" in verdict.message.lower()


def test_a_read_only_toolset_does_not_allow_write_tool_names(h: Harness) -> None:
    ts = toolset(h, allowed_side_effects=frozenset({SideEffect.READ}))
    assert governed_claude_options(ts).allowed_tools == ["mcp__keelgate__market_quote"]
