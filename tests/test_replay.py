"""Rebuild a run from its trace id and replay it with recorded tool outputs."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from keelgate import telemetry as tel
from keelgate.loop import (
    InMemoryCheckpointStore,
    NotReplayableError,
    Recording,
    StopConditions,
    StopReason,
    VerdictDecision,
    diff,
    find_run,
    replay,
)
from keelgate.loop.roles import CallableVerifier, Verdict
from keelgate.loop.state import ActionOutcome
from keelgate.telemetry import attributes as attr
from keelgate.testing import Reply
from tests.conftest import MARKET_OPEN, run
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
from tests.test_telemetry_instrumentation import Capture

GOAL = "Research AAPL and buy a small position."


@pytest.fixture
def rig(tmp_path: Path, rego_engine: Any) -> Iterator[Rig]:
    r = build_rig(tmp_path / "rig", engine=rego_engine)
    yield r
    r.close()


def go(rig: Rig, script: list[Reply], run_id: str = "r1", **kw: Any) -> Any:
    return run(
        rig.loop(fake(script), **kw).run_or_resume(
            goal=GOAL, tenant_id=TENANT, agent_id=AGENT, as_of=MARKET_OPEN, run_id=run_id
        )
    )


def recording(rig: Rig, **kw: Any) -> Recording:
    return Recording.from_store(rig.checkpoints, TENANT, **kw)


# ------------------------------------------------------------------ rebuilding


def test_a_run_is_rebuilt_from_its_trace_id(rig: Rig) -> None:
    result = go(rig, three_step_script())
    rec = recording(rig, trace_id=result.state.trace_id)
    assert rec.run_id == "r1" and rec.trace_id == result.state.trace_id
    assert [[a.tool for a in s.actions] for s in rec.steps] == [
        ["market_quote"],
        ["paper_order"],
        [],
    ]
    assert rec.steps[2].final_answer == "Bought 5000 of AAPL at the quoted price."
    assert rec.stop_reason is StopReason.GOAL_REACHED and rec.final_answer
    assert rec.steps[1].actions[0].outcome is not None
    assert rec.steps[1].actions[0].outcome.status == "ok"


def test_the_trace_id_matches_the_one_on_the_runs_spans(rig: Rig) -> None:
    cap = Capture()
    with tel.use(cap.telemetry):
        result = go(rig, three_step_script())
    span_traces = {format(s.context.trace_id, "032x") for s in cap.spans}
    assert span_traces == {result.state.trace_id}
    assert recording(rig, trace_id=next(iter(span_traces))).run_id == "r1"


def test_lookup_by_run_id_works_and_exactly_one_key_is_required(rig: Rig) -> None:
    go(rig, three_step_script())
    assert recording(rig, run_id="r1").run_id == "r1"
    with pytest.raises(ValueError, match="exactly one"):
        recording(rig)
    with pytest.raises(ValueError, match="exactly one"):
        recording(rig, run_id="r1", trace_id="x")


def test_an_unknown_trace_id_and_another_tenants_run_are_not_found(rig: Rig) -> None:
    result = go(rig, three_step_script())
    with pytest.raises(NotReplayableError, match="no run with trace id"):
        recording(rig, trace_id="0" * 32)
    # the trace id exists, but not in this tenant
    assert find_run(rig.checkpoints, "another-tenant", result.state.trace_id) is None
    with pytest.raises(NotReplayableError):
        Recording.from_store(rig.checkpoints, "another-tenant", trace_id=result.state.trace_id)


def test_a_store_without_history_cannot_be_replayed_from() -> None:
    class Bare:
        def save(self, state: Any) -> None: ...
        def load(self, tenant_id: str, run_id: str) -> None:
            return None

        def runs(self, tenant_id: str, *, prefix: str = "") -> list[str]:
            return ["r"]

    with pytest.raises(NotReplayableError, match="no checkpoint history"):
        Recording.from_store(Bare(), "t", run_id="r")  # type: ignore[arg-type]
    with pytest.raises(NotReplayableError):
        Recording.from_history([])


# ------------------------------------------------------------------ replaying


def test_a_replay_is_identical_and_executes_nothing(rig: Rig) -> None:
    result = go(rig, three_step_script())
    assert len(rig.executions) == 1
    report = run(replay(recording(rig, trace_id=result.state.trace_id)))
    assert report.identical, (report.divergences, report.mismatched_calls)
    assert len(rig.executions) == 1  # the replay placed no order
    assert report.replayed.final_answer == report.original.final_answer
    assert [s.iteration for s in report.replayed.steps] == [1, 2, 3]


def test_a_replay_reproduces_tool_outputs_exactly_including_publication_times(rig: Rig) -> None:
    script = [
        Reply.call("get_news", topic="rates"),
        Reply.call("market_quote", symbol="AAPL"),
        Reply.say("Rates headline noted."),
    ]
    go(rig, script)
    rec = recording(rig, run_id="r1")
    news = rec.steps[0].actions[0].outcome
    assert news is not None and news.published_at is not None
    report = run(replay(rec))
    assert report.identical
    again = report.replayed.steps[0].actions[0].outcome
    assert again is not None
    assert again.output_json == news.output_json and again.published_at == news.published_at


def test_a_denied_action_replays_as_denied_with_its_recorded_reasons(rig: Rig) -> None:
    script = [
        Reply.call("paper_order", **order("bad-1", symbol="TSLA")),
        Reply.say("I could not trade TSLA."),
    ]
    go(rig, script)
    rec = recording(rig, run_id="r1")
    denied = rec.steps[0].actions[0]
    assert denied.status == "denied" and denied.outcome is not None
    assert denied.outcome.error_code == "policy_denied"
    report = run(replay(rec))
    assert report.identical and len(rig.executions) == 0
    again = report.replayed.steps[0].actions[0].outcome
    assert again is not None and again.details == denied.outcome.details


def test_a_verifier_rejection_and_revision_replay_faithfully(rig: Rig) -> None:
    calls = {"n": 0}

    def verify(request: Any) -> Verdict:
        calls["n"] += 1
        if calls["n"] == 1:
            return Verdict(VerdictDecision.REJECT, ("not grounded",), ("flag-a",))
        return Verdict.accept("ok")

    script = [
        Reply.say("first draft"),
        Reply.say("revised answer"),
    ]
    go(rig, script, verifier=CallableVerifier(verify))
    rec = recording(rig, run_id="r1")
    assert rec.steps[0].verdict == ("REJECT", ("not grounded",), ("flag-a",))
    assert rec.steps[1].verdict is not None and rec.steps[1].verdict[0] == "ACCEPT"
    report = run(replay(rec))
    assert report.identical, report.divergences
    assert report.replayed.final_answer == "revised answer"


def test_a_run_resumed_after_a_crash_replays_as_one_run(tmp_path: Path, rego_engine: Any) -> None:
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    state = {"hit": 0}

    def crash(name: str) -> None:
        if name == "after_gateway_call":
            state["hit"] += 1
            if state["hit"] == 1:
                raise SimulatedCrash("died after the write ran")

    with pytest.raises(SimulatedCrash):
        go(first, three_step_script(), failpoint=crash)
    first.close()
    second = build_rig(
        tmp_path / "rig",
        signer=first.signer,
        grant_token=first.grant_token,
        engine=rego_engine,
        clock=first.clock,
    )
    try:
        done = go(second, three_step_script())
        assert done.ok and len(second.executions) == 1
        report = run(replay(recording(second, trace_id=done.state.trace_id)))
        assert report.identical, report.divergences
        assert len(second.executions) == 1
    finally:
        second.close()


def test_replay_emits_its_own_trace_that_points_at_the_original(rig: Rig) -> None:
    result = go(rig, three_step_script())
    rec = recording(rig, run_id="r1")
    cap = Capture()
    with tel.use(cap.telemetry):
        report = run(replay(rec))
    assert report.identical
    [marker] = cap.named("keelgate.replay")
    assert marker.attributes[attr.REPLAY] is True
    assert marker.attributes[attr.REPLAY_OF] == result.state.trace_id
    traces = {format(s.context.trace_id, "032x") for s in cap.spans}
    assert len(traces) == 1 and result.state.trace_id not in traces  # a new trace
    # the replay executes no tool, so there is no policy decision to re-record
    assert cap.named("keelgate.policy.decide") == []


# ------------------------------------------------------------------ what cannot be replayed


def test_a_run_waiting_for_approval_has_no_result_to_replay(rig: Rig) -> None:
    script = [Reply.call("paper_order", **order("big-1", notional=30_000)), Reply.say("x")]
    stopped = go(rig, script)
    assert stopped.stop_reason is StopReason.APPROVAL_PENDING
    with pytest.raises(NotReplayableError, match="awaiting_approval"):
        run(replay(recording(rig, run_id="r1")))


def test_a_run_with_an_unknown_outcome_is_not_replayable(tmp_path: Path, rego_engine: Any) -> None:
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    first.crash_in_tool = True
    with pytest.raises(SimulatedCrash):
        go(first, three_step_script())
    first.close()
    second = build_rig(
        tmp_path / "rig",
        signer=first.signer,
        grant_token=first.grant_token,
        engine=rego_engine,
        clock=first.clock,
    )
    try:
        stopped = go(second, three_step_script())
        assert stopped.stop_reason is StopReason.OUTCOME_UNKNOWN
        with pytest.raises(NotReplayableError, match="unknown"):
            run(replay(recording(second, run_id="r1")))
    finally:
        second.close()


def test_a_run_stopped_with_a_saved_but_unrun_plan_is_not_replayable(rig: Rig) -> None:
    from keelgate.llm import Usage

    big = Usage(input_tokens=0, output_tokens=2000)
    stopped = go(
        rig,
        [Reply.call("paper_order", usage=big, **order("p-1")), Reply.say("done")],
        stop=StopConditions(max_tokens=1000),
    )
    assert stopped.stop_reason is StopReason.TOKEN_BUDGET
    with pytest.raises(NotReplayableError, match="pending"):
        run(replay(recording(rig, run_id="r1")))


# ------------------------------------------------------------------ detecting divergence


def test_diff_reports_a_changed_output_argument_verdict_and_answer(rig: Rig) -> None:
    go(rig, three_step_script())
    base = recording(rig, run_id="r1")
    assert diff(base, base) == []

    step0 = base.steps[0]
    action = step0.actions[0]
    assert action.outcome is not None
    changed_outcome = dataclasses.replace(
        action,
        outcome=ActionOutcome(status="ok", output_json='{"symbol":"AAPL","price":1.0}'),
    )
    out = dataclasses.replace(
        base, steps=(dataclasses.replace(step0, actions=(changed_outcome,)), *base.steps[1:])
    )
    assert [d.where for d in diff(base, out)] == ["step 1, action 0: outcome"]

    changed_args = dataclasses.replace(action, arguments={"symbol": "MSFT"})
    out = dataclasses.replace(
        base, steps=(dataclasses.replace(step0, actions=(changed_args,)), *base.steps[1:])
    )
    assert [d.where for d in diff(base, out)] == ["step 1, action 0: call"]

    out = dataclasses.replace(base, final_answer="something else")
    assert [d.where for d in diff(base, out)] == ["final answer"]
    short = dataclasses.replace(base, steps=base.steps[:2])
    assert diff(base, short)[0].where == "steps"


def test_a_replay_of_a_tampered_recording_surfaces_the_loops_own_behaviour(rig: Rig) -> None:
    """If a recorded verdict is missing, the replayed loop cannot proceed the same way."""
    go(rig, three_step_script())
    base = recording(rig, run_id="r1")
    last = dataclasses.replace(base.steps[-1], verdict=None)
    broken = dataclasses.replace(base, steps=(*base.steps[:-1], last))
    report = run(replay(broken))
    assert not report.identical
    assert report.replayed.stop_reason is StopReason.ERROR


# ------------------------------------------------------------------ stores


def test_the_in_memory_store_keeps_ordered_history() -> None:
    store = InMemoryCheckpointStore()
    from tests.test_checkpoint_contract import state

    for seq in (1, 2, 3):
        store.save(state(seq=seq, iteration=seq))
    assert [s.checkpoint_seq for s in store.history("t1", "r1")] == [1, 2, 3]
    assert store.history("t1", "missing") == [] and store.history("t2", "r1") == []


def test_langgraph_history_is_ordered_and_tenant_scoped(tmp_path: Path) -> None:
    pytest.importorskip("langgraph")
    from keelgate.loop.langgraph_store import LangGraphCheckpointStore
    from tests.test_checkpoint_contract import state

    store = LangGraphCheckpointStore.sqlite(tmp_path / "lg.sqlite")
    for seq in (1, 2, 3):
        store.save(state(seq=seq, iteration=seq))
    store.save(state(tenant="t2", seq=1))
    assert [s.checkpoint_seq for s in store.history("t1", "r1")] == [1, 2, 3]
    assert [s.tenant_id for s in store.history("t2", "r1")] == ["t2"]
    assert store.history("t1", "missing") == []


def test_a_langgraph_backed_run_replays_identically(tmp_path: Path, rig: Rig) -> None:
    pytest.importorskip("langgraph")
    from keelgate.loop.langgraph_store import LangGraphCheckpointStore

    store = LangGraphCheckpointStore.sqlite(tmp_path / "lg.sqlite")
    go(rig, three_step_script(), checkpoints=store)
    report = run(replay(Recording.from_store(store, TENANT, run_id="r1")))
    assert report.identical, report.divergences
