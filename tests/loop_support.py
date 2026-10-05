"""A restartable rig for loop tests.

``build_rig(dir)`` wires a gateway over **file-backed** state (idempotency, budgets,
revocations, audit, approvals). Building a second rig over the same directory with the same
signer is a faithful "process restart": everything durable survives, everything in memory is
gone. Genuine tool executions are appended to a file, so duplicates are countable across
restarts, and even across real subprocesses.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from keelgate.approvals import ApprovalQueue
from keelgate.audit import AuditLog, SqliteAuditStore
from keelgate.capabilities import (
    GrantSigner,
    GrantVerifier,
    SqliteBudgetLedger,
    SqliteRevocationList,
    issue_grant,
)
from keelgate.llm import PricingTable
from keelgate.loop import (
    LLMPlanner,
    Loop,
    LoopType,
    SqliteCheckpointStore,
    StopConditions,
    Verifier,
)
from keelgate.policy import PolicyContext, RegoEngine
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import (
    SideEffect,
    SqliteIdempotencyStore,
    ToolGateway,
    ToolRegistry,
    tool,
)
from tests.conftest import LIMITS, MARKET_OPEN, Clock

TENANT = "tenant-1"
AGENT = "agent-1"
ALL_CAPS = ("market_data:read", "trade:propose", "trade:paper_execute", "report:write")


class SimulatedCrash(BaseException):
    """Raised from a failpoint to stand in for the process dying at that instant."""


class QuoteIn(BaseModel):
    symbol: str


class QuoteOut(BaseModel):
    symbol: str
    price: float


class OrderIn(BaseModel):
    symbol: str
    notional: float = Field(gt=0, allow_inf_nan=False)
    client_order_id: str = Field(min_length=1, max_length=64)


class OrderOut(BaseModel):
    order_id: str
    status: str


class NewsIn(BaseModel):
    topic: str


class NewsOut(BaseModel):
    headline: str
    published_at: datetime


def policy_for(as_of: datetime) -> PolicyContext:
    return PolicyContext(
        as_of=as_of,
        execution_mode="paper",
        limits=LIMITS,
        exposure={"daily_notional": 0},
    )


@dataclass
class Rig:
    root: Path
    clock: Clock
    signer: GrantSigner
    audit: AuditLog
    approvals: ApprovalQueue
    registry: ToolRegistry
    gateway: ToolGateway
    checkpoints: SqliteCheckpointStore
    grant_token: str
    executions_file: Path
    crash_in_tool: bool = False
    news_published: datetime = field(default_factory=lambda: MARKET_OPEN - timedelta(hours=1))
    tool_crash_exit: bool = False
    closers: list[Any] = field(default_factory=list)

    @property
    def executions(self) -> list[dict[str, Any]]:
        if not self.executions_file.exists():
            return []
        return [json.loads(line) for line in self.executions_file.read_text().splitlines() if line]

    def loop(
        self,
        llm: FakeLLM,
        *,
        stop: StopConditions | None = None,
        verifier: Verifier | None = None,
        failpoint: Any = None,
        loop_cls: type[Loop] = Loop,
        **kwargs: Any,
    ) -> Loop:
        config: dict[str, Any] = {
            "gateway": self.gateway,
            "registry": self.registry,
            "planner": LLMPlanner(llm, "fake-model"),
            "checkpoints": self.checkpoints,
            "grant_token": self.grant_token,
            "policy_context": policy_for,
            "stop": stop or StopConditions(max_iterations=10),
            "verifier": verifier,
            "audit": self.audit,
            "approvals": self.approvals,
            "clock": self.clock,
            "failpoint": failpoint,
        }
        config.update(kwargs)  # a test may override any default, approvals included
        return loop_cls(**config)

    def close(self) -> None:
        """Release every file handle, so a temp directory can be deleted on Windows."""
        self.checkpoints.close()
        for close in self.closers:
            close()


def build_rig(
    root: Path,
    *,
    signer: GrantSigner | None = None,
    grant_token: str | None = None,
    engine: Any = None,
    clock: Clock | None = None,
) -> Rig:
    root.mkdir(parents=True, exist_ok=True)
    clock = clock or Clock(MARKET_OPEN)
    signer = signer or GrantSigner.generate()
    revocations = SqliteRevocationList(root / "revocations.sqlite")
    verifier = GrantVerifier(
        {signer.key_id: signer.public_key_pem()}, clock=clock, revocations=revocations
    )
    audit_store = SqliteAuditStore(root / "audit.sqlite")
    audit = AuditLog(audit_store, clock=clock)
    approvals = ApprovalQueue(root / "approvals.sqlite", audit=audit, clock=clock)
    executions_file = root / "executions.jsonl"
    registry = ToolRegistry()

    rig_ref: dict[str, Rig] = {}

    def record(entry: dict[str, Any]) -> None:
        with executions_file.open("a") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    @tool(capability="market_data:read", side_effect=SideEffect.READ, cost_estimate=1.0)
    def market_quote(args: QuoteIn) -> QuoteOut:
        """Latest quote for a symbol."""
        return QuoteOut(symbol=args.symbol, price=187.25)

    @tool(capability="market_data:read", side_effect=SideEffect.READ, cost_estimate=0.0)
    def get_news(args: NewsIn) -> NewsOut:
        """Latest headline on a topic."""
        return NewsOut(
            headline=f"HEADLINE-{args.topic}", published_at=rig_ref["rig"].news_published
        )

    @tool(
        capability="trade:paper_execute",
        side_effect=SideEffect.WRITE,
        cost_estimate=2.0,
        idempotency_key=lambda a: a.client_order_id,
        resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
    )
    def paper_order(args: OrderIn) -> OrderOut:
        """Place a paper order."""
        record({"tool": "paper_order", **args.model_dump()})
        if rig_ref["rig"].crash_in_tool:
            if rig_ref["rig"].tool_crash_exit:
                os._exit(137)  # a real process death, mid tool body
            raise SimulatedCrash("process died inside the tool")
        return OrderOut(order_id=f"ord-{args.client_order_id}", status="filled")

    for t in (market_quote, get_news, paper_order):
        registry.register(t)

    ledger = SqliteBudgetLedger(root / "budget.sqlite")
    idempotency = SqliteIdempotencyStore(root / "idempotency.sqlite")
    gateway = ToolGateway(
        registry=registry,
        verifier=verifier,
        engine=engine or RegoEngine(),
        audit=audit,
        approvals=approvals,
        ledger=ledger,
        idempotency=idempotency,
    )
    token = (
        grant_token
        or issue_grant(
            signer,
            agent_id=AGENT,
            tenant_id=TENANT,
            capabilities=ALL_CAPS,
            max_cost=1000.0,
            ttl=timedelta(hours=12),
            clock=clock,
        ).token
    )
    rig = Rig(
        root=root,
        clock=clock,
        signer=signer,
        audit=audit,
        approvals=approvals,
        registry=registry,
        gateway=gateway,
        checkpoints=SqliteCheckpointStore(root / "checkpoints.sqlite"),
        grant_token=token,
        executions_file=executions_file,
        closers=[
            revocations.close,
            audit_store.close,
            approvals.close,
            ledger.close,
            idempotency.close,
        ],
    )
    rig_ref["rig"] = rig
    return rig


def order(order_id: str, notional: float = 5000, symbol: str = "AAPL") -> dict[str, Any]:
    return {"symbol": symbol, "notional": notional, "client_order_id": order_id}


def three_step_script(order_id: str = "o-1") -> list[Reply]:
    """Quote, then place an order, then answer: the shape of the acceptance example."""
    return [
        Reply.call("market_quote", symbol="AAPL"),
        Reply.call("paper_order", **order(order_id)),
        Reply.say("Bought 5000 of AAPL at the quoted price."),
    ]


def fake(script: list[Reply], *, pricing: PricingTable | None = None, **kw: Any) -> FakeLLM:
    """An indexed FakeLLM: its reply is a pure function of the loop's call index, so a
    restarted process continues the script from the right place."""
    return FakeLLM(script, indexed=True, pricing=pricing, **kw)


__all__ = [
    "AGENT",
    "TENANT",
    "LoopType",
    "NewsOut",
    "Rig",
    "SimulatedCrash",
    "build_rig",
    "fake",
    "order",
    "policy_for",
    "three_step_script",
]

_ = UTC  # imported for callers that build datetimes alongside the rig
