"""Billing: plan and subscription state, Stripe Checkout and portal, invoice preview, webhook."""

from __future__ import annotations

import logging
from typing import Any

import stripe
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from keelgate_cloud import metering
from keelgate_cloud.api.deps import State, User, require_roles
from keelgate_cloud.billing import webhook
from keelgate_cloud.billing.client import client_for
from keelgate_cloud.billing.estimate import estimate_invoice
from keelgate_cloud.billing.sync import lookup_key
from keelgate_cloud.errors import ApiError, ConflictError, ValidationFailedError

log = logging.getLogger("keelgate_cloud.billing")

router = APIRouter(tags=["billing"])


class BillingNotConfiguredError(ApiError):
    """Stripe is not set up on this server."""

    status = 503
    code = "billing_not_configured"


class BillingProviderError(ApiError):
    """Stripe failed or refused the request."""

    status = 502
    code = "billing_provider_error"


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str


def _api(state: State) -> Any:
    api = client_for(state)
    if api is None:
        raise BillingNotConfiguredError("Stripe is not configured on this server")
    return api


def _provider_failure(exc: stripe.StripeError) -> BillingProviderError:
    # Log the class and Stripe's request id, never the message (it may echo parameters).
    log.warning("stripe call failed: %s (%s)", type(exc).__name__, exc.request_id)
    return BillingProviderError("the billing provider could not complete the request")


def _org_row(state: State, org_id: str) -> dict[str, Any]:
    with state.db.tx(org_id) as conn:
        row = conn.execute(
            "SELECT name, plan, billing_status, trial_ends_at, stripe_customer_id, "
            "stripe_subscription_id FROM orgs"
        ).fetchone()
    assert row is not None  # noqa: S101  (the caller authenticated against this org)
    return dict(row)


def _plan_view(plan: Any) -> dict[str, Any]:
    return {
        "id": plan.id,
        "name": plan.name,
        "description": plan.description,
        "price_monthly_cents": plan.price_monthly_cents,
        "included_gated_actions": plan.included_gated_actions,
        "features": list(plan.features),
        "self_serve": plan.self_serve,
        "trial_days": plan.trial_days,
    }


def _subscription_view(state: State, org: dict[str, Any]) -> dict[str, Any] | None:
    """The subscription as Stripe knows it, or just its id when Stripe cannot be reached."""
    sub_id = org["stripe_subscription_id"]
    if not sub_id:
        return None
    view: dict[str, Any] = {
        "id": sub_id,
        "status": org["billing_status"],
        "current_period_end": None,
    }
    api = client_for(state)
    if api is not None:
        try:
            sub = api.v1.subscriptions.retrieve(sub_id)
        except stripe.StripeError as exc:
            log.warning("could not read subscription: %s", type(exc).__name__)
        else:
            view["status"] = sub.status
            end = getattr(sub, "current_period_end", None)
            if end is None:  # newer API versions carry the period on the items
                items = getattr(getattr(sub, "items", None), "data", None) or []
                end = getattr(items[0], "current_period_end", None) if items else None
            view["current_period_end"] = end
    return view


@router.get("/api/billing", dependencies=[require_roles("owner", "admin")])
def get_billing(principal: User, state: State) -> dict[str, Any]:
    """The org's plan, billing status, subscription and the plans on offer."""
    org = _org_row(state, principal.org_id)
    return {
        "plan": org["plan"],
        "billing_status": org["billing_status"],
        "trial_ends_at": org["trial_ends_at"].isoformat() if org["trial_ends_at"] else None,
        "subscription": _subscription_view(state, org),
        "plans": [_plan_view(state.catalogue.get(i)) for i in state.catalogue.ids()],
        "portal_available": bool(org["stripe_customer_id"]) and client_for(state) is not None,
    }


def _ensure_customer(state: State, api: Any, org_id: str, email: str | None) -> str:
    org = _org_row(state, org_id)
    if org["stripe_customer_id"]:
        return str(org["stripe_customer_id"])
    params: dict[str, Any] = {"name": org["name"], "metadata": {"org_id": org_id}}
    if email:
        params["email"] = email
    # The key is per org, so two concurrent first-time checkouts get the same customer.
    customer = api.v1.customers.create(params, {"idempotency_key": f"keelgate-customer-{org_id}"})
    with state.db.tx(org_id) as conn:
        conn.execute(
            "UPDATE orgs SET stripe_customer_id = %s WHERE stripe_customer_id IS NULL",
            (customer.id,),
        )
        row = conn.execute("SELECT stripe_customer_id FROM orgs").fetchone()
    assert row is not None  # noqa: S101
    return str(row["stripe_customer_id"])


