"""Stripe webhooks: verify, de-duplicate, then apply to the org.

Order of work for a delivery:

1. verify the signature over the raw body (``stripe.Webhook.construct_event``: HMAC-SHA256 and a
   timestamp tolerance, so unsigned, tampered or replayed-late payloads never get further);
2. find the org (read-only, through the ``stripe_customer`` lookup or the checkout metadata);
3. in ONE transaction scoped to that org: insert the event id (``ON CONFLICT DO NOTHING``; a
   duplicate stops here) and apply the change. A failure rolls back both, so Stripe's retry is
   processed afresh rather than skipped as a duplicate.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import psycopg
import stripe

from keelgate_cloud.errors import ApiError

if TYPE_CHECKING:
    from keelgate_cloud.db import Database
    from keelgate_cloud.plans import PlanCatalogue

log = logging.getLogger("keelgate_cloud.billing")

# Stripe subscription status -> orgs.billing_status. ``None`` leaves the org's status alone.
# unpaid / paused / incomplete_expired mean Stripe has given up or stopped: the org is cut off.
# ``incomplete`` is a first payment still in flight: nothing is granted yet.
STATUS_MAP: Final[dict[str, str | None]] = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "unpaid": "canceled",
    "paused": "canceled",
    "incomplete_expired": "canceled",
    "incomplete": None,
}
_ORG_COLUMNS: Final = (
    "SELECT id, stripe_customer_id, stripe_subscription_id, billing_status FROM orgs"
)
_LOOKUP_KEY: Final = re.compile(r"^keelgate_([a-z0-9_]+?)_monthly$")


class InvalidWebhookError(ApiError):
    """The delivery is not a validly signed Stripe event."""

    status = 400
    code = "invalid_webhook"


class WebhookNotConfiguredError(ApiError):
    """No webhook secret is set, so no delivery can be trusted."""

    status = 503
    code = "webhook_not_configured"


@dataclass
class Outcome:
    """What a delivery did: ``processed``, ``duplicate``, ``unknown_customer`` or ``recorded``."""

    status: str
    event_id: str
    event_type: str
    org_id: str | None = None
    changes: dict[str, Any] = field(default_factory=dict)


def verify(payload: bytes, signature: str | None, secret: str | None) -> dict[str, Any]:
    """The event as a plain dict, or raise. Never returns anything for an unverified payload."""
    if not secret:
        raise WebhookNotConfiguredError("the Stripe webhook secret is not configured")
    if not signature:
        raise InvalidWebhookError("missing Stripe-Signature header")
    try:
        stripe.Webhook.construct_event(payload, signature, secret)  # tolerance: 300 s
        event = json.loads(payload)
    except stripe.SignatureVerificationError as exc:
        raise InvalidWebhookError("invalid signature") from exc
    except ValueError as exc:  # not JSON, or not an event
        raise InvalidWebhookError("malformed payload") from exc
    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
        raise InvalidWebhookError("malformed payload")
    return event


def _plain(value: Any) -> dict[str, Any]:
    """A Stripe SDK object (or None) as a plain dict."""
    to_dict = getattr(value, "to_dict", None)
    return dict(to_dict() if callable(to_dict) else value or {})


def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        return None


def _id_of(value: Any) -> str | None:
    """An expandable Stripe field is either an id or an object with an id."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return str(value["id"])
    return None


def _invoice_subscription(invoice: dict[str, Any]) -> str | None:
    sub = _id_of(invoice.get("subscription"))
    if sub:
        return sub
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    return _id_of(details.get("subscription"))


