"""Report metered usage to the Stripe meter, exactly once per increment.

For each Stripe-billed org, in the org's own transaction:

1. lock the ``usage_reports`` row for (org, period, metric),
2. compute ``delta = used - reported`` and claim it by setting ``reported = used``,
3. send one meter event worth ``delta`` with ``identifier = "<org>:<period>:<new reported>"``,
4. commit. If Stripe fails the transaction rolls back and the claim disappears.

Concurrent reporters queue on the row lock, so the same delta is never claimed twice, and Stripe
de-duplicates a re-sent event by its identifier. The one gap left is a crash between Stripe
accepting the event and our commit: the next run re-claims that delta under an identifier that
may differ if usage grew meanwhile. The window is one network round trip and the cost is a
slight over-count in the customer's disfavour, so operators should treat a crash during
``report-usage`` as a reason to compare the meter with ``usage_reports``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import stripe

from keelgate_cloud import metering
from keelgate_cloud.billing.sync import METER_CUSTOMER_KEY, METER_EVENT_NAME, METER_VALUE_KEY

if TYPE_CHECKING:
    from keelgate_cloud.db import Database
    from keelgate_cloud.plans import PlanCatalogue

log = logging.getLogger("keelgate_cloud.billing")

_BILLED_STATUSES = ("trialing", "active", "past_due")


@dataclass(frozen=True)
class UsageReport:
    """What happened for one org."""

    org_id: str
    period: str
    sent: int  # the delta sent to Stripe (0 = nothing new)
    reported_total: int
    error: str | None = None


def billed_orgs(db: Database) -> list[str]:
    """Ids of orgs with a Stripe customer and subscription in a billed state.

    Read from the PII-free ``org_index`` (migration 0002), so no policy on ``orgs`` is loosened.
    """
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT org_id FROM org_index WHERE has_subscription AND billing_status = ANY(%s) "
            "ORDER BY org_id",
            (list(_BILLED_STATUSES),),
        ).fetchall()
    return [str(r["org_id"]) for r in rows]


def report_org(
    db: Database, api: Any, catalogue: PlanCatalogue, org_id: str, period: str
) -> UsageReport:
    """Send the unreported gated actions of one org. Raises on a Stripe failure (claim undone)."""
    metric = metering.GATED_ACTIONS
    with db.tx(org_id) as conn:
        org = conn.execute(
            "SELECT plan, stripe_customer_id, stripe_subscription_id FROM orgs"
        ).fetchone()
        if org is None or not org["stripe_customer_id"] or not org["stripe_subscription_id"]:
            return UsageReport(org_id, period, 0, 0)
        plan = catalogue.get(org["plan"])
        if plan.included_gated_actions is None:
            return UsageReport(org_id, period, 0, 0)  # no meter on this plan
        conn.execute(
            "INSERT INTO usage_reports (org_id, period, metric) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (org_id, period, metric),
        )
        row = conn.execute(
            "SELECT reported FROM usage_reports WHERE period = %s AND metric = %s FOR UPDATE",
            (period, metric),
        ).fetchone()
        reported = int(row["reported"]) if row else 0
        used = metering.used(conn, period, metric)
        delta = used - reported
        if delta <= 0:
            return UsageReport(org_id, period, 0, reported)
        conn.execute(
            "UPDATE usage_reports SET reported = %s, updated_at = now() "
            "WHERE period = %s AND metric = %s AND reported = %s",
            (used, period, metric, reported),
        )
        api.v1.billing.meter_events.create(
            {
                "event_name": METER_EVENT_NAME,
                "identifier": f"{org_id}:{period}:{used}",
                "payload": {
                    METER_CUSTOMER_KEY: org["stripe_customer_id"],
                    METER_VALUE_KEY: str(delta),
                },
            }
        )
        return UsageReport(org_id, period, delta, used)


def report_usage(
    db: Database, api: Any, catalogue: PlanCatalogue, period: str | None = None
) -> list[UsageReport]:
    """Report every billed org. One org's Stripe failure does not stop the others."""
    period = period or metering.period_of(metering.today())
    results: list[UsageReport] = []
    for org_id in billed_orgs(db):
        try:
            results.append(report_org(db, api, catalogue, org_id, period))
        except stripe.StripeError as exc:
            log.warning("usage report failed for org %s: %s", org_id, type(exc).__name__)
            results.append(UsageReport(org_id, period, 0, 0, error=type(exc).__name__))
    return results
