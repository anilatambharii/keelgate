"""The local invoice estimate: plan configuration plus this period's metered usage.

This is the number of record. Stripe's own preview is shown beside it, never instead of it.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from keelgate_cloud import metering

if TYPE_CHECKING:
    from keelgate_cloud.plans import Plan


def estimate_invoice(plan: Plan, period: str, gated_actions_used: int) -> dict[str, Any]:
    """Lines and totals (US cents) for ``plan`` after the period's gated actions."""
    start, end = metering.period_bounds(period)
    lines: list[dict[str, Any]] = []
    notes: list[str] = []

    base = plan.price_monthly_cents or 0
    lines.append(
        {
            "description": f"{plan.name} plan, monthly subscription",
            "quantity": 1,
            "unit_amount_cents": base,
            "amount_cents": base,
        }
    )

    cap = plan.included_gated_actions
    if cap is None:
        lines.append(
            {
                "description": "Gated actions (no monthly cap on this plan)",
                "quantity": gated_actions_used,
                "unit_amount_cents": 0,
                "amount_cents": 0,
            }
        )
    else:
        included_used = min(gated_actions_used, cap)
        lines.append(
            {
                "description": f"Gated actions included in the plan (up to {cap:,})",
                "quantity": included_used,
                "unit_amount_cents": 0,
                "amount_cents": 0,
            }
        )
        beyond = gated_actions_used - cap
        if plan.overage == "block":
            if beyond >= 0:
                notes.append(
                    "The monthly allowance is used up; further gated actions are refused "
                    "(no overage is billed on this plan)."
                )
        elif beyond > 0:
            unit = float(plan.overage)
            lines.append(
                {
                    "description": "Gated actions beyond the allowance",
                    "quantity": beyond,
                    "unit_amount_cents": unit,
                    "amount_cents": math.ceil(beyond * unit - 1e-9),
                }
            )

    subtotal = sum(int(line["amount_cents"]) for line in lines)
    return {
        "currency": "usd",
        "period": {"id": period, "start": start.isoformat(), "end": end.isoformat()},
        "lines": lines,
        "subtotal_cents": subtotal,
        "total_cents": subtotal,
        "source": "local",
        "notes": notes,
    }
