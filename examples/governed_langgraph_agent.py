"""A governed LangGraph agent: a real ``create_react_agent`` whose tools are Keelgate's.

    pip install "keelgate[langgraph]"
    python examples/governed_langgraph_agent.py

The agent is an ordinary LangGraph ReAct agent. What Keelgate changes is the tools: instead of
plain Python functions, the agent is handed ``governed_langchain_tools(...)``, so every call it
makes goes registry, grant, policy, approvals, audit before anything runs, and every result comes
back labelled as untrusted data. LangGraph still owns the agent loop; Keelgate owns what the
agent is allowed to do.

To run with no API key, the model is a script (``FakeLLM``) wrapped as a LangChain chat model
with ``KeelgateChatModel``. To use a real model, pass a LangChain chat model of your choice to
the agent constructor instead; nothing else changes.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from keelgate.adapters import GovernedToolset
from keelgate.adapters.langgraph import KeelgateChatModel, governed_langchain_tools
from keelgate.approvals import ApprovalQueue
from keelgate.audit import AuditLog, verify_chain
from keelgate.capabilities import GrantSigner, GrantVerifier, issue_grant
from keelgate.policy import PolicyContext, RegoEngine
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import CallContext, SideEffect, ToolGateway, ToolRegistry, tool

AS_OF = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)  # a Monday, 10:30 in New York
LIMITS: dict[str, Any] = {
    "max_notional_per_action": 50_000,
    "max_daily_exposure": 100_000,
    "restricted_symbols": ["TSLA"],
    "approval_one_click_notional": 10_000,
    "approval_explicit_notional": 25_000,
    "trading_hours": {"tz": "America/New_York", "open_minute": 570, "close_minute": 960},
}
ORDERS: list[dict[str, Any]] = []


class QuoteIn(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z]{1,5}$")


class QuoteOut(BaseModel):
    symbol: str
    price: float


class OrderIn(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z]{1,5}$")
    notional: float = Field(gt=0, allow_inf_nan=False)
    client_order_id: str = Field(min_length=1, max_length=64)


class OrderOut(BaseModel):
    order_id: str


@tool(capability="market_data:read", side_effect=SideEffect.READ)
def market_quote(args: QuoteIn) -> QuoteOut:
    """Latest price for a symbol."""
    return QuoteOut(symbol=args.symbol, price=187.25)


@tool(
    capability="trade:paper_execute",
    side_effect=SideEffect.WRITE,
    idempotency_key=lambda a: a.client_order_id,
    resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
)
def place_paper_order(args: OrderIn) -> OrderOut:
    """Place a PAPER order. Nothing real is ever executed."""
    ORDERS.append(args.model_dump())
    return OrderOut(order_id=f"paper-{len(ORDERS)}")


def build_toolset() -> tuple[GovernedToolset, AuditLog]:
    signer = GrantSigner.generate()
    audit = AuditLog(clock=lambda: AS_OF)
    registry = ToolRegistry()
    registry.register(market_quote)
    registry.register(place_paper_order)
    gateway = ToolGateway(
        registry=registry,
        verifier=GrantVerifier({signer.key_id: signer.public_key_pem()}, clock=lambda: AS_OF),
        engine=RegoEngine(),
        audit=audit,
        approvals=ApprovalQueue(audit=audit, clock=lambda: AS_OF),
    )
    token = issue_grant(
        signer,
        agent_id="langgraph-agent",
        tenant_id="acme",
        capabilities=["market_data:read", "trade:paper_execute"],
        max_cost=50,
        ttl=timedelta(hours=1),
        clock=lambda: AS_OF,
    ).token

    def context() -> CallContext:
        # Trusted facts about the world, built by you on every call. Never by the model.
        policy = PolicyContext(
            as_of=AS_OF, execution_mode="paper", limits=LIMITS, exposure={"daily_notional": 0}
        )
        return CallContext(tenant_id="acme", policy_context=policy)

    toolset = GovernedToolset(
        gateway=gateway, registry=registry, grant_token=token, context_factory=context
    )
    return toolset, audit


async def run_agent() -> tuple[list[dict[str, Any]], str, bool]:
    # Imported here so the file can be read (and fail helpfully) without LangGraph installed.
    from langchain_core.messages import ToolMessage  # noqa: PLC0415

    try:  # LangGraph 1.x moved the prebuilt agent into the langchain package
        from langchain.agents import create_agent as make_agent  # noqa: PLC0415
    except ImportError:
        from langgraph.prebuilt import create_react_agent as make_agent  # noqa: PLC0415

    toolset, audit = build_toolset()
    script = [
        Reply.call("market_quote", symbol="AAPL"),
        Reply.call("place_paper_order", symbol="TSLA", notional=1_000, client_order_id="lg-1"),
        Reply.call("place_paper_order", symbol="AAPL", notional=2_500, client_order_id="lg-2"),
        Reply.say("AAPL is at 187.25. TSLA is restricted, so I bought 2,500 of AAPL instead."),
    ]
    model = KeelgateChatModel(llm=FakeLLM(script, indexed=True))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # the older entry point still works
        agent = make_agent(model, governed_langchain_tools(toolset))
    result = await agent.ainvoke({"messages": [("user", "Research AAPL and TSLA and invest.")]})
    results = [json.loads(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]
    chain = verify_chain(audit.records("acme"), tenant_id="acme")
    return results, str(result["messages"][-1].content), chain.ok


def main() -> int:
    try:
        results, answer, chain_ok = asyncio.run(run_agent())
    except ImportError as exc:
        print(f"This example needs LangGraph: pip install 'keelgate[langgraph]' ({exc})")
        return 2
    print("A real LangGraph agent, governed by Keelgate\n")
    for r in results:
        detail = r.get("error", {}).get("code", "")
        print(f"  tool call -> {r['status']:<8} {detail}")
    print(f"\n  agent said: {answer}")
    print(f"  paper orders actually placed: {[o['client_order_id'] for o in ORDERS]}")
    print(f"  audit chain intact: {chain_ok}")
    return 0 if chain_ok and [o["client_order_id"] for o in ORDERS] == ["lg-2"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
