"""Outcome metrics: how a downstream project plugs its own measures into Keelgate evals.

An :class:`OutcomeMetric` turns a batch of :class:`OutcomeRecord` (what was predicted or decided,
and what actually happened) into a number. Tycheon registers financial metrics (Brier score,
Sharpe, calibration error) this way, by declaring an entry point in its own ``pyproject.toml``:

    [project.entry-points."keelgate.outcome_metrics"]
    brier_score = "tycheon.metrics:BrierScore"

The entry point may name a class (instantiated with no arguments), a zero-argument factory, or a
ready instance. The group name ``keelgate.outcome_metrics`` is part of the integration contract.

Plugins are ordinary installed Python code and run in-process with the same authority as the rest
of the program: install only packages you trust. A plugin that fails to load or to compute is
reported (with its error type) and does not stop the other metrics.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from keelgate.evals._types import MetricResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ENTRY_POINT_GROUP: Final = "keelgate.outcome_metrics"


@dataclass(frozen=True)
class OutcomeRecord:
    """One decision and what came of it. ``predicted`` and ``realized`` are plugin-defined."""

    record_id: str
    predicted: Mapping[str, Any]
    realized: Mapping[str, Any]
    tenant_id: str = ""
    run_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class OutcomeMetric(Protocol):
    """A named measure over a batch of outcomes."""

    name: str
    higher_is_better: bool

    def compute(self, records: Sequence[OutcomeRecord]) -> MetricResult: ...


class AccuracyMetric:
    """Fraction of records whose ``predicted["label"]`` equals ``realized["label"]``."""

    name = "accuracy"
    higher_is_better = True

    def compute(self, records: Sequence[OutcomeRecord]) -> MetricResult:
        scored = [r for r in records if "label" in r.predicted and "label" in r.realized]
        hits = sum(r.predicted["label"] == r.realized["label"] for r in scored)
        value = hits / len(scored) if scored else 0.0
        return MetricResult(self.name, value, len(scored), True, {"hits": hits})


@dataclass(frozen=True)
class LoadedMetric:
    entry_point: str
    metric: OutcomeMetric | None
    error: str = ""
    source: str = ""


def _instantiate(obj: Any) -> Any:
    if inspect.isclass(obj):
        return obj()
    if callable(obj) and not isinstance(obj, OutcomeMetric):
        return obj()
    return obj


def discover_metrics(group: str = ENTRY_POINT_GROUP) -> list[LoadedMetric]:
    """Every metric registered under the entry-point group, loaded defensively."""
    loaded: list[LoadedMetric] = []
    for ep in sorted(entry_points(group=group), key=lambda e: e.name):
        source = f"{ep.dist.name}" if ep.dist else "unknown"
        try:
            candidate = _instantiate(ep.load())
        except Exception as exc:
            loaded.append(
                LoadedMetric(ep.name, None, f"{type(exc).__name__} while loading", source)
            )
            continue
        if not isinstance(candidate, OutcomeMetric):
            loaded.append(
                LoadedMetric(ep.name, None, "does not implement the OutcomeMetric protocol", source)
            )
            continue
        loaded.append(LoadedMetric(ep.name, candidate, "", source))
    return loaded


def run_metrics(
    records: Sequence[OutcomeRecord], metrics: Sequence[LoadedMetric]
) -> tuple[list[MetricResult], list[str]]:
    """Compute every loaded metric; return the results and one line per problem."""
    results: list[MetricResult] = []
    problems: list[str] = []
    for item in metrics:
        if item.metric is None:
            problems.append(f"metric {item.entry_point!r} ({item.source}): {item.error}")
            continue
        try:
            result = item.metric.compute(records)
        except Exception as exc:
            problems.append(f"metric {item.entry_point!r} ({item.source}): {type(exc).__name__}")
            continue
        results.append(
            MetricResult(
                name=result.name or item.entry_point,
                value=float(result.value),
                n=result.n,
                higher_is_better=item.metric.higher_is_better,
                details=dict(result.details),
                source=item.source,
            )
        )
    return results, problems


def sample_records() -> list[OutcomeRecord]:
    """A small fixed dataset so metrics can be exercised without a project's own data.

    Twenty binary forecasts: a probability for "up" and what happened. Deterministic.
    """
    probabilities = [0.9, 0.8, 0.7, 0.75, 0.6, 0.55, 0.4, 0.3, 0.2, 0.1] * 2
    realized_up = [1, 1, 1, 0, 1, 0, 0, 0, 0, 0, 1, 1, 0, 1, 1, 0, 0, 1, 0, 0]
    return [
        OutcomeRecord(
            record_id=f"sample-{n}",
            predicted={"p_up": p, "label": "up" if p >= 0.5 else "down"},  # noqa: PLR2004
            realized={"up": bool(up), "label": "up" if up else "down"},
        )
        for n, (p, up) in enumerate(zip(probabilities, realized_up, strict=True))
    ]


def load_records(path: str) -> list[OutcomeRecord]:
    """Read ``OutcomeRecord`` rows from a JSON-lines file (one JSON object per line)."""
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    out: list[OutcomeRecord] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            out.append(
                OutcomeRecord(
                    record_id=str(row.get("record_id", f"row-{number}")),
                    predicted=dict(row["predicted"]),
                    realized=dict(row["realized"]),
                    tenant_id=str(row.get("tenant_id", "")),
                    run_id=str(row.get("run_id", "")),
                    metadata=dict(row.get("metadata", {})),
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: not a valid outcome record ({exc})") from exc
    return out
