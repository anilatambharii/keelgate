"""The Stripe webhook: signatures, de-duplication, and what each event does to an org."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from keelgate_cloud.db import Database
from tests.harness import Org
from tests.test_billing_support import (
    WEBHOOK_SECRET,
    billing_app,
    body_of,
    fixture,
    link,
    new_ids,
    org_row,
    sign,
)

URL = "/api/stripe/webhook"


@pytest.fixture(scope="module")
def client(db: Database) -> TestClient:
    return TestClient(billing_app(db))


def deliver(
    client: TestClient,
    event: dict[str, Any],
    *,
    secret: str = WEBHOOK_SECRET,
    at: int | None = None,
) -> Any:
    payload = body_of(event)
    return client.post(
        URL,
        content=payload,
        headers={
            "stripe-signature": sign(payload, secret, at=at),
            "content-type": "application/json",
        },
    )


def story(
    client: TestClient, db: Database, *, subscribed: bool = False
) -> tuple[Org, dict[str, str]]:
    """An org linked to a fresh Stripe customer, ready for events."""
    org = Org(client)
    ids = {**new_ids(), "ORG": org.id}
    link(db, org, customer=ids["CUSTOMER"], sub=ids["SUB"] if subscribed else None)
    return org, ids


# ----------------------------------------------------------------------------- signatures


def test_a_signed_event_is_accepted(client: TestClient, db: Database) -> None:
    _, ids = story(client, db)
    response = deliver(client, fixture("subscription_created_team_trialing", **ids))
    assert response.status_code == 200
    assert response.json() == {"received": True, "status": "processed"}


def test_a_missing_signature_is_rejected_and_nothing_changes(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db)
    event = fixture("subscription_deleted", **ids)
    response = client.post(URL, content=body_of(event))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_webhook"
    assert org_row(db, org)["billing_status"] == "trialing"


def test_a_wrong_secret_is_rejected(client: TestClient, db: Database) -> None:
    org, ids = story(client, db)
    event = fixture("subscription_deleted", **ids)
    wrong_secret = "whsec_somebody_else"  # pragma: allowlist secret
    assert deliver(client, event, secret=wrong_secret).status_code == 400
    assert org_row(db, org)["billing_status"] == "trialing"


def test_a_tampered_body_is_rejected(client: TestClient, db: Database) -> None:
    org, ids = story(client, db)
    event = fixture("invoice_payment_failed", **ids)
    payload = body_of(event)
    signature = sign(payload)
    tampered = payload.replace(b"in_test_1", b"in_test_2")
    response = client.post(URL, content=tampered, headers={"stripe-signature": signature})
    assert response.status_code == 400
    assert org_row(db, org)["billing_status"] == "trialing"


def test_a_stale_timestamp_is_rejected(client: TestClient, db: Database) -> None:
    org, ids = story(client, db)
    event = fixture("subscription_deleted", **ids)
    response = deliver(client, event, at=1_000_000_000)  # signed correctly, but years ago
    assert response.status_code == 400
    assert org_row(db, org)["billing_status"] == "trialing"


def test_a_garbage_signature_header_and_non_json_are_rejected(client: TestClient) -> None:
    assert (
        client.post(URL, content=b"{}", headers={"stripe-signature": "nonsense"}).status_code == 400
    )
    payload = b"not json"
    response = client.post(URL, content=payload, headers={"stripe-signature": sign(payload)})
    assert response.status_code == 400


def test_the_server_without_a_webhook_secret_trusts_nothing(db: Database) -> None:
    bare = TestClient(billing_app(db, stripe_webhook_secret=None))
    payload = b"{}"
    response = bare.post(URL, content=payload, headers={"stripe-signature": sign(payload)})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "webhook_not_configured"


# ------------------------------------------------------------------------------- dedupe


def test_a_replayed_event_is_acknowledged_and_does_nothing(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db, subscribed=True)
    event = fixture("subscription_updated_team_past_due", **ids)
    first = deliver(client, event)
    assert first.json()["status"] == "processed"
    assert org_row(db, org)["billing_status"] == "past_due"

    with db.tx(org.id) as conn:  # an operator fixes the org by hand
        conn.execute("UPDATE orgs SET billing_status = 'active'")
    again = deliver(client, event)  # Stripe retries the very same event id
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert org_row(db, org)["billing_status"] == "active"  # the replay did not re-apply


def test_a_failed_handler_is_not_recorded_so_stripe_can_retry(
    client: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from keelgate_cloud.billing import webhook  # noqa: PLC0415

    org, ids = story(client, db, subscribed=True)
    event = fixture("invoice_payment_failed", **ids)

    def explode(*_a: Any, **_k: Any) -> Any:
        raise ZeroDivisionError

    boom = TestClient(client.app, raise_server_exceptions=False)
    with monkeypatch.context() as m:
        m.setattr(webhook, "_apply_invoice", explode)
        assert deliver(boom, event).status_code == 500
    assert deliver(client, event).json()["status"] == "processed"  # same event id, now handled
    assert org_row(db, org)["billing_status"] == "past_due"


# ---------------------------------------------------------------------- what events do


def test_checkout_completed_links_customer_and_subscription(
    client: TestClient, db: Database
) -> None:
    org = Org(client)
    ids = {**new_ids(), "ORG": org.id}
    link(db, org, customer=ids["CUSTOMER"])  # our checkout saves the customer before redirecting
    response = deliver(client, fixture("checkout_session_completed", **ids))
    assert response.json()["status"] == "processed"
    row = org_row(db, org)
    assert row["stripe_customer_id"] == ids["CUSTOMER"]
    assert row["stripe_subscription_id"] == ids["SUB"]


def test_a_checkout_for_an_org_without_a_customer_is_ignored(
    client: TestClient, db: Database
) -> None:
    """A Payment Link (or anyone) naming an org in client_reference_id cannot attach a customer."""
    org = Org(client)
    ids = {**new_ids(), "ORG": org.id}
    deliver(client, fixture("checkout_session_completed", **ids))
    row = org_row(db, org)
    assert row["stripe_customer_id"] is None
    assert row["stripe_subscription_id"] is None


def test_checkout_for_a_different_customer_than_the_orgs_is_ignored(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db)
    other = {**ids, "CUSTOMER": "cus_test_someone_else", "SUB": "sub_test_other"}
    deliver(client, fixture("checkout_session_completed", **other))
    row = org_row(db, org)
    assert row["stripe_customer_id"] == ids["CUSTOMER"]
    assert row["stripe_subscription_id"] is None


def test_subscription_created_sets_plan_status_subscription_and_trial(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db)
    deliver(client, fixture("subscription_created_team_trialing", **ids))
    row = org_row(db, org)
    assert (row["plan"], row["billing_status"]) == ("team", "trialing")
    assert row["stripe_subscription_id"] == ids["SUB"]
    assert row["trial_ends_at"].timestamp() == 1761200000


def test_subscription_updated_changes_the_plan_and_status(client: TestClient, db: Database) -> None:
    org, ids = story(client, db)
    deliver(client, fixture("subscription_created_team_trialing", **ids))
    deliver(client, fixture("subscription_updated_team_active", **ids))
    assert org_row(db, org)["billing_status"] == "active"
    deliver(client, fixture("subscription_updated_business_active", **ids))
    row = org_row(db, org)
    assert (row["plan"], row["billing_status"]) == ("business", "active")


def test_a_plan_change_changes_what_the_org_may_do(client: TestClient, db: Database) -> None:
    org, ids = story(client, db)
    deliver(client, fixture("subscription_created_team_trialing", **ids))
    app_state = client.app.state.kg  # type: ignore[attr-defined]  # FastAPI keeps state untyped
    assert app_state.plan_of(org.id).max_agents == 10
    deliver(client, fixture("subscription_updated_business_active", **ids))
    plan = app_state.plan_of(org.id)
    assert plan.id == "business" and plan.max_agents is None and plan.audit_immutable


def test_subscription_deleted_cancels_and_clears_the_subscription(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db, subscribed=True)
    deliver(client, fixture("subscription_deleted", **ids))
    row = org_row(db, org)
    assert row["billing_status"] == "canceled"
    assert row["stripe_subscription_id"] is None


def test_cancellation_of_an_old_subscription_does_not_cancel_the_new_one(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db, subscribed=True)
    old = {**ids, "SUB": "sub_test_the_old_one"}
    deliver(client, fixture("subscription_deleted", **old))
    row = org_row(db, org)
    assert row["stripe_subscription_id"] == ids["SUB"]
    assert row["billing_status"] == "trialing"


def test_a_canceled_org_is_cut_off_from_the_agent_api(client: TestClient, db: Database) -> None:
    org, ids = story(client, db, subscribed=True)
    before = client.get("/v1/whoami", headers=org.key_auth)
    assert before.status_code == 200, before.text
    deliver(client, fixture("subscription_deleted", **ids))
    after = client.get("/v1/whoami", headers=org.key_auth)
    assert after.status_code == 402
    assert after.json()["error"]["code"] == "subscription_inactive"


def test_payment_failed_then_paid(client: TestClient, db: Database) -> None:
    org, ids = story(client, db, subscribed=True)
    deliver(client, fixture("invoice_payment_failed", **ids))
    assert org_row(db, org)["billing_status"] == "past_due"
    deliver(client, fixture("invoice_paid", **ids))
    assert org_row(db, org)["billing_status"] == "active"


def test_a_zero_dollar_trial_invoice_does_not_end_the_trial(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db, subscribed=True)
    deliver(client, fixture("invoice_paid_zero_trial", **ids))
    assert org_row(db, org)["billing_status"] == "trialing"


def test_a_paid_invoice_cannot_revive_a_canceled_org(client: TestClient, db: Database) -> None:
    org, ids = story(client, db, subscribed=True)
    deliver(client, fixture("subscription_deleted", **ids))
    deliver(client, fixture("invoice_paid", **ids))
    assert org_row(db, org)["billing_status"] == "canceled"


def test_an_unhandled_event_type_is_acknowledged_and_recorded(
    client: TestClient, db: Database
) -> None:
    _, ids = story(client, db)
    event = fixture("customer_updated_unhandled", **ids)
    assert deliver(client, event).json()["status"] == "recorded"
    assert deliver(client, event).json()["status"] == "duplicate"
    with db.tx() as conn:
        row = conn.execute(
            "SELECT type FROM stripe_events WHERE event_id = %s", (event["id"],)
        ).fetchone()
    assert row == {"type": "customer.updated"}


def test_an_unknown_customer_gets_200_and_changes_nothing(client: TestClient, db: Database) -> None:
    org, ids = story(client, db)
    stranger = {**ids, "CUSTOMER": "cus_test_nobody_knows", "ORG": "not-a-uuid"}
    response = deliver(client, fixture("subscription_updated_business_active", **stranger))
    assert response.status_code == 200
    assert response.json()["status"] == "unknown_customer"
    assert org_row(db, org)["plan"] == "team"


def test_a_subscription_for_an_unrecognised_plan_does_not_grant_one(
    client: TestClient, db: Database
) -> None:
    org, ids = story(client, db)
    event = fixture("subscription_updated_business_active", **ids)
    item = event["data"]["object"]["items"]["data"][0]["price"]
    item["metadata"] = {}
    item["lookup_key"] = None
    event["data"]["object"]["metadata"] = {"keelgate_plan": "platinum"}
    item["product"] = "prod_unknown"
    # no Stripe client lookup can name it either: the mock's canned product has no keelgate_plan
    deliver(client, event)
    row = org_row(db, org)
    assert row["plan"] == "team"  # unchanged, never a guess
    assert row["billing_status"] == "active"
