"""Where a loop runs: the ``LoopRunner`` interface.

``Loop`` itself is durable (it checkpoints every step and resumes without repeating a WRITE), but
something has to *start it again* after a crash. That is the runner. :class:`InProcessRunner` just
awaits the loop. An orchestrator such as Temporal supplies its own runner (see
``keelgate.adapters.temporal``) that restarts the run on another worker; because
``Loop.run_or_resume`` is idempotent, a restarted activity picks up the checkpoint rather than
starting over.

A :class:`LoopSpec` is plain, serialisable data, so it can cross a process or network boundary.
Stop conditions that are code (a goal predicate) cannot, so they stay with the runner's loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from keelgate.loop._stop import StopConditions

if TYPE_CHECKING:
    from keelgate.loop._engine import Loop, LoopResult


@dataclass(frozen=True)
class LoopSpec:
    """Everything needed to start or resume one run, as plain data."""

    goal: str
    tenant_id: str
    agent_id: str
    as_of: datetime
    run_id: str
    trace_id: str | None = None
    max_iterations: int | None = 10
    max_tokens: int | None = None
    max_dollars: float | None = None
    timeout_seconds: float | None = None
    max_verifier_rejections: int | None = 3

    def stop_conditions(self) -> StopConditions:
        return StopConditions(
            max_iterations=self.max_iterations,
            max_tokens=self.max_tokens,
            max_dollars=self.max_dollars,
            timeout=None
            if self.timeout_seconds is None
            else timedelta(seconds=self.timeout_seconds),
            max_verifier_rejections=self.max_verifier_rejections,
        )


@dataclass(frozen=True)
class LoopOutcome:
    """The serialisable result of a run."""

    run_id: str
    stop_reason: str | None
    final_answer: str | None
    iterations: int
    ok: bool
    resumable: bool

    @classmethod
    def of(cls, run_id: str, result: LoopResult) -> LoopOutcome:
        return cls(
            run_id=run_id,
            stop_reason=result.stop_reason.value if result.stop_reason else None,
            final_answer=result.final_answer,
            iterations=result.state.iteration,
            ok=result.ok,
            resumable=result.resumable,
        )


class LoopRunner(Protocol):
    """Starts a run, or resumes it if a checkpoint exists. Safe to call again after a crash."""

    async def run(self, spec: LoopSpec) -> LoopOutcome: ...


class InProcessRunner:
    """Runs the loop in the calling process."""

    def __init__(self, loop: Loop) -> None:
        self._loop = loop

    async def run(self, spec: LoopSpec) -> LoopOutcome:
        result = await self._loop.run_or_resume(
            goal=spec.goal,
            tenant_id=spec.tenant_id,
            agent_id=spec.agent_id,
            as_of=spec.as_of,
            run_id=spec.run_id,
            trace_id=spec.trace_id,
            stop=spec.stop_conditions(),
        )
        return LoopOutcome.of(spec.run_id, result)
