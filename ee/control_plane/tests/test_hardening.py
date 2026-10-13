"""Fixes for the findings of the adversarial review, each pinned by a test that failed before."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelgate.approvals import ApprovalTier, EvidenceBundle, action_hash
from keelgate.cloud import CloudApprovalQueue, CloudClient

from keelgate_cloud import policy_registry
from keelgate_cloud.db import Database
from keelgate_cloud.sso import _is_public
from tests.harness import PASSWORD, Org, live_server, make_app, unique_email


@pytest.fixture(scope="module")
def client(db: Database) -> TestClient:
    return TestClient(make_app(db))


def event(org: Org, kind: str = "tool.result", actor: str = "a", payload: str = "{}") -> dict:  # type: ignore[type-arg]
    return {"tenant_id": org.id, "event_type": kind, "actor": actor, "payload_json": payload}


# ----------------------------------------------- admin cannot mint clearance (HIGH-2)


def test_an_admin_cannot_create_a_user_with_explicit_signoff_clearance(client: TestClient) -> None:
    org = Org(client)
    admin = org.invite("admin")
    body = {
        "email": unique_email(),
        "role": "approver",
        "password": PASSWORD,
        "approval_tier": "EXPLICIT_SIGNOFF",
    }
    denied = client.post("/api/users", json=body, headers=admin)
    assert denied.status_code == 403
    allowed = client.post("/api/users", json={**body, "email": unique_email()}, headers=org.auth)
    assert allowed.status_code == 201
    assert allowed.json()["approval_tier"] == "EXPLICIT_SIGNOFF"
    plain = {**body, "email": unique_email(), "approval_tier": "ONE_CLICK"}
    assert client.post("/api/users", json=plain, headers=admin).status_code == 201


# ------------------------------------------------- request size limits (HIGH-4)


def test_unauthenticated_endpoints_refuse_large_bodies(client: TestClient) -> None:
    big = json.dumps({"email": "a@b.co", "password": "x" * 20_000})
    for path in ("/api/login", "/api/signup"):
        response = client.post(path, content=big, headers={"content-type": "application/json"})
        assert response.status_code == 413, path
        assert response.json()["error"]["code"] == "payload_too_large"


def test_a_body_without_a_declared_length_is_still_capped(client: TestClient) -> None:
    def chunks() -> Any:
        for _ in range(10):
            yield b"x" * 2048

    response = client.post(
        "/api/login", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


def test_authenticated_bodies_have_a_larger_but_finite_cap(client: TestClient) -> None:
    org = Org(client)
    huge = json.dumps({"name": "k" * (5 * 1024 * 1024)})
    response = client.post(
        "/api/keys", content=huge, headers={**org.auth, "content-type": "application/json"}
    )
    assert response.status_code == 413


# ------------------------------------------- sign-up and sign-in throttling (HIGH-4)


@pytest.fixture
def strict(db: Database) -> TestClient:
    return TestClient(make_app(db, signup_per_minute=3, login_per_minute=6))


def test_signups_are_throttled_per_address(strict: TestClient) -> None:
    codes = [
        strict.post(
            "/api/signup",
            json={"org_name": "X", "email": unique_email(), "password": PASSWORD},
        ).status_code
        for _ in range(6)
    ]
    assert codes[:3] == [201, 201, 201]
    assert codes[3:] == [429, 429, 429]


def test_password_guessing_is_throttled_per_account_and_per_address(strict: TestClient) -> None:
    org = Org(strict)  # uses up one signup
    guesses = [
        strict.post("/api/login", json={"email": org.email, "password": f"wrong-password-{n}"})
        for n in range(6)
    ]
    assert [g.status_code for g in guesses[:2]] == [401, 401]
    assert guesses[-1].status_code == 429
    # Even the right password is refused while the account is being hammered.
    assert (
        strict.post("/api/login", json={"email": org.email, "password": PASSWORD}).status_code
        == 429
    )


# --------------------------------------------- idempotency cannot suppress events (MED-3)


def test_reusing_an_idempotency_key_for_a_different_event_is_refused(client: TestClient) -> None:
    org = Org(client)
    headers = {**org.key_auth, "Idempotency-Key": "k-1"}
    first = client.post("/v1/audit/events", json=event(org), headers=headers)
    assert first.status_code == 200
    other = event(org, kind="tool.call", payload='{"n": 2}')
    clash = client.post("/v1/audit/events", json=other, headers=headers)
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "idempotency_key_reuse"
    usage = client.get("/api/usage", headers=org.auth).json()["usage"]
    assert usage["gated_actions"] == 0  # the refused tool.call was not pretended recorded
    # The honest retry of the same event still replays.
    assert client.post("/v1/audit/events", json=event(org), headers=headers).json() == first.json()


def test_a_failed_approval_releases_its_idempotency_key(client: TestClient) -> None:
    org = Org(client)
    headers = {**org.key_auth, "Idempotency-Key": "ap-1"}
    good = {
        "tenant_id": org.id,
        "agent_id": "a",
        "tool_name": "place_order",
        "args_hash": action_hash("place_order", {"q": 1}),
        "tier": "ONE_CLICK",
        "evidence": {"tool": "place_order", "args": {"q": 1}, "rationale": "r"},
    }
    bad = {**good, "evidence": {}}
    assert client.post("/v1/approvals", json=bad, headers=headers).status_code == 422
    # Same key, same (bad) request: still refused for its own reason, never stuck "in progress".
    assert client.post("/v1/approvals", json=bad, headers=headers).status_code == 422
    # A corrected request is a different request, so it needs a new key...
    clash = client.post("/v1/approvals", json=good, headers=headers)
    assert clash.status_code in (200, 409)
    fresh = client.post(
        "/v1/approvals", json=good, headers={**org.key_auth, "Idempotency-Key": "ap-2"}
    )
    assert fresh.status_code == 200
    replay = client.post(
        "/v1/approvals", json=good, headers={**org.key_auth, "Idempotency-Key": "ap-2"}
    )
    assert replay.json() == fresh.json()
    reuse = client.post(
        "/v1/approvals",
        json={**good, "tool_name": "other"},
        headers={**org.key_auth, "Idempotency-Key": "ap-2"},
    )
    assert reuse.status_code == 409
    assert reuse.json()["error"]["code"] == "idempotency_key_reuse"


# -------------------------------------------- limits cannot be multiplied by keys (MED-2)


def test_the_agent_rate_limit_is_per_org_not_per_key(db: Database) -> None:
    http = TestClient(make_app(db, plan={"rate_limit_per_minute": 4}))
    org = Org(http)
    second = org.create_key("second")
    third = org.create_key("third")
    statuses = []
    for key in (org.api_key, second, third) * 3:
        statuses.append(
            http.get("/v1/audit/head", headers={"Authorization": f"Bearer {key}"}).status_code
        )
    assert statuses.count(200) == 4
    assert 429 in statuses


def test_an_org_cannot_mint_unlimited_api_keys(client: TestClient) -> None:
    org = Org(client)  # one key already
    for n in range(24):
        assert client.post("/api/keys", json={"name": f"k{n}"}, headers=org.auth).status_code == 201
    capped = client.post("/api/keys", json={"name": "one too many"}, headers=org.auth)
    assert capped.status_code == 409
    assert capped.json()["error"]["code"] == "key_cap_reached"
    first = client.get("/api/keys", headers=org.auth).json()["items"][-1]["id"]
    assert client.delete(f"/api/keys/{first}", headers=org.auth).status_code == 204
    assert (
        client.post("/api/keys", json={"name": "after revoke"}, headers=org.auth).status_code == 201
    )


# ------------------------------------------------------------- smaller fixes


def test_server_only_event_types_are_not_accepted_from_agents(client: TestClient) -> None:
    org = Org(client)
    for kind in ("sso.login", "settings.changed", "policy.deployed", "audit.exported", "x.y"):
        response = client.post("/v1/audit/events", json=event(org, kind=kind), headers=org.key_auth)
        assert response.status_code == 422, kind


def test_absurd_payloads_and_cursors_are_422_not_500(client: TestClient) -> None:
    org = Org(client)
    deep = "[" * 200_000
    response = client.post("/v1/audit/events", json=event(org, payload=deep), headers=org.key_auth)
    assert response.status_code == 422
    huge = client.get("/v1/audit/records", params={"after_seq": 2**70}, headers=org.key_auth)
    assert huge.status_code == 422


def test_a_viewer_does_not_see_the_sign_off_code(client: TestClient) -> None:
    app = make_app(client.app.state.kg.db)  # type: ignore[attr-defined]
    with live_server(app) as base:
        http = TestClient(app)
        org = Org(http)
        viewer = org.invite("viewer")
        queue = CloudApprovalQueue(CloudClient(base, org.api_key))
        queue.submit(
            tenant_id=org.id,
            agent_id="a",
            tool_name="t",
            args_hash=action_hash("t", {}),
            tier=ApprovalTier.EXPLICIT_SIGNOFF,
            evidence=EvidenceBundle(tool="t", args={}, rationale="r"),
        )
        seen = http.get("/api/approvals", headers=viewer).json()["items"][0]
        assert seen["signoff_code"] is None
        mine = http.get("/api/approvals", headers=org.auth).json()["items"][0]
        assert mine["signoff_code"]


def test_the_api_docs_can_be_turned_off(db: Database) -> None:
    http = TestClient(make_app(db, docs_enabled=False))
    assert http.get("/api/docs").status_code == 404
    assert http.get("/openapi.json").status_code == 404
    assert TestClient(make_app(db)).get("/api/docs").status_code == 200


# --------------------------------------------------------------- SSRF: NAT64 (MED-6)


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::a9fe:a9fe",  # NAT64 wrapping the cloud metadata address
        "64:ff9b::7f00:1",  # NAT64 wrapping loopback
        "::127.0.0.1",
        "::7f00:1",
        "::ffff:169.254.169.254",
        "::1",
        "10.0.0.5",
    ],
)
def test_wrapped_private_addresses_are_not_public(address: str) -> None:
    assert not _is_public(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "64:ff9b::808:808", "2606:4700:4700::1111"])
def test_public_addresses_still_are(address: str) -> None:
    assert _is_public(address)


# ------------------------------------------- one org cannot hog policy evaluation (HIGH-3)


def test_an_org_is_limited_in_concurrent_policy_evaluations(client: TestClient) -> None:
    org = Org(client)
    for _ in range(policy_registry.MAX_EVALUATIONS_PER_ORG):
        policy_registry.acquire_org_slot(org.id)
    try:
        busy = client.post(
            "/api/policies/simulate",
            json={"files": {"p.rego": "package a\n"}, "query": "data.a.decision", "cases": []},
            headers=org.auth,
        )
        assert busy.status_code == 429
        assert busy.json()["error"]["code"] == "policy_busy"
        other = Org(client)  # another org is unaffected
        ok = client.post(
            "/api/policies/simulate",
            json={"files": {"p.rego": "package a\n"}, "query": "data.a.decision", "cases": []},
            headers=other.auth,
        )
        assert ok.status_code != 429
    finally:
        for _ in range(policy_registry.MAX_EVALUATIONS_PER_ORG):
            policy_registry.release_org_slot(org.id)
    after = client.post(
        "/api/policies/simulate",
        json={"files": {"p.rego": "package a\n"}, "query": "data.a.decision", "cases": []},
        headers=org.auth,
    )
    assert after.status_code != 429
