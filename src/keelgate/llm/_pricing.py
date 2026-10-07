"""Model pricing, supplied by the deployment.

Keelgate ships **no default prices**. Prices change, and a stale built-in table would
quietly make every dollar budget wrong. Pass the table you trust; a model with no entry
has an unknown cost, and a loop with a dollar budget stops rather than spend unmetered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from keelgate.llm._types import Usage

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float

    def __post_init__(self) -> None:
        if self.input_per_mtok < 0 or self.output_per_mtok < 0:
            raise ValueError("prices must not be negative")


class PricingTable:
    """Looks a model up by exact name, then by the longest matching prefix.

    The prefix rule lets ``claude-sonnet-5`` cover dated variants of that model.
    """

    def __init__(self, prices: Mapping[str, ModelPrice] | None = None) -> None:
        self._prices = dict(prices or {})

    def price_for(self, model: str) -> ModelPrice | None:
        if model in self._prices:
            return self._prices[model]
        matches = [name for name in self._prices if model.startswith(name)]
        return self._prices[max(matches, key=len)] if matches else None

    def usage(self, model: str, input_tokens: int, output_tokens: int) -> Usage:
        """A :class:`Usage` with ``cost_usd`` filled in when the model is priced."""
        price = self.price_for(model)
        cost = (
            (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1e6
            if price is not None
            else None
        )
        return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)
