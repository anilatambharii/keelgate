"""Example outcome metrics. Not part of Keelgate; discovered through an entry point."""

from __future__ import annotations

from typing import TYPE_CHECKING

from keelgate.evals import MetricResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from keelgate.evals import OutcomeRecord


def _pairs(records: Sequence[OutcomeRecord]) -> list[tuple[float, float]]:
    return [
        (float(r.predicted["p_up"]), 1.0 if r.realized["up"] else 0.0)
        for r in records
        if "p_up" in r.predicted and "up" in r.realized
    ]


class BrierScore:
    """Mean squared error of a probability forecast. 0 is perfect; lower is better."""

    name = "brier_score"
    higher_is_better = False

    def compute(self, records: Sequence[OutcomeRecord]) -> MetricResult:
        pairs = _pairs(records)
        value = sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else 0.0
        return MetricResult(self.name, value, len(pairs), self.higher_is_better)


class ExpectedCalibrationError:
    """Gap between stated confidence and observed frequency, over ten probability bins."""

    name = "expected_calibration_error"
    higher_is_better = False
    bins = 10

    def compute(self, records: Sequence[OutcomeRecord]) -> MetricResult:
        pairs = _pairs(records)
        if not pairs:
            return MetricResult(self.name, 0.0, 0, self.higher_is_better)
        total = 0.0
        for index in range(self.bins):
            low, high = index / self.bins, (index + 1) / self.bins
            members = [
                (p, y) for p, y in pairs if low <= p < high or (index == self.bins - 1 and p == 1.0)
            ]
            if members:
                confidence = sum(p for p, _ in members) / len(members)
                frequency = sum(y for _, y in members) / len(members)
                total += len(members) / len(pairs) * abs(confidence - frequency)
        return MetricResult(self.name, total, len(pairs), self.higher_is_better)
