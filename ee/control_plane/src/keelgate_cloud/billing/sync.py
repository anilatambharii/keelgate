"""Create or update Stripe products, prices and the usage meter from ``plans.yaml``.

Safe to re-run: every object is found again by a stable key before anything is created.

* Product   id ``keelgate_<plan>`` (a custom id, so there is exactly one per plan).
* Base price  lookup key ``keelgate_<plan>_monthly``. A changed amount creates a new price,
  moves the lookup key to it and archives the old one (Stripe prices are immutable).
* Overage price  lookup key ``keelgate_<plan>_overage``, only when the plan's ``overage`` is a
  number. ``block`` (every plan today) creates no overage price: the cap is a hard stop.
* One billing meter, event name ``keelgate_gated_actions``, when any plan has an allowance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import stripe

if TYPE_CHECKING:
    from keelgate_cloud.plans import Plan, PlanCatalogue

METER_EVENT_NAME: Final = "keelgate_gated_actions"
METER_CUSTOMER_KEY: Final = "stripe_customer_id"
METER_VALUE_KEY: Final = "value"
CURRENCY: Final = "usd"


def product_id(plan_id: str) -> str:
    """The Stripe product id of a plan."""
    return f"keelgate_{plan_id}"


def lookup_key(plan_id: str, kind: str) -> str:
    """The price lookup key: ``kind`` is ``monthly`` or ``overage``."""
    return f"keelgate_{plan_id}_{kind}"


@dataclass(frozen=True)
class SyncAction:
    """One thing the sync did (or found already done)."""

    verb: str  # created | updated | unchanged | archived
    what: str  # meter | product | price
    name: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.verb:<9} {self.what:<7} {self.name}" + (
            f" ({self.detail})" if self.detail else ""
        )


def _meta(obj: Any) -> dict[str, str]:
    metadata = getattr(obj, "metadata", None)
    to_dict = getattr(metadata, "to_dict", None)
    return dict(to_dict() if callable(to_dict) else metadata or {})


def _is_missing(exc: stripe.InvalidRequestError) -> bool:
    return exc.code == "resource_missing"


def _meter_params() -> dict[str, Any]:
    return {
        "display_name": "Keelgate gated actions",
        "event_name": METER_EVENT_NAME,
        "default_aggregation": {"formula": "sum"},
        "customer_mapping": {"event_payload_key": METER_CUSTOMER_KEY, "type": "by_id"},
        "value_settings": {"event_payload_key": METER_VALUE_KEY},
    }


def sync_meter(api: Any) -> tuple[str, SyncAction]:
    """Find the gated-actions meter or create it. Returns its id."""
    for meter in api.v1.billing.meters.list({"status": "active", "limit": 100}).data:
        if meter.event_name == METER_EVENT_NAME:
            return str(meter.id), SyncAction("unchanged", "meter", METER_EVENT_NAME, meter.id)
    meter = api.v1.billing.meters.create(_meter_params())
    return str(meter.id), SyncAction("created", "meter", METER_EVENT_NAME, meter.id)


def sync_product(api: Any, plan: Plan) -> SyncAction:
    """The plan's product, created or brought in line."""
    pid = product_id(plan.id)
    wanted = {"name": f"Keelgate {plan.name}", "description": plan.description}
    metadata = {"keelgate_plan": plan.id}
    try:
        current = api.v1.products.retrieve(pid)
    except stripe.InvalidRequestError as exc:
        if not _is_missing(exc):
            raise
        api.v1.products.create({"id": pid, **wanted, "metadata": metadata})
        return SyncAction("created", "product", pid)
    current_meta = _meta(current)
    drift = (
        current.name != wanted["name"]
        or (current.description or "") != wanted["description"]
        or current_meta.get("keelgate_plan") != plan.id
        or not current.active
    )
    if not drift:
        return SyncAction("unchanged", "product", pid)
    api.v1.products.update(pid, {**wanted, "active": True, "metadata": metadata})
    return SyncAction("updated", "product", pid)


def _find_price(api: Any, key: str) -> Any | None:
    found = api.v1.prices.list({"lookup_keys": [key], "active": True, "limit": 1}).data
    return found[0] if found else None


