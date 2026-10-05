"""Shared fixtures: a controllable clock, a real policy pack, and a wired gateway."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel, Field

from keelgate.approvals import ApprovalQueue, ApprovalTier, Approver
from keelgate.audit import AuditLog
from keelgate.capabilities import GrantSigner, GrantVerifier, InMemoryRevocationList, issue_grant
from keelgate.policy import PolicyContext, PolicyEngine, RegoEngine
from keelgate.tools import (
    CallContext,
    SideEffect,
    ToolGateway,
    ToolRegistry,
    tool,
)

T = TypeVar("T")

# Keelgate's own fixtures, loaded here rather than through the pytest11 entry point (see addopts).
pytest_plugins = ["keelgate.testing.plugin"]

OPA_URL_DEFAULT = os.environ.get("KEELGATE_TEST_OPA_URL", "http://localhost:8181")
REQUIRE_INTEGRATION = bool(os.environ.get("KEELGATE_REQUIRE_INTEGRATION"))


def skip_or_fail(reason: str) -> None:
    """Skip locally when a service is down; fail in CI, where it must be up.

    A test suite that quietly skips its integration tests proves nothing, so CI
    sets KEELGATE_REQUIRE_INTEGRATION=1 and a missing service becomes a failure.
    """
    if REQUIRE_INTEGRATION:
        pytest.fail(f"integration service required but unavailable: {reason}")
    pytest.skip(reason)


# 2026-10-05 is a Monday; 14:30Z is 10:30 in New York (EDT), inside trading hours.
MARKET_OPEN = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)

LIMITS: dict[str, Any] = {
    "max_notional_per_action": 50_000,
    "max_daily_exposure": 100_000,
    "restricted_symbols": ["TSLA", "GME"],
    "approval_one_click_notional": 10_000,
    "approval_explicit_notional": 25_000,
    "trading_hours": {"tz": "America/New_York", "open_minute": 570, "close_minute": 960},
}


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)


class Clock:
    """A clock tests can move. Shared by grants, audit and approvals."""

    def __init__(self, start: datetime = MARKET_OPEN) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def policy_context(**overrides: Any) -> PolicyContext:
    values: dict[str, Any] = {
        "as_of": MARKET_OPEN,
        "execution_mode": "paper",
        "limits": LIMITS,
        "exposure": {"daily_notional": 0},
    }
    values.update(overrides)
    return PolicyContext(**values)


class QuoteIn(BaseModel):
    symbol: str


class QuoteOut(BaseModel):
    symbol: str
    price: float


class TradeIn(BaseModel):
    symbol: str
    notional: float = Field(gt=0, allow_inf_nan=False)
    client_order_id: str = Field(min_length=1, max_length=64)


class TradeOut(BaseModel):
    order_id: str
    status: str


class ReportIn(BaseModel):
    title: str


class ReportOut(BaseModel):
    saved: bool


@dataclass
class Harness:
    clock: Clock
    signer: GrantSigner
    verifier: GrantVerifier
    revocations: InMemoryRevocationList
    audit: AuditLog
    queue: ApprovalQueue
    registry: ToolRegistry
    gateway: ToolGateway
    engine: PolicyEngine
    executed: list[dict[str, Any]] = field(default_factory=list)

    def grant(
        self,
        capabilities: tuple[str, ...] = (
            "market_data:read",
            "trade:propose",
            "trade:paper_execute",
            "report:write",
        ),
        *,
        tenant: str = "tenant-1",
        agent: str = "agent-1",
        cost: float = 100.0,
        ttl: timedelta = timedelta(hours=1),
    ) -> str:
        return issue_grant(
            self.signer,
            agent_id=agent,
            tenant_id=tenant,
            capabilities=capabilities,
            max_cost=cost,
            ttl=ttl,
            clock=self.clock,
        ).token

    def ctx(self, tenant: str = "tenant-1", **overrides: Any) -> CallContext:
        values: dict[str, Any] = {"tenant_id": tenant, "policy_context": policy_context()}
        values.update(overrides)
        return CallContext(**values)

    def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        token: str | None = None,
        context: CallContext | None = None,
    ) -> Any:
        return run(
            self.gateway.call(
                tool_name=tool_name,
                arguments=arguments,
                grant_token=token if token is not None else self.grant(),
                context=context or self.ctx(),
            )
        )

    @property
    def approver(self) -> Approver:
        return Approver(
            approver_id="alice", tenant_id="tenant-1", max_tier=ApprovalTier.EXPLICIT_SIGNOFF
        )


def build_harness(
    *,
    engine: PolicyEngine | None = None,
    audit: AuditLog | None = None,
    tools_extra: Callable[[ToolRegistry, list[dict[str, Any]]], None] | None = None,
) -> Harness:
    clock = Clock()
    signer = GrantSigner.generate()
    revocations = InMemoryRevocationList()
    verifier = GrantVerifier(
        {signer.key_id: signer.public_key_pem()}, clock=clock, revocations=revocations
    )
    audit = audit or AuditLog(clock=clock)
    queue = ApprovalQueue(audit=audit, clock=clock)
    executed: list[dict[str, Any]] = []
    registry = ToolRegistry()

    @tool(capability="market_data:read", side_effect=SideEffect.READ, cost_estimate=1.0)
    def market_quote(args: QuoteIn) -> QuoteOut:
        """Latest quote for a symbol."""
        return QuoteOut(symbol=args.symbol, price=101.5)

    @tool(
        capability="trade:paper_execute",
        side_effect=SideEffect.WRITE,
        cost_estimate=2.0,
        idempotency_key=lambda a: a.client_order_id,
        resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
    )
    def trade_paper_execute(args: TradeIn) -> TradeOut:
        """Place a paper trade."""
        executed.append({"tool": "trade_paper_execute", **args.model_dump()})
        return TradeOut(order_id=f"ord-{len(executed)}", status="filled")

    @tool(
        capability="trade:propose",
        side_effect=SideEffect.PROPOSE,
        resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
    )
    def trade_propose(args: TradeIn) -> TradeOut:
        """Record a trade proposal; nothing is executed."""
        return TradeOut(order_id="proposal", status="proposed")

    @tool(
        capability="report:write",
        side_effect=SideEffect.WRITE,
        idempotency_key=lambda a: a.title,
    )
    def report_write(args: ReportIn) -> ReportOut:
        """Save a report."""
        executed.append({"tool": "report_write", **args.model_dump()})
        return ReportOut(saved=True)

    for t in (market_quote, trade_paper_execute, trade_propose, report_write):
        registry.register(t)
    if tools_extra is not None:
        tools_extra(registry, executed)

    chosen: PolicyEngine = engine or RegoEngine()
    gateway = ToolGateway(
        registry=registry,
        verifier=verifier,
        engine=chosen,
        audit=audit,
        approvals=queue,
    )
    return Harness(
        clock=clock,
        signer=signer,
        verifier=verifier,
        revocations=revocations,
        audit=audit,
        queue=queue,
        registry=registry,
        gateway=gateway,
        engine=chosen,
        executed=executed,
    )


@pytest.fixture(scope="session")
def rego_engine() -> RegoEngine:
    """One compiled pack for the session; compiling is the slow part."""
    return RegoEngine()


@pytest.fixture
def h(rego_engine: RegoEngine) -> Harness:
    return build_harness(engine=rego_engine)
