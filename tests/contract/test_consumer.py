"""Keelgate as a downstream library sees it: public API only, no peeking at internals.

Every import below is from a documented public module (an AST test in this directory enforces
that). If one of these tests breaks, a promise in docs/integration-contract.md broke with it.
"""

from __future__ import annotations

import asyncio
import re
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

import keelgate
from keelgate import telemetry
from keelgate.adapters import GovernedToolset
from keelgate.approvals import ApprovalQueue, ApprovalTier, Approver
from keelgate.audit import AuditLog, verify_chain
from keelgate.capabilities import GrantSigner, GrantVerifier, issue_grant
from keelgate.context import AsOfViolationError, ContextBuilder, ContextItem, ItemKind
from keelgate.evals import (
    OutcomeMetric,
    OutcomeRecord,
    discover_metrics,
    run_suite,
)
from keelgate.llm import LLMClient, LLMRequest, Message, PricingTable, Role
from keelgate.loop import (
    InMemoryCheckpointStore,
    LLMPlanner,
    Loop,
    Recording,
    StopConditions,
    StopReason,
    replay,
)
from keelgate.memory import Attribution, SemanticMemory, SqliteMemoryBackend
from keelgate.policy import Decision, PolicyContext, PolicyDecision, PolicyEngine, PolicyInput, deny
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import (
    CallContext,
    DirectInvocationError,
    OutcomeStatus,
    SideEffect,
    ToolGateway,
    ToolRegistry,
    tool,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)


class In(BaseModel):
    ref: str


class Out(BaseModel):
    ok: bool


class Verdicts:
    """A scripted policy: tool name -> decision. Deny by default, like the real ones."""

    name = "consumer-test-policy"

    def __init__(self, rules: dict[str, Decision] | None = None) -> None:
        self.rules = rules or {}
        self.asked: list[PolicyInput] = []

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        self.asked.append(policy_input)
        effect = self.rules.get(policy_input.action.tool, Decision.DENY)
        tier = ApprovalTier.ONE_CLICK if effect is Decision.REQUIRE_APPROVAL else None
        return PolicyDecision(
            effect=effect,
            reasons=(f"scripted {effect.value}",),
            approval_tier=tier,
            policy_version="v1",
            engine=self.name,
        )


class World:
    """A complete stack built only from public pieces."""

    def __init__(self, rules: dict[str, Decision] | None = None, **gateway: Any) -> None:
        self.effects: list[str] = []
        self.signer = GrantSigner.generate()
        self.verifier = GrantVerifier(
            {self.signer.key_id: self.signer.public_key_pem()}, clock=lambda: NOW
        )
        self.audit = AuditLog(clock=lambda: NOW)
        self.queue = ApprovalQueue(audit=self.audit, clock=lambda: NOW)
        self.registry = ToolRegistry()
        world = self

        @tool(capability="demo:read", side_effect=SideEffect.READ)
        def lookup(args: In) -> Out:
            """Read something."""
            return Out(ok=True)

        @tool(
            capability="demo:write",
            side_effect=SideEffect.WRITE,
            idempotency_key=lambda a: a.ref,
        )
        def act(args: In) -> Out:
            """Change something."""
            world.effects.append(args.ref)
            return Out(ok=True)

        self.lookup, self.act = lookup, act
        self.registry.register(lookup)
        self.registry.register(act)
        self.policy: PolicyEngine = Verdicts(rules)
        self.gateway = ToolGateway(
            registry=self.registry,
            verifier=self.verifier,
            engine=self.policy,
            audit=self.audit,
            approvals=self.queue,
            **gateway,
        )

    def token(self, caps: tuple[str, ...] = ("demo:read", "demo:write"), tenant: str = "t1") -> str:
        return issue_grant(
            self.signer,
            agent_id="a1",
            tenant_id=tenant,
            capabilities=caps,
            max_cost=100,
            ttl=timedelta(hours=1),
            clock=lambda: NOW,
        ).token

    def call(self, tool_name: str, ref: str = "r1", *, token: str | None = None, **ctx: Any) -> Any:
        context = CallContext(
            tenant_id=ctx.pop("tenant_id", "t1"),
            policy_context=PolicyContext(as_of=NOW, execution_mode="paper"),
            **ctx,
        )
        return asyncio.run(
            self.gateway.call(
                tool_name=tool_name,
                arguments={"ref": ref},
                grant_token=token or self.token(),
                context=context,
            )
        )


# ------------------------------------------------------------------ the safety guarantees


def test_a_write_without_a_policy_allow_never_runs() -> None:
    world = World()  # the policy denies everything it is not told to allow
    outcome = world.call("act")
    assert outcome.status is OutcomeStatus.DENIED and world.effects == []
    assert verify_chain(world.audit.records("t1"), tenant_id="t1").ok


def test_an_allowed_write_runs_once_and_a_retry_replays() -> None:
    world = World({"act": Decision.ALLOW})
    first, second = world.call("act", "same"), world.call("act", "same")
    assert first.status is second.status is OutcomeStatus.OK
    assert world.effects == ["same"] and second.replayed


