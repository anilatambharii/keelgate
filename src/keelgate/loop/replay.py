"""Rebuild a run from its trace id, and replay it deterministically.

A loop run is fully described by its checkpoint history: what the planner proposed at each step,
what each tool returned, what the verifier said. :class:`Recording` rebuilds that from the history
(find it by the run's trace id, the same id its OpenTelemetry trace carries).

:func:`replay` then runs a **fresh loop** whose model and tools are replaced by the recording:

* the planner returns the recorded plan for each iteration;
* the verifier returns the recorded verdict;
* the gateway returns the recorded tool outcome. It never calls a tool, so a replay has no side
  effects and needs no grants, no policy engine and no network.

The replayed run is checkpointed and rebuilt into a second :class:`Recording`; :func:`diff`
compares the two. A replay is *identical* when every step, tool call, argument, outcome, verdict
and the final answer match. If the loop's own logic changed since the recording (a different
stop rule, a changed observation format that alters what the planner would be shown), the divergence
shows up here.

What it does not do: it does not re-ask a model (so it cannot tell you whether a *new* model would
choose differently; that is what the evals are for), and it does not re-evaluate policy against
today's rules. A run that was left waiting for approval, or whose outcome is still unknown, has no
recorded result to replay and is refused with :class:`NotReplayableError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, PrivateAttr

from keelgate.llm.types import LLMError
from keelgate.loop.checkpoint import InMemoryCheckpointStore
from keelgate.loop.roles import Plan, ProposedAction, Verdict, VerdictDecision
from keelgate.loop.state import ActionOutcome, ActionStatus, LoopState, LoopType, StopReason
from keelgate.loop.stop import StopConditions
from keelgate.policy.types import PolicyContext
from keelgate.telemetry import attributes as attr
from keelgate.telemetry.core import span
from keelgate.tools.outcomes import (
    ErrorCode,
    OutcomeStatus,
    ToolError,
    ToolOutcome,
    Untrusted,
)
from keelgate.tools.spec import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from keelgate.loop.checkpoint import CheckpointStore
    from keelgate.loop.roles import PlanRequest, VerifyRequest

REPLAYABLE: Final = frozenset({ActionStatus.DONE, ActionStatus.DENIED, ActionStatus.ERROR})
# After these the run is over, so a faithful replay must end the same way.
TERMINAL_STOPS: Final = frozenset({StopReason.GOAL_REACHED, StopReason.VERIFIER_REJECTIONS})


class NotReplayableError(Exception):
    """The recording lacks a recorded result for some action, or the run cannot be found."""


class ReplayExhaustedError(LLMError):
    """The replayed loop asked for more than the recording contains."""

    def __init__(self) -> None:
        super().__init__("the recording has no further steps", provider="replay")


# ------------------------------------------------------------------ the recording


@dataclass(frozen=True)
class RecordedAction:
    tool: str
    arguments: dict[str, Any]
    rationale: str
    status: str
    outcome: ActionOutcome | None


@dataclass(frozen=True)
class RecordedStep:
    iteration: int
    actions: tuple[RecordedAction, ...]
    final_answer: str | None
    verdict: tuple[str, tuple[str, ...], tuple[str, ...]] | None  # decision, reasons, flags


@dataclass(frozen=True)
class Recording:
    tenant_id: str
    run_id: str
    trace_id: str
    agent_id: str
    goal: str
    as_of: datetime
    loop_type: LoopType
    steps: tuple[RecordedStep, ...]
    stop_reason: StopReason | None
    final_answer: str | None
    checkpoints: int = 0

    @classmethod
    def from_history(cls, history: Sequence[LoopState]) -> Recording:
        if not history:
            raise NotReplayableError("no checkpoints to rebuild a run from")
        ordered = sorted(history, key=lambda s: s.checkpoint_seq)
        final = ordered[-1]
        steps: list[RecordedStep] = []
        for iteration in sorted({s.iteration for s in ordered if s.iteration > 0}):
            snaps = [s for s in ordered if s.iteration == iteration]
            planned = snaps[0]  # the first save of an iteration is the plan write-ahead
            settled = next((s for s in reversed(snaps) if s.actions), planned)
            outcomes = {a.action_id: a for a in settled.actions}
            actions = tuple(
                RecordedAction(
                    tool=a.tool,
                    arguments=dict(a.arguments),
                    rationale=a.rationale,
                    status=outcomes.get(a.action_id, a).status.value,
                    outcome=outcomes.get(a.action_id, a).outcome,
                )
                for a in planned.actions
            )
            verdict = next((v for v in final.verdicts if v.iteration == iteration), None)
            steps.append(
                RecordedStep(
                    iteration=iteration,
                    actions=actions,
                    final_answer=planned.pending_final_answer,
                    verdict=(verdict.decision, verdict.reasons, verdict.flags) if verdict else None,
                )
            )
        return cls(
            tenant_id=final.tenant_id,
            run_id=final.run_id,
            trace_id=final.trace_id,
            agent_id=final.agent_id,
            goal=final.goal,
            as_of=final.as_of,
            loop_type=final.loop_type,
            steps=tuple(steps),
            stop_reason=final.stop_reason,
            final_answer=final.final_answer,
            checkpoints=len(ordered),
        )

    @classmethod
    def from_store(
        cls,
        store: CheckpointStore,
        tenant_id: str,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
    ) -> Recording:
        """Rebuild a run by trace id (or run id) from a store that keeps history."""
        if (trace_id is None) == (run_id is None):
            raise ValueError("give exactly one of trace_id and run_id")
        history = getattr(store, "history", None)
        if not callable(history):
            raise NotReplayableError(
                f"{type(store).__name__} keeps no checkpoint history; replay needs one that does"
            )
        found = run_id or find_run(store, tenant_id, trace_id or "")
        if found is None:
            raise NotReplayableError(f"no run with trace id {trace_id!r} for tenant {tenant_id!r}")
        states = history(tenant_id, found)
        if not states:
            raise NotReplayableError(f"no run {found!r} for tenant {tenant_id!r}")
        return cls.from_history(states)

    def check_replayable(self) -> None:
        for step in self.steps:
            for action in step.actions:
                if ActionStatus(action.status) not in REPLAYABLE or action.outcome is None:
                    raise NotReplayableError(
                        f"step {step.iteration}: {action.tool} is {action.status}, which has no "
                        "recorded result to replay"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "agent_id": self.agent_id,
            "as_of": self.as_of.isoformat(),
            "loop_type": self.loop_type.value,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "final_answer": self.final_answer,
            "steps": [
                {
                    "iteration": s.iteration,
                    "final_answer": s.final_answer,
                    "verdict": list(s.verdict) if s.verdict else None,
                    "actions": [
                        {
                            "tool": a.tool,
                            "status": a.status,
                            "args_keys": sorted(a.arguments),
                            "outcome": a.outcome.status if a.outcome else None,
                            "error_code": a.outcome.error_code if a.outcome else None,
                        }
                        for a in s.actions
                    ],
                }
                for s in self.steps
            ],
        }


def find_run(store: CheckpointStore, tenant_id: str, trace_id: str) -> str | None:
    """The run id whose trace id is ``trace_id`` within one tenant (never across tenants)."""
    for run_id in store.runs(tenant_id):
        state = store.load(tenant_id, run_id)
        if state is not None and state.trace_id == trace_id:
            return run_id
    return None


# ------------------------------------------------------------------ replay doubles


class _RecordedOutput(BaseModel):
    """Stands in for a tool's output model; dumps to exactly the recorded JSON."""

    _raw: str = PrivateAttr(default="")
    _published_at: datetime | None = PrivateAttr(default=None)

    @classmethod
    def of(cls, raw: str, published_at: datetime | None) -> _RecordedOutput:
        out = cls()
        out._raw = raw
        out._published_at = published_at
        return out

    @property
    def published_at(self) -> datetime | None:
        return self._published_at

    def model_dump_json(self, **_: Any) -> str:
        return self._raw


