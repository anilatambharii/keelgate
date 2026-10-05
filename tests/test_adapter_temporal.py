"""Temporal adapter, run against a real local Temporal test server.

``WorkflowEnvironment.start_local`` downloads/starts the Temporal dev server. If that is not
possible (offline machine, no binary), the tests skip locally and FAIL in CI, where
KEELGATE_REQUIRE_INTEGRATION is set, so they can never silently pass.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("temporalio")

from temporalio.testing import WorkflowEnvironment

from keelgate.adapters.temporal import TemporalRunner, build_worker
from keelgate.loop import InProcessRunner, LoopSpec
from tests.conftest import MARKET_OPEN, skip_or_fail
from tests.loop_support import AGENT, TENANT, build_rig, fake, three_step_script

FINAL = "Bought 5000 of AAPL at the quoted price."


class WorkerFaultError(Exception):
    """An ordinary failure (not a BaseException): Temporal sees a failed activity attempt."""


def spec(run_id: str, **kw: Any) -> LoopSpec:
    return LoopSpec(
        goal="Research AAPL and buy a small position.",
        tenant_id=TENANT,
        agent_id=AGENT,
        as_of=MARKET_OPEN,
        run_id=run_id,
        **kw,
    )


async def start_env() -> WorkflowEnvironment:
    try:
        return await WorkflowEnvironment.start_local()
    except Exception as exc:  # no dev-server binary / no network
        skip_or_fail(f"Temporal dev server unavailable: {type(exc).__name__}: {exc}")
        raise


def test_in_process_runner_runs_the_loop_to_the_goal(tmp_path: Path, rego_engine: Any) -> None:
    rig = build_rig(tmp_path / "rig", engine=rego_engine)
    try:
        runner = InProcessRunner(rig.loop(fake(three_step_script())))
        outcome = asyncio.run(runner.run(spec("r-inproc")))
        assert outcome.ok and outcome.final_answer == FINAL and not outcome.resumable
        # the same run again is an idempotent resume: nothing is executed twice
        again = asyncio.run(runner.run(spec("r-inproc")))
        assert again.ok and len(rig.executions) == 1
    finally:
        rig.close()


def test_a_loop_runs_to_completion_as_a_temporal_workflow(tmp_path: Path, rego_engine: Any) -> None:
    rig = build_rig(tmp_path / "rig", engine=rego_engine)

    async def scenario() -> Any:
        env = await start_env()
        try:
            queue = f"q-{uuid.uuid4().hex[:8]}"
            worker = build_worker(
                env.client, lambda: rig.loop(fake(three_step_script())), task_queue=queue
            )
            async with worker:
                return await TemporalRunner(env.client, task_queue=queue).run(spec("r-wf"))
        finally:
            await env.shutdown()

    try:
        outcome = asyncio.run(scenario())
        assert outcome.ok and outcome.final_answer == FINAL
        assert outcome.iterations >= 2
        assert len(rig.executions) == 1  # exactly one real WRITE
    finally:
        rig.close()


def test_a_failed_attempt_is_retried_by_temporal_without_a_duplicate_write(
    tmp_path: Path, rego_engine: Any
) -> None:
    """The worker 'dies' right after the WRITE ran. Temporal retries; the resume must not
    place the order a second time."""
    rig = build_rig(tmp_path / "rig", engine=rego_engine)
    faults = {"remaining": 1}

    def failpoint(name: str) -> None:
        if name == "after_gateway_call" and faults["remaining"] and rig.executions:
            faults["remaining"] -= 1
            raise WorkerFaultError("worker lost after the WRITE")

    async def scenario() -> Any:
        env = await start_env()
        try:
            queue = f"q-{uuid.uuid4().hex[:8]}"
            worker = build_worker(
                env.client,
                lambda: rig.loop(fake(three_step_script()), failpoint=failpoint),
                task_queue=queue,
            )
            async with worker:
                return await asyncio.wait_for(
                    TemporalRunner(env.client, task_queue=queue).run(spec("r-retry")),
                    timeout=timedelta(seconds=90).total_seconds(),
                )
        finally:
            await env.shutdown()

    try:
        outcome = asyncio.run(scenario())
        assert faults["remaining"] == 0  # the fault really fired
        assert outcome.ok and outcome.final_answer == FINAL
        assert len(rig.executions) == 1  # the WRITE ran once despite the retried attempt
    finally:
        rig.close()


def test_a_budget_stop_is_a_resumable_result_not_a_retried_failure(
    tmp_path: Path, rego_engine: Any
) -> None:
    rig = build_rig(tmp_path / "rig", engine=rego_engine)

    async def scenario() -> Any:
        env = await start_env()
        try:
            queue = f"q-{uuid.uuid4().hex[:8]}"
            worker = build_worker(
                env.client, lambda: rig.loop(fake(three_step_script())), task_queue=queue
            )
            async with worker:
                return await TemporalRunner(env.client, task_queue=queue).run(
                    spec("r-budget", max_iterations=1)
                )
        finally:
            await env.shutdown()

    try:
        outcome = asyncio.run(scenario())
        assert outcome.stop_reason == "max_iterations" and outcome.resumable and not outcome.ok
    finally:
        rig.close()


def test_the_workflow_id_is_derived_from_tenant_and_run() -> None:
    assert TemporalRunner.workflow_id(spec("abc")) == f"keelgate-{TENANT}-abc"
