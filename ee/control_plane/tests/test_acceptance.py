"""K5 acceptance: a new org signs up, creates a key, runs a governed agent against the hosted
control plane, a person approves an action in the (API behind the) dashboard, and the org sees its
usage and a Stripe test invoice preview.

Everything is real except Stripe, which is stripe-mock (a request-shape validator with canned
responses) and the human, who is this test calling the same endpoints the dashboard calls.
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from keelgate.audit import AuditRecord, verify_chain

from keelgate_cloud.db import Database
from tests.harness import PASSWORD, live_server, unique_email
from tests.test_billing_support import billing_app, body_of, deliver_signed, fixture, new_ids

ROOT = Path(__file__).resolve().parents[3]

LIMITS = {
    "max_notional_per_action": 50_000,
    "max_daily_exposure": 100_000,
    "restricted_symbols": ["TSLA"],
    "approval_one_click_notional": 10_000,
    "approval_explicit_notional": 25_000,
    "trading_hours": {"tz": "America/New_York", "open_minute": 570, "close_minute": 960},
}


def case(
    name: str, capability: str, side_effect: str, resource: dict[str, Any], expect: dict
) -> dict:  # type: ignore[type-arg]
    return {
        "name": name,
        "input": {
            "action": {
                "tool": "t",
                "side_effect": side_effect,
                "capability": capability,
                "args": {},
            },
            "actor": {"agent_id": "research-agent", "tenant_id": "t", "grant_id": "g"},
            "resource": resource,
            "context": {
                "as_of": "2026-10-05T14:30:00+00:00",
                "execution_mode": "paper",
                "exposure": {"daily_notional": 0},
                "limits": LIMITS,
            },
        },
        "expect": expect,
    }


CASES = [
    case("quote is allowed", "market_data:read", "READ", {}, {"effect": "ALLOW"}),
    case(
        "small paper trade is allowed",
        "trade:paper_execute",
        "WRITE",
        {"symbol": "AAPL", "notional": 5000},
        {"effect": "ALLOW"},
    ),
    case(
        "restricted symbol is denied",
        "trade:paper_execute",
        "WRITE",
        {"symbol": "TSLA", "notional": 1000},
        {"effect": "DENY"},
    ),
    case(
        "large trade needs explicit sign-off",
        "trade:paper_execute",
        "WRITE",
        {"symbol": "AAPL", "notional": 30000},
        {"effect": "REQUIRE_APPROVAL", "approval_tier": "EXPLICIT_SIGNOFF"},
    ),
]


def load_example() -> Any:
    spec = importlib.util.spec_from_file_location(
        "cloud_governed_agent", ROOT / "examples" / "cloud_governed_agent.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stack(db: Database):  # type: ignore[no-untyped-def]
    app = billing_app(db, public_url="http://127.0.0.1:0")
    with live_server(app) as base:
        yield base


def test_the_whole_story(stack: str, db: Database) -> None:
    base = stack
    human = httpx.Client(base_url=base, timeout=20)

    # 1. A new org signs up (on a Team trial) and creates an API key.
    email = unique_email("founder")
    signup = human.post(
        "/api/signup", json={"org_name": "Acme Treasury", "email": email, "password": PASSWORD}
    )
    assert signup.status_code == 201, signup.text
    token = signup.json()["token"]
    org_id = signup.json()["org"]["id"]
    auth = {"Authorization": f"Bearer {token}"}
    assert signup.json()["org"]["billing_status"] == "trialing"
    key = human.post("/api/keys", json={"name": "research agent"}, headers=auth).json()["key"]

    # The owner may decide EXPLICIT_SIGNOFF requests.
    me = human.get("/api/me", headers=auth).json()["user"]
    assert (
        human.patch(
            f"/api/users/{me['id']}", json={"approval_tier": "EXPLICIT_SIGNOFF"}, headers=auth
        ).status_code
        == 204
    )

    # 2. The org deploys its policy to the hosted registry: tests must pass first.
    templates = human.get("/api/policies/templates", headers=auth).json()["items"]
    finance = next(t for t in templates if t["name"] == "finance_basic")
    created = human.post(
        "/api/policies",
        json={
            "name": "finance_basic",
            "files": finance["files"],
            "query": finance["query"],
            "cases": CASES,
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    assert created.json()["tests_passed"] is True
    deploy = human.post("/api/policies/finance_basic/versions/1/deploy", headers=auth)
    assert deploy.status_code == 200, deploy.text

    # 3. A governed agent runs against the hosted control plane (the shipped example).
    example = load_example()
    outcome: dict[str, Any] = {}

    def agent() -> None:
        outcome["code"] = asyncio.run(
            example.run_agent(
                base, key, policy_pack="finance_basic", wait_s=60, announce=lambda *_: None
            )
        )

    thread = threading.Thread(target=agent)
    thread.start()

    # 4. The large trade is parked in the hosted inbox; a person approves it.
    pending: list[dict[str, Any]] = []
    for _ in range(120):
        pending = human.get("/api/approvals", headers=auth).json()["items"]
        if pending:
            break
        time.sleep(0.25)
    assert len(pending) == 1, "the agent never filed an approval request"
    request = pending[0]
    assert request["tier"] == "EXPLICIT_SIGNOFF"
    assert request["evidence"]["rationale"].startswith("Breakout")
    approved = human.post(
        f"/api/approvals/{request['request_id']}/approve",
        json={"signoff_code": request["signoff_code"], "note": "reviewed"},
        headers=auth,
    )
    assert approved.status_code == 200, approved.text

    thread.join(timeout=60)
    assert not thread.is_alive()
    assert outcome["code"] == 0, "the agent did not finish cleanly"

    # 5. Usage: five governed calls (quote, small, restricted, large parked, large resumed), one
    #    approval. The audit chain verifies with the open-source verifier.
    usage = human.get("/api/usage", headers=auth).json()
    assert usage["usage"]["gated_actions"] == 5
    assert usage["usage"]["approvals"] == 1
    assert usage["agents"] == 1
    assert usage["gated_actions_remaining"] == 1_000_000 - 5

    export = human.get("/api/audit/export", params={"format": "json"}, headers=auth).json()
    records = [AuditRecord.model_validate(r) for r in export["records"]]
    assert verify_chain(records, tenant_id=org_id, expected_head=None)
    kinds = [r.event_type for r in records]
    assert kinds.count("tool.call") == 5
    assert "policy.deployed" in kinds
    assert {"approval.requested", "approval.decided", "approval.consumed"} <= set(kinds)

    # 6. Billing: the trial converts through Stripe (test mode). The org links to a Stripe
    #    customer and subscription via the signed webhook, then previews its next invoice.
    ids = {**new_ids(), "ORG": org_id}
    with db.tx(org_id) as conn:  # our checkout endpoint saves the customer before redirecting
        conn.execute("UPDATE orgs SET stripe_customer_id = %s", (ids["CUSTOMER"],))
    checkout = fixture("checkout_session_completed", **ids)
    delivered = deliver_signed(human, checkout)
    assert delivered.status_code == 200, delivered.text
    preview = human.get("/api/billing/invoice-preview", headers=auth).json()
    assert preview["source"] == "local"
    assert preview["currency"] == "usd"
    assert preview["total_cents"] == 49_900  # Team base price; usage is inside the allowance
    base_line = preview["lines"][0]
    assert base_line["amount_cents"] == 49_900
    assert preview["stripe_preview"] is not None
    assert preview["stripe_preview"]["source"] == "stripe"
    assert body_of(checkout)  # the fixture is a real, signed Stripe-shaped event
