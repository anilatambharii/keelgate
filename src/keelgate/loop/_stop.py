"""When a loop must stop.

Every limit is optional and independent, and the first one reached wins. A stop is not a
failure: the checkpoint is kept, and a resume with a larger budget carries on from exactly
where it stopped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from keelgate.loop._state import LoopState

GoalPredicate = Callable[["LoopState"], bool]


@dataclass(frozen=True)
class StopConditions:
    """Limits for one run.

    ``max_iterations`` counts plan steps. ``max_tokens`` and ``max_dollars`` count what the
    model has *already* used, so a call that crosses the line is accepted, but nothing
    further is paid for or acted on. ``timeout`` is **active** time: time spent stopped,
    for example waiting for a human, does not count. ``max_verifier_rejections`` counts every
    non-ACCEPT verdict. ``goal`` is checked after each observation.

    A dollar budget needs a priced model; with an unpriced one the loop stops with
    ``COST_UNKNOWN`` instead of spending unmetered.
    """

    max_iterations: int | None = 10
    max_tokens: int | None = None
    max_dollars: float | None = None
    timeout: timedelta | None = None
    max_verifier_rejections: int | None = 3
    goal: GoalPredicate | None = None

    def __post_init__(self) -> None:
        for name in ("max_iterations", "max_tokens", "max_verifier_rejections"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_dollars is not None and not self.max_dollars > 0:
            raise ValueError("max_dollars must be positive")
        if self.timeout is not None and self.timeout <= timedelta(0):
            raise ValueError("timeout must be positive")
