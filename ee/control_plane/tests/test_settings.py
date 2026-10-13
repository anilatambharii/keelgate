"""Per-org settings and the residency label."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from keelgate.audit import AuditRecord, verify_chain

from keelgate_cloud.db import Database
from tests.harness import Org, make_app


@pytest.fixture(scope="module")
def client(db: Database) -> TestClient:
    return TestClient(make_app(db))


def test_every_member_reads_settings(client: TestClient) -> None:
    org = Org(client, "Globex")
    for headers in (org.auth, org.invite("viewer"), org.invite("approver"), org.invite("admin")):
        body = client.get("/api/settings", headers=headers).json()
        assert body["name"] == "Globex" and body["plan"] == "team" and body["residency"] == "us"
        assert body["slug"].startswith("globex-")
        assert body["retention"] == {
            "trace_retention_days": 30,
            "audit_min_retention_days": 30,
            "audit_immutable": False,
        }
        assert body["features"] == ["sso"]
        assert body["residency_enforced"] is False and "single region" in body["residency_note"]
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers=org.key_auth).status_code == 401


def test_regions_list(client: TestClient) -> None:
    org = Org(client)
    body = client.get("/api/settings/residency-regions", headers=org.invite("viewer")).json()
    assert [r["id"] for r in body["regions"]] == ["us", "eu", "apac"]
    assert all(r["label"] for r in body["regions"]) and body["enforced"] is False
    assert client.get("/api/settings/residency-regions").status_code == 401


def test_owner_changes_residency_and_name_and_it_is_audited(client: TestClient) -> None:
    org = Org(client, "Initech")
    response = client.patch(
        "/api/settings", json={"residency": "eu", "name": "  Initech EU  "}, headers=org.auth
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["residency"] == "eu" and body["name"] == "Initech EU"
    assert body["residency_enforced"] is False
    assert client.get("/api/me", headers=org.auth).json()["org"]["residency"] == "eu"
    events = client.get("/api/audit?event_type=settings.changed", headers=org.auth).json()
    assert len(events["records"]) == 1
    event = events["records"][0]
    assert event["actor"].startswith("user:")
    assert event["payload"] == {
        "changes": {
            "name": {"old": "Initech", "new": "Initech EU"},
            "residency": {"old": "us", "new": "eu"},
        }
    }
    # the chain stays valid
    records = client.get("/api/audit/export", headers=org.auth).json()["records"]
    assert verify_chain([AuditRecord(**r) for r in records], tenant_id=org.id)


def test_a_no_op_change_records_nothing(client: TestClient) -> None:
    org = Org(client)
    assert (
        client.patch("/api/settings", json={"residency": "us"}, headers=org.auth).status_code == 200
    )
    assert (
        client.get("/api/audit?event_type=settings.changed", headers=org.auth).json()["records"]
        == []
    )


def test_only_the_owner_may_change_settings(client: TestClient) -> None:
    org = Org(client)
    for role in ("admin", "approver", "viewer"):
        response = client.patch(
            "/api/settings", json={"residency": "apac"}, headers=org.invite(role)
        )
        assert response.status_code == 403, role
    assert client.patch("/api/settings", json={"residency": "apac"}).status_code == 401
    assert client.get("/api/settings", headers=org.auth).json()["residency"] == "us"
    assert (
        client.get("/api/audit?event_type=settings.changed", headers=org.auth).json()["records"]
        == []
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"residency": "mars"},
        {"residency": "EU"},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"plan": "enterprise"},
        {"features": ["sso"]},
        {"name": None, "residency": None},
    ],
)
def test_bad_input_is_refused(client: TestClient, body: dict[str, object]) -> None:
    org = Org(client)
    assert client.patch("/api/settings", json=body, headers=org.auth).status_code == 422
    after = client.get("/api/settings", headers=org.auth).json()
    assert after["plan"] == "team" and after["residency"] == "us" and after["name"] == "Acme"


def test_settings_are_per_org(client: TestClient) -> None:
    a, b = Org(client, "A"), Org(client, "B")
    client.patch("/api/settings", json={"residency": "apac", "name": "A2"}, headers=a.auth)
    seen = client.get("/api/settings", headers=b.auth).json()
    assert seen["residency"] == "us" and seen["name"] == "B"
    assert (
        client.get("/api/audit?event_type=settings.changed", headers=b.auth).json()["records"] == []
    )
    # a client cannot forge the event type through the agent API
    forged = client.post(
        "/v1/audit/events",
        json={
            "tenant_id": b.id,
            "event_type": "settings.changed",
            "actor": "x",
            "payload_json": "{}",
        },
        headers=b.key_auth,
    )
    assert forged.status_code == 422
