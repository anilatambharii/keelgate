"""Helpers shared by the billing tests (no tests here)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import stripe

from keelgate_cloud.db import Database
from tests.harness import Org, make_app

if TYPE_CHECKING:
    from fastapi import FastAPI

FIXTURES = Path(__file__).parent / "fixtures" / "stripe"
WEBHOOK_SECRET = "whsec_test_secret_for_the_billing_tests"  # pragma: allowlist secret
MOCK_URL = "http://localhost:12111"
MOCK_KEY = "sk_test_local"  # pragma: allowlist secret


def sign(payload: bytes, secret: str = WEBHOOK_SECRET, *, at: int | None = None) -> str:
    """Stripe's documented scheme: ``t=<ts>,v1=hmac_sha256(secret, "<ts>.<payload>")``."""
    ts = int(time.time()) if at is None else at
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def fixture(name: str, **fill: str) -> dict[str, Any]:
    """A recorded-style event with its placeholders filled; the event id is fresh by default."""
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    fill.setdefault("EVENT", f"evt_test_{uuid.uuid4().hex[:16]}")
    for key, value in fill.items():
        text = text.replace(f"__{key}__", value)
    event: dict[str, Any] = json.loads(text)
    return event


def body_of(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode()


def billing_app(db: Database, **over: Any) -> FastAPI:
    base: dict[str, Any] = {
        "stripe_api_key": MOCK_KEY,
        "stripe_api_base": MOCK_URL,
        "stripe_webhook_secret": WEBHOOK_SECRET,
    }
    base.update(over)
    return make_app(db, **base)


def link(db: Database, org: Org, *, customer: str | None = None, sub: str | None = None) -> None:
    """Give the org a Stripe customer (and subscription) as checkout would."""
    with db.tx(org.id) as conn:
        conn.execute(
            "UPDATE orgs SET stripe_customer_id = %s, stripe_subscription_id = %s",
            (customer, sub),
        )


def org_row(db: Database, org: Org) -> dict[str, Any]:
    with db.tx(org.id) as conn:
        return dict(conn.execute("SELECT * FROM orgs").fetchone() or {})


def new_ids() -> dict[str, str]:
    """Fresh Stripe-looking ids for one org's story."""
    tag = uuid.uuid4().hex[:14]
    return {"CUSTOMER": f"cus_test_{tag}", "SUB": f"sub_test_{tag}"}


class RecordingHttp(stripe.RequestsClient):
    """The SDK's HTTP client, remembering what it sent (to stripe-mock, on localhost)."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []

    def request_with_retries(self, method, url, headers, post_data=None, *args, **kwargs):  # type: ignore[no-untyped-def]  # mirrors the SDK signature
        self.sent.append(
            {"method": method, "url": url, "headers": dict(headers), "data": post_data}
        )
        return super().request_with_retries(method, url, headers, post_data, *args, **kwargs)

    def to(self, path: str) -> list[dict[str, Any]]:
        return [s for s in self.sent if s["url"].split("?")[0].endswith(path)]


def recording_client() -> tuple[Any, RecordingHttp]:
    http = RecordingHttp()
    client = stripe.StripeClient(
        MOCK_KEY, base_addresses={"api": MOCK_URL}, http_client=http, max_network_retries=0
    )
    return client, http


def deliver_signed(client: Any, event: dict[str, Any], *, secret: str = WEBHOOK_SECRET) -> Any:
    """POST a Stripe event to the webhook with a valid signature (any httpx-style client)."""
    payload = body_of(event)
    return client.post(
        "/api/stripe/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload, secret), "content-type": "application/json"},
    )
