"""A governed agent whose audit log, approvals inbox and policy live in Keelgate Cloud.

    python examples/cloud_governed_agent.py --url http://localhost:8000 --api-key kg_live_...
    python examples/cloud_governed_agent.py ... --policy-pack finance_basic   # registry pack

Everything the agent does goes through the same gateway as the quickstart. What changes is where
the records live: audit events are chained and stored by the control plane, an action that needs a
person waits in the hosted approvals inbox, and (with ``--policy-pack``) the policy is the version
your team deployed there. The policy gate still runs in this process, and the paper-trade tool
only appends to a list: nothing real is ever executed.

The third proposal needs a person. The script prints where to approve it and waits.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from keelgate.approvals import ApprovalStatus
from keelgate.audit import AuditLog, verify_chain
from keelgate.capabilities import GrantSigner, GrantVerifier, issue_grant
from keelgate.cloud import CloudApprovalQueue, CloudAuditStore, CloudClient, CloudPolicyEngine
from keelgate.policy import PolicyContext, PolicyEngine, RegoEngine
from keelgate.tools import CallContext, SideEffect, ToolGateway, ToolOutcome, ToolRegistry, tool

# Pinned to a Monday at 10:30 in New York so the shipped pack's trading-hours rule is satisfied
# whenever you run this. In a real deployment as_of comes from your own trusted clock.
AS_OF = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
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


BLOTTER: list[TradeIn] = []  # the only side effect: a paper order list


@tool(capability="market_data:read", side_effect=SideEffect.READ)
def market_quote(args: QuoteIn) -> QuoteOut:
    """Latest quote for a symbol."""
    return QuoteOut(symbol=args.symbol, price=187.25)


@tool(
    capability="trade:paper_execute",
    side_effect=SideEffect.WRITE,
    cost_estimate=1.0,
    idempotency_key=lambda a: a.client_order_id,
    resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
)
def trade_paper_execute(args: TradeIn) -> TradeOut:
    """Place a PAPER trade. Nothing real is ever executed."""
    BLOTTER.append(args)
    return TradeOut(order_id=f"paper-{len(BLOTTER)}", status="filled")


def show(label: str, outcome: ToolOutcome) -> None:
    detail = ""
    if outcome.error is not None:
        reasons = outcome.error.details.get("reasons")
        detail = outcome.error.code.value + (f" - {'; '.join(reasons)}" if reasons else "")
    elif outcome.approval_id:
        detail = f"needs {outcome.approval_tier.value if outcome.approval_tier else '?'}"
    print(f"  {label:<44} -> {outcome.status.value:<18} {detail}")


def wait_for_decision(
    queue: CloudApprovalQueue, tenant: str, request_id: str, wait_s: float
) -> str:
    """Poll the hosted inbox until a person decides (or the wait runs out)."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        status = queue.get(tenant, request_id).status
        if status is not ApprovalStatus.PENDING:
            return status.value
        time.sleep(1.0)
    return ApprovalStatus.PENDING.value


async def run_agent(
    url: str,
    api_key: str,
    *,
    policy_pack: str | None = None,
    wait_s: float = 300.0,
    announce: Any = print,
) -> int:
    """Run the demo against a control plane. Returns a process exit code."""
    client = CloudClient(url, api_key)
    tenant = client.whoami()["tenant_id"]
    engine: PolicyEngine = (
        CloudPolicyEngine.from_registry(client, policy_pack) if policy_pack else RegoEngine()
    )
    audit = AuditLog(CloudAuditStore(CloudClient(url, api_key)))
    approvals = CloudApprovalQueue(CloudClient(url, api_key))

    signer = GrantSigner.generate(key_id="demo-key")
    verifier = GrantVerifier({"demo-key": signer.public_key_pem()}, clock=lambda: AS_OF)
    registry = ToolRegistry()
    registry.register(market_quote)
    registry.register(trade_paper_execute)
    gateway = ToolGateway(
        registry=registry, verifier=verifier, engine=engine, audit=audit, approvals=approvals
    )
    grant = issue_grant(
        signer,
        agent_id="research-agent",
        tenant_id=tenant,
        capabilities=["market_data:read", "trade:paper_execute"],
        max_cost=50,
        ttl=timedelta(hours=1),
        clock=lambda: AS_OF,
    )
    context = PolicyContext(
        as_of=AS_OF, execution_mode="paper", limits=LIMITS, exposure={"daily_notional": 0}
    )

    async def call(name: str, arguments: dict[str, Any], **extra: Any) -> ToolOutcome:
        return await gateway.call(
            tool_name=name,
            arguments=arguments,
            grant_token=grant.token,
            context=CallContext(tenant_id=tenant, policy_context=context, **extra),
        )

    announce(f"\nKeelgate Cloud: tenant {tenant}   policy: {engine.name}\n")
    show("market.quote AAPL (READ)", await call("market_quote", {"symbol": "AAPL"}))
    small = {"symbol": "AAPL", "notional": 5_000, "client_order_id": "cloud-order-1"}
    show("paper trade AAPL $5,000", await call("trade_paper_execute", small))
    show(
        "paper trade TSLA $1,000 (restricted)",
        await call(
            "trade_paper_execute",
            {"symbol": "TSLA", "notional": 1_000, "client_order_id": "cloud-order-2"},
        ),
    )
    big = {"symbol": "AAPL", "notional": 30_000, "client_order_id": "cloud-order-3"}
    parked = await call(
        "trade_paper_execute",
        big,
        rationale="Breakout above the 50-day average on rising volume.",
        confidence=0.71,
        verifier_flags=("price-cross-checked",),
    )
    show("paper trade AAPL $30,000", parked)
    if parked.approval_id is None:
        announce("  unexpected: no approval was requested")
        return 1
    request = approvals.get(tenant, parked.approval_id)
    announce(f"\n  waiting for a person to decide request {request.request_id}")
    announce(f"  tier {request.tier.value}; sign-off code {request.signoff_code}")
    decision = await asyncio.to_thread(
        wait_for_decision, approvals, tenant, request.request_id, wait_s
    )
    announce(f"  decision: {decision}")
    if decision != ApprovalStatus.APPROVED.value:
        return 2
    before = len(BLOTTER)
    show(
        "resume with the approval",
        await call("trade_paper_execute", big, approval_id=request.request_id),
    )
    announce(f"  orders on the paper blotter: {before} -> {len(BLOTTER)}")

    records = list(audit.records(tenant))
    head = audit.head(tenant)
    result = verify_chain(records, tenant_id=tenant, expected_head=head)
    announce(
        f"\n  audit chain: {'OK' if result.ok else 'FAILED'} - {result.records_checked} records"
    )
    return 0 if result.ok and len(BLOTTER) == before + 1 else 1


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True, help="control plane base URL")
    parser.add_argument("--api-key", required=True, help="an API key from the dashboard")
    parser.add_argument("--policy-pack", help="use this pack from the policy registry")
    parser.add_argument("--wait", type=float, default=300.0, help="seconds to wait for approval")
    args = parser.parse_args(argv)
    return asyncio.run(
        run_agent(args.url, args.api_key, policy_pack=args.policy_pack, wait_s=args.wait)
    )


if __name__ == "__main__":
    sys.exit(main())
