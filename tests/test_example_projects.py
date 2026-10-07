"""The documented example projects run, offline, and do what their pages say."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
PINNED = "2026-10-05T14:30:00+00:00"  # a Monday, 10:30 in New York: inside trading hours


def run_example(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, our own scripts
        [sys.executable, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def test_the_tutorial_script_runs_and_shows_every_outcome() -> None:
    result = run_example(str(EXAMPLES / "tutorial_governed_agent.py"))
    out = result.stdout
    assert result.returncode == 0, result.stderr + out
    for fragment in (
        "allowed   -> OK",
        "retry     -> OK",  # an exact retry replays; the blotter below shows it ran once
        "denied    -> DENIED             policy_denied",
        "parked    -> APPROVAL_REQUIRED  EXPLICIT_SIGNOFF",
        "approved  -> OK",
        "goal reached: True",
        "chain intact: True",
        "3 paper orders: ['t-1', 't-3', 'loop-1']",
    ):
        assert fragment in out, fragment


def test_the_custom_policy_pack_example_runs_with_the_shipped_rego_engine() -> None:
    result = run_example(str(EXAMPLES / "custom_policy_pack" / "run.py"))
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout
    assert "destination is not on the allowlist" in out
    assert "amount exceeds the per-transfer cap" in out
    assert "APPROVAL_REQUIRED" in out and "transfers actually sent: ['t-1']" in out


def test_the_custom_pack_decides_like_its_rego_tests_say() -> None:
    from keelgate.policy import (
        Decision,
        PolicyAction,
        PolicyActor,
        PolicyContext,
        PolicyInput,
        RegoEngine,
    )

    engine = RegoEngine(EXAMPLES / "custom_policy_pack", query="data.acme.transfer_limits.decision")
    limits = {
        "allowed_destinations": ["acct-vendors"],
        "max_transfer": 10_000,
        "max_daily_total": 25_000,
        "approval_threshold": 2_000,
    }

    def decide(
        capability: str, side_effect: str, resource: dict[str, Any], mode: str = "paper"
    ) -> Any:
        action = PolicyAction(tool="t", side_effect=side_effect, capability=capability)  # type: ignore[arg-type]
        context = PolicyContext(
            as_of=datetime(2026, 10, 5, 14, 30, tzinfo=UTC),
            execution_mode=mode,
            limits=limits,
            exposure={"daily_total": 0},
        )
        actor = PolicyActor(agent_id="a", tenant_id="t", grant_id="g")
        policy_input = PolicyInput(action=action, actor=actor, resource=resource, context=context)
        return asyncio.run(engine.decide(policy_input))

    ok = {"destination": "acct-vendors", "amount": 100}
    assert decide("treasury:transfer", "WRITE", ok).effect is Decision.ALLOW
    assert decide("treasury:transfer", "WRITE", {**ok, "destination": "x"}).effect is Decision.DENY
    assert decide("treasury:transfer", "WRITE", ok, mode="live").effect is Decision.DENY
    assert decide("treasury:wire", "WRITE", ok).effect is Decision.DENY  # unknown capability
    assert decide("treasury:transfer", "WRITE", {**ok, "amount": 5_000}).approval_tier is not None


def test_the_governed_langgraph_agent_runs_and_the_restricted_order_never_executes() -> None:
    pytest.importorskip("langgraph")
    result = run_example(str(EXAMPLES / "governed_langgraph_agent.py"))
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout
    assert "tool call -> DENIED   policy_denied" in out
    assert "paper orders actually placed: ['lg-2']" in out and "audit chain intact: True" in out


# ------------------------------------------------------------------ the MCP server example


def test_print_config_emits_client_json_for_claude_desktop_and_cursor() -> None:
    result = run_example(str(EXAMPLES / "mcp_server.py"), "--print-config", "--enable-paper-orders")
    config = json.loads(result.stdout)
    entry = config["mcpServers"]["keelgate-demo"]
    assert Path(entry["command"]).exists()
    assert entry["args"][0].endswith("mcp_server.py") and "--enable-paper-orders" in entry["args"]


async def _serve(state_dir: Path, *flags: str) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            str(EXAMPLES / "mcp_server.py"),
            "--state-dir",
            str(state_dir),
            "--as-of",
            PINNED,
            *flags,
        ],
    )
    seen: dict[str, Any] = {}
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        seen["tools"] = sorted(t.name for t in (await session.list_tools()).tools)

        async def call(name: str, args: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
            result = await session.call_tool(name, args)
            block = result.content[0]
            assert isinstance(block, types.TextContent)
            return bool(result.is_error), json.loads(block.text)

        seen["quote"] = await call("market_quote", {"symbol": "AAPL"})
        if "place_paper_order" in seen["tools"]:
            order = {"symbol": "TSLA", "notional": 1000, "client_order_id": "m-1"}
            seen["restricted"] = await call("place_paper_order", order)
            order = {"symbol": "AAPL", "notional": 1000, "client_order_id": "m-2"}
            seen["small"] = await call("place_paper_order", order)
            order = {"symbol": "AAPL", "notional": 30000, "client_order_id": "m-3"}
            seen["big"] = await call("place_paper_order", order)
    return seen


def test_the_mcp_server_is_read_only_by_default(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    seen = asyncio.run(_serve(tmp_path))
    assert seen["tools"] == ["market_quote"]
    is_error, body = seen["quote"]
    assert not is_error and body["untrusted_tool_output"]["price"] == 187.25


def test_the_mcp_server_gates_paper_orders_and_keeps_a_verifiable_audit_trail(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mcp")
    from keelgate.approvals import ApprovalQueue
    from keelgate.audit import AuditLog, SqliteAuditStore, verify_chain

    seen = asyncio.run(_serve(tmp_path, "--enable-paper-orders"))
    assert seen["tools"] == ["market_quote", "place_paper_order"]

    is_error, body = seen["restricted"]
    assert is_error and body["error"]["code"] == "policy_denied"  # TSLA is restricted
    is_error, body = seen["small"]
    assert not is_error and body["status"] == "OK"
    is_error, body = seen["big"]
    assert is_error and body["status"] == "APPROVAL_REQUIRED"  # parked for a human

    # the human side: the parked request is waiting in the approvals database
    queue = ApprovalQueue(tmp_path / "approvals.sqlite")
    pending = queue.list_pending("demo")
    assert len(pending) == 1 and pending[0].tool_name == "place_paper_order"
    queue.close()

    # and everything that happened is in a hash chain anyone can verify
    store = SqliteAuditStore(tmp_path / "audit.sqlite")
    chain = verify_chain(AuditLog(store).records("demo"), tenant_id="demo")
    assert chain.ok and chain.records_checked >= 6
    store.close()
