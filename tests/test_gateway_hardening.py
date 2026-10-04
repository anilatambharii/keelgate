"""The gateway's last lines of defence: what happens when something unexpected breaks.

These paths exist so that a bug in the gateway, a racing writer or a failing disk
degrades to a refusal rather than to an unauthorised action. They are only
reachable by forcing the failure, so each test does exactly that.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from keelgate.audit import AuditLog
from keelgate.tools import (
    Claim,
    ClaimState,
    ErrorCode,
    InMemoryIdempotencyStore,
    OutcomeStatus,
    SideEffect,
    ToolGateway,
    ToolRefusedError,
    ToolRegistry,
    tool,
)
from tests.conftest import Harness, QuoteIn, build_harness, run
from tests.test_gateway import TRADE, FlakyStore, Out


class BigOut(BaseModel):
    blob: str


def extra_tools(registry: ToolRegistry, executed: list[dict[str, Any]]) -> None:
    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="big_write",
        idempotency_key=lambda a: a.symbol,
    )
    def big_write(args: QuoteIn) -> BigOut:
        executed.append({"tool": "big_write"})
        return BigOut(blob="x" * 40_000)

    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="odd_key_write",
        idempotency_key=lambda a: 42,  # type: ignore[arg-type,return-value]
    )
    def odd_key_write(args: QuoteIn) -> Out:
        executed.append({"tool": "odd_key_write"})
        return Out(ok=True)

    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="long_key_write",
        idempotency_key=lambda a: "k" * 500,
    )
    def long_key_write(args: QuoteIn) -> Out:
        executed.append({"tool": "long_key_write"})
        return Out(ok=True)

    @tool(capability="market_data:read", side_effect=SideEffect.READ, name="refusing_read")
    def refusing_read(args: QuoteIn) -> Out:
        raise ToolRefusedError("not now")

    @tool(capability="market_data:read", side_effect=SideEffect.READ, name="wrong_shape_read")
    def wrong_shape_read(args: QuoteIn) -> Out:
        return {"nope": 1}  # type: ignore[return-value]

    @tool(
        capability="market_data:read", side_effect=SideEffect.READ, name="hanging_read", timeout_s=5
    )
    async def hanging_read(args: QuoteIn) -> Out:
        await asyncio.sleep(5)
        return Out(ok=True)

    for t in (
        big_write,
        odd_key_write,
        long_key_write,
        refusing_read,
        wrong_shape_read,
        hanging_read,
    ):
        registry.register(t)


@pytest.fixture
def hh(rego_engine: Any) -> Harness:
    return build_harness(engine=rego_engine, tools_extra=extra_tools)


def spent(h: Harness, token: str) -> float:
    return h.gateway._ledger.spent(h.verifier.verify(token).grant_id)


# ---------------------------------------------------- bugs inside the gateway


def test_an_unexpected_bug_before_execution_fails_closed_and_refunds(
    hh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("a bug in the policy gate")

    monkeypatch.setattr(ToolGateway, "_policy_gate", boom)
    token = hh.grant(cost=10.0)
    out = hh.call("trade_paper_execute", TRADE, token=token)
    assert out.status is OutcomeStatus.ERROR
    assert out.error is not None and out.error.code is ErrorCode.INTERNAL
    assert "policy gate" not in str(out.error)  # internals never reach the caller
    assert hh.executed == []
    assert spent(hh, token) == 0.0  # the reservation was handed back


def test_the_final_cleared_check_stops_a_call_that_was_never_cleared(
    hh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a refactor ever lets a call fall through the gate without clearing it,
    execution still refuses. This is the backstop behind every other check."""

    async def forgets_to_clear(*_a: Any, **_k: Any) -> None:
        return None  # "no refusal" but never sets st.cleared

    monkeypatch.setattr(ToolGateway, "_policy_gate", forgets_to_clear)
    token = hh.grant(cost=10.0)
    out = hh.call("trade_paper_execute", TRADE, token=token)
    assert out.error is not None and out.error.code is ErrorCode.INTERNAL
    assert "not cleared" in out.error.message
    assert hh.executed == []
    assert spent(hh, token) == 0.0


