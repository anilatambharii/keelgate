"""Run a custom policy pack: the code behind the "write your own policy" guide.

    python examples/custom_policy_pack/run.py

Keelgate's policy layer is one small protocol (``PolicyEngine``). The shipped ``RegoEngine`` loads
any directory of ``.rego`` files and asks the rule you name, so a custom pack is: a Rego file with
a ``decision`` rule, its tests (``opa test examples/custom_policy_pack``), and this much Python.

Nothing here is specific to money movement. It uses only Keelgate's public API.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from keelgate.approvals import ApprovalQueue
from keelgate.audit import AuditLog
from keelgate.capabilities import GrantSigner, GrantVerifier, issue_grant
from keelgate.policy import PolicyContext, RegoEngine
from keelgate.tools import CallContext, SideEffect, ToolGateway, ToolRegistry, tool

PACK = Path(__file__).parent
QUERY = "data.acme.transfer_limits.decision"  # <package>.<rule> in transfer_limits.rego
AS_OF = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
LIMITS = {
    "allowed_destinations": ["acct-payroll", "acct-vendors"],
    "max_transfer": 10_000,
    "max_daily_total": 25_000,
    "approval_threshold": 2_000,
}
SENT: list[dict[str, Any]] = []


class TransferIn(BaseModel):
    destination: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, allow_inf_nan=False)
    transfer_id: str = Field(min_length=1, max_length=64)


class TransferOut(BaseModel):
    sent: bool


@tool(
    capability="treasury:transfer",
    side_effect=SideEffect.WRITE,
    idempotency_key=lambda a: a.transfer_id,
    # These two fields are exactly what the Rego pack reads as input.resource.
    resource=lambda a: {"destination": a.destination, "amount": a.amount},
)
def transfer_funds(args: TransferIn) -> TransferOut:
    """Move money between internal accounts (a paper ledger here)."""
    SENT.append(args.model_dump())
    return TransferOut(sent=True)


def main() -> int:
    engine = RegoEngine(PACK, query=QUERY)  # a pack that does not compile fails here, at start-up
    signer = GrantSigner.generate()
    audit = AuditLog(clock=lambda: AS_OF)
    registry = ToolRegistry()
    registry.register(transfer_funds)
    gateway = ToolGateway(
        registry=registry,
        verifier=GrantVerifier({signer.key_id: signer.public_key_pem()}, clock=lambda: AS_OF),
        engine=engine,
        audit=audit,
        approvals=ApprovalQueue(audit=audit, clock=lambda: AS_OF),
    )
    token = issue_grant(
        signer,
        agent_id="treasury-agent",
        tenant_id="acme",
        capabilities=["treasury:transfer"],
        max_cost=10,
        ttl=timedelta(hours=1),
        clock=lambda: AS_OF,
    ).token
    context = CallContext(
        tenant_id="acme",
        policy_context=PolicyContext(
            as_of=AS_OF,
            execution_mode="paper",
            limits=LIMITS,
            exposure={"daily_total": 0},
        ),
    )

    async def propose(destination: str, amount: float, transfer_id: str) -> Any:
        return await gateway.call(
            tool_name="transfer_funds",
            arguments={"destination": destination, "amount": amount, "transfer_id": transfer_id},
            grant_token=token,
            context=context,
        )

    print(f"policy pack: transfer_limits, version {engine.policy_version[:19]}...")
    cases = [
        ("a small transfer to an allowed account", ("acct-vendors", 1_500, "t-1")),
        ("an account that is not on the allowlist", ("acct-offshore", 100, "t-2")),
        ("over the per-transfer cap", ("acct-vendors", 12_000, "t-3")),
        ("above the auto-approval threshold", ("acct-vendors", 3_000, "t-4")),
    ]
    statuses = []
    for label, args in cases:
        outcome = asyncio.run(propose(*args))
        why = outcome.error.details.get("reasons") if outcome.error else None
        print(f"  {label:<42} -> {outcome.status.value:<18} {why or ''}")
        statuses.append(outcome.status.value)
    print(f"transfers actually sent: {[s['transfer_id'] for s in SENT]}")
    expected = ["OK", "DENIED", "DENIED", "APPROVAL_REQUIRED"]
    return 0 if statuses == expected and [s["transfer_id"] for s in SENT] == ["t-1"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
