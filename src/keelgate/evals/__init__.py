"""Agent evals, red-team suites and outcome-metric plugins.

* ``unit``: tool choice and argument validity.
* ``trajectory``: does the verifier catch seeded faults.
* ``redteam``: assume the model is fooled; does the harness still block the harm.
* ``outcome``: metrics that plugins register through the ``keelgate.outcome_metrics`` entry point.

Run them with ``keelgate eval run`` (or ``make eval``).
"""

from keelgate.evals._context import EvalContext
from keelgate.evals._metrics import (
    ENTRY_POINT_GROUP,
    AccuracyMetric,
    LoadedMetric,
    OutcomeMetric,
    OutcomeRecord,
    discover_metrics,
    load_records,
    run_metrics,
    sample_records,
)
from keelgate.evals._report import (
    baseline_of,
    compare,
    to_dict,
    to_html,
    to_markdown,
    write_html,
    write_json,
    write_markdown,
)
from keelgate.evals._runner import SUITES, run_outcome, run_suite, run_suites
from keelgate.evals._stack import EvalStack
from keelgate.evals._types import CaseResult, EvalReport, MetricResult, SuiteResult

__all__ = [
    "ENTRY_POINT_GROUP",
    "SUITES",
    "AccuracyMetric",
    "CaseResult",
    "EvalContext",
    "EvalReport",
    "EvalStack",
    "LoadedMetric",
    "MetricResult",
    "OutcomeMetric",
    "OutcomeRecord",
    "SuiteResult",
    "baseline_of",
    "compare",
    "discover_metrics",
    "load_records",
    "run_metrics",
    "run_outcome",
    "run_suite",
    "run_suites",
    "sample_records",
    "to_dict",
    "to_html",
    "to_markdown",
    "write_html",
    "write_json",
    "write_markdown",
]