class ReplayPlanner:
    """Returns the recorded plan for each iteration. Never calls a model."""

    def __init__(self, recording: Recording) -> None:
        self._steps = {s.iteration: s for s in recording.steps}

    async def plan(self, request: PlanRequest) -> Plan:
        step = self._steps.get(request.iteration)
        if step is None:
            raise ReplayExhaustedError
        return Plan(
            actions=tuple(
                ProposedAction(tool=a.tool, arguments=dict(a.arguments), rationale=a.rationale)
                for a in step.actions
            ),
            final_answer=step.final_answer,
            model="replay",
        )


class ReplayVerifier:
    """Returns the recorded verdict for each iteration."""

    def __init__(self, recording: Recording) -> None:
        self._verdicts = {s.iteration: s.verdict for s in recording.steps if s.verdict}

    async def verify(self, request: VerifyRequest) -> Verdict:
        recorded = self._verdicts.get(request.iteration)
        if recorded is None:
            raise ReplayExhaustedError
        decision, reasons, flags = recorded
        return Verdict(VerdictDecision(decision), tuple(reasons), tuple(flags))


class ReplayGateway:
    """Answers each tool call with the next recorded outcome. Runs no tool, checks no grant."""

    def __init__(self, recording: Recording) -> None:
        self._queue = [a for s in recording.steps for a in s.actions]
        self._next = 0
        self.mismatches: list[str] = []

    async def call(
        self, *, tool_name: str, arguments: Mapping[str, Any], grant_token: str, context: Any
    ) -> ToolOutcome:
        del grant_token, context  # a replay needs no authority: nothing executes
        if self._next >= len(self._queue):
            self.mismatches.append(f"unexpected extra call to {tool_name}")
            raise ReplayExhaustedError
        recorded = self._queue[self._next]
        self._next += 1
        if recorded.tool != tool_name or _canon(recorded.arguments) != _canon(arguments):
            self.mismatches.append(f"call {self._next}: expected {recorded.tool}, got {tool_name}")
        return _to_outcome(tool_name, recorded)