def plan_of_subscription(
    sub: dict[str, Any], catalogue: PlanCatalogue, api: Any | None
) -> str | None:
    """The Keelgate plan id a subscription is for, or ``None`` if it cannot be told.

    Looked up on the base price (metadata, then lookup key), then the product's metadata (fetched
    from Stripe when only its id is present), then the subscription's own metadata.
    """
    candidates: list[str | None] = []
    for item in (sub.get("items") or {}).get("data") or []:
        price = item.get("price") or {}
        if (price.get("metadata") or {}).get("keelgate_kind") == "overage":
            continue
        candidates.append((price.get("metadata") or {}).get("keelgate_plan"))
        match = _LOOKUP_KEY.match(str(price.get("lookup_key") or ""))
        candidates.append(match.group(1) if match else None)
        product = price.get("product")
        if isinstance(product, dict):
            candidates.append((product.get("metadata") or {}).get("keelgate_plan"))
        elif (
            isinstance(product, str) and api is not None and not _first_valid(candidates, catalogue)
        ):
            try:
                fetched = api.v1.products.retrieve(product)
                candidates.append(_plain(fetched.metadata).get("keelgate_plan"))
            except stripe.StripeError as exc:
                log.warning("could not read product %s: %s", product, type(exc).__name__)
    candidates.append((sub.get("metadata") or {}).get("keelgate_plan"))
    return _first_valid(candidates, catalogue)


def _first_valid(candidates: list[str | None], catalogue: PlanCatalogue) -> str | None:
    for candidate in candidates:
        if candidate in catalogue.ids() and catalogue.get(candidate).hosted:
            return candidate
    return None


def _find_org(db: Database, event: dict[str, Any]) -> dict[str, Any] | None:
    """The org this event is about, or ``None``."""
    obj = (event.get("data") or {}).get("object") or {}
    etype = event["type"]
    if etype == "checkout.session.completed":
        org_id = _uuid_or_none(obj.get("client_reference_id")) or _uuid_or_none(
            (obj.get("metadata") or {}).get("org_id")
        )
        if org_id:
            with db.tx(org_id) as conn:
                row = conn.execute(_ORG_COLUMNS).fetchone()
            if row:
                return dict(row)
        return None
    customer = _id_of(obj.get("customer"))
    if not customer:
        return None
    with db.tx(stripe_customer=customer) as conn:
        row = conn.execute(
            "SELECT id, stripe_customer_id, stripe_subscription_id, billing_status FROM orgs"
        ).fetchone()
    return dict(row) if row else None


def _record_event(conn: psycopg.Connection[Any], event: dict[str, Any]) -> bool:
    """Insert the event id; ``False`` if it was already there."""
    cur = conn.execute(
        "INSERT INTO stripe_events (event_id, type) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (event["id"], str(event.get("type", ""))[:100]),
    )
    return cur.rowcount == 1


def _timestamp(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if isinstance(value, int | float) else None


def _apply_checkout(
    conn: psycopg.Connection[Any], org: dict[str, Any], obj: dict[str, Any]
) -> dict[str, Any]:
    customer = _id_of(obj.get("customer"))
    subscription = _id_of(obj.get("subscription"))
    if org["stripe_customer_id"] and customer and org["stripe_customer_id"] != customer:
        log.warning("checkout for org %s names a different customer; ignored", org["id"])
        return {}
    changes: dict[str, Any] = {}
    if customer and not org["stripe_customer_id"]:
        changes["stripe_customer_id"] = customer
    if subscription:
        changes["stripe_subscription_id"] = subscription
    return _update_org(conn, changes)


def _apply_subscription(
    conn: psycopg.Connection[Any],
    org: dict[str, Any],
    event_type: str,
    sub: dict[str, Any],
    plan_id: str | None,
) -> dict[str, Any]:
    sub_id = _id_of(sub)
    status = (
        "canceled" if event_type.endswith(".deleted") else STATUS_MAP.get(str(sub.get("status")))
    )
    current = org["stripe_subscription_id"]
    if status == "canceled" and current and current != sub_id:
        log.info("ignoring cancellation of old subscription %s for org %s", sub_id, org["id"])
        return {}
    changes: dict[str, Any] = {}
    if status == "canceled":
        changes["stripe_subscription_id"] = None
    elif status is not None:
        changes["stripe_subscription_id"] = sub_id
    if status is not None:
        changes["billing_status"] = status
    if plan_id and status != "canceled":
        changes["plan"] = plan_id
    trial_end = _timestamp(sub.get("trial_end"))
    if trial_end is not None:
        changes["trial_ends_at"] = trial_end
    return _update_org(conn, changes)


def _apply_invoice(
    conn: psycopg.Connection[Any], org: dict[str, Any], event_type: str, invoice: dict[str, Any]
) -> dict[str, Any]:
    sub_id = _invoice_subscription(invoice)
    current = org["stripe_subscription_id"]
    if sub_id and current and sub_id != current:
        return {}  # an invoice of some other subscription
    if event_type == "invoice.payment_failed":
        if org["billing_status"] in ("canceled", "trial_expired"):
            return {}
        return _update_org(conn, {"billing_status": "past_due"})
    # invoice.paid: a $0 invoice at the start of a trial must not end the trial early.
    paying = int(invoice.get("amount_paid") or 0) > 0
    if org["billing_status"] == "past_due" or (paying and org["billing_status"] != "canceled"):
        return _update_org(conn, {"billing_status": "active"})
    return {}


_COLUMNS: Final = frozenset(
    {
        "plan",
        "billing_status",
        "stripe_customer_id",
        "stripe_subscription_id",
        "trial_ends_at",
    }
)


def _update_org(conn: psycopg.Connection[Any], changes: dict[str, Any]) -> dict[str, Any]:
    if not changes:
        return {}
    unknown = set(changes) - _COLUMNS
    if unknown:
        raise ValueError(f"not a billing column: {sorted(unknown)}")
    assignments = ", ".join(f"{name} = %s" for name in changes)
    conn.execute(f"UPDATE orgs SET {assignments}", tuple(changes.values()))
    return changes


_HANDLED: Final = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
        "invoice.paid",
    }
)


