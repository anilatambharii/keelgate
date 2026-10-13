"""Billing endpoints: state, checkout, portal, invoice preview. Requests go to stripe-mock."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

import pytest
import stripe
from fastapi.testclient import TestClient

from keelgate_cloud import metering
from keelgate_cloud.db import Database
from tests.harness import Org, make_app
from tests.test_billing_support import (
    RecordingHttp,
    billing_app,
    link,
    new_ids,
    org_row,
    recording_client,
)


@pytest.fixture
def setup(db: Database) -> tuple[TestClient, RecordingHttp]:
    """An app whose Stripe client is the real SDK talking to stripe-mock, recording requests."""
    app = billing_app(db)
    client, http = recording_client()
    app.state.kg.extras["stripe_client"] = client
    return TestClient(app), http


def form(request: dict[str, Any]) -> dict[str, str]:
    return dict(parse_qsl(str(request["data"])))


# ---------------------------------------------------------------------------- GET /api/billing


def test_billing_state_for_a_trialing_org(setup: tuple[TestClient, RecordingHttp]) -> None:
    client, _ = setup
    org = Org(client)
    body = client.get("/api/billing", headers=org.auth).json()
    assert body["plan"] == "team"
    assert body["billing_status"] == "trialing"
    assert body["trial_ends_at"]
    assert body["subscription"] is None
    assert body["portal_available"] is False
    ids = [p["id"] for p in body["plans"]]
    assert ids == ["community", "team", "business", "enterprise"]
    team = body["plans"][1]
    assert team == {
        "id": "team",
        "name": "Team",
        "description": "Hosted control plane for small teams.",
        "price_monthly_cents": 49900,
        "included_gated_actions": 1_000_000,
        "features": ["sso"],
        "self_serve": True,
        "trial_days": 14,
    }
    assert body["plans"][3]["self_serve"] is False


def test_billing_state_with_a_subscription(
    setup: tuple[TestClient, RecordingHttp], db: Database
) -> None:
    client, _ = setup
    org = Org(client)
    ids = new_ids()
    link(db, org, customer=ids["CUSTOMER"], sub=ids["SUB"])
    body = client.get("/api/billing", headers=org.auth).json()
    assert body["portal_available"] is True
    assert body["subscription"]["id"] == ids["SUB"]
    assert set(body["subscription"]) == {"id", "status", "current_period_end"}


def test_billing_state_works_without_stripe_configured(db: Database) -> None:
    client = TestClient(make_app(db))
    org = Org(client)
    body = client.get("/api/billing", headers=org.auth).json()
    assert body["portal_available"] is False
    assert (
        client.post("/api/billing/checkout", json={"plan": "team"}, headers=org.auth).json()[
            "error"
        ]["code"]
        == "billing_not_configured"
    )


@pytest.mark.parametrize(("role", "status"), [("admin", 200), ("approver", 403), ("viewer", 403)])
def test_who_may_read_billing(
    setup: tuple[TestClient, RecordingHttp], role: str, status: int
) -> None:
    client, _ = setup
    org = Org(client)
    assert client.get("/api/billing", headers=org.invite(role)).status_code == status
    assert client.get("/api/billing").status_code == 401


# ----------------------------------------------------------------------------------- checkout


def test_checkout_creates_a_customer_once_and_a_trial_subscription_session(
    setup: tuple[TestClient, RecordingHttp], db: Database
) -> None:
    client, http = setup
    org = Org(client)
    first = client.post("/api/billing/checkout", json={"plan": "team"}, headers=org.auth)
    assert first.status_code == 200, first.text
    assert first.json()["url"].startswith("https://checkout.stripe.com/")

    customers = http.to("/v1/customers")
    assert len(customers) == 1
    assert customers[0]["headers"]["Idempotency-Key"] == f"keelgate-customer-{org.id}"
    assert form(customers[0])["metadata[org_id]"] == org.id
    assert org_row(db, org)["stripe_customer_id"]

    session = form(http.to("/v1/checkout/sessions")[0])
    assert session["mode"] == "subscription"
    assert session["client_reference_id"] == org.id
    assert session["metadata[org_id]"] == org.id
    assert session["subscription_data[trial_period_days]"] == "14"
    assert session["subscription_data[metadata][org_id]"] == org.id
    assert session["subscription_data[metadata][keelgate_plan]"] == "team"
    assert session["line_items[0][quantity]"] == "1"
    assert session["customer"] == org_row(db, org)["stripe_customer_id"]

    again = client.post("/api/billing/checkout", json={"plan": "business"}, headers=org.auth)
    assert again.status_code == 200
    assert len(http.to("/v1/customers")) == 1  # the saved customer is reused


def test_checkout_looks_the_price_up_by_its_lookup_key(
    setup: tuple[TestClient, RecordingHttp],
) -> None:
    client, http = setup
    org = Org(client)
    client.post("/api/billing/checkout", json={"plan": "business"}, headers=org.auth)
    lookups = [r["url"] for r in http.to("/v1/prices")]
    assert any("keelgate_business_monthly" in u for u in lookups)


@pytest.mark.parametrize(
    ("plan", "code"),
    [
        ("enterprise", "plan_not_self_serve"),
        ("community", "plan_not_self_serve"),
        ("platinum", "unknown_plan"),
    ],
)
def test_only_self_serve_plans_can_be_bought(
    setup: tuple[TestClient, RecordingHttp], plan: str, code: str
) -> None:
    client, http = setup
    org = Org(client)
    response = client.post("/api/billing/checkout", json={"plan": plan}, headers=org.auth)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert not http.sent  # refused before Stripe was called
    if plan == "enterprise":
        assert "contract" in response.json()["error"]["message"]


@pytest.mark.parametrize("role", ["admin", "approver", "viewer"])
def test_only_the_owner_can_check_out(setup: tuple[TestClient, RecordingHttp], role: str) -> None:
    client, http = setup
    org = Org(client)
    response = client.post("/api/billing/checkout", json={"plan": "team"}, headers=org.invite(role))
    assert response.status_code == 403
    assert not http.sent


def test_checkout_refuses_an_org_that_already_subscribes(
    setup: tuple[TestClient, RecordingHttp], db: Database
) -> None:
    client, _ = setup
    org = Org(client)
    ids = new_ids()
    link(db, org, customer=ids["CUSTOMER"], sub=ids["SUB"])
    response = client.post("/api/billing/checkout", json={"plan": "team"}, headers=org.auth)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_subscribed"


def test_checkout_says_so_when_the_plan_was_never_synced(db: Database) -> None:
    app = billing_app(db)
    empty = SimpleNamespace(data=[])
    app.state.kg.extras["stripe_client"] = SimpleNamespace(
        v1=SimpleNamespace(prices=SimpleNamespace(list=lambda *_a, **_k: empty))
    )
    client = TestClient(app)
    org = Org(client)
    response = client.post("/api/billing/checkout", json={"plan": "team"}, headers=org.auth)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plan_not_synced"


def test_a_stripe_outage_is_a_502_without_stripes_message(db: Database) -> None:
    def down(*_a: Any, **_k: Any) -> Any:
        raise stripe.APIConnectionError("secret detail sk_test_leak")

    app = billing_app(db)
    app.state.kg.extras["stripe_client"] = SimpleNamespace(
        v1=SimpleNamespace(prices=SimpleNamespace(list=down))
    )
    client = TestClient(app)
    org = Org(client)
    response = client.post("/api/billing/checkout", json={"plan": "team"}, headers=org.auth)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "billing_provider_error"
    assert "sk_test_leak" not in response.text


# ------------------------------------------------------------------------------------- portal


def test_portal_needs_a_customer(setup: tuple[TestClient, RecordingHttp]) -> None:
    client, http = setup
    org = Org(client)
    response = client.post("/api/billing/portal", headers=org.auth)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_customer"
    assert not http.sent


def test_portal_session_for_the_orgs_own_customer(
    setup: tuple[TestClient, RecordingHttp], db: Database
) -> None:
    client, http = setup
    org = Org(client)
    ids = new_ids()
    link(db, org, customer=ids["CUSTOMER"])
    response = client.post("/api/billing/portal", headers=org.auth)
    assert response.status_code == 200, response.text
    assert response.json()["url"].startswith("https://")
    sent = form(http.to("/v1/billing_portal/sessions")[0])
    assert sent["customer"] == ids["CUSTOMER"]
    assert sent["return_url"].endswith("/billing")


def test_portal_is_owner_only(setup: tuple[TestClient, RecordingHttp]) -> None:
    client, _ = setup
    org = Org(client)
    for role in ("admin", "approver", "viewer"):
        assert client.post("/api/billing/portal", headers=org.invite(role)).status_code == 403


def test_one_org_cannot_reach_another_orgs_billing(
    setup: tuple[TestClient, RecordingHttp], db: Database
) -> None:
    client, http = setup
    a, b = Org(client, "A"), Org(client, "B")
    ia, ib = new_ids(), new_ids()
    link(db, a, customer=ia["CUSTOMER"], sub=ia["SUB"])
    link(db, b, customer=ib["CUSTOMER"], sub=ib["SUB"])
    assert client.get("/api/billing", headers=a.auth).json()["subscription"]["id"] == ia["SUB"]
    assert client.get("/api/billing", headers=b.auth).json()["subscription"]["id"] == ib["SUB"]
    client.post("/api/billing/portal", headers=b.auth)
    assert form(http.to("/v1/billing_portal/sessions")[-1])["customer"] == ib["CUSTOMER"]
    previews = client.get("/api/billing/invoice-preview", headers=a.auth)
    assert form(http.to("/v1/invoices/create_preview")[-1])["customer"] == ia["CUSTOMER"]
    assert previews.status_code == 200


# ------------------------------------------------------------------------------ invoice preview


def test_preview_local_estimate_for_team(
    setup: tuple[TestClient, RecordingHttp], db: Database
) -> None:
    client, _ = setup
    org = Org(client)
    with db.tx(org.id) as conn:
        metering.record(conn, org.id, metering.GATED_ACTIONS, 1234)
    body = client.get("/api/billing/invoice-preview", headers=org.auth).json()
    assert body["currency"] == "usd"
    assert body["source"] == "local"
    assert body["period"]["id"] == metering.period_of(metering.today())
    assert body["lines"][0] == {
        "description": "Team plan, monthly subscription",
        "quantity": 1,
        "unit_amount_cents": 49900,
        "amount_cents": 49900,
    }
    assert body["lines"][1]["quantity"] == 1234
    assert body["lines"][1]["amount_cents"] == 0
    assert body["subtotal_cents"] == body["total_cents"] == 49900
    assert body["stripe_preview"] is None  # no subscription yet
    assert any("figure of record" in n for n in body["notes"])


def test_preview_includes_stripes_own_preview_alongside(
    setup: tuple[TestClient, RecordingHttp], db: Database
) -> None:
    client, http = setup
    org = Org(client)
    ids = new_ids()
    link(db, org, customer=ids["CUSTOMER"], sub=ids["SUB"])
    body = client.get("/api/billing/invoice-preview", headers=org.auth).json()
    assert body["source"] == "local"
    assert body["total_cents"] == 49900
    preview = body["stripe_preview"]
    assert preview["source"] == "stripe"
    assert preview["available"] is True
    assert {"total_cents", "subtotal_cents", "lines", "currency"} <= set(preview)
    sent = form(http.to("/v1/invoices/create_preview")[0])
    assert sent == {"customer": ids["CUSTOMER"], "subscription": ids["SUB"]}


def test_preview_survives_a_stripe_outage(db: Database) -> None:
    def down(*_a: Any, **_k: Any) -> Any:
        raise stripe.APIConnectionError("down")

    app = billing_app(db)
    app.state.kg.extras["stripe_client"] = SimpleNamespace(
        v1=SimpleNamespace(invoices=SimpleNamespace(create_preview=down))
    )
    client = TestClient(app)
    org = Org(client)
    link(db, org, customer="cus_test_x", sub="sub_test_x")
    body = client.get("/api/billing/invoice-preview", headers=org.auth).json()
    assert body["total_cents"] == 49900
    assert body["stripe_preview"] == {"source": "stripe", "available": False}


def test_preview_roles(setup: tuple[TestClient, RecordingHttp]) -> None:
    client, _ = setup
    org = Org(client)
    assert (
        client.get("/api/billing/invoice-preview", headers=org.invite("admin")).status_code == 200
    )
    assert (
        client.get("/api/billing/invoice-preview", headers=org.invite("viewer")).status_code == 403
    )
    assert (
        client.get("/api/billing/invoice-preview", headers=org.invite("approver")).status_code
        == 403
    )
