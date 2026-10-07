"""Token counting for the context budget."""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class ApproxTokenCounter:
    """Roughly four characters a token, rounded up.

    Deterministic and dependency-free. It over-counts slightly for English and
    under-counts for some scripts, so keep a safety margin in the budget, or supply a
    provider-exact counter that implements :class:`TokenCounter`.
    """

    def __init__(self, chars_per_token: float = 4.0) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._ratio = chars_per_token

    def count(self, text: str) -> int:
        return math.ceil(len(text) / self._ratio) if text else 0