def test_a_capability_that_was_not_granted_cannot_be_used() -> None:
    world = World({"act": Decision.ALLOW})
    outcome = world.call("act", token=world.token(("demo:read",)))
    assert outcome.status is not OutcomeStatus.OK and world.effects == []


def test_a_grant_for_another_tenant_is_refused() -> None:
    world = World({"act": Decision.ALLOW})
    outcome = world.call("act", token=world.token(tenant="someone-else"))
    assert outcome.status is not OutcomeStatus.OK and world.effects == []


def test_wildcard_capabilities_cannot_be_minted() -> None:
    world = World()
    with pytest.raises(ValueError, match=r"."):
        world.token(("demo:*",))


def test_a_tool_cannot_be_called_around_the_gateway() -> None:
    world = World()
    with pytest.raises(DirectInvocationError):
        world.act(In(ref="x"))
    assert world.effects == []


def test_an_approval_is_required_bound_to_the_arguments_and_single_use() -> None:
    world = World({"act": Decision.REQUIRE_APPROVAL})
    parked = world.call("act", "ap-1")
    assert parked.status is OutcomeStatus.APPROVAL_REQUIRED and world.effects == []
    request = world.queue.get("t1", parked.approval_id)
    human = Approver(approver_id="alice", tenant_id="t1", max_tier=ApprovalTier.EXPLICIT_SIGNOFF)
    world.queue.approve("t1", request.request_id, human, signoff_code=request.signoff_code)

    swapped = world.call("act", "different", approval_id=request.request_id)
    assert swapped.status is not OutcomeStatus.OK  # approved for other arguments
    ran = world.call("act", "ap-1", approval_id=request.request_id)
    assert ran.status is OutcomeStatus.OK and world.effects == ["ap-1"]
    again = world.call("act", "ap-1b", approval_id=request.request_id)
    assert again.status is not OutcomeStatus.OK  # spent


def test_a_policy_that_raises_fails_closed() -> None:
    class Broken:
        name = "broken"

        async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
            raise RuntimeError("boom")

    world = World()
    world.gateway._engine = Broken()
    assert world.call("act").status is OutcomeStatus.DENIED and world.effects == []


def test_deny_is_a_ready_made_fail_closed_decision() -> None:
    decision = deny("because", engine="mine")
    assert decision.effect is Decision.DENY and decision.engine == "mine"


def test_the_audit_chain_detects_tampering() -> None:
    world = World({"act": Decision.ALLOW})
    world.call("act")
    records = list(world.audit.records("t1"))
    assert verify_chain(records, tenant_id="t1").ok
    forged = list(records)
    forged[-1] = forged[-1].model_copy(update={"actor": "someone-else"})
    assert not verify_chain(forged, tenant_id="t1").ok


def test_read_only_calls_are_possible_through_the_governed_toolset() -> None:
    world = World({"act": Decision.ALLOW})
    toolset = GovernedToolset(
        gateway=world.gateway,
        registry=world.registry,
        grant_token=world.token(),
        context_factory=lambda: CallContext(
            tenant_id="t1", policy_context=PolicyContext(as_of=NOW, execution_mode="paper")
        ),
        allowed_side_effects=frozenset({SideEffect.READ}),
    )
    assert [t.spec.name for t in toolset.tools()] == ["lookup"]


# ------------------------------------------------------------------ the loop


def make_loop(world: World, llm: LLMClient, **kw: Any) -> tuple[Loop, InMemoryCheckpointStore]:
    store = InMemoryCheckpointStore()
    loop = Loop(
        gateway=world.gateway,
        registry=world.registry,
        planner=LLMPlanner(llm, "m"),
        checkpoints=store,
        grant_token=world.token(),
        policy_context=lambda as_of: PolicyContext(as_of=as_of, execution_mode="paper"),
        audit=world.audit,
        clock=lambda: NOW,
        **kw,
    )
    return loop, store


def run_loop(loop: Loop, run_id: str = "r1", **kw: Any) -> Any:
    return asyncio.run(
        loop.run(goal="g", tenant_id="t1", agent_id="a1", as_of=NOW, run_id=run_id, **kw)
    )


SCRIPT = [Reply.call("lookup", ref="x"), Reply.call("act", ref="o-1"), Reply.say("done")]


def test_the_loop_runs_to_the_goal_replays_and_leaves_a_verifiable_audit() -> None:
    world = World({"act": Decision.ALLOW})
    loop, store = make_loop(world, FakeLLM(SCRIPT, indexed=True))
    result = run_loop(loop)
    assert result.ok and world.effects == ["o-1"]
    assert verify_chain(world.audit.records("t1"), tenant_id="t1").ok
    report = asyncio.run(replay(Recording.from_store(store, "t1", trace_id=result.state.trace_id)))
    assert report.identical and world.effects == ["o-1"]  # nothing re-ran