@router.post("/api/billing/checkout", dependencies=[require_roles("owner")])
def checkout(body: CheckoutBody, principal: User, state: State) -> dict[str, str]:
    """Start a Stripe Checkout subscription for a self-serve plan."""
    try:
        plan = state.catalogue.get(body.plan)
    except KeyError:
        raise ValidationFailedError(f"unknown plan {body.plan!r}", code="unknown_plan") from None
    if not plan.self_serve:
        what = (
            "Enterprise is sold by contract: contact sales"
            if plan.id == "enterprise"
            else f"the {plan.name} plan is not sold through checkout"
        )
        raise ValidationFailedError(what, code="plan_not_self_serve")
    org = _org_row(state, principal.org_id)
    if org["stripe_subscription_id"] and org["billing_status"] in (
        "trialing",
        "active",
        "past_due",
    ):
        raise ConflictError(
            "this organisation already has a subscription; change it in the billing portal",
            code="already_subscribed",
        )
    api = _api(state)
    try:
        prices = [
            api.v1.prices.list({"lookup_keys": [lookup_key(plan.id, kind)], "active": True}).data
            for kind in ("monthly", "overage")
        ]
        if not prices[0]:
            raise ConflictError(
                f"the {plan.name} price is not in Stripe yet: run `sync-stripe`",
                code="plan_not_synced",
            )
        items: list[dict[str, Any]] = [{"price": prices[0][0].id, "quantity": 1}]
        if prices[1]:
            items.append({"price": prices[1][0].id})  # metered: no quantity
        customer = _ensure_customer(state, api, principal.org_id, principal.email)
        dashboard = state.settings.dashboard_url.rstrip("/")
        subscription_data: dict[str, Any] = {
            "metadata": {"org_id": principal.org_id, "keelgate_plan": plan.id}
        }
        if plan.trial_days:
            subscription_data["trial_period_days"] = plan.trial_days
        session = api.v1.checkout.sessions.create(
            {
                "mode": "subscription",
                "customer": customer,
                "line_items": items,
                "client_reference_id": principal.org_id,
                "metadata": {"org_id": principal.org_id, "keelgate_plan": plan.id},
                "subscription_data": subscription_data,
                "success_url": f"{dashboard}/billing?checkout=success",
                "cancel_url": f"{dashboard}/billing?checkout=cancelled",
            }
        )
    except stripe.StripeError as exc:
        raise _provider_failure(exc) from exc
    return {"url": str(session.url)}


@router.post("/api/billing/portal", dependencies=[require_roles("owner")])
def portal(principal: User, state: State) -> dict[str, str]:
    """A Stripe customer-portal session to manage payment methods, invoices and the plan."""
    org = _org_row(state, principal.org_id)
    if not org["stripe_customer_id"]:
        raise ConflictError("this organisation has no billing account yet", code="no_customer")
    api = _api(state)
    try:
        session = api.v1.billing_portal.sessions.create(
            {
                "customer": org["stripe_customer_id"],
                "return_url": f"{state.settings.dashboard_url.rstrip('/')}/billing",
            }
        )
    except stripe.StripeError as exc:
        raise _provider_failure(exc) from exc
    return {"url": str(session.url)}


def _stripe_preview(state: State, org: dict[str, Any]) -> dict[str, Any] | None:
    api = client_for(state)
    if api is None or not org["stripe_subscription_id"] or not org["stripe_customer_id"]:
        return None
    try:
        invoice = api.v1.invoices.create_preview(
            {"customer": org["stripe_customer_id"], "subscription": org["stripe_subscription_id"]}
        )
    except stripe.StripeError as exc:
        log.warning("stripe invoice preview failed: %s", type(exc).__name__)
        return {"source": "stripe", "available": False}
    return {
        "source": "stripe",
        "available": True,
        "currency": invoice.currency,
        "subtotal_cents": invoice.subtotal,
        "total_cents": invoice.total,
        "lines": [
            {
                "description": line.description,
                "quantity": line.quantity,
                "amount_cents": line.amount,
            }
            for line in invoice.lines.data
        ],
    }


@router.get("/api/billing/invoice-preview", dependencies=[require_roles("owner", "admin")])
def invoice_preview(principal: User, state: State) -> dict[str, Any]:
    """The next invoice: the local estimate is the figure of record; Stripe's is alongside."""
    org = _org_row(state, principal.org_id)
    period = metering.period_of(metering.today())
    with state.db.tx(principal.org_id) as conn:
        used = metering.used(conn, period, metering.GATED_ACTIONS)
    body = estimate_invoice(state.catalogue.get(org["plan"]), period, used)
    body["notes"].append(
        "The local estimate is computed from the plan configuration and this period's usage "
        "and is the figure of record. stripe_preview, when present, is Stripe's own preview "
        "and can differ (proration, taxes, trial)."
    )
    body["stripe_preview"] = _stripe_preview(state, org)
    return body


@router.post("/api/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, state: State) -> dict[str, Any]:
    """Stripe calls this. Authenticated by signature only."""
    payload = await request.body()
    event = webhook.verify(
        payload, request.headers.get("stripe-signature"), state.settings.stripe_webhook_secret
    )
    outcome = await run_in_threadpool(
        webhook.process_event, state.db, event, state.catalogue, client_for(state)
    )
    return {"received": True, "status": outcome.status}