def _same_base(price: Any, plan: Plan) -> bool:
    recurring = price.recurring
    return bool(
        price.unit_amount == plan.price_monthly_cents
        and price.currency == CURRENCY
        and price.product == product_id(plan.id)
        and recurring is not None
        and recurring.interval == "month"
        and recurring.interval_count == 1
        and recurring.usage_type == "licensed"
    )


def _replace_price(
    api: Any, params: dict[str, Any], old: Any | None, name: str
) -> list[SyncAction]:
    new = api.v1.prices.create({**params, "lookup_key": name, "transfer_lookup_key": True})
    actions = [SyncAction("created", "price", name, new.id)]
    if old is not None and old.id != new.id:
        api.v1.prices.update(old.id, {"active": False})
        actions.append(SyncAction("archived", "price", name, old.id))
    return actions


def sync_base_price(api: Any, plan: Plan) -> list[SyncAction]:
    """The recurring monthly price of a plan."""
    key = lookup_key(plan.id, "monthly")
    current = _find_price(api, key)
    if current is not None and _same_base(current, plan):
        return [SyncAction("unchanged", "price", key, current.id)]
    params = {
        "product": product_id(plan.id),
        "currency": CURRENCY,
        "unit_amount": plan.price_monthly_cents,
        "recurring": {"interval": "month"},
        "nickname": f"{plan.name} monthly",
        "metadata": {"keelgate_plan": plan.id, "keelgate_kind": "base"},
    }
    return _replace_price(api, params, current, key)


def _overage_tiers(plan: Plan, cents: float) -> list[dict[str, Any]]:
    included = plan.included_gated_actions
    if included is None:
        raise ValueError(f"plan {plan.id}: a numeric overage needs included_gated_actions")
    per_action = format(Decimal(str(cents)).normalize(), "f")
    return [
        {"up_to": included, "unit_amount": 0},
        {"up_to": "inf", "unit_amount_decimal": per_action},
    ]


def _overage_fingerprint(plan: Plan, cents: float) -> str:
    return f"{plan.included_gated_actions}:{Decimal(str(cents)).normalize():f}"


def sync_overage_price(api: Any, plan: Plan, meter_id: str | None) -> list[SyncAction]:
    """The graduated metered price (first N free), or none when the plan blocks at the cap."""
    key = lookup_key(plan.id, "overage")
    current = _find_price(api, key)
    if plan.overage == "block":
        if current is None:
            return []
        api.v1.prices.update(current.id, {"active": False})
        return [SyncAction("archived", "price", key, f"{current.id}: plan no longer bills overage")]
    tiers = _overage_tiers(plan, float(plan.overage))  # also rejects a missing allowance
    if meter_id is None:
        raise ValueError(f"plan {plan.id}: an overage price needs the usage meter")
    fingerprint = _overage_fingerprint(plan, float(plan.overage))
    if current is not None:
        meta = _meta(current)
        meter = getattr(current.recurring, "meter", None)
        if meta.get("keelgate_config") == fingerprint and meter == meter_id:
            return [SyncAction("unchanged", "price", key, current.id)]
    params = {
        "product": product_id(plan.id),
        "currency": CURRENCY,
        "billing_scheme": "tiered",
        "tiers_mode": "graduated",
        "tiers": tiers,
        "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_id},
        "nickname": f"{plan.name} gated actions beyond the allowance",
        "metadata": {
            "keelgate_plan": plan.id,
            "keelgate_kind": "overage",
            "keelgate_config": fingerprint,
        },
    }
    return _replace_price(api, params, current, key)


def sync_stripe(api: Any, catalogue: PlanCatalogue) -> list[SyncAction]:
    """Make Stripe match the catalogue. Returns what was done, in order."""
    plans = [
        p for p in (catalogue.get(i) for i in catalogue.ids()) if p.price_monthly_cents is not None
    ]
    actions: list[SyncAction] = []
    meter_id: str | None = None
    if any(p.included_gated_actions is not None for p in plans):
        meter_id, meter_action = sync_meter(api)
        actions.append(meter_action)
    for plan in plans:
        actions.append(sync_product(api, plan))
        actions.extend(sync_base_price(api, plan))
        actions.extend(sync_overage_price(api, plan, meter_id))
    return actions
