"""The instrumented paths: one trace per run, policy/approval/memory spans, resume continuity."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from keelgate import telemetry as tel
from keelgate.loop import StopReason
from keelgate.memory import Attribution, SemanticMemory, SqliteMemoryBackend
from keelgate.telemetry import attributes as attr
from keelgate.testing import FakeLLM, Reply
from keelgate.tools.outcomes import OutcomeStatus
from tests.conftest import MARKET_OPEN, Clock, Harness, build_harness, run
from tests.loop_support import (
    AGENT,
    TENANT,
    Rig,
    SimulatedCrash,
    build_rig,
    fake,
    order,
    three_step_script,
)

GOAL = "Research AAPL and buy a small position."


class Capture:
    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.telemetry = tel.Telemetry(tracer_provider=provider)

    @property
    def spans(self) -> list[Any]:
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> list[Any]:
        return [s for s in self.spans if s.name == name]

    def by_id(self) -> dict[int, Any]:
        return {s.context.span_id: s for s in self.spans}

    def parent_of(self, span: Any) -> Any:
        return self.by_id().get(span.parent.span_id) if span.parent else None


@pytest.fixture
def cap() -> Iterator[Capture]:
    c = Capture()
    with tel.use(c.telemetry):
        yield c


@pytest.fixture
def rig(tmp_path: Path, rego_engine: Any) -> Iterator[Rig]:
    r = build_rig(tmp_path / "rig", engine=rego_engine)
    yield r
    r.close()


def go(rig: Rig, script: list[Reply], run_id: str = "r1", **kw: Any) -> Any:
    llm = tel.InstrumentedLLM(fake(script))
    loop = rig.loop(llm, **kw)
    return run(
        loop.run_or_resume(
            goal=GOAL, tenant_id=TENANT, agent_id=AGENT, as_of=MARKET_OPEN, run_id=run_id
        )
    )


# ------------------------------------------------------------------ one trace per run


def test_a_run_is_one_trace_whose_id_is_the_runs_trace_id(rig: Rig, cap: Capture) -> None:
    result = go(rig, three_step_script())
    assert result.ok
    trace_ids = {s.context.trace_id for s in cap.spans}
    assert len(trace_ids) == 1
    assert format(next(iter(trace_ids)), "032x") == result.state.trace_id
    [root] = cap.named(f"invoke_agent {AGENT}")
    assert root.parent is None
    assert format(root.context.span_id, "016x") == result.state.root_span_id
    a = root.attributes
    assert a[attr.GEN_AI_OPERATION_NAME] == "invoke_agent" and a[attr.RESUMED] is False
    assert a[attr.TENANT_ID] == TENANT and a[attr.RUN_ID] == "r1"
    assert a[attr.STOP_REASON] == "goal_reached" and a[attr.ITERATION] == 3


def test_the_span_tree_nests_llm_tool_and_policy_spans_under_loop_steps(
    rig: Rig, cap: Capture
) -> None:
    go(rig, three_step_script())
    [root] = cap.named(f"invoke_agent {AGENT}")
    steps = [s for s in cap.spans if s.name.startswith("keelgate.loop.")]
    assert {s.name for s in steps} >= {
        "keelgate.loop.plan",
        "keelgate.loop.act",
        "keelgate.loop.observe",
        "keelgate.loop.verify",
    }
    assert all(cap.parent_of(s) is root for s in steps)
    for chat in cap.named("chat fake-model"):
        assert cap.parent_of(chat).name == "keelgate.loop.plan"
    tools = cap.named("execute_tool market_quote") + cap.named("execute_tool paper_order")
    assert len(tools) == 2 and all(cap.parent_of(t).name == "keelgate.loop.act" for t in tools)
    [decision] = cap.named("keelgate.policy.decide")  # reads skip policy; the order does not
    assert cap.parent_of(decision).name == "execute_tool paper_order"


def test_policy_decision_spans_carry_effect_engine_version_and_reasons(
    rig: Rig, cap: Capture
) -> None:
    allowed = go(rig, three_step_script("ok-1"), run_id="r-ok")
    assert allowed.ok
    [ok] = cap.named("keelgate.policy.decide")
    assert ok.attributes[attr.POLICY_EFFECT] == "ALLOW"
    assert ok.attributes[attr.POLICY_ENGINE] == rig.gateway._engine.name
    assert str(ok.attributes[attr.POLICY_VERSION]).startswith("sha256:")
    assert ok.attributes[attr.TOOL_SIDE_EFFECT] == "WRITE"

    cap.exporter.clear()
    denied_script = [
        Reply.call("paper_order", **order("bad-1", symbol="TSLA")),
        Reply.say("could not trade"),
    ]
    go(rig, denied_script, run_id="r-deny")
    [bad] = cap.named("keelgate.policy.decide")
    assert bad.attributes[attr.POLICY_EFFECT] == "DENY"
    assert "restricted" in str(bad.attributes[attr.POLICY_REASONS])
    [tool] = cap.named("execute_tool paper_order")
    assert tool.attributes[attr.TOOL_STATUS] == "DENIED"
    assert tool.attributes[attr.TOOL_ERROR_CODE] == "policy_denied"


def test_no_span_carries_arguments_tool_output_or_model_text(rig: Rig, cap: Capture) -> None:
    go(rig, three_step_script("o-sensitive-1"))
    blob = " ".join(
        str(dict(s.attributes)) + str([dict(e.attributes) for e in s.events]) + s.name
        for s in cap.spans
    )
    for leaked in ("AAPL", "187.25", "o-sensitive-1", "5000", "Bought 5000"):
        assert leaked not in blob, leaked
    [tool] = cap.named("execute_tool paper_order")
    assert len(str(tool.attributes[attr.ARGS_HASH])) == 64  # a hash stands in for the arguments


def test_every_span_in_a_run_is_attributed_to_its_tenant_and_agent(rig: Rig, cap: Capture) -> None:
    go(rig, three_step_script())
    for s in cap.spans:
        if s.name.startswith(("invoke_agent", "keelgate.loop", "chat", "execute_tool")):
            assert s.attributes[attr.TENANT_ID] == TENANT, s.name
    assert cap.telemetry.costs.total(tenant_id=TENANT, agent_id=AGENT).calls == 3


# ------------------------------------------------------------------ resume continuity


def test_a_resumed_run_continues_the_same_trace_under_the_original_root(
    rig: Rig, cap: Capture
) -> None:
    hits = {"n": 0}

    def crash_once(name: str) -> None:
        if name == "after_gateway_call":
            hits["n"] += 1
            if hits["n"] == 1:
                raise SimulatedCrash("died")

    with pytest.raises(SimulatedCrash):
        go(rig, three_step_script(), failpoint=crash_once)
    first_trace = {s.context.trace_id for s in cap.spans}
    assert len(first_trace) == 1

    rig2 = build_rig(
        rig.root,
        signer=rig.signer,
        grant_token=rig.grant_token,
        engine=rig.gateway._engine,
        clock=rig.clock,
    )
    try:
        done = go(rig2, three_step_script())
    finally:
        rig2.close()
    assert done.ok
    assert {s.context.trace_id for s in cap.spans} == first_trace  # still ONE trace
    roots = cap.named(f"invoke_agent {AGENT}")
    assert [r.attributes[attr.RESUMED] for r in roots] == [False, True]
    original, resumed = roots
    assert resumed.parent.span_id == original.context.span_id
    assert done.state.root_span_id == format(original.context.span_id, "016x")


def test_a_caller_supplied_trace_id_is_joined(rig: Rig, cap: Capture) -> None:
    wanted = "ab" * 16
    llm = tel.InstrumentedLLM(fake(three_step_script()))
    result = run(
        rig.loop(llm).run(
            goal=GOAL,
            tenant_id=TENANT,
            agent_id=AGENT,
            as_of=MARKET_OPEN,
            run_id="r-joined",
            trace_id=wanted,
        )
    )
    assert result.state.trace_id == wanted
    assert {format(s.context.trace_id, "032x") for s in cap.spans} == {wanted}


def test_without_a_configured_provider_runs_still_get_a_trace_id(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = go(rig, three_step_script())  # default telemetry: the no-op global provider
    assert result.ok and len(result.state.trace_id) == 32
    assert int(result.state.trace_id, 16) > 0
    assert result.state.root_span_id == ""


# ------------------------------------------------------------------ approvals


def test_approval_lifecycle_spans(rego_engine: Any, cap: Capture) -> None:
    h: Harness = build_harness(engine=rego_engine)
    big = {"symbol": "AAPL", "notional": 30_000, "client_order_id": "cl-big"}
    parked = h.call("trade_paper_execute", big)
    assert parked.status is OutcomeStatus.APPROVAL_REQUIRED
    [submit] = cap.named("keelgate.approval.submit")
    assert submit.attributes[attr.APPROVAL_TIER] == "EXPLICIT_SIGNOFF"
    assert submit.attributes[attr.APPROVAL_ID] == parked.approval_id
    assert cap.parent_of(submit).name == "execute_tool trade_paper_execute"
    tool = cap.named("execute_tool trade_paper_execute")[0]
    assert tool.attributes[attr.TOOL_STATUS] == "APPROVAL_REQUIRED"
    assert tool.attributes[attr.APPROVAL_ID] == parked.approval_id

    request = h.queue.get("tenant-1", parked.approval_id)
    h.queue.approve("tenant-1", request.request_id, h.approver, signoff_code=request.signoff_code)
    [approve] = cap.named("keelgate.approval.approve")
    assert approve.attributes[attr.APPROVAL_STATUS] == "APPROVED"

    done = h.call("trade_paper_execute", big, context=h.ctx(approval_id=request.request_id))
    assert done.status is OutcomeStatus.OK
    [consume] = cap.named("keelgate.approval.consume")
    assert consume.attributes[attr.APPROVAL_STATUS] == "CONSUMED"


def test_a_rejected_approval_is_traced(rego_engine: Any, cap: Capture) -> None:
    h = build_harness(engine=rego_engine)
    parked = h.call(
        "trade_paper_execute", {"symbol": "AAPL", "notional": 30_000, "client_order_id": "cl-r"}
    )
    request = h.queue.get("tenant-1", parked.approval_id)
    h.queue.reject("tenant-1", request.request_id, h.approver)
    [rejected] = cap.named("keelgate.approval.reject")
    assert rejected.attributes[attr.APPROVAL_STATUS] == "REJECTED"


# ------------------------------------------------------------------ memory


def test_memory_operations_are_spans_that_record_counts_never_content(cap: Capture) -> None:
    clock = Clock(MARKET_OPEN)
    memory = SemanticMemory(SqliteMemoryBackend(), tenant_id="t1", clock=clock)
    who = Attribution(agent_id="a", trace_id="tr")
    record = memory.assert_fact(
        "rates",
        "The policy rate is 5.25 percent.",
        valid_from=MARKET_OPEN - timedelta(days=1),
        attribution=who,
    )
    found = memory.search("policy rate", as_of=MARKET_OPEN)
    memory.get(record.record_id, as_of=MARKET_OPEN)
    assert len(found) == 1
    ops = {
        s.attributes[attr.MEMORY_OP]: s for s in cap.spans if s.name.startswith("keelgate.memory")
    }
    assert {"write", "search", "get"} <= set(ops)
    assert ops["search"].attributes[attr.MEMORY_RESULTS] == 1
    assert ops["write"].attributes[attr.MEMORY_TIER] == "semantic"
    assert ops["write"].attributes[attr.TENANT_ID] == "t1"
    blob = " ".join(str(dict(s.attributes)) for s in cap.spans)
    assert "5.25" not in blob and "policy rate" not in blob


def test_a_failing_memory_operation_records_the_error_type_only(cap: Capture) -> None:
    memory = SemanticMemory(SqliteMemoryBackend(), tenant_id="t1", clock=Clock(MARKET_OPEN))
    with pytest.raises(ValueError, match="positive"):
        memory.search("x", as_of=MARKET_OPEN, limit=0)
    [s] = [s for s in cap.spans if s.name == "keelgate.memory.search"]
    assert s.attributes["error.type"] == "ValueError"


def test_the_fake_llm_script_is_unaffected_by_instrumentation(rig: Rig, cap: Capture) -> None:
    llm = FakeLLM(three_step_script(), indexed=True)
    result = run(
        rig.loop(tel.InstrumentedLLM(llm)).run(
            goal=GOAL, tenant_id=TENANT, agent_id=AGENT, as_of=MARKET_OPEN, run_id="r-x"
        )
    )
    assert result.stop_reason is StopReason.GOAL_REACHED and llm.calls_made == 3
