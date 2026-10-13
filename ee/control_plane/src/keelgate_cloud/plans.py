"""Plans, loaded from ``plans.yaml``. Nothing about a plan is hard-coded elsewhere."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PLANS_FILE: Final = Path(__file__).with_name("plans.yaml")

PlanId = Literal["community", "team", "business", "enterprise"]
Overage = Literal["block"] | float


class Plan(BaseModel):
    """One plan's price and limits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str
    hosted: bool
    price_monthly_cents: int | None = Field(ge=0)
    included_gated_actions: int | None = Field(ge=0)
    overage: Overage
    trace_retention_days: int | None = Field(ge=0)
    audit_min_retention_days: int | None = Field(ge=0)
    audit_immutable: bool
    max_agents: int | None = Field(ge=0)
    rate_limit_per_minute: int = Field(ge=0)
    features: tuple[str, ...]
    trial_days: int = Field(ge=0)

    def has(self, feature: str) -> bool:
        """Whether the plan includes a feature switch such as ``sso``."""
        return feature in self.features

    @property
    def self_serve(self) -> bool:
        """Sold through Stripe checkout: it has a price and is hosted."""
        return self.hosted and self.price_monthly_cents is not None


class PlanCatalogue:
    """All plans, by id."""

    def __init__(self, plans: dict[str, Plan]) -> None:
        if "community" not in plans:
            raise ValueError("the catalogue needs a community plan")
        self._plans = plans

    def get(self, plan_id: str) -> Plan:
        """The plan, or raise ``KeyError``: an unknown plan must never default to a paid one."""
        return self._plans[plan_id]

    def __iter__(self) -> object:
        return iter(self._plans.values())

    def ids(self) -> list[str]:
        """Plan ids in file order."""
        return list(self._plans)


def load_catalogue(path: Path = PLANS_FILE) -> PlanCatalogue:
    """Parse and validate a plans file."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    plans = {pid: Plan(id=pid, **body) for pid, body in document["plans"].items()}
    return PlanCatalogue(plans)


@lru_cache(maxsize=1)
def default_catalogue() -> PlanCatalogue:
    """The catalogue shipped with the package."""
    return load_catalogue()
