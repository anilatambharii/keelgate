"""Keelgate research loop: a budgeted, resumable agent loop whose tools are also served over MCP.

    python examples/research_loop.py
    python examples/research_loop.py --trace    # also export the trace to Jaeger (make up)

What it shows, in order (no network, no API keys, nothing real is ever executed):

1. A 3-step loop (quote -> paper order -> answer) driven by a scripted ``FakeLLM``.
2. The loop hits its **token budget** and stops. The step it was about to take is saved, not run.
3. A "restart": every in-memory object is thrown away and rebuilt over the same files, then the
   run is **resumed from its checkpoint** with a larger budget. The model is not asked to plan
   again, and the paper order is placed exactly once.
4. The same governed tools are **served over MCP**; a client calls one (allowed) and tries
   another (denied by policy). Tool output reaches the client labelled untrusted.

With --trace the whole run, including the restart, is ONE trace in Jaeger
(http://localhost:16686): a root span, a span per loop step, a chat span per model call with
tokens and cost, and a tool span per call with a policy-decision span under each WRITE. Prompts,
arguments and tool output are never put in spans.

The "model" is a script. What matters is that every proposal goes through the gateway, and the
gateway, not the proposer, decides.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import Client, types
from pydantic import BaseModel, Field

from keelgate import telemetry
from keelgate.adapters import GovernedToolset
from keelgate.adapters.mcp import GovernedMCPServer
from keelgate.approvals import ApprovalQueue
from keelgate.audit import AuditLog, SqliteAuditStore
from keelgate.capabilities import GrantSigner, GrantVerifier, SqliteBudgetLedger, issue_grant
from keelgate.llm import ModelPrice, PricingTable
from keelgate.loop import LLMPlanner, Loop, SqliteCheckpointStore, StopConditions, StopReason
from keelgate.policy import PolicyContext, RegoEngine
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import (
    CallContext,
    SideEffect,
    SqliteIdempotencyStore,
    ToolGateway,
    ToolRegistry,
    tool,
)

AS_OF = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)  # Monday, 10:30 in New York
TENANT, AGENT, RUN_ID = "acme", "research-agent", "research-1"
GOAL = "Research AAPL and buy a small paper position."
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


class OrderIn(BaseModel):
    symbol: str
    notional: float = Field(gt=0, allow_inf_nan=False)
    client_order_id: str = Field(min_length=1, max_length=64)


class OrderOut(BaseModel):
    order_id: str
    status: str


# Orders actually placed. Because the loop is durable, this must end with exactly one entry.
BLOTTER: list[str] = []
TRACE: dict[str, str] = {}  # the run's trace id, for the closing printout


@tool(capability="market_data:read", side_effect=SideEffect.READ, cost_estimate=1.0)
def market_quote(args: QuoteIn) -> QuoteOut:
    """Latest quote for a symbol."""
    return QuoteOut(symbol=args.symbol, price=187.25)


@tool(
    capability="trade:paper_execute",
    side_effect=SideEffect.WRITE,
    cost_estimate=2.0,
    idempotency_key=lambda a: a.client_order_id,
    resource=lambda a: {"symbol": a.symbol, "notional": a.notional},
)
def paper_order(args: OrderIn) -> OrderOut:
    """Place a PAPER order. Nothing real is ever executed."""
    BLOTTER.append(args.client_order_id)
    return OrderOut(order_id=f"paper-{len(BLOTTER)}", status="filled")


def policy_context(as_of: datetime) -> PolicyContext:
    return PolicyContext(
        as_of=as_of, execution_mode="paper", limits=LIMITS, exposure={"daily_notional": 0}
    )


class Process:
    """Everything one 'process' holds. Building a second one over the same directory is a restart:
    the files survive (checkpoints, idempotency keys, budget ledger, audit); the objects do not."""

    def __init__(self, root: Path, signer: GrantSigner, engine: RegoEngine) -> None:
        self.verifier = GrantVerifier({signer.key_id: signer.public_key_pem()}, clock=lambda: AS_OF)
        self._audit_store = SqliteAuditStore(root / "audit.sqlite")
        self.audit = AuditLog(self._audit_store, clock=lambda: AS_OF)
        self.registry = ToolRegistry()
        self.registry.register(market_quote)
        self.registry.register(paper_order)
        self.ledger = SqliteBudgetLedger(root / "budget.sqlite")
        self.idempotency = SqliteIdempotencyStore(root / "idempotency.sqlite")
        self.checkpoints = SqliteCheckpointStore(root / "checkpoints.sqlite")
        self.gateway = ToolGateway(
            registry=self.registry,
            verifier=self.verifier,
            engine=engine,
            audit=self.audit,
            approvals=ApprovalQueue(audit=self.audit, clock=lambda: AS_OF),
            ledger=self.ledger,
            idempotency=self.idempotency,
        )
        self.grant = issue_grant(
            signer,
            agent_id=AGENT,
            tenant_id=TENANT,
            capabilities=["market_data:read", "trade:paper_execute"],
            max_cost=100,
            ttl=timedelta(hours=12),
            clock=lambda: AS_OF,
        ).token

    def loop(self, llm: FakeLLM) -> Loop:
        return Loop(
            gateway=self.gateway,
            registry=self.registry,
            planner=LLMPlanner(llm, MODEL),
            checkpoints=self.checkpoints,
            grant_token=self.grant,
            policy_context=policy_context,
            audit=self.audit,
            clock=lambda: AS_OF,
        )

    def close(self) -> None:
        self.checkpoints.close()
        self._audit_store.close()
        self.idempotency.close()
        self.ledger.close()


MODEL = "scripted-model"
PRICES = PricingTable({MODEL: ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)})
FIRST_BUDGET = 900  # tokens


def script() -> list[Reply]:
    """Each planning call reports 100 tokens in and 400 out, priced from the table above.

    The output is what crosses the budget: the loop refuses a call whose *prompt* alone would
    not fit, but cannot know in advance how long an answer will be.
    """
    used = PRICES.usage(MODEL, 100, 400)
    return [
        Reply.call("market_quote", symbol="AAPL", usage=used),
        Reply.call(
            "paper_order", symbol="AAPL", notional=5_000, client_order_id="order-1", usage=used
        ),
        Reply.say("Bought 5,000 of AAPL at the quoted price of 187.25.", usage=used),
    ]


async def serve_over_mcp(process: Process) -> None:
    toolset = GovernedToolset(
        gateway=process.gateway,
        registry=process.registry,
        grant_token=process.grant,
        context_factory=lambda: CallContext(tenant_id=TENANT, policy_context=policy_context(AS_OF)),
    )
    server = GovernedMCPServer(toolset, name="keelgate-research")
    async with Client(server.server) as client:
        listed = [t.name for t in (await client.list_tools()).tools]
        print(f"   tools served over MCP: {', '.join(listed)}")
        for label, name, args in (
            ("market_quote AAPL", "market_quote", {"symbol": "AAPL"}),
            (
                "paper_order TSLA (restricted)",
                "paper_order",
                {"symbol": "TSLA", "notional": 1_000, "client_order_id": "mcp-1"},
            ),
        ):
            result = await client.call_tool(name, args)
            block = result.content[0]
            if not isinstance(block, types.TextContent):
                raise TypeError("expected a text result")
            body = json.loads(block.text)
            verdict = body.get("error", {}).get("code", body["status"])
            print(f"   MCP call {label:<32} -> {verdict}  (is_error={result.is_error})")
            if "untrusted_tool_output" in body:
                print("      output is labelled under the key 'untrusted_tool_output'")


async def main(trace: bool = False) -> int:
    root = Path(tempfile.mkdtemp(prefix="keelgate-research-"))
    engine = RegoEngine()
    signer = GrantSigner.generate(key_id="demo-key")
    tel = telemetry.instrument(service_name="keelgate-research-loop") if trace else None
    print("\nKeelgate research loop - budgeted, resumable, governed\n")
    try:
        print(f"1. Run the loop with a {FIRST_BUDGET}-token budget")
        first = Process(root, signer, engine)
        scripted = FakeLLM(script(), indexed=True, pricing=PRICES, model=MODEL)
        llm = telemetry.InstrumentedLLM(scripted)  # every model call becomes a span with cost
        stopped = await first.loop(llm).run(
            goal=GOAL,
            tenant_id=TENANT,
            agent_id=AGENT,
            as_of=AS_OF,
            run_id=RUN_ID,
            stop=StopConditions(max_tokens=FIRST_BUDGET),
        )
        print(
            f"   stopped: {stopped.stop_reason.value if stopped.stop_reason else '?'}"
            f" after {stopped.state.tokens_used} tokens, {scripted.calls_made} model calls"
        )
        print(f"   resumable: {stopped.resumable}   orders placed so far: {len(BLOTTER)}")
        if stopped.stop_reason is not StopReason.TOKEN_BUDGET or BLOTTER:
            print("   unexpected: the budget stop did not behave as designed")
            return 1
        first.close()

        print("\n2. 'Restart': rebuild every object over the same files, resume with 10,000 tokens")
        second = Process(root, signer, engine)
        done = await second.loop(llm).run_or_resume(
            goal=GOAL,
            tenant_id=TENANT,
            agent_id=AGENT,
            as_of=AS_OF,
            run_id=RUN_ID,
            stop=StopConditions(max_tokens=10_000),
        )
        print(f"   finished: {done.ok}   answer: {done.final_answer!r}")
        print(
            f"   model calls in total: {scripted.calls_made} (the saved plan was NOT re-planned)"
            f"   orders placed: {BLOTTER}"
        )
        if not done.ok or BLOTTER != ["order-1"]:
            print("   unexpected: resume did not complete exactly once")
            return 1
        chain = second.audit.verify_chain(TENANT)
        print(f"   audit chain across both processes: {'OK' if chain.ok else 'BROKEN'}")
        TRACE["id"] = done.state.trace_id

        print("\n3. Serve the same governed tools over MCP")
        await serve_over_mcp(second)
        second.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if tel is not None:
        costs = tel.costs.total(tenant_id=TENANT, agent_id=AGENT)
        print(
            f"\n4. Telemetry: {costs.calls} model calls, {costs.total_tokens} tokens, "
            f"${costs.cost_usd:.6f} attributed to {TENANT}/{AGENT}"
        )
        tel.shutdown()  # flush the batch exporter
        print(f"   trace id: {TRACE['id']}")
        print(f"   open: http://localhost:16686/trace/{TRACE['id']}")
    print("\nDone: one budget stop, one resume, one order, every call governed.\n")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Budgeted, resumable research loop")
    parser.add_argument("--trace", action="store_true", help="export the trace over OTLP/HTTP")
    sys.exit(asyncio.run(main(trace=parser.parse_args().trace)))
