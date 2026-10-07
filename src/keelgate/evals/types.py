"""Result types shared by every eval suite and the report."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CaseResult:
    """One eval case. ``passed`` always means the desired property held.

    For red-team cases that is "the attack was blocked"; for a unit eval, "the model chose the
    right tool with valid arguments"; for a trajectory eval, "the verifier behaved correctly".
    """

    suite: str
    case_id: str
    title: str
    passed: bool
    detail: str = ""
    category: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    skipped: bool = False


@dataclass(frozen=True)
class MetricResult:
    name: str
    value: float
    n: int
    higher_is_better: bool = True
    details: dict[str, Any] = field(default_factory=dict)
    source: str = "builtin"


@dataclass
class SuiteResult:
    name: str
    description: str
    cases: list[CaseResult] = field(default_factory=list)
    metrics: list[MetricResult] = field(default_factory=list)
    error: str = ""

    @property
    def ran(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.skipped]

    @property
    def passed(self) -> bool:
        return not self.error and all(c.passed for c in self.ran)

    @property
    def pass_rate(self) -> float:
        ran = self.ran
        return sum(c.passed for c in ran) / len(ran) if ran else 1.0


@dataclass
class EvalReport:
    version: str
    generated_at: str
    mode: str
    suites: list[SuiteResult] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def metrics(self) -> list[MetricResult]:
        return [m for s in self.suites for m in s.metrics]

    @property
    def failures(self) -> list[CaseResult]:
        return [c for s in self.suites for c in s.ran if not c.passed]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.suites) and not self.regressions

    def suite(self, name: str) -> SuiteResult | None:
        return next((s for s in self.suites if s.name == name), None)