def _canon(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _to_outcome(tool: str, recorded: RecordedAction) -> ToolOutcome:
    out = recorded.outcome
    assert out is not None  # noqa: S101 - check_replayable() guarantees it
    if out.status == "ok":
        output: Untrusted[BaseModel] | None = (
            Untrusted(_RecordedOutput.of(out.output_json, out.published_at))
            if out.output_json is not None
            else None
        )
        return ToolOutcome(OutcomeStatus.OK, tool, "replay", output=output, replayed=out.replayed)
    try:
        code = ErrorCode(out.error_code or "")
    except ValueError:
        code = ErrorCode.INTERNAL
    status = OutcomeStatus.DENIED if out.status == "denied" else OutcomeStatus.ERROR
    error = ToolError(code=code, message=out.message, hint=out.hint, details=dict(out.details))
    return ToolOutcome(status, tool, "replay", error=error)


# ------------------------------------------------------------------ replay and compare


@dataclass(frozen=True)
class Divergence:
    where: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class ReplayReport:
    original: Recording
    replayed: Recording
    divergences: tuple[Divergence, ...] = field(default_factory=tuple)
    mismatched_calls: tuple[str, ...] = ()

    @property
    def identical(self) -> bool:
        return not self.divergences and not self.mismatched_calls


def diff(original: Recording, replayed: Recording) -> list[Divergence]:
    out: list[Divergence] = []
    if len(original.steps) != len(replayed.steps):
        out.append(Divergence("steps", len(original.steps), len(replayed.steps)))
    for a, b in zip(original.steps, replayed.steps, strict=False):
        where = f"step {a.iteration}"
        if a.final_answer != b.final_answer:
            out.append(Divergence(f"{where}: final answer", a.final_answer, b.final_answer))
        if a.verdict != b.verdict:
            out.append(Divergence(f"{where}: verdict", a.verdict, b.verdict))
        if len(a.actions) != len(b.actions):
            out.append(Divergence(f"{where}: action count", len(a.actions), len(b.actions)))
        for n, (x, y) in enumerate(zip(a.actions, b.actions, strict=False)):
            if (x.tool, _canon(x.arguments)) != (y.tool, _canon(y.arguments)):
                out.append(Divergence(f"{where}, action {n}: call", x.tool, y.tool))
            if _outcome_key(x.outcome) != _outcome_key(y.outcome):
                out.append(
                    Divergence(
                        f"{where}, action {n}: outcome",
                        _outcome_key(x.outcome),
                        _outcome_key(y.outcome),
                    )
                )
    if original.stop_reason in TERMINAL_STOPS:
        if original.stop_reason != replayed.stop_reason:
            out.append(Divergence("stop reason", original.stop_reason, replayed.stop_reason))
        if original.final_answer != replayed.final_answer:
            out.append(Divergence("final answer", original.final_answer, replayed.final_answer))
    return out


def _outcome_key(outcome: ActionOutcome | None) -> tuple[Any, ...] | None:
    if outcome is None:
        return None
    return (outcome.status, outcome.error_code, outcome.output_json, outcome.message)


async def replay(recording: Recording) -> ReplayReport:
    """Replay a recording through a fresh loop and report any divergence."""
    from keelgate.loop.engine import Loop  # noqa: PLC0415 - engine imports this module's peers

    recording.check_replayable()
    store = InMemoryCheckpointStore()
    gateway = ReplayGateway(recording)
    loop = Loop(
        gateway=gateway,
        registry=ToolRegistry(),
        planner=ReplayPlanner(recording),
        checkpoints=store,
        grant_token="replay",  # noqa: S106 - unused: a replay executes nothing
        policy_context=lambda as_of: PolicyContext(as_of=as_of, execution_mode="paper"),
        verifier=ReplayVerifier(recording),
        stop=StopConditions(max_iterations=None, max_verifier_rejections=None),
        loop_type=recording.loop_type,
    )
    run_id = f"replay-{recording.run_id}"
    with span(
        "keelgate.replay",
        attributes={
            attr.REPLAY: True,
            attr.REPLAY_OF: recording.trace_id,
            attr.TENANT_ID: recording.tenant_id,
        },
    ):
        await loop.run(
            goal=recording.goal,
            tenant_id=recording.tenant_id,
            agent_id=recording.agent_id,
            as_of=recording.as_of,
            run_id=run_id,
        )
    replayed = Recording.from_history(store.history(recording.tenant_id, run_id))
    return ReplayReport(
        original=recording,
        replayed=replayed,
        divergences=tuple(diff(recording, replayed)),
        mismatched_calls=tuple(gateway.mismatches),
    )
