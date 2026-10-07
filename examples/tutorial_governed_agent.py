"""Build a governed agent in 10 minutes: the runnable code behind docs/tutorial.md.

    pip install keelgate
    python examples/tutorial_governed_agent.py

Every section is embedded in the tutorial page, and this file is executed by the test suite, so the
tutorial cannot drift from code that works. It uses only Keelgate's public API and needs no network
and no API key: the "model" is a script (``FakeLLM``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

# --8<-- [start:imports]
from keelgate.approvals import ApprovalQueue, ApprovalTier, Approver
from keelgate.audit import AuditLog, verify_chain
from keelgate.capabilities import GrantSigner, GrantVerifier, issue_grant
from keelgate.loop import InMemoryCheckpointStore, LLMPlanner, Loop, StopConditions
from keelgate.policy import PolicyContext, RegoEngine
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import CallContext, SideEffect, ToolGateway, ToolRegistry, tool

# --8<-- [end:imports]

# A Monday, 10:30 in New York: inside the trading hours the policy pack enforces. In production
# `as_of` comes from your own trusted clock (or a replay cursor), never from the model.
AS_OF = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
LIMITS: dict[str, Any] = {
    "max_notional_per_action": 50_000,
    "max_daily_exposure": 100_000,
    "restricted_symbols": ["TSLA"],
    "approval_one_click_notional": 10_000,
    "approval_explicit_notional": 25_000,
    "trading_hours": {"tz": "America/New_York", "open_minute": 570, "close_minute": 960},
}
BLOTTER: list[dict[str, Any]] = []  # the only side effect: a paper order list


# --8<-- [start:tools]
class QuoteIn(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z]{1,5}$")


class QuoteOut(BaseModel):
    symbol: str
    price: float


class TradeIn(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z]{1,5}$")
    notional: float = Field(gt=0, allow_inf_nan=False)
    client_order_id: str = Field(min_length=1, max_length=64)


class TradeOut(BaseModel):
    order_id: str


@tool(capability="market_data:read", side_effect=SideEffect.READ)
def quote(args: QuoteIn) -> QuoteOut:
    """Latest price for a symbol."""
    return QuoteOut(symbol=args.symbol, price=187.25)


@tool(
    capability="trade:paper_execute",
    side_effect=SideEffect.WRITE,
    # The same client_order_id can never place the order twice, even if the agent retries.
    idempotency_key=lambda a: a.client_order_id,
    # What the policy is allowed to know about this action, derived from *validated* input.
    resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
)
def paper_trade(args: TradeIn) -> TradeOut:
    """Place a PAPER trade. Nothing real is ever executed."""
    BLOTTER.append(args.model_dump())
    return TradeOut(order_id=f"paper-{len(BLOTTER)}")


# --8<-- [end:tools]


# --8<-- [start:authority]
signer = GrantSigner.generate()  # holds the private key; in production, load it from a vault
verifier = GrantVerifier({signer.key_id: signer.public_key_pem()}, clock=lambda: AS_OF)
audit = AuditLog(clock=lambda: AS_OF)
approvals = ApprovalQueue(audit=audit, clock=lambda: AS_OF)

registry = ToolRegistry()
registry.register(quote)
registry.register(paper_trade)

gateway = ToolGateway(
    registry=registry,
    verifier=verifier,
    engine=RegoEngine(),  # the finance_basic policy pack, evaluated in-process
    audit=audit,
    approvals=approvals,
)

# A signed, expiring grant: this agent, this tenant, exactly these capabilities, a budget.
grant = issue_grant(
    signer,
    agent_id="research-agent",
    tenant_id="acme",
    capabilities=["market_data:read", "trade:paper_execute"],
    max_cost=50,
    ttl=timedelta(hours=1),
    clock=lambda: AS_OF,
)
# --8<-- [end:authority]


def context(**extra: Any) -> CallContext:
    """Per-call facts. `policy_context` is trusted: it comes from you, not from the model."""
    policy = PolicyContext(
        as_of=AS_OF, execution_mode="paper", limits=LIMITS, exposure={"daily_notional": 0}
    )
    return CallContext(tenant_id="acme", policy_context=policy, **extra)


# --8<-- [start:calls]
async def propose(tool_name: str, arguments: dict[str, Any], **extra: Any) -> Any:
    return await gateway.call(
        tool_name=tool_name, arguments=arguments, grant_token=grant.token, context=context(**extra)
    )


async def gate_demo() -> dict[str, Any]:
    results: dict[str, Any] = {}
    small = {"symbol": "AAPL", "notional": 5_000, "client_order_id": "t-1"}
    results["allowed"] = await propose("paper_trade", small)
    results["retry"] = await propose("paper_trade", small)  # an exact retry replays, never re-runs
    restricted = {"symbol": "TSLA", "notional": 1_000, "client_order_id": "t-2"}
    results["denied"] = await propose("paper_trade", restricted)

    big = {"symbol": "AAPL", "notional": 30_000, "client_order_id": "t-3"}
    results["parked"] = await propose("paper_trade", big)  # needs a human
    request = approvals.get("acme", results["parked"].approval_id)
    alice = Approver(approver_id="alice", tenant_id="acme", max_tier=ApprovalTier.EXPLICIT_SIGNOFF)
    approvals.approve("acme", request.request_id, alice, signoff_code=request.signoff_code)
    results["approved"] = await propose("paper_trade", big, approval_id=request.request_id)
    return results


# --8<-- [end:calls]


# --8<-- [start:loop]
_checkpoints = InMemoryCheckpointStore()  # use SqliteCheckpointStore(path) to survive restarts


def build_loop(llm: FakeLLM) -> Loop:
    return Loop(
        gateway=gateway,
        registry=registry,
        planner=LLMPlanner(llm, "any-model"),
        checkpoints=_checkpoints,
        grant_token=grant.token,
        policy_context=lambda as_of: PolicyContext(
            as_of=as_of, execution_mode="paper", limits=LIMITS, exposure={"daily_notional": 0}
        ),
        stop=StopConditions(max_iterations=6),  # also: tokens, dollars, a timeout, a goal predicate
        audit=audit,
        approvals=approvals,
        clock=lambda: AS_OF,
    )


# --8<-- [end:loop]


def main() -> int:
    print("1. The gate")
    results = asyncio.run(gate_demo())
    for name, outcome in results.items():
        detail = outcome.error.code.value if outcome.error else (outcome.approval_tier or "")
        print(f"   {name:<9} -> {outcome.status.value:<18} {detail}")

    # --8<-- [start:run]
    print("2. The loop (a scripted model: quote, then trade, then answer)")
    script = [
        Reply.call("quote", symbol="MSFT"),
        Reply.call("paper_trade", symbol="MSFT", notional=2_000, client_order_id="loop-1"),
        Reply.say("Bought 2,000 of MSFT at the quoted price of 187.25."),
    ]
    result = asyncio.run(
        build_loop(FakeLLM(script, indexed=True)).run(
            goal="Check MSFT and buy a small position.",
            tenant_id="acme",
            agent_id="research-agent",
            as_of=AS_OF,
            run_id="tutorial-1",
        )
    )
    print(f"   goal reached: {result.ok}; trace id: {result.state.trace_id}")
    # --8<-- [end:run]

    # --8<-- [start:audit]
    chain = verify_chain(audit.records("acme"), tenant_id="acme")
    print(f"3. Audit: {chain.records_checked} records, chain intact: {chain.ok}")
    # --8<-- [end:audit]
    print(
        f"4. The blotter has {len(BLOTTER)} paper orders: {[b['client_order_id'] for b in BLOTTER]}"
    )
    return 0 if result.ok and chain.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
