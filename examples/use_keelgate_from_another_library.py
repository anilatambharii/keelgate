"""Use Keelgate from another library: the pattern Tycheon follows.

A downstream library ("acme_forecasts" here) depends on Keelgate and imports ONLY its public API.
It brings four things of its own:

1. **tools**: what its agent may do, declared with ``@tool`` (a read, and a write);
2. **a policy**: its own deterministic gate, implementing the ``PolicyEngine`` protocol;
3. **a metric**: how to score its outcomes, discoverable through an entry point;
4. **a harness**: the wiring that turns those into a governed, budgeted, resumable, traceable
   agent, and tests that run it with a scripted model, offline.

Run it:  python examples/use_keelgate_from_another_library.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from keelgate.audit import AuditLog
from keelgate.capabilities import GrantSigner, GrantVerifier, issue_grant
from keelgate.evals import MetricResult, OutcomeMetric, OutcomeRecord
from keelgate.loop import (
    InMemoryCheckpointStore,
    LLMPlanner,
    Loop,
    LoopResult,
    Recording,
    StopConditions,
    replay,
)
from keelgate.policy import Decision, PolicyContext, PolicyDecision, PolicyInput
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import SideEffect, ToolGateway, ToolRegistry, tool

if TYPE_CHECKING:
    from collections.abc import Sequence

AS_OF = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
TENANT, AGENT = "acme", "forecaster"
UP_THRESHOLD = 0.5  # a forecast of at least this probability counts as "up"


# --------------------------------------------------------------------------- 1. tools


class LookupIn(BaseModel):
    series: str = Field(pattern=r"^[a-z_]{1,32}$")


class LookupOut(BaseModel):
    series: str
    probability_up: float


class PublishIn(BaseModel):
    series: str = Field(pattern=r"^[a-z_]{1,32}$")
    probability_up: float
    forecast_id: str = Field(min_length=1, max_length=64)


class PublishOut(BaseModel):
    published: bool


PUBLISHED: list[dict[str, Any]] = []  # stands in for your real sink; a test counts entries


@tool(capability="forecast:read", side_effect=SideEffect.READ)
def forecast_lookup(args: LookupIn) -> LookupOut:
    """The model's current probability that a series goes up."""
    return LookupOut(series=args.series, probability_up=0.64)


@tool(
    capability="forecast:publish",
    side_effect=SideEffect.WRITE,
    # A retry with the same forecast_id can never publish twice.
    idempotency_key=lambda a: a.forecast_id,
    # The facts your policy needs, derived from validated input (never from model prose).
    resource=lambda a: {"series": a.series, "probability_up": a.probability_up},
)
def forecast_publish(args: PublishIn) -> PublishOut:
    """Publish a forecast to the shared dashboard."""
    PUBLISHED.append(args.model_dump())
    return PublishOut(published=True)


# --------------------------------------------------------------------------- 2. a policy


class ForecastPolicy:
    """Deny by default; allow publishing only a probability that is a real probability.

    Implementing ``PolicyEngine`` is two things: a ``name`` and ``async decide``. It must be a
    deterministic function of its input, and it must fail closed: anything unexpected is a DENY.
    """

    name = "acme-forecast-policy"

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        p = policy_input.resource.get("probability_up")
        ok = (
            policy_input.action.tool == "forecast_publish"
            and isinstance(p, float)
            and 0.0 <= p <= 1.0
            and policy_input.context.execution_mode == "paper"
        )
        return PolicyDecision(
            effect=Decision.ALLOW if ok else Decision.DENY,
            reasons=("a valid probability",) if ok else ("not a publishable forecast",),
            policy_version="acme-forecast-policy/1",
            engine=self.name,
        )


# --------------------------------------------------------------------------- 3. a metric


class HitRate:
    """Share of forecasts whose direction was right. Register it as an entry point:

    [project.entry-points."keelgate.outcome_metrics"]
    hit_rate = "acme_forecasts:HitRate"
    """

    name = "hit_rate"
    higher_is_better = True

    def compute(self, records: Sequence[OutcomeRecord]) -> MetricResult:
        scored = [r for r in records if "p_up" in r.predicted and "up" in r.realized]
        hits = sum((r.predicted["p_up"] >= UP_THRESHOLD) == bool(r.realized["up"]) for r in scored)
        return MetricResult(self.name, hits / len(scored) if scored else 0.0, len(scored))


# --------------------------------------------------------------------------- 4. a harness


@dataclass
class Agent:
    """Everything a run needs, wired. Build one per tenant."""

    loop: Loop
    checkpoints: InMemoryCheckpointStore
    audit: AuditLog


def build_agent(llm: Any) -> Agent:
    signer = GrantSigner.generate()
    verifier = GrantVerifier({signer.key_id: signer.public_key_pem()}, clock=lambda: AS_OF)
    audit = AuditLog(clock=lambda: AS_OF)
    registry = ToolRegistry()
    registry.register(forecast_lookup)
    registry.register(forecast_publish)
    gateway = ToolGateway(
        registry=registry, verifier=verifier, engine=ForecastPolicy(), audit=audit
    )
    grant = issue_grant(
        signer,
        agent_id=AGENT,
        tenant_id=TENANT,
        capabilities=["forecast:read", "forecast:publish"],
        max_cost=100,
        ttl=timedelta(hours=1),
        clock=lambda: AS_OF,
    )
    checkpoints = InMemoryCheckpointStore()
    loop = Loop(
        gateway=gateway,
        registry=registry,
        planner=LLMPlanner(llm, "any-model"),
        checkpoints=checkpoints,
        grant_token=grant.token,
        # Trusted facts about the world. Never derived from model output.
        policy_context=lambda as_of: PolicyContext(as_of=as_of, execution_mode="paper"),
        stop=StopConditions(max_iterations=5),
        audit=audit,
        clock=lambda: AS_OF,
    )
    return Agent(loop, checkpoints, audit)


def run_once() -> tuple[Agent, LoopResult]:
    """A scripted model looks a series up, publishes a forecast, and reports."""
    script = [
        Reply.call("forecast_lookup", series="spx"),
        Reply.call("forecast_publish", series="spx", probability_up=0.64, forecast_id="f-1"),
        Reply.say("Published a 0.64 probability that spx goes up."),
    ]
    agent = build_agent(FakeLLM(script, indexed=True))
    result = asyncio.run(
        agent.loop.run(
            goal="Publish today's spx forecast.",
            tenant_id=TENANT,
            agent_id=AGENT,
            as_of=AS_OF,
            run_id="demo-1",
        )
    )
    return agent, result


def main() -> int:
    agent, result = run_once()
    print(f"goal reached: {result.ok}; forecasts published: {len(PUBLISHED)}")
    print(f"audit chain intact: {agent.audit.verify_chain(TENANT).ok}")
    recording = Recording.from_store(agent.checkpoints, TENANT, trace_id=result.state.trace_id)
    report = asyncio.run(replay(recording))
    print(f"replays identically: {report.identical}; forecasts published still {len(PUBLISHED)}")
    metric: OutcomeMetric = HitRate()
    sample = [
        OutcomeRecord("a", {"p_up": 0.7}, {"up": True}),
        OutcomeRecord("b", {"p_up": 0.3}, {"up": True}),
    ]
    print(f"hit_rate over {len(sample)} outcomes: {metric.compute(sample).value:.2f}")
    return 0 if result.ok and report.identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
