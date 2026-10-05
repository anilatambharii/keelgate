"""Kill-and-resume: a crash at ANY point never repeats a completed WRITE.

"Killing" here raises ``SimulatedCrash`` (a BaseException, so no ``except Exception`` in the
code under test can swallow it) from a failpoint at a named crash window. A "restart" is a
brand-new rig over the same directory: every file-backed store survives, every in-memory
object is rebuilt. Real subprocess kills are in ``test_loop_subprocess.py``.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from keelgate.approvals import ApprovalTier, Approver
from keelgate.loop import ActionStatus, StopReason
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

GOAL = "Research AAPL and buy a small position."
FAILPOINTS = [
    "after_plan",
    "before_action",
    "after_gateway_call",
    "after_action_checkpoint",
    "after_observe",
    "after_verify",
]
FINAL = "Bought 5000 of AAPL at the quoted price."


def crash_at(point: str, occurrence: int) -> Any:
    counts: dict[str, int] = {}

    def hook(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1
        if name == point and counts[name] == occurrence:
            raise SimulatedCrash(f"{point}#{occurrence}")

    return hook


def restart(rig: Rig, engine: Any) -> Rig:
    """A new "process": file-backed state survives, everything in memory is rebuilt."""
    rig.close()
    return build_rig(
        rig.root, signer=rig.signer, grant_token=rig.grant_token, engine=engine, clock=rig.clock
    )


def go(rig: Rig, *, failpoint: Any = None, script: Any = None, **loop_kwargs: Any) -> Any:
    loop = rig.loop(fake(script or three_step_script()), failpoint=failpoint, **loop_kwargs)
    return run(
        loop.run_or_resume(
            goal=GOAL, tenant_id=TENANT, agent_id=AGENT, as_of=MARKET_OPEN, run_id="r1"
        )
    )


# --------------------------------------------------------------- every crash window


@pytest.mark.parametrize("occurrence", [1, 2, 3])
@pytest.mark.parametrize("point", FAILPOINTS)
def test_a_crash_at_any_window_resumes_without_a_duplicate_write(
    tmp_path: Path, rego_engine: Any, point: str, occurrence: int
) -> None:
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    crashed = False
    try:
        go(first, failpoint=crash_at(point, occurrence))
    except SimulatedCrash:
        crashed = True
    # (some windows are not reached a 3rd time; then nothing crashes and the run just finishes)

    second = restart(first, rego_engine)
    result = go(second)

    assert result.ok and result.final_answer == FINAL
    assert [e["client_order_id"] for e in second.executions] == ["o-1"], (
        f"{point}#{occurrence} (crashed={crashed}) produced {second.executions}"
    )
    assert second.audit.verify_chain(TENANT).ok


def test_a_crash_after_the_tool_ran_but_before_the_checkpoint_replays_the_stored_result(
    tmp_path: Path, rego_engine: Any
) -> None:
    """The window checkpoints cannot cover. Only the durable idempotency store can."""
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    with pytest.raises(SimulatedCrash):
        go(first, failpoint=crash_at("after_gateway_call", 2))  # 2nd gateway call is the WRITE
    assert len(first.executions) == 1  # it genuinely ran...
    saved = first.checkpoints.load(TENANT, "r1")
    assert (
        saved is not None and saved.actions[0].status is ActionStatus.PENDING
    )  # ...but is not recorded

    second = restart(first, rego_engine)
    result = go(second)
    assert result.ok and len(second.executions) == 1
    replayed = [
        a.outcome.replayed
        for s in second.checkpoints.history(TENANT, "r1")
        for a in s.actions
        if a.tool == "paper_order" and a.outcome and a.outcome.status == "ok"
    ]
    assert replayed and all(replayed)  # the resumed step got the stored result, not a new execution


def test_a_crash_after_planning_runs_the_saved_actions_not_a_fresh_plan(
    tmp_path: Path, rego_engine: Any
) -> None:
    """Write-ahead: the model proposed an order, the process died, and the model is NOT asked again."""
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    with pytest.raises(SimulatedCrash):
        go(first, failpoint=crash_at("after_plan", 2))
    assert first.executions == []

    second = restart(first, rego_engine)
    llm = fake(three_step_script())
    loop = second.loop(llm)
    result = run(loop.resume(TENANT, "r1"))

    assert result.ok and [e["client_order_id"] for e in second.executions] == ["o-1"]
    assert (
        llm.calls_made == 1
    )  # only the closing answer: the order plan was reused, not regenerated


def test_a_model_that_would_say_something_different_cannot_change_a_saved_action(
    tmp_path: Path, rego_engine: Any
) -> None:
    """After a crash the replacement model wants a different order id. The saved one still wins."""
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    with pytest.raises(SimulatedCrash):
        go(first, failpoint=crash_at("after_plan", 2))

    second = restart(first, rego_engine)
    changed = [
        three_step_script()[0],
        three_step_script("DIFFERENT-ID")[1],  # a replanning model would send this one
        three_step_script()[2],
    ]
    result = go(second, script=changed)
    assert result.ok
    assert [e["client_order_id"] for e in second.executions] == ["o-1"]


def test_repeated_crashes_never_duplicate_the_write(tmp_path: Path, rego_engine: Any) -> None:
    rig = build_rig(tmp_path / "rig", engine=rego_engine)
    for point in ("after_plan", "after_gateway_call", "after_observe", "after_verify"):
        with contextlib.suppress(SimulatedCrash):
            go(rig, failpoint=crash_at(point, 2))
        rig = restart(rig, rego_engine)
    result = go(rig)
    assert result.ok and len(rig.executions) == 1


# ----------------------------------------------------- a crash inside the tool body


def test_a_crash_inside_the_tool_body_leaves_the_outcome_unknown_and_is_never_retried(
    tmp_path: Path, rego_engine: Any
) -> None:
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    first.crash_in_tool = True
    with pytest.raises(SimulatedCrash):
        go(first)
    assert len(first.executions) == 1  # the body started: it may or may not have taken effect

    second = restart(first, rego_engine)
    stopped = go(second)
    assert stopped.stop_reason is StopReason.OUTCOME_UNKNOWN and stopped.resumable
    assert stopped.state.actions[0].status is ActionStatus.UNKNOWN
    assert len(second.executions) == 1  # NOT retried

    again = go(second)  # asking again changes nothing
    assert again.stop_reason is StopReason.OUTCOME_UNKNOWN and len(second.executions) == 1


def test_a_human_reconciles_an_unknown_write_and_the_loop_finishes_without_repeating_it(
    tmp_path: Path, rego_engine: Any
) -> None:
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    first.crash_in_tool = True
    with pytest.raises(SimulatedCrash):
        go(first)
    second = restart(first, rego_engine)
    stopped = go(second)
    action_id = stopped.state.actions[0].action_id

    loop = second.loop(fake(three_step_script()))
    state = run(
        loop.reconcile(
            TENANT, "r1", action_id, executed=True, by="alice", note="checked the broker"
        )
    )
    assert state.action(action_id).status is ActionStatus.DONE
    assert "reconciled by alice" in (
        state.action(action_id).outcome.message if state.action(action_id).outcome else ""
    )

    result = go(second)
    assert result.ok and len(second.executions) == 1
    audited = [r for r in second.audit.records(TENANT) if r.event_type == "loop.reconciled"]
    assert [(r.actor, r.payload["source"], r.payload["executed"]) for r in audited] == [
        ("alice", "human", True)
    ]


def test_a_human_can_abandon_an_unknown_write_and_the_loop_moves_on(
    tmp_path: Path, rego_engine: Any
) -> None:
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    first.crash_in_tool = True
    with pytest.raises(SimulatedCrash):
        go(first)
    second = restart(first, rego_engine)
    action_id = go(second).state.actions[0].action_id
    state = run(
        second.loop(fake([])).reconcile(TENANT, "r1", action_id, executed=False, by="alice")
    )
    assert state.action(action_id).status is ActionStatus.ABANDONED
    assert go(second).ok and len(second.executions) == 1  # still never run a second time


# ------------------------------------------------------------- approvals and crashes


def test_a_crash_after_an_approved_write_ran_replays_it_and_does_not_re_execute(
    tmp_path: Path, rego_engine: Any
) -> None:
    script = [
        Reply.call("paper_order", **order("big-1", notional=30_000)),
        Reply.say("Bought, with approval."),
    ]
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    parked = go(first, script=script)
    assert parked.stop_reason is StopReason.APPROVAL_PENDING

    approval_id = parked.state.actions[0].approval_id or ""
    request = first.approvals.get(TENANT, approval_id)
    approver = Approver(
        approver_id="alice", tenant_id=TENANT, max_tier=ApprovalTier.EXPLICIT_SIGNOFF
    )
    first.approvals.approve(TENANT, approval_id, approver, signoff_code=request.signoff_code)

    with pytest.raises(SimulatedCrash):
        go(first, script=script, failpoint=crash_at("after_gateway_call", 1))
    assert len(first.executions) == 1  # approved, consumed and executed; then the process died

    second = restart(first, rego_engine)
    result = go(second, script=script)  # the approval is CONSUMED now; the replay must still work
    assert result.ok and len(second.executions) == 1


# ------------------------------------------------------------------- the property


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    crashes=st.lists(
        st.tuples(st.sampled_from(FAILPOINTS), st.integers(min_value=1, max_value=3)),
        min_size=0,
        max_size=4,
    )
)
def test_any_sequence_of_crashes_still_executes_the_write_exactly_once(
    crashes: list[tuple[str, int]], rego_engine: Any
) -> None:
    with tempfile.TemporaryDirectory() as root:
        rig = build_rig(Path(root) / "rig", engine=rego_engine)
        for point, occurrence in crashes:
            with contextlib.suppress(SimulatedCrash):
                go(rig, failpoint=crash_at(point, occurrence))
            rig = restart(rig, rego_engine)
        result = go(rig)

        assert result.ok and result.final_answer == FINAL
        assert [e["client_order_id"] for e in rig.executions] == ["o-1"]
        assert rig.audit.verify_chain(TENANT).ok
        rig.close()


# ------------------------------------------------- an outcome confirmer settles unknowns


class Confirmer:
    name = "orders-ledger"

    def __init__(self, verdict: Any) -> None:
        self.verdict = verdict
        self.asked: list[str] = []
        self.statuses: list[Any] = []

    async def confirm(self, action: Any, state: Any) -> Any:
        self.asked.append(action.action_id)
        self.statuses.append(action.status)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def unknown_after_crash(tmp_path: Path, rego_engine: Any) -> Rig:
    first = build_rig(tmp_path / "rig", engine=rego_engine)
    first.crash_in_tool = True
    with pytest.raises(SimulatedCrash):
        go(first)
    return restart(first, rego_engine)


def test_a_confirmer_that_says_it_executed_lets_the_loop_finish_without_repeating(
    tmp_path: Path, rego_engine: Any
) -> None:
    rig = unknown_after_crash(tmp_path, rego_engine)
    confirmer = Confirmer(True)
    done = go(rig, confirmer=confirmer)
    assert done.ok and len(confirmer.asked) == 1
    assert len(rig.executions) == 1  # confirmed as done, never re-run
    assert confirmer.statuses == [ActionStatus.UNKNOWN]  # it was asked about an unknown action
    events = [r for r in rig.audit.records(TENANT) if r.event_type == "loop.reconciled"]
    assert len(events) == 1 and events[0].actor == "orders-ledger"
    assert events[0].payload["executed"] is True and events[0].payload["source"] == "confirmer"
    assert "market" not in str(events[0].payload) and "5000" not in str(events[0].payload)
    assert rig.audit.verify_chain(TENANT).ok


def test_a_confirmer_that_says_it_did_not_execute_abandons_the_action_and_moves_on(
    tmp_path: Path, rego_engine: Any
) -> None:
    rig = unknown_after_crash(tmp_path, rego_engine)
    confirmer = Confirmer(False)
    done = go(rig, confirmer=confirmer)
    assert done.ok and len(confirmer.asked) == 1  # the loop moved on and finished
    assert len(rig.executions) == 1  # abandoned, not retried under the same key


@pytest.mark.parametrize("verdict", [None, "yes", 1, RuntimeError("ledger down")])
def test_an_uncertain_or_failing_confirmer_never_settles_anything(
    tmp_path: Path, rego_engine: Any, verdict: Any
) -> None:
    rig = unknown_after_crash(tmp_path, rego_engine)
    stopped = go(rig, confirmer=Confirmer(verdict))
    assert stopped.stop_reason is StopReason.OUTCOME_UNKNOWN
    assert stopped.state.actions[0].status is ActionStatus.UNKNOWN
    assert len(rig.executions) == 1
