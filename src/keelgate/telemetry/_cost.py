"""Per-tenant and per-agent LLM cost and token tracking.

``CostTracker`` keeps exact in-process totals (so a test, a report or a budget check can read
them without a metrics backend) and, when given a meter, also emits them as OpenTelemetry
metrics: the GenAI ``gen_ai.client.token.usage`` histogram and a ``keelgate.llm.cost`` counter in
USD, both attributed by tenant, agent and model.

A call whose model has no price is counted in tokens and in ``unpriced_calls``, but adds nothing
to the dollar total: Keelgate never invents a price (see ``keelgate.llm.PricingTable``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from keelgate.telemetry import attributes as attr

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter

    from keelgate.llm._types import Usage

UNATTRIBUTED: Final = "unattributed"


@dataclass(frozen=True)
class CostTotals:
    """Calls, tokens and dollars for one tenant/agent selection. Unpriced calls add tokens but no
    dollars.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CostTracker:
    """Per-tenant and per-agent token and dollar totals, also emitted as OpenTelemetry metrics.

    Never invents a price.
    """

    def __init__(self, meter: Meter | None = None) -> None:
        self._lock = threading.Lock()
        self._totals: dict[tuple[str, str, str], CostTotals] = {}
        self._tokens: Histogram | None = None
        self._cost: Counter | None = None
        if meter is not None:
            self._tokens = meter.create_histogram(
                attr.METRIC_TOKEN_USAGE,
                unit="{token}",
                description="Input and output tokens used by model calls",
            )
            self._cost = meter.create_counter(
                attr.METRIC_COST, unit="USD", description="Model spend in US dollars"
            )

    def record(
        self,
        usage: Usage,
        *,
        tenant_id: str | None,
        agent_id: str | None,
        model: str,
    ) -> None:
        tenant, agent = tenant_id or UNATTRIBUTED, agent_id or UNATTRIBUTED
        key = (tenant, agent, model)
        priced = usage.cost_usd is not None
        with self._lock:
            old = self._totals.get(key, CostTotals())
            self._totals[key] = CostTotals(
                calls=old.calls + 1,
                input_tokens=old.input_tokens + usage.input_tokens,
                output_tokens=old.output_tokens + usage.output_tokens,
                cost_usd=old.cost_usd + (usage.cost_usd or 0.0),
                unpriced_calls=old.unpriced_calls + (0 if priced else 1),
            )
        labels = {attr.TENANT_ID: tenant, attr.AGENT_ID: agent, attr.GEN_AI_REQUEST_MODEL: model}
        if self._tokens is not None:
            self._tokens.record(usage.input_tokens, {**labels, attr.GEN_AI_TOKEN_TYPE: "input"})
            self._tokens.record(usage.output_tokens, {**labels, attr.GEN_AI_TOKEN_TYPE: "output"})
        if self._cost is not None and usage.cost_usd:
            self._cost.add(usage.cost_usd, labels)

    def total(self, *, tenant_id: str | None = None, agent_id: str | None = None) -> CostTotals:
        """Totals, optionally narrowed to one tenant and/or agent."""
        calls = inp = out = unpriced = 0
        cost = 0.0
        with self._lock:
            for (tenant, agent, _model), t in self._totals.items():
                if tenant_id is not None and tenant != tenant_id:
                    continue
                if agent_id is not None and agent != agent_id:
                    continue
                calls += t.calls
                inp += t.input_tokens
                out += t.output_tokens
                cost += t.cost_usd
                unpriced += t.unpriced_calls
        return CostTotals(calls, inp, out, cost, unpriced)

    def by_tenant(self) -> dict[str, CostTotals]:
        with self._lock:
            tenants = sorted({t for (t, _a, _m) in self._totals})
        return {t: self.total(tenant_id=t) for t in tenants}

    def by_agent(self, tenant_id: str) -> dict[str, CostTotals]:
        with self._lock:
            agents = sorted({a for (t, a, _m) in self._totals if t == tenant_id})
        return {a: self.total(tenant_id=tenant_id, agent_id=a) for a in agents}
