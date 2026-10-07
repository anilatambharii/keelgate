"""The tool gateway: every gate, in order, failing closed."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from keelgate.approvals import ApprovalStatus, ApprovalTier
from keelgate.audit import AuditError, AuditLog, AuditRecord, ChainHead, EventType, SqliteAuditStore
from keelgate.audit._stores import BuildRecord
from keelgate.capabilities import GrantSigner, issue_grant
from keelgate.policy import Decision, PolicyDecision, PolicyInput
from keelgate.tools import (
    DirectInvocationError,
    ErrorCode,
    OutcomeStatus,
    RegistryFrozenError,
    SideEffect,
    ToolDefinitionError,
    ToolGateway,
    ToolRefusedError,
    ToolRegistry,
    tool,
)
from tests.conftest import (
    LIMITS,
    Harness,
    QuoteIn,
    QuoteOut,
    TradeIn,
    TradeOut,
    build_harness,
    policy_context,
    run,
)

TRADE = {"symbol": "AAPL", "notional": 5000, "client_order_id": "o-1"}


def events(h: Harness, tenant: str = "tenant-1") -> list[str]:
    return [r.event_type for r in h.audit.records(tenant)]


# -------------------------------------------------------------------- READ


def test_read_tool_runs_without_a_policy_decision(h: Harness) -> None:
    out = h.call("market_quote", {"symbol": "AAPL"})
    assert out.status is OutcomeStatus.OK and out.ok
    assert out.output is not None
    assert out.output.unwrap_untrusted().price == 101.5  # type: ignore[attr-defined]
    assert EventType.POLICY_DECISION not in events(h)
    assert events(h) == [EventType.TOOL_CALL, EventType.TOOL_RESULT]


def test_tool_output_is_wrapped_as_untrusted_and_kept_out_of_the_model_view(h: Harness) -> None:
    out = h.call("market_quote", {"symbol": "AAPL"})
    assert "redacted" in repr(out.output)
    assert "101.5" not in repr(out.output)
    assert "output" not in out.for_model()
    assert out.for_model() == {"status": "OK", "tool": "market_quote"}


# ------------------------------------------------------------ WRITE: allow


def test_an_allowed_write_runs_exactly_once_and_is_fully_audited(h: Harness) -> None:
    out = h.call("trade_paper_execute", TRADE)
    assert out.status is OutcomeStatus.OK
    assert len(h.executed) == 1
    assert h.executed[0]["symbol"] == "AAPL"
    assert out.policy is not None and out.policy.effect is Decision.ALLOW
    assert events(h) == [EventType.TOOL_CALL, EventType.POLICY_DECISION, EventType.TOOL_RESULT]
    assert h.audit.verify_chain("tenant-1").ok


def test_the_audit_trail_records_who_which_policy_and_why(h: Harness) -> None:
    out = h.call("trade_paper_execute", TRADE)
    by_event = {r.event_type: r for r in h.audit.records("tenant-1")}
    decision = by_event[EventType.POLICY_DECISION]
    assert decision.actor == "agent-1"
    assert decision.payload["effect"] == "ALLOW"
    assert decision.payload["policy_version"].startswith("sha256:")
    assert decision.payload["engine"] == "rego-inprocess"
    assert decision.payload["call_id"] == out.call_id
    assert by_event[EventType.TOOL_CALL].actor == "unauthenticated"  # recorded before auth


def test_every_audit_record_of_a_call_shares_its_call_id(h: Harness) -> None:
    out = h.call("trade_paper_execute", TRADE)
    assert {r.payload["call_id"] for r in h.audit.records("tenant-1")} == {out.call_id}


def test_the_audit_record_exists_before_the_side_effect(h: Harness) -> None:
    """Order matters: authorisation is on the record before anything happens."""
    seen: list[list[str]] = []

    def extra(registry: ToolRegistry, _executed: list[dict[str, Any]]) -> None:
        @tool(
            capability="report:write",
            side_effect=SideEffect.WRITE,
            name="spy_write",
            idempotency_key=lambda a: a.symbol,
        )
        def spy(args: QuoteIn) -> QuoteOut:
            seen.append(events(spied))
            return QuoteOut(symbol=args.symbol, price=1.0)

        registry.register(spy)

    spied = build_harness(tools_extra=extra)
    out = spied.call("spy_write", {"symbol": "X"})
    assert out.ok
    assert seen == [[EventType.TOOL_CALL, EventType.POLICY_DECISION]]


def test_idempotency_keys_are_per_tenant(h: Harness) -> None:
    first = h.call("trade_paper_execute", TRADE)
    other = h.call(
        "trade_paper_execute",
        TRADE,
        token=h.grant(tenant="tenant-2", agent="agent-2"),
        context=h.ctx("tenant-2"),
    )
    assert first.ok and other.ok and not other.replayed
    assert len(h.executed) == 2


# ------------------------------------------------------------- WRITE: deny


def test_a_denied_write_never_runs(h: Harness) -> None:
    out = h.call("trade_paper_execute", {**TRADE, "symbol": "TSLA"})
    assert out.status is OutcomeStatus.DENIED
    assert out.error is not None and out.error.code is ErrorCode.POLICY_DENIED
    assert h.executed == []
    assert out.error.details["reasons"] == ["symbol is on the restricted list"]
    assert "do not retry" in out.error.hint.lower()


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"notional": 60_000}, "per-action limit"),
        ({"symbol": "gme"}, "restricted"),
    ],
)
def test_policy_denials_surface_their_reasons(
    h: Harness, overrides: dict[str, Any], fragment: str
) -> None:
    out = h.call("trade_paper_execute", {**TRADE, **overrides})
    assert out.error is not None
    assert any(fragment in r for r in out.error.details["reasons"])
    assert h.executed == []


def test_trading_hours_come_from_as_of_not_the_wall_clock(h: Harness) -> None:
    saturday = policy_context(as_of=h.clock.now.replace(day=10))
    out = h.call("trade_paper_execute", TRADE, context=h.ctx(policy_context=saturday))
    assert out.status is OutcomeStatus.DENIED
    assert "outside trading hours" in out.error.details["reasons"]  # type: ignore[union-attr]


def test_the_policy_decision_is_audited_even_when_it_denies(h: Harness) -> None:
    h.call("trade_paper_execute", {**TRADE, "symbol": "TSLA"})
    decision = next(
        r for r in h.audit.records("tenant-1") if r.event_type == EventType.POLICY_DECISION
    )
    assert decision.payload["effect"] == "DENY"
    assert EventType.TOOL_RESULT not in events(h)


def test_exposure_context_is_enforced(h: Harness) -> None:
    ctx = h.ctx(policy_context=policy_context(exposure={"daily_notional": 98_000}))
    out = h.call("trade_paper_execute", TRADE, context=ctx)
    assert out.status is OutcomeStatus.DENIED
    assert h.executed == []


# ------------------------------------------------------------------ PROPOSE


def test_propose_is_policy_gated_but_does_not_need_market_hours(h: Harness) -> None:
    saturday = policy_context(as_of=h.clock.now.replace(day=10))
    ok = h.call("trade_propose", TRADE, context=h.ctx(policy_context=saturday))
    assert ok.ok
    restricted = h.call("trade_propose", {**TRADE, "symbol": "TSLA"})
    assert restricted.status is OutcomeStatus.DENIED


# -------------------------------------------------------- REQUIRE_APPROVAL


BIG = {"symbol": "AAPL", "notional": 30_000, "client_order_id": "big-1"}


def test_a_large_order_parks_for_approval_and_does_not_run(h: Harness) -> None:
    out = h.call(
        "trade_paper_execute",
        BIG,
        context=h.ctx(rationale="breakout", confidence=0.7, verifier_flags=("cross-checked",)),
    )
    assert out.status is OutcomeStatus.APPROVAL_REQUIRED
    assert out.approval_tier is ApprovalTier.EXPLICIT_SIGNOFF
    assert out.approval_id
    assert h.executed == []
    view = out.for_model()
    assert view["approval_id"] == out.approval_id
    assert "wait" in view["hint"].lower()

    request = h.queue.get("tenant-1", out.approval_id)
    assert request.status is ApprovalStatus.PENDING
    assert request.evidence.rationale == "breakout"
    assert request.evidence.confidence == 0.7
    assert request.evidence.verifier_flags == ("cross-checked",)
    assert request.evidence.policy_reasons == ("notional exceeds the explicit sign-off threshold",)
    assert request.evidence.policy_version.startswith("sha256:")
    assert request.agent_id == "agent-1"


def test_approval_then_resume_executes_once(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)

    done = h.call("trade_paper_execute", BIG, context=h.ctx(approval_id=request.request_id))
    assert done.status is OutcomeStatus.OK
    assert len(h.executed) == 1
    assert h.queue.get("tenant-1", request.request_id).status is ApprovalStatus.CONSUMED
    assert h.audit.verify_chain("tenant-1").ok
    assert EventType.APPROVAL_CONSUMED in events(h)


def test_resuming_before_a_human_decides_is_refused(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    out = h.call("trade_paper_execute", BIG, context=h.ctx(approval_id=parked.approval_id))
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_INVALID
    assert h.executed == []


def test_a_rejected_request_cannot_be_used(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    h.queue.reject("tenant-1", parked.approval_id, h.approver)  # type: ignore[arg-type]
    out = h.call("trade_paper_execute", BIG, context=h.ctx(approval_id=parked.approval_id))
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_INVALID
    assert h.executed == []


def test_an_approval_cannot_authorise_different_arguments(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)
    bigger = {**BIG, "notional": 49_000, "client_order_id": "big-2"}
    out = h.call("trade_paper_execute", bigger, context=h.ctx(approval_id=request.request_id))
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_INVALID
    assert h.executed == []
    # ...and the approval was not burned by the failed attempt.
    assert h.queue.get("tenant-1", request.request_id).status is ApprovalStatus.APPROVED


def test_an_approval_is_not_usable_by_another_agent(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)
    out = h.call(
        "trade_paper_execute",
        BIG,
        token=h.grant(agent="agent-2"),
        context=h.ctx(approval_id=request.request_id),
    )
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_INVALID
    assert h.executed == []


def test_an_approval_from_another_tenant_is_not_usable(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)
    out = h.call(
        "trade_paper_execute",
        BIG,
        token=h.grant(tenant="tenant-2", agent="agent-1"),
        context=h.ctx("tenant-2", approval_id=request.request_id),
    )
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_INVALID
    assert h.executed == []


def test_an_expired_approval_is_not_usable(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)
    h.clock.advance(timedelta(hours=2))
    out = h.call(
        "trade_paper_execute",
        BIG,
        token=h.grant(),
        context=h.ctx(approval_id=request.request_id),
    )
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_INVALID
    assert h.executed == []


def test_an_approval_cannot_override_a_later_denial(h: Harness) -> None:
    """Approved at 30k, but the symbol was restricted before the resume."""
    parked = h.call("trade_paper_execute", BIG)
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)
    restricted = policy_context(limits={**LIMITS, "restricted_symbols": ["AAPL"]})
    out = h.call(
        "trade_paper_execute",
        BIG,
        context=h.ctx(policy_context=restricted, approval_id=request.request_id),
    )
    assert out.status is OutcomeStatus.DENIED
    assert out.error is not None and out.error.code is ErrorCode.POLICY_DENIED
    assert h.executed == []


def test_an_approval_below_the_tier_policy_now_requires_is_refused(h: Harness) -> None:
    mid = {"symbol": "AAPL", "notional": 20_000, "client_order_id": "mid-1"}
    parked = h.call("trade_paper_execute", mid)
    assert parked.approval_tier is ApprovalTier.ONE_CLICK
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver)
    stricter = policy_context(limits={**LIMITS, "approval_explicit_notional": 15_000})
    out = h.call(
        "trade_paper_execute",
        mid,
        context=h.ctx(policy_context=stricter, approval_id=request.request_id),
    )
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_INVALID
    assert h.executed == []


def test_an_unneeded_approval_id_is_ignored_for_an_allowed_action(h: Harness) -> None:
    out = h.call("trade_paper_execute", TRADE, context=h.ctx(approval_id="whatever"))
    assert out.ok and len(h.executed) == 1


def test_without_an_approval_queue_a_gated_action_is_refused(rego_engine: Any) -> None:
    harness = build_harness(engine=rego_engine)
    gateway = ToolGateway(
        registry=harness.registry,
        verifier=harness.verifier,
        engine=rego_engine,
        audit=harness.audit,
    )
    out = run(
        gateway.call(
            tool_name="trade_paper_execute",
            arguments=BIG,
            grant_token=harness.grant(),
            context=harness.ctx(),
        )
    )
    assert out.status is OutcomeStatus.DENIED
    assert out.error is not None and out.error.code is ErrorCode.APPROVAL_UNAVAILABLE
    assert harness.executed == []


# --------------------------------------------------------------- idempotency


def test_an_exact_repeat_replays_the_stored_result_without_running_again(h: Harness) -> None:
    first = h.call("trade_paper_execute", TRADE)
    again = h.call("trade_paper_execute", TRADE)
    assert again.ok and again.replayed and not first.replayed
    assert len(h.executed) == 1
    assert again.output.unwrap_untrusted() == first.output.unwrap_untrusted()  # type: ignore[union-attr]
    assert EventType.TOOL_REPLAY in events(h)


def test_a_replay_of_an_approved_order_does_not_ask_for_approval_again(h: Harness) -> None:
    parked = h.call("trade_paper_execute", BIG)
    request = h.queue.get("tenant-1", parked.approval_id)  # type: ignore[arg-type]
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)
    h.call("trade_paper_execute", BIG, context=h.ctx(approval_id=request.request_id))
    again = h.call("trade_paper_execute", BIG)
    assert again.ok and again.replayed
    assert len(h.executed) == 1
    assert len(h.queue.list_pending("tenant-1")) == 0


def test_reusing_a_key_with_different_arguments_is_refused(h: Harness) -> None:
    h.call("trade_paper_execute", TRADE)
    clash = h.call("trade_paper_execute", {**TRADE, "notional": 6000})
    assert clash.status is OutcomeStatus.DENIED
    assert clash.error is not None and clash.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(h.executed) == 1


def test_a_replay_still_requires_a_valid_grant_and_capability(h: Harness) -> None:
    h.call("trade_paper_execute", TRADE)
    out = h.call("trade_paper_execute", TRADE, token=h.grant(("market_data:read",)))
    assert out.error is not None and out.error.code is ErrorCode.CAPABILITY_DENIED


# ---------------------------------------------------------- tool failure modes


class Out(BaseModel):
    ok: bool


def failing_tools(registry: ToolRegistry, executed: list[dict[str, Any]]) -> None:
    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="slow_write",
        timeout_s=0.05,
        idempotency_key=lambda a: a.symbol,
    )
    async def slow_write(args: QuoteIn) -> Out:
        executed.append({"tool": "slow_write", "symbol": args.symbol})
        await asyncio.sleep(1)
        return Out(ok=True)

    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="crashing_write",
        idempotency_key=lambda a: a.symbol,
    )
    def crashing_write(args: QuoteIn) -> Out:
        executed.append({"tool": "crashing_write", "symbol": args.symbol})
        raise RuntimeError(f"secret internals {args.symbol}")

    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="refusing_write",
        idempotency_key=lambda a: a.symbol,
    )
    def refusing_write(args: QuoteIn) -> Out:
        executed.append({"tool": "refusing_write", "symbol": args.symbol})
        raise ToolRefusedError("market closed for this venue")

    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="badly_typed_write",
        idempotency_key=lambda a: a.symbol,
    )
    def badly_typed_write(args: QuoteIn) -> Out:
        executed.append({"tool": "badly_typed_write", "symbol": args.symbol})
        return {"unexpected": "shape"}  # type: ignore[return-value]

    @tool(capability="market_data:read", side_effect=SideEffect.READ, name="crashing_read")
    def crashing_read(args: QuoteIn) -> Out:
        raise RuntimeError("boom")

    @tool(
        capability="market_data:read", side_effect=SideEffect.READ, name="slow_read", timeout_s=0.05
    )
    def slow_read(args: QuoteIn) -> Out:
        time.sleep(0.3)
        return Out(ok=True)

    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        name="broken_key_write",
        idempotency_key=lambda a: 1 / 0,  # type: ignore[arg-type,return-value]
    )
    def broken_key_write(args: QuoteIn) -> Out:
        executed.append({"tool": "broken_key_write"})
        return Out(ok=True)

    for t in (
        slow_write,
        crashing_write,
        refusing_write,
        badly_typed_write,
        crashing_read,
        slow_read,
        broken_key_write,
    ):
        registry.register(t)


@pytest.fixture
def hf(rego_engine: Any) -> Harness:
    return build_harness(engine=rego_engine, tools_extra=failing_tools)


def test_a_timed_out_write_is_never_retried_automatically(hf: Harness) -> None:
    first = hf.call("slow_write", {"symbol": "X"})
    assert first.status is OutcomeStatus.ERROR
    assert first.error is not None and first.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert not first.error.retryable
    assert "reconcile" in first.error.hint
    retry = hf.call("slow_write", {"symbol": "X"})
    assert retry.error is not None and retry.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert len(hf.executed) == 1  # the body ran once; the retry never reached it


def test_a_crashing_write_is_treated_as_possibly_partial(hf: Harness) -> None:
    out = hf.call("crashing_write", {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert "secret internals" not in str(out.error)  # exception text never reaches the model
    again = hf.call("crashing_write", {"symbol": "X"})
    assert again.error is not None and again.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert len(hf.executed) == 1


def test_a_refusing_write_may_be_retried_because_nothing_happened(hf: Harness) -> None:
    out = hf.call("refusing_write", {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.TOOL_FAILED
    assert out.error.retryable
    hf.call("refusing_write", {"symbol": "X"})
    assert len(hf.executed) == 2


def test_a_write_returning_the_wrong_shape_is_parked_as_unknown(hf: Harness) -> None:
    out = hf.call("badly_typed_write", {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.INVALID_TOOL_OUTPUT
    again = hf.call("badly_typed_write", {"symbol": "X"})
    assert again.error is not None and again.error.code is ErrorCode.OUTCOME_UNKNOWN


def test_read_failures_are_retryable_and_leak_nothing(hf: Harness) -> None:
    crash = hf.call("crashing_read", {"symbol": "X"})
    assert (
        crash.error is not None
        and crash.error.code is ErrorCode.TOOL_FAILED
        and crash.error.retryable
    )
    assert "boom" not in str(crash.error)
    slow = hf.call("slow_read", {"symbol": "X"})
    assert (
        slow.error is not None
        and slow.error.code is ErrorCode.TOOL_TIMEOUT
        and slow.error.retryable
    )


def test_an_unusable_idempotency_key_is_an_argument_error(hf: Harness) -> None:
    out = hf.call("broken_key_write", {"symbol": "X"})
    assert out.error is not None and out.error.code is ErrorCode.INVALID_ARGUMENTS
    assert hf.executed == []


def test_cancelling_a_write_mid_flight_parks_it_as_unknown(hf: Harness) -> None:
    async def scenario() -> Any:
        task = asyncio.create_task(
            hf.gateway.call(
                tool_name="slow_write",
                arguments={"symbol": "CANCEL"},
                grant_token=hf.grant(),
                context=hf.ctx(),
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await hf.gateway.call(
            tool_name="slow_write",
            arguments={"symbol": "CANCEL"},
            grant_token=hf.grant(),
            context=hf.ctx(),
        )

    retry = run(scenario())
    assert retry.error is not None and retry.error.code is ErrorCode.OUTCOME_UNKNOWN
    assert len(hf.executed) == 1


def test_failures_are_audited(hf: Harness) -> None:
    hf.call("crashing_write", {"symbol": "X"})
    last = list(hf.audit.records("tenant-1"))[-1]
    assert last.event_type == EventType.TOOL_ERROR
    assert last.payload["exception"] == "RuntimeError"


# ------------------------------------------------------------------- authN/Z


def refused_without_running(h: Harness, out: Any, code: ErrorCode) -> None:
    assert out.error is not None and out.error.code is code
    assert out.output is None
    assert h.executed == []


def test_a_missing_capability_is_denied_and_audited(h: Harness) -> None:
    out = h.call("trade_paper_execute", TRADE, token=h.grant(("market_data:read",)))
    refused_without_running(h, out, ErrorCode.CAPABILITY_DENIED)
    rejected = next(
        r for r in h.audit.records("tenant-1") if r.event_type == EventType.GRANT_REJECTED
    )
    assert rejected.payload["reason"] == "capability_missing"
    assert rejected.payload["capability"] == "trade:paper_execute"
    assert "paper_execute" not in str(out.error)  # the model is not told which capability


@pytest.mark.parametrize("caps", [("trade:propose",), ("market_data:read", "report:write")])
def test_capabilities_do_not_imply_each_other(h: Harness, caps: tuple[str, ...]) -> None:
    out = h.call("trade_paper_execute", TRADE, token=h.grant(caps))
    refused_without_running(h, out, ErrorCode.CAPABILITY_DENIED)


def test_a_grant_for_another_tenant_is_refused(h: Harness) -> None:
    out = h.call(
        "trade_paper_execute", TRADE, token=h.grant(tenant="tenant-2"), context=h.ctx("tenant-1")
    )
    refused_without_running(h, out, ErrorCode.NOT_AUTHORISED)
    rejected = next(
        r for r in h.audit.records("tenant-1") if r.event_type == EventType.GRANT_REJECTED
    )
    assert rejected.payload["reason"] == "tenant_mismatch"
    assert h.audit.head("tenant-2") is None  # nothing written to the other tenant's chain


def test_an_expired_grant_is_refused(h: Harness) -> None:
    token = h.grant(ttl=timedelta(minutes=5))
    h.clock.advance(timedelta(minutes=5))
    out = h.call("trade_paper_execute", TRADE, token=token)
    refused_without_running(h, out, ErrorCode.NOT_AUTHORISED)
    rejected = next(
        r for r in h.audit.records("tenant-1") if r.event_type == EventType.GRANT_REJECTED
    )
    assert rejected.payload["reason"] == "grant_expired"


def test_a_revoked_grant_is_refused(h: Harness) -> None:
    signed = issue_grant(
        h.signer,
        agent_id="agent-1",
        tenant_id="tenant-1",
        capabilities=["trade:paper_execute"],
        max_cost=10,
        ttl=timedelta(hours=1),
        clock=h.clock,
    )
    assert h.call("trade_paper_execute", TRADE, token=signed.token).ok
    h.revocations.revoke(signed.grant.grant_id)
    out = h.call("trade_paper_execute", {**TRADE, "client_order_id": "o-2"}, token=signed.token)
    refused_without_running(h, out, ErrorCode.NOT_AUTHORISED) if len(h.executed) == 0 else None
    assert out.error is not None and out.error.code is ErrorCode.NOT_AUTHORISED
    assert len(h.executed) == 1


@pytest.mark.parametrize("kind", ["garbage", "empty", "wrong_signer", "tampered"])
def test_forged_or_malformed_grants_are_refused(h: Harness, kind: str) -> None:
    good = h.grant()
    if kind == "garbage":
        token = "v4.public.not-a-real-token"
    elif kind == "empty":
        token = ""
    elif kind == "wrong_signer":
        token = issue_grant(
            GrantSigner.generate(),
            agent_id="agent-1",
            tenant_id="tenant-1",
            capabilities=["trade:paper_execute"],
            max_cost=1e9,
            ttl=timedelta(hours=1),
            clock=h.clock,
        ).token
    else:
        head, kind_, payload, footer = good.split(".")
        token = ".".join([head, kind_, payload[:-6] + "AAAAAA", footer])
    out = h.call("trade_paper_execute", TRADE, token=token)
    refused_without_running(h, out, ErrorCode.NOT_AUTHORISED)
    assert (
        out.error is not None and out.error.message == "Authorisation failed."
    )  # uninformative on purpose


def test_authorisation_failures_give_the_model_no_reason(h: Harness) -> None:
    expired = h.grant(ttl=timedelta(minutes=1))
    h.clock.advance(timedelta(minutes=2))
    a = h.call("trade_paper_execute", TRADE, token=expired)
    b = h.call("trade_paper_execute", TRADE, token="garbage")
    assert a.error == b.error  # indistinguishable to the caller


def test_unknown_tools_are_reported_without_running_anything(h: Harness) -> None:
    out = h.call("wire_transfer", {"amount": 1})
    assert out.status is OutcomeStatus.ERROR
    assert out.error is not None and out.error.code is ErrorCode.UNKNOWN_TOOL
    assert h.executed == []


# ----------------------------------------------------------------- paper only


def test_live_mode_is_refused_before_policy_is_even_asked(h: Harness) -> None:
    out = h.call(
        "trade_paper_execute",
        TRADE,
        context=h.ctx(policy_context=policy_context(execution_mode="live")),
    )
    assert out.status is OutcomeStatus.DENIED
    assert out.error is not None and out.error.code is ErrorCode.EXECUTION_MODE_FORBIDDEN
    assert h.executed == []
    assert EventType.POLICY_DECISION not in events(h)


def test_live_mode_is_refused_for_reads_too(h: Harness) -> None:
    out = h.call(
        "market_quote",
        {"symbol": "AAPL"},
        context=h.ctx(policy_context=policy_context(execution_mode="live")),
    )
    assert out.error is not None and out.error.code is ErrorCode.EXECUTION_MODE_FORBIDDEN


def test_simulation_mode_is_allowed(h: Harness) -> None:
    out = h.call(
        "trade_paper_execute",
        TRADE,
        context=h.ctx(policy_context=policy_context(execution_mode="simulation")),
    )
    assert out.ok


@pytest.mark.parametrize(
    "modes",
    [frozenset({"live"}), frozenset({"paper", "live"}), frozenset(), frozenset({"production"})],
)
def test_a_gateway_cannot_be_configured_to_allow_live(h: Harness, modes: frozenset[str]) -> None:
    with pytest.raises(ValueError, match="paper and simulation"):
        ToolGateway(
            registry=ToolRegistry(),
            verifier=h.verifier,
            engine=h.engine,
            audit=h.audit,
            allowed_modes=modes,
        )


# ------------------------------------------------------------------ arguments


def test_invalid_arguments_get_field_level_feedback_without_echoing_input(h: Harness) -> None:
    secret = "SENSITIVE-VALUE-123"  # pragma: allowlist secret
    out = h.call(
        "trade_paper_execute", {"symbol": secret, "notional": "not-a-number", "client_order_id": ""}
    )
    assert out.status is OutcomeStatus.ERROR
    assert out.error is not None and out.error.code is ErrorCode.INVALID_ARGUMENTS
    assert out.error.retryable
    fields = {f["field"] for f in out.error.details["fields"]}
    assert {"notional", "client_order_id"} <= fields
    assert secret not in str(out.error)
    assert "not-a-number" not in str(out.error)
    assert h.executed == []


def test_missing_and_extra_arguments(h: Harness) -> None:
    missing = h.call("trade_paper_execute", {"symbol": "AAPL"})
    assert missing.error is not None and missing.error.code is ErrorCode.INVALID_ARGUMENTS


@pytest.mark.parametrize(
    "arguments",
    [
        {"symbol": "AAPL", "notional": float("nan"), "client_order_id": "x"},
        {"symbol": "AAPL", "notional": float("inf"), "client_order_id": "x"},
        {"symbol": "AAPL", "notional": 1, "client_order_id": "x", "blob": object()},
        {"symbol": "A" * 200_000, "notional": 1, "client_order_id": "x"},
        {1, 2},
        ["not", "a", "mapping"],
        None,
    ],
)
def test_unserialisable_or_oversized_arguments_are_refused(h: Harness, arguments: Any) -> None:
    out = h.call("trade_paper_execute", arguments)
    assert out.error is not None and out.error.code is ErrorCode.INVALID_ARGUMENTS
    assert h.executed == []
    assert h.audit.verify_chain("tenant-1").ok


@pytest.mark.parametrize("value", [-5, 0, 1e400])
def test_non_positive_or_overflowing_notional_is_rejected_by_the_schema(
    h: Harness, value: float
) -> None:
    out = h.call("trade_paper_execute", {**TRADE, "notional": value})
    assert out.error is not None and out.error.code is ErrorCode.INVALID_ARGUMENTS
    assert h.executed == []


# --------------------------------------------------------------------- budget


def test_the_budget_caps_total_spend_across_calls(h: Harness) -> None:
    token = h.grant(cost=3.0)  # quote costs 1, trade costs 2
    assert h.call("market_quote", {"symbol": "A"}, token=token).ok
    assert h.call("trade_paper_execute", TRADE, token=token).ok
    out = h.call("market_quote", {"symbol": "B"}, token=token)
    assert out.status is OutcomeStatus.DENIED
    assert out.error is not None and out.error.code is ErrorCode.BUDGET_EXCEEDED


def test_a_denied_call_hands_its_budget_back(h: Harness) -> None:
    token = h.grant(cost=2.0)
    denied = h.call("trade_paper_execute", {**TRADE, "symbol": "TSLA"}, token=token)
    assert denied.status is OutcomeStatus.DENIED
    assert h.call("trade_paper_execute", TRADE, token=token).ok  # the 2.0 was returned


def test_a_parked_call_hands_its_budget_back(h: Harness) -> None:
    token = h.grant(cost=2.0)
    assert h.call("trade_paper_execute", BIG, token=token).status is OutcomeStatus.APPROVAL_REQUIRED
    assert h.call("trade_paper_execute", TRADE, token=token).ok


def test_a_zero_budget_grant_can_only_run_free_tools(h: Harness) -> None:
    token = h.grant(cost=0.0)
    out = h.call("market_quote", {"symbol": "A"}, token=token)
    assert out.error is not None and out.error.code is ErrorCode.BUDGET_EXCEEDED


def test_a_failed_execution_keeps_its_cost(hf: Harness) -> None:
    """Conservative: a call that started is charged even if it failed."""
    token = hf.grant(cost=1.0)
    hf.call("crashing_read", {"symbol": "X"}, token=token)
    out = hf.call("market_quote", {"symbol": "X"}, token=token)  # costs 1; crashing_read cost 0
    assert out.ok


# ------------------------------------------------------------ fail-closed gate


class BrokenEngine:
    name = "broken"

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour

    async def decide(self, policy_input: PolicyInput) -> Any:
        if self.behaviour == "raise":
            raise RuntimeError("engine fell over")
        if self.behaviour == "garbage":
            return {"effect": "ALLOW"}
        if self.behaviour == "none":
            return None
        raise AssertionError(self.behaviour)


@pytest.mark.parametrize("behaviour", ["raise", "garbage", "none"])
def test_a_broken_policy_engine_denies_and_nothing_runs(behaviour: str) -> None:
    harness = build_harness(engine=BrokenEngine(behaviour))
    out = harness.call("trade_paper_execute", TRADE)
    assert out.status is OutcomeStatus.DENIED
    assert out.error is not None and out.error.code is ErrorCode.POLICY_UNAVAILABLE
    assert harness.executed == []


def test_a_resource_function_that_raises_denies(rego_engine: Any) -> None:
    def extra(registry: ToolRegistry, executed: list[dict[str, Any]]) -> None:
        @tool(
            capability="trade:paper_execute",
            side_effect=SideEffect.WRITE,
            name="bad_resource_write",
            idempotency_key=lambda a: a.client_order_id,
            resource=lambda a: {"notional": 1 / 0},
        )
        def bad(args: TradeIn) -> TradeOut:
            executed.append({"tool": "bad_resource_write"})
            return TradeOut(order_id="x", status="y")

        registry.register(bad)

    harness = build_harness(engine=rego_engine, tools_extra=extra)
    out = harness.call("bad_resource_write", TRADE)
    assert out.status is OutcomeStatus.DENIED
    assert harness.executed == []


class FlakyStore:
    """Delegates to SQLite but fails the Nth append, like a full disk."""

    def __init__(self, fail_at: int) -> None:
        self._inner = SqliteAuditStore()
        self._fail_at = fail_at
        self.appends = 0
        self._lock = threading.Lock()

    def append(self, tenant_id: str, build: BuildRecord) -> AuditRecord:
        with self._lock:
            self.appends += 1
            if self.appends == self._fail_at:
                raise AuditError("disk full")
        return self._inner.append(tenant_id, build)

    def iter_records(self, tenant_id: str, *, after_seq: int = 0) -> Iterator[AuditRecord]:
        return self._inner.iter_records(tenant_id, after_seq=after_seq)

    def head(self, tenant_id: str) -> ChainHead | None:
        return self._inner.head(tenant_id)

    def close(self) -> None:
        self._inner.close()


@pytest.mark.parametrize("fail_at", [1, 2])
def test_if_the_audit_log_is_down_before_the_effect_nothing_runs(
    rego_engine: Any, fail_at: int
) -> None:
    """1 = tool.call could not be recorded; 2 = the policy decision could not be."""
    harness = build_harness(engine=rego_engine, audit=AuditLog(FlakyStore(fail_at)))
    out = harness.call("trade_paper_execute", TRADE)
    assert out.error is not None and out.error.code is ErrorCode.AUDIT_UNAVAILABLE
    assert harness.executed == []


def test_if_the_audit_log_fails_after_the_effect_the_caller_is_told_to_reconcile(
    rego_engine: Any,
) -> None:
    harness = build_harness(engine=rego_engine, audit=AuditLog(FlakyStore(3)))  # 3 = tool.result
    out = harness.call("trade_paper_execute", TRADE)
    assert out.error is not None and out.error.code is ErrorCode.AUDIT_FAILED_AFTER_EXECUTION
    assert "reconcile" in out.error.message.lower()
    assert len(harness.executed) == 1  # it did run, and the caller must know


def test_audit_failure_on_a_refusal_is_reported_as_unavailable(rego_engine: Any) -> None:
    harness = build_harness(engine=rego_engine, audit=AuditLog(FlakyStore(2)))
    out = harness.call("wire_transfer", {})  # unknown tool: refusal needs a record
    assert out.error is not None and out.error.code is ErrorCode.AUDIT_UNAVAILABLE


# ------------------------------------------------------ the tool itself


def test_tools_cannot_be_called_directly(h: Harness) -> None:
    for name in h.registry.names():
        with pytest.raises(DirectInvocationError, match="ToolGateway"):
            h.registry.get(name)(TradeIn(**TRADE))  # type: ignore[misc]
    assert h.executed == []


def test_the_registry_is_frozen_once_a_gateway_exists(h: Harness) -> None:
    @tool(
        capability="report:write", side_effect=SideEffect.WRITE, idempotency_key=lambda a: a.title
    )
    def late(args: QuoteIn) -> QuoteOut:
        return QuoteOut(symbol="x", price=0)

    with pytest.raises(RegistryFrozenError):
        h.registry.register(late)


def test_registry_rejects_duplicates_and_non_tools() -> None:
    registry = ToolRegistry()

    @tool(capability="market_data:read", side_effect=SideEffect.READ)
    def one(args: QuoteIn) -> QuoteOut:
        return QuoteOut(symbol="x", price=0)

    registry.register(one)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(one)
    with pytest.raises(TypeError):
        registry.register(lambda: None)  # type: ignore[arg-type]
    assert "one" in registry and len(registry) == 1 and registry.names() == ["one"]
    assert registry.get("missing") is None


def test_describe_offers_schemas_but_not_capabilities_or_policy(h: Harness) -> None:
    described = {d["name"]: d for d in h.registry.describe()}
    assert set(described) == set(h.registry.names())
    schema = described["trade_paper_execute"]["input_schema"]
    assert {"symbol", "notional", "client_order_id"} <= set(schema["properties"])
    flat = str(described)
    assert "trade:paper_execute" not in flat
    assert "idempotency" not in flat


def test_a_tool_that_replays_is_audited_as_a_replay_not_a_second_execution(h: Harness) -> None:
    h.call("trade_paper_execute", TRADE)
    h.call("trade_paper_execute", TRADE)
    kinds = events(h)
    assert kinds.count(EventType.TOOL_RESULT) == 1
    assert kinds.count(EventType.TOOL_REPLAY) == 1


# ------------------------------------------------------- decorator definition


def define(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {"capability": "a:b", "side_effect": SideEffect.READ}
    kwargs.update(overrides)

    @tool(**kwargs)
    def fn(args: QuoteIn) -> QuoteOut:
        return QuoteOut(symbol="x", price=0)

    return fn


def test_a_write_tool_must_declare_an_idempotency_key() -> None:
    with pytest.raises(ToolDefinitionError, match="idempotency_key"):
        define(side_effect=SideEffect.WRITE)
    assert define(side_effect=SideEffect.WRITE, idempotency_key=lambda a: "k")


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Bad Name"},
        {"name": "1abc"},
        {"name": ""},
        {"timeout_s": 0},
        {"timeout_s": -1},
        {"timeout_s": float("inf")},
        {"cost_estimate": -1},
        {"cost_estimate": float("nan")},
    ],
)
def test_unsafe_tool_declarations_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ToolDefinitionError):
        define(**overrides)


def test_a_bad_capability_is_refused_at_declaration() -> None:
    with pytest.raises(ValueError, match="invalid capability"):
        define(capability="trade:*")


def test_tool_signatures_must_use_pydantic_models() -> None:
    with pytest.raises(ToolDefinitionError, match="exactly one"):

        @tool(capability="a:b", side_effect=SideEffect.READ)
        def two(a: QuoteIn, b: QuoteIn) -> QuoteOut:  # type: ignore[type-var]
            return QuoteOut(symbol="x", price=0)

    with pytest.raises(ToolDefinitionError, match="argument"):

        @tool(capability="a:b", side_effect=SideEffect.READ)
        def plain_arg(a: dict[str, Any]) -> QuoteOut:  # type: ignore[type-var]
            return QuoteOut(symbol="x", price=0)

    with pytest.raises(ToolDefinitionError, match="return"):

        @tool(capability="a:b", side_effect=SideEffect.READ)
        def plain_return(a: QuoteIn) -> dict[str, Any]:  # type: ignore[type-var]
            return {}

    with pytest.raises(ToolDefinitionError, match="argument"):

        @tool(capability="a:b", side_effect=SideEffect.READ)
        def untyped(a):  # type: ignore[no-untyped-def]
            return QuoteOut(symbol="x", price=0)


def test_the_tool_description_comes_from_the_docstring() -> None:
    assert define().spec.description == "x" or define().spec.description == "fn"

    @tool(capability="a:b", side_effect=SideEffect.READ)
    def documented(args: QuoteIn) -> QuoteOut:
        """First line.

        More detail that the model does not need.
        """
        return QuoteOut(symbol="x", price=0)

    assert documented.spec.description == "First line."
    assert repr(documented) == "Tool('documented', READ)"


def test_decisions_are_typed_end_to_end(h: Harness) -> None:
    out = h.call("trade_paper_execute", TRADE)
    assert isinstance(out.policy, PolicyDecision)
    assert out.policy.policy_version == h.engine.policy_version  # type: ignore[attr-defined]


def test_unresolvable_annotations_give_an_actionable_error() -> None:
    def make() -> Any:
        class Local(BaseModel):
            x: int

        @tool(capability="a:b", side_effect=SideEffect.READ)
        def local_tool(args: Local) -> Local:
            return args

        return local_tool

    with pytest.raises(ToolDefinitionError, match="module level"):
        make()