def test_a_budget_stop_is_resumable_and_never_repeats_a_write() -> None:
    from keelgate.llm import Usage

    world = World({"act": Decision.ALLOW})
    big = Usage(input_tokens=0, output_tokens=900)
    script = [
        Reply.call("lookup", ref="x", usage=big),
        Reply.call("act", ref="o-1", usage=big),
        Reply.say("done", usage=big),
    ]
    loop, _ = make_loop(world, FakeLLM(script, indexed=True))
    stopped = run_loop(loop, stop=StopConditions(max_tokens=1500))
    assert stopped.stop_reason is StopReason.TOKEN_BUDGET and stopped.resumable
    assert world.effects == []
    done = asyncio.run(loop.resume("t1", "r1", stop=StopConditions(max_tokens=100_000)))
    assert done.ok and world.effects == ["o-1"]


def test_a_denied_write_is_reported_to_the_model_not_executed() -> None:
    world = World()  # act is denied
    loop, _ = make_loop(world, FakeLLM(SCRIPT, indexed=True))
    result = run_loop(loop)
    assert result.ok and world.effects == []


# ------------------------------------------------------------------ context, memory, llm


def test_context_refuses_anything_published_after_as_of() -> None:
    builder = ContextBuilder(as_of=NOW, token_budget=1000)
    future = ContextItem.outside(
        ItemKind.DOCUMENT,
        "from the future",
        item_id="f",
        published_at=NOW + timedelta(seconds=1),
        origin="doc",
    )
    with pytest.raises(AsOfViolationError):
        builder.add(future)


def test_memory_is_bitemporal_and_tenant_scoped() -> None:
    backend = SqliteMemoryBackend()
    who = Attribution(agent_id="a", trace_id="t")
    mine = SemanticMemory(backend, tenant_id="t1", clock=lambda: NOW)
    theirs = SemanticMemory(backend, tenant_id="t2", clock=lambda: NOW)
    mine.assert_fact(
        "rate", "The rate is 5 percent.", valid_from=NOW - timedelta(days=1), attribution=who
    )
    assert [r.content for r in mine.search("rate", as_of=NOW)] == ["The rate is 5 percent."]
    assert theirs.search("rate", as_of=NOW) == ()
    assert mine.search("rate", as_of=NOW - timedelta(days=2)) == ()  # not yet valid


def test_the_llm_boundary_is_a_small_protocol_and_prices_are_never_invented() -> None:
    fake = FakeLLM([Reply.say("hi")])
    assert isinstance(fake, LLMClient)
    reply = asyncio.run(
        fake.complete(LLMRequest(model="m", messages=(Message(role=Role.USER, content="x"),)))
    )
    assert reply.text == "hi"
    assert PricingTable().price_for("any-model") is None


# ------------------------------------------------------------------ telemetry and evals


def test_telemetry_exports_a_trace_per_run_with_the_documented_attributes() -> None:
    exporter = InMemorySpanExporter()
    previous = telemetry.active()
    tel = telemetry.instrument(exporter=exporter, batch=False)
    try:
        world = World({"act": Decision.ALLOW})
        loop, _ = make_loop(world, telemetry.InstrumentedLLM(FakeLLM(SCRIPT, indexed=True)))
        result = run_loop(loop)
        tel.force_flush()
    finally:
        telemetry.activate(previous)
        tel.shutdown()
    spans = exporter.get_finished_spans()
    assert {format(s.context.trace_id, "032x") for s in spans} == {result.state.trace_id}
    names = {s.name for s in spans}
    assert "keelgate.policy.decide" in names and "chat m" in names
    tool_span = next(s for s in spans if s.name == "execute_tool act")
    assert tool_span.attributes[telemetry.attributes.GEN_AI_TOOL_NAME] == "act"
    assert "o-1" not in str([dict(s.attributes) for s in spans])  # no arguments in spans


def test_the_red_team_suite_blocks_every_attack() -> None:
    suite = asyncio.run(run_suite("redteam"))
    assert suite.passed and len(suite.cases) >= 30
    assert {c.category for c in suite.cases} >= {
        "tool_output_injection",
        "document_injection",
        "memory_poisoning",
        "capability_escalation",
        "asof_leakage",
    }


def test_outcome_metrics_follow_a_two_member_protocol() -> None:
    class Mine:
        name = "mine"
        higher_is_better = True

        def compute(self, records: Any) -> Any:
            from keelgate.evals import MetricResult

            return MetricResult(self.name, 1.0, len(records))

    assert isinstance(Mine(), OutcomeMetric)
    assert Mine().compute([OutcomeRecord("a", {}, {})]).n == 1
    assert isinstance(discover_metrics(), list)


# ------------------------------------------------------------------ the documented example


def test_the_use_keelgate_from_another_library_example_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = runpy.run_path(str(ROOT / "examples" / "use_keelgate_from_another_library.py"))
    assert namespace["main"]() == 0
    out = capsys.readouterr().out
    assert "goal reached: True; forecasts published: 1" in out
    assert "audit chain intact: True" in out and "replays identically: True" in out


def test_the_version_is_exposed() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", keelgate.__version__)
