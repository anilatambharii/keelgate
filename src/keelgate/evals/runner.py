"""Run eval suites and assemble the report."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from keelgate import __version__
from keelgate.evals.context import EvalContext
from keelgate.evals.metrics import (
    AccuracyMetric,
    LoadedMetric,
    OutcomeRecord,
    discover_metrics,
    run_metrics,
    sample_records,
)
from keelgate.evals.redteam import run_redteam
from keelgate.evals.stack import EvalStack
from keelgate.evals.trajectory import run_trajectory
from keelgate.evals.types import CaseResult, EvalReport, SuiteResult
from keelgate.evals.unit import run_unit
from keelgate.policy import RegoEngine

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

SUITES: Final = ("unit", "trajectory", "redteam", "outcome")


async def run_outcome(
    records: Sequence[OutcomeRecord] | None = None,
    metrics: Sequence[LoadedMetric] | None = None,
) -> SuiteResult:
    """Run every registered ``OutcomeMetric`` (plus the built-in accuracy) over the records."""
    suite = SuiteResult("outcome", "Outcome metrics discovered through keelgate.outcome_metrics")
    data = list(records) if records is not None else sample_records()
    loaded = list(metrics) if metrics is not None else discover_metrics()
    loaded.insert(0, LoadedMetric("accuracy", AccuracyMetric(), source="keelgate"))
    started = time.perf_counter()
    results, problems = run_metrics(data, loaded)
    suite.metrics = results
    for result in results:
        direction = "higher" if result.higher_is_better else "lower"
        suite.cases.append(
            CaseResult(
                suite="outcome",
                case_id=result.name,
                title=f"{result.name} over {result.n} outcome record(s)",
                passed=True,
                detail=f"{result.value:.4f} ({direction} is better)",
                category=result.source,
                evidence={"value": result.value, "n": result.n, **result.details},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
    for problem in problems:
        suite.cases.append(
            CaseResult(
                suite="outcome",
                case_id=problem.split(":")[0],
                title="outcome metric plugin",
                passed=False,
                detail=problem,
                category="plugin",
            )
        )
    return suite


async def run_suites(
    names: Sequence[str] = SUITES,
    ctx: EvalContext | None = None,
    *,
    records: Sequence[OutcomeRecord] | None = None,
    new_stack: Callable[[], EvalStack] | None = None,
) -> EvalReport:
    """Run the named suites. ``new_stack`` lets a test swap in a deliberately weakened stack."""
    ctx = ctx or EvalContext()
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        raise ValueError(f"unknown suite(s) {unknown}; choose from {list(SUITES)}")
    report = EvalReport(
        version=__version__,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        mode=ctx.mode,
    )
    engine = RegoEngine()
    factory = new_stack or (lambda: EvalStack(engine=engine))
    for name in names:
        try:
            if name == "unit":
                report.suites.append(await run_unit(ctx))
            elif name == "trajectory":
                report.suites.append(await run_trajectory(ctx))
            elif name == "redteam":
                suite = SuiteResult(
                    "redteam", "Assume the model is fooled: does the harness still block the harm?"
                )
                suite.cases = await run_redteam(ctx, new_stack=factory)
                report.suites.append(suite)
            else:
                report.suites.append(await run_outcome(records))
        except Exception as exc:
            report.suites.append(
                SuiteResult(name, "", error=f"{type(exc).__name__}: suite did not run")
            )
    if ctx.is_live:
        report.notes.append("live mode: a real model answered; results can vary between runs.")
    return report
