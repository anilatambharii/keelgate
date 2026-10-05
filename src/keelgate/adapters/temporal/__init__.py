"""Temporal adapter: run a Keelgate loop as a Temporal workflow (``keelgate[temporal]``).

Temporal supplies what the default LangGraph-style checkpointing cannot: a durable *driver*. If the
worker dies mid-run, Temporal re-runs the activity on another worker. That is safe because the
activity calls ``Loop.run_or_resume``, which loads the loop's own checkpoint and never repeats a
completed WRITE (idempotency keys), and never auto-retries a WRITE whose outcome is unknown.

Division of labour:

* the **workflow** is deterministic orchestration only: one activity, a retry policy, a timeout;
* the **activity** does all the real work (model calls, tool calls) through the governed gateway;
* the loop's checkpoint store must be shared between workers (SQLite on a shared volume, or a
  database), because that is how a replacement worker resumes.

A stop for budget or approval is a normal *result* (``resumable=True``), not an activity failure,
so Temporal does not retry it.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

try:
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy
    from temporalio.worker import Worker
except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
        "keelgate.adapters.temporal needs the Temporal SDK: pip install 'keelgate[temporal]'"
    ) from exc

with workflow.unsafe.imports_passed_through():
    from keelgate.loop.runner import InProcessRunner, LoopOutcome, LoopRunner, LoopSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from temporalio.client import Client

    from keelgate.loop.engine import Loop

ACTIVITY_NAME: Final = "keelgate_run_loop"
WORKFLOW_NAME: Final = "KeelgateLoopWorkflow"
DEFAULT_TASK_QUEUE: Final = "keelgate"
HEARTBEAT_INTERVAL_S: Final = 5.0


class LoopActivities:
    """The activity implementation. ``loop_factory`` builds the loop on the worker."""

    def __init__(self, loop_factory: Callable[[], Loop]) -> None:
        self._runner: LoopRunner = InProcessRunner(loop_factory())

    @activity.defn(name=ACTIVITY_NAME)
    async def run_loop(self, spec: LoopSpec) -> LoopOutcome:
        beat = asyncio.create_task(_heartbeat())
        try:
            return await self._runner.run(spec)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat


async def _heartbeat() -> None:
    while True:
        activity.heartbeat()
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


@dataclass(frozen=True)
class WorkflowOptions:
    """Timeouts and retries for the loop activity."""

    start_to_close: timedelta = timedelta(hours=1)
    heartbeat_timeout: timedelta = timedelta(seconds=30)
    max_attempts: int = 5


@workflow.defn(name=WORKFLOW_NAME)
class KeelgateLoopWorkflow:
    @workflow.run
    async def run(self, spec: LoopSpec) -> LoopOutcome:
        options = WorkflowOptions()
        outcome: LoopOutcome = await workflow.execute_activity(
            ACTIVITY_NAME,
            spec,
            result_type=LoopOutcome,
            start_to_close_timeout=options.start_to_close,
            heartbeat_timeout=options.heartbeat_timeout,
            retry_policy=RetryPolicy(maximum_attempts=options.max_attempts),
        )
        return outcome


def build_worker(
    client: Client,
    loop_factory: Callable[[], Loop],
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    **worker_kwargs: Any,
) -> Worker:
    """A worker serving the loop workflow and its activity."""
    activities = LoopActivities(loop_factory)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[KeelgateLoopWorkflow],
        activities=[activities.run_loop],
        **worker_kwargs,
    )


class TemporalRunner:
    """A :class:`~keelgate.loop.runner.LoopRunner` that starts the loop as a Temporal workflow.

    The workflow id is derived from the tenant and run id, so starting the same run twice attaches
    to the existing workflow instead of creating a second one.
    """

    def __init__(self, client: Client, *, task_queue: str = DEFAULT_TASK_QUEUE) -> None:
        self._client = client
        self._task_queue = task_queue

    @staticmethod
    def workflow_id(spec: LoopSpec) -> str:
        return f"keelgate-{spec.tenant_id}-{spec.run_id}"

    async def run(self, spec: LoopSpec) -> LoopOutcome:
        outcome: LoopOutcome = await self._client.execute_workflow(
            WORKFLOW_NAME,
            spec,
            id=self.workflow_id(spec),
            task_queue=self._task_queue,
            result_type=LoopOutcome,
        )
        return outcome


__all__ = [
    "ACTIVITY_NAME",
    "DEFAULT_TASK_QUEUE",
    "KeelgateLoopWorkflow",
    "LoopActivities",
    "TemporalRunner",
    "WorkflowOptions",
    "build_worker",
]
