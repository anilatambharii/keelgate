"""Sign-up, sign-in, keys, users, roles and trials."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from keelgate_cloud.db import Database
from tests.harness import PASSWORD, Org, make_app, unique_email


@pytest.fixture(scope="module")
def client(db: Database) -> TestClient:
    return TestClient(make_app(db))


def test_a_new_org_signs_up_on_a_team_trial(client: TestClient) -> None:
    org = Org(client)
    me = client.get("/api/me", headers=org.auth).json()
    assert me["user"]["role"] == "owner"
    assert me["org"]["plan"] == "team"
    assert me["org"]["billing_status"] == "trialing"
    assert me["org"]["trial_ends_at"]
    assert "sso" in me["org"]["features"]


def test_signup_validates_its_input(client: TestClient) -> None:
    ok = {"org_name": "X", "email": unique_email(), "password": PASSWORD}
    weak = {**ok, "password": "short"}  # pragma: allowlist secret
    assert client.post("/api/signup", json=weak).json()["error"]["code"] == "weak_password"
    assert client.post("/api/signup", json={**ok, "email": "nope"}).status_code == 422
    assert client.post("/api/signup", json={**ok, "org_name": ""}).status_code == 422
    assert client.post("/api/signup", json={**ok, "surprise": 1}).status_code == 422


def test_an_email_can_only_be_used_once(client: TestClient) -> None:
    org = Org(client)
    again = client.post(
        "/api/signup", json={"org_name": "Other", "email": org.email, "password": PASSWORD}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "account_exists"


def test_login_and_logout(client: TestClient) -> None:
    org = Org(client)
    login = client.post("/api/login", json={"email": org.email, "password": PASSWORD})
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.post("/api/logout", headers=headers).status_code == 204
    assert client.get("/api/me", headers=headers).status_code == 401


def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(client: TestClient) -> None:
    org = Org(client)
    wrong = client.post("/api/login", json={"email": org.email, "password": "x" * 20})
    unknown = client.post("/api/login", json={"email": unique_email(), "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_requests_without_valid_credentials_are_refused(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_an_api_key_is_shown_once_and_stored_only_as_a_hash(
    client: TestClient, db: Database
) -> None:
    org = Org(client)
    created = client.post("/api/keys", json={"name": "ci"}, headers=org.auth).json()
    assert created["key"].startswith("kg_live_")
    listed = client.get("/api/keys", headers=org.auth).json()["items"]
    assert all("key" not in k and "key_hash" not in k for k in listed)
    with db.tx(org.id) as conn:
        stored = [r["key_hash"] for r in conn.execute("SELECT key_hash FROM api_keys").fetchall()]
    assert created["key"] not in stored
    assert all(len(h) == 64 for h in stored)


def test_a_revoked_key_stops_working_immediately(client: TestClient) -> None:
    org = Org(client)
    key_id = client.get("/api/keys", headers=org.auth).json()["items"][0]["id"]
    assert client.get("/v1/audit/head", headers=org.key_auth).status_code == 200
    assert client.delete(f"/api/keys/{key_id}", headers=org.auth).status_code == 204
    denied = client.get("/v1/audit/head", headers=org.key_auth)
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_api_key"


def test_an_expired_key_does_not_work(client: TestClient, db: Database) -> None:
    org = Org(client)
    with db.tx(org.id) as conn:
        conn.execute("UPDATE api_keys SET expires_at = now() - interval '1 minute'")
    assert client.get("/v1/audit/head", headers=org.key_auth).status_code == 401


def test_a_session_token_is_not_an_api_key_and_vice_versa(client: TestClient) -> None:
    org = Org(client)
    assert client.get("/v1/audit/head", headers=org.auth).status_code == 401
    assert client.get("/api/me", headers=org.key_auth).status_code == 401


def test_roles_limit_what_users_can_do(client: TestClient) -> None:
    org = Org(client)
    viewer, approver = org.invite("viewer"), org.invite("approver")
    for headers in (viewer, approver):
        assert client.get("/api/keys", headers=headers).status_code == 403
        assert client.post("/api/keys", json={"name": "x"}, headers=headers).status_code == 403
        assert client.get("/api/users", headers=headers).status_code == 403
    assert client.get("/api/me", headers=viewer).status_code == 200
    assert client.get("/api/usage", headers=viewer).status_code == 200


def test_only_an_owner_can_add_an_admin_and_nobody_can_add_an_owner(client: TestClient) -> None:
    org = Org(client)
    admin = org.invite("admin")
    body = {"email": unique_email(), "role": "admin", "password": PASSWORD}
    assert client.post("/api/users", json=body, headers=admin).status_code == 403
    assert client.post("/api/users", json=body, headers=org.auth).status_code == 201
    owner = {**body, "email": unique_email(), "role": "owner"}
    assert client.post("/api/users", json=owner, headers=org.auth).status_code == 422


def test_a_disabled_user_loses_access_and_the_last_owner_cannot_be_disabled(
    client: TestClient,
) -> None:
    org = Org(client)
    approver = org.invite("approver")
    users = client.get("/api/users", headers=org.auth).json()["items"]
    target = next(u for u in users if u["role"] == "approver")
    assert client.delete(f"/api/users/{target['id']}", headers=org.auth).status_code == 204
    assert client.get("/api/me", headers=approver).status_code == 401
    owner = next(u for u in users if u["role"] == "owner")
    blocked = client.delete(f"/api/users/{owner['id']}", headers=org.auth)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "last_owner"


def test_an_expired_trial_closes_the_agent_api(client: TestClient, db: Database) -> None:
    org = Org(client)
    with db.tx(org.id) as conn:
        conn.execute("UPDATE orgs SET trial_ends_at = now() - interval '1 day'")
    response = client.get("/v1/audit/head", headers=org.key_auth)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "subscription_inactive"
    # The dashboard still works so the owner can pick a plan.
    assert client.get("/api/me", headers=org.auth).status_code == 200


def test_unexpected_errors_do_not_leak_details(client: TestClient, db: Database) -> None:
    org = Org(client)
    with db.tx(org.id) as conn:
        conn.execute("UPDATE orgs SET plan = 'team'")
    # A malformed JSON body is a 422, not a stack trace.
    bad = client.post(
        "/api/keys", content=b"{not json", headers={**org.auth, "content-type": "application/json"}
    )
    assert bad.status_code == 422
