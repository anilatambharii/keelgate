"""Keelgate quickstart: ALLOW, DENY and REQUIRE_APPROVAL, then a verified audit chain.

    keelgate quickstart            # in-process policy, no services needed
    keelgate quickstart --tamper   # also show tampering being caught
    KEELGATE_OPA_URL=http://localhost:8181 keelgate quickstart   # real OPA (make up)

The "model" here is just a script proposing tool calls. What matters is that every
proposal goes through the gateway, and the gateway, not the proposer, decides.
Nothing here touches real money: the trade tool only appends to a list.

The policy clock is pinned to a Monday at 10:30 New York time so the demo behaves
the same whenever you run it. In a real deployment ``as_of`` comes from your own
trusted clock or replay cursor, never from the model.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from keelgate.approvals import ApprovalQueue, ApprovalTier, Approver
from keelgate.audit import AuditLog, AuditRecord, verify_chain
from keelgate.capabilities import GrantSigner, GrantVerifier, issue_grant
from keelgate.policy import OpaHttpEngine, PolicyContext, PolicyEngine, RegoEngine
from keelgate.tools import CallContext, SideEffect, ToolGateway, ToolOutcome, ToolRegistry, tool

AS_OF = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)  # Monday, 10:30 in New York

LIMITS: dict[str, Any] = {
    "max_notional_per_action": 50_000,
    "max_daily_exposure": 100_000,
    "restricted_symbols": ["TSLA"],
    "approval_one_click_notional": 10_000,
    "approval_explicit_notional": 25_000,
    "trading_hours": {"tz": "America/New_York", "open_minute": 570, "close_minute": 960},
}


class QuoteIn(BaseModel):
    symbol: str


class QuoteOut(BaseModel):
    symbol: str
    price: float


class TradeIn(BaseModel):
    symbol: str
    notional: float = Field(gt=0, allow_inf_nan=False)
    client_order_id: str


class TradeOut(BaseModel):
    order_id: str
    status: str


PAPER_BLOTTER: list[TradeIn] = []  # the only "side effect": a paper order list


@tool(capability="market_data:read", side_effect=SideEffect.READ)
def market_quote(args: QuoteIn) -> QuoteOut:
    """Latest quote for a symbol."""
    return QuoteOut(symbol=args.symbol, price=187.25)


@tool(
    capability="trade:paper_execute",
    side_effect=SideEffect.WRITE,
    cost_estimate=1.0,
    # A retry with the same client_order_id can never place the order twice.
    idempotency_key=lambda a: a.client_order_id,
    # What the policy needs to know about this action, derived from validated input.
    resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
)
def trade_paper_execute(args: TradeIn) -> TradeOut:
    """Place a PAPER trade. Nothing real is ever executed."""
    PAPER_BLOTTER.append(args)
    return TradeOut(order_id=f"paper-{len(PAPER_BLOTTER)}", status="filled")


def make_engine() -> PolicyEngine:
    url = os.environ.get("KEELGATE_OPA_URL")
    return OpaHttpEngine(url) if url else RegoEngine()


def show(label: str, outcome: ToolOutcome) -> None:
    detail = ""
    if outcome.error is not None:
        reasons = outcome.error.details.get("reasons")
        detail = f"{outcome.error.code.value}" + (f" - {'; '.join(reasons)}" if reasons else "")
    elif outcome.approval_id:
        detail = f"needs {outcome.approval_tier.value if outcome.approval_tier else '?'}"
    elif outcome.replayed:
        detail = "replayed stored result, nothing re-executed"
    print(f"  {label:<44} -> {outcome.status.value:<18} {detail}")


async def main(tamper: bool) -> int:
    engine = make_engine()
    signer = GrantSigner.generate(key_id="demo-key")
    verifier = GrantVerifier({"demo-key": signer.public_key_pem()}, clock=lambda: AS_OF)
    audit = AuditLog(clock=lambda: AS_OF)
    approvals = ApprovalQueue(audit=audit, clock=lambda: AS_OF)

    registry = ToolRegistry()
    registry.register(market_quote)
    registry.register(trade_paper_execute)
    gateway = ToolGateway(
        registry=registry, verifier=verifier, engine=engine, audit=audit, approvals=approvals
    )

    # A signed, expiring grant bound to one agent, one tenant and a budget.
    grant = issue_grant(
        signer,
        agent_id="research-agent",
        tenant_id="acme",
        capabilities=["market_data:read", "trade:paper_execute"],
        max_cost=50,
        ttl=timedelta(hours=1),
        clock=lambda: AS_OF,
    )
    policy_context = PolicyContext(
        as_of=AS_OF, execution_mode="paper", limits=LIMITS, exposure={"daily_notional": 0}
    )

    def ctx(**extra: Any) -> CallContext:
        return CallContext(tenant_id="acme", policy_context=policy_context, **extra)

    async def call(name: str, arguments: dict[str, Any], **extra: Any) -> ToolOutcome:
        return await gateway.call(
            tool_name=name, arguments=arguments, grant_token=grant.token, context=ctx(**extra)
        )

    print("\nKeelgate quickstart - the LLM proposes, deterministic code decides\n")
    print(f"policy engine: {engine.name}   as_of: {AS_OF.isoformat()}   mode: paper\n")

    print("1. Proposals the gateway ALLOWS")
    show("market.quote AAPL (READ)", await call("market_quote", {"symbol": "AAPL"}))
    small = {"symbol": "AAPL", "notional": 5_000, "client_order_id": "order-1"}
    show("paper trade AAPL $5,000", await call("trade_paper_execute", small))
    show("same order again (idempotent retry)", await call("trade_paper_execute", small))

    print("\n2. Proposals the gateway DENIES")
    show(
        "paper trade TSLA $1,000 (restricted symbol)",
        await call(
            "trade_paper_execute",
            {"symbol": "TSLA", "notional": 1_000, "client_order_id": "order-2"},
        ),
    )
    show(
        "paper trade AAPL $80,000 (over per-action cap)",
        await call(
            "trade_paper_execute",
            {"symbol": "AAPL", "notional": 80_000, "client_order_id": "order-3"},
        ),
    )

    print("\n3. A proposal that REQUIRES a human")
    big = {"symbol": "AAPL", "notional": 30_000, "client_order_id": "order-4"}
    parked = await call(
        "trade_paper_execute",
        big,
        rationale="Breakout above the 50-day average on rising volume.",
        confidence=0.71,
        verifier_flags=("price-cross-checked",),
    )
    show("paper trade AAPL $30,000", parked)
    if parked.approval_id is None:
        print("  unexpected: no approval was requested")
        return 1
    request = approvals.get("acme", parked.approval_id)
    print(f"     evidence for the approver: {request.evidence.rationale!r}")
    print(f"     tier {request.tier.value}; signoff code {request.signoff_code}")

    before = len(PAPER_BLOTTER)
    approver = Approver(
        approver_id="alice", tenant_id="acme", max_tier=ApprovalTier.EXPLICIT_SIGNOFF
    )
    approvals.approve("acme", request.request_id, approver, signoff_code=request.signoff_code)
    print("     alice approved it (a human, outside the model)")
    show(
        "resume with the approval",
        await call("trade_paper_execute", big, approval_id=request.request_id),
    )
    print(f"     orders on the paper blotter: {before} -> {len(PAPER_BLOTTER)}")

    print("\n4. The audit chain")
    records = list(audit.records("acme"))
    for r in records:
        print(f"  #{r.seq:<2} {r.event_type:<19} {r.actor:<15} {summarise(r)}  {r.hash[:10]}")
    result = audit.verify_chain("acme")
    head = audit.head("acme")
    print(f"\n  verify_chain: {'OK' if result.ok else 'FAILED'} - {result.records_checked} records")
    print(
        f"  head to anchor outside this process: seq {head.seq if head else 0}, "
        f"{head.hash[:16] if head else ''}..."
    )

    if tamper:
        print("\n5. Tampering is detected")
        # Rewrite history: pretend the blocked restricted-symbol trade had been allowed.
        index = next(
            i
            for i, r in enumerate(records)
            if r.event_type == "policy.decision" and r.payload["effect"] == "DENY"
        )
        original = records[index]
        edited_payload = original.payload_json.replace('"effect":"DENY"', '"effect":"ALLOW"')
        if edited_payload == original.payload_json:
            print("  demo bug: the edit changed nothing")
            return 1
        forged = list(records)
        forged[index] = AuditRecord.model_validate(
            {**original.model_dump(), "payload_json": edited_payload}
        )
        bad = verify_chain(forged, tenant_id="acme")
        print(
            f"  rewrote record #{original.seq} (DENY -> ALLOW): verify_chain -> "
            f"{'OK (!)' if bad.ok else 'FAILED'}: {bad.error}"
        )
        shortened = verify_chain(records[:-2], tenant_id="acme", expected_head=head)
        print(
            f"  deleted the last 2 records, checked against the anchored head -> "
            f"{'OK (!)' if shortened.ok else 'FAILED'}: {shortened.error}"
        )
        if bad.ok or shortened.ok:
            return 1

    if isinstance(engine, OpaHttpEngine):
        await engine.aclose()
    print()
    return 0 if result.ok else 1


def summarise(record: AuditRecord) -> str:
    p = record.payload
    if record.event_type == "policy.decision":
        return f"{p['effect']:<17} {', '.join(p['reasons']) or '-'}"[:48].ljust(48)
    if record.event_type == "tool.call":
        return f"{p['tool']:<17}".ljust(48)
    if record.event_type.startswith("approval."):
        return f"{p.get('decision', p.get('tier', '')):<17}".ljust(48)
    return str(p.get("tool", p.get("reason", ""))).ljust(48)


def run(argv: list[str] | None = None) -> int:
    """Run the quickstart; ``--tamper`` also shows tampering being caught."""
    args = sys.argv[1:] if argv is None else argv
    return asyncio.run(main(tamper="--tamper" in args))


if __name__ == "__main__":
    sys.exit(run())