def test_a_write_that_reaches_execution_without_a_key_is_refused(
    hh: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ToolGateway, "_idempotency_gate", lambda *_a, **_k: None)
    out = hh.call("trade_paper_execute", TRADE)
    assert out.error is not None and out.error.code is ErrorCode.INTERNAL
    assert hh.executed == []


def test_losing_the_claim_race_does_not_run_the_tool(hh: Harness) -> None:
    class Racy(InMemoryIdempotencyStore):
        """peek says the key is free; by the time we claim, someone else has it."""

        def peek(self, *_a: Any, **_k: Any) -> Claim:
            return Claim(ClaimState.NEW)

        def claim(self, *_a: Any, **_k: Any) -> Claim:
            return Claim(ClaimState.IN_FLIGHT)

    gateway = ToolGateway(
        registry=hh.registry,
        verifier=hh.verifier,
        engine=hh.engine,
        audit=hh.audit,
        approvals=hh.queue,
        idempotency=Racy(),
    )
    token = hh.grant(cost=10.0)
    out = run(
        gateway.call(
            tool_name="trade_paper_execute", arguments=TRADE, grant_token=token, context=hh.ctx()
        )
    )
    assert out.error is not None and out.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert out.error.retryable  # in flight elsewhere: waiting is reasonable
    assert hh.executed == []
    assert spent(hh, token) == 0.0


# --------------------------------------------------------- idempotency keys


@pytest.mark.parametrize("tool_name", ["odd_key_write", "long_key_write"])
def test_malformed_idempotency_keys_are_refused_before_anything_runs(
    hh: Harness, tool_name: str
) -> None:
    out = hh.call(tool_name, {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.INVALID_ARGUMENTS
    assert hh.executed == []


# ------------------------------------------------------------ output handling


def test_oversized_output_is_hashed_not_stored_in_the_audit_log(hh: Harness) -> None:
    out = hh.call("big_write", {"symbol": "X"})
    assert out.ok
    record = next(r for r in hh.audit.records("tenant-1") if r.event_type == "tool.result")
    assert record.payload["output_truncated"] is True
    assert "output" not in record.payload
    assert len(record.payload["output_sha256"]) == 64
    assert hh.audit.verify_chain("tenant-1").ok


def test_small_output_is_stored_in_full(hh: Harness) -> None:
    hh.call("market_quote", {"symbol": "AAPL"})
    record = next(r for r in hh.audit.records("tenant-1") if r.event_type == "tool.result")
    assert record.payload["output"] == {"symbol": "AAPL", "price": 101.5}


def test_a_read_tool_returning_the_wrong_shape_is_an_error_not_a_crash(hh: Harness) -> None:
    out = hh.call("wrong_shape_read", {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.INVALID_TOOL_OUTPUT


def test_a_read_tool_that_declines_may_be_retried(hh: Harness) -> None:
    out = hh.call("refusing_read", {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.TOOL_FAILED
    assert out.error.retryable


def test_cancelling_a_read_propagates_and_leaves_no_claim_behind(hh: Harness) -> None:
    async def scenario() -> None:
        task = asyncio.create_task(
            hh.gateway.call(
                tool_name="hanging_read",
                arguments={"symbol": "X"},
                grant_token=hh.grant(),
                context=hh.ctx(),
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


# ------------------------------------------------------------- audit failures


def test_a_tool_failure_that_cannot_be_recorded_says_so(rego_engine: Any) -> None:
    """tool.call(1), policy.decision(2) succeed; recording the failure (3) does not."""
    from tests.test_gateway import failing_tools

    harness = build_harness(
        engine=rego_engine, audit=AuditLog(FlakyStore(3)), tools_extra=failing_tools
    )
    out = harness.call("crashing_write", {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.AUDIT_FAILED_AFTER_EXECUTION
    assert len(harness.executed) == 1