def process_event(
    db: Database, event: dict[str, Any], catalogue: PlanCatalogue, api: Any | None
) -> Outcome:
    """Handle one verified event. Idempotent by event id."""
    etype = str(event.get("type", ""))
    outcome = Outcome("recorded", event["id"], etype)
    if etype not in _HANDLED:
        with db.tx() as conn:
            if not _record_event(conn, event):
                outcome.status = "duplicate"
        return outcome

    obj = (event.get("data") or {}).get("object") or {}
    org = _find_org(db, event)
    if org is None:
        log.warning("stripe event %s (%s) is for an unknown customer", event["id"], etype)
        with db.tx() as conn:
            outcome.status = "unknown_customer" if _record_event(conn, event) else "duplicate"
        return outcome

    plan_id = None
    if etype.startswith("customer.subscription."):
        plan_id = plan_of_subscription(obj, catalogue, api)  # may call Stripe: outside the tx
        if plan_id is None and etype != "customer.subscription.deleted":
            log.warning("subscription %s has no recognisable Keelgate plan", obj.get("id"))

    org_id = str(org["id"])
    outcome.org_id = org_id
    try:
        with db.tx(org_id) as conn:
            if not _record_event(conn, event):
                outcome.status = "duplicate"
                return outcome
            locked = conn.execute(  # re-read under a row lock: decide on current state
                "SELECT id, stripe_customer_id, stripe_subscription_id, billing_status "
                "FROM orgs FOR UPDATE"
            ).fetchone()
            org = dict(locked) if locked else org
            if etype == "checkout.session.completed":
                outcome.changes = _apply_checkout(conn, org, obj)
            elif etype.startswith("customer.subscription."):
                outcome.changes = _apply_subscription(conn, org, etype, obj, plan_id)
            else:
                outcome.changes = _apply_invoice(conn, org, etype, obj)
    except psycopg.errors.UniqueViolation:
        # e.g. the customer id already belongs to another org: nothing to retry.
        log.error(
            "stripe event %s conflicts with another org's customer; recorded only", event["id"]
        )
        with db.tx() as conn:
            _record_event(conn, event)
        outcome.status = "recorded"
        return outcome
    outcome.status = "processed"
    log.info(
        "stripe event %s (%s) applied to org %s: %s",
        event["id"],
        etype,
        org_id,
        sorted(outcome.changes),
    )
    return outcome
