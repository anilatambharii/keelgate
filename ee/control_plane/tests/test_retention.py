"""Retention sweeping: per-plan windows, batching, tenant isolation, audit never deleted."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from keelgate_cloud import retention
from keelgate_cloud.db import Database
from keelgate_cloud.plans import default_catalogue
from tests.harness import PASSWORD, Org, make_app

NOW = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def client(db: Database) -> TestClient:
    return TestClient(make_app(db))


class Owner:
    """Privileged helper for arranging and inspecting rows with chosen timestamps."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def run(self, org_id: str, query: str, params: tuple[Any, ...] = ()) -> list[Any]:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SELECT set_config('app.org_id', %s, true)", (org_id,))
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    def count(self, org_id: str, table: str) -> int:
        return int(self.run(org_id, f"SELECT count(*) FROM {table}")[0][0])

    def trace(self, org_id: str, age: timedelta, n: int = 1) -> None:
        for _ in range(n):
            self.run(
                org_id,
                "INSERT INTO traces (org_id, trace_id, span_id, name, status, ingested_at) "
                "VALUES (%s, %s, %s, 's', 'OK', %s)",
                (org_id, uuid.uuid4().hex, uuid.uuid4().hex[:16], NOW - age),
            )


@pytest.fixture
def owner(pg_dsns: dict[str, str]) -> Owner:
    return Owner(pg_dsns["owner"])


def with_plan(owner: Owner, org: Org, plan: str) -> None:
    owner.run(org.id, "UPDATE orgs SET plan = %s", (plan,))


def day(n: int) -> timedelta:
    return timedelta(days=n)


def test_traces_follow_each_orgs_plan_window(
    db: Database, owner: Owner, client: TestClient
) -> None:
    team, business, enterprise = Org(client), Org(client), Org(client)
    with_plan(owner, business, "business")
    with_plan(owner, enterprise, "enterprise")
    for org in (team, business, enterprise):
        for age in (day(10), day(40), day(400)):
            owner.trace(org.id, age)
    report = retention.sweep(db, default_catalogue(), now=NOW)
    assert owner.count(team.id, "traces") == 1  # 30-day window keeps only the 10-day-old trace
    assert owner.count(business.id, "traces") == 2  # 365-day window drops the 400-day-old one
    assert owner.count(enterprise.id, "traces") == 3  # null = keep
    assert report["orgs"][team.id]["deleted"]["traces"] == 2
    assert report["orgs"][business.id]["deleted"]["traces"] == 1
    assert report["orgs"][enterprise.id]["deleted"]["traces"] == 0
    assert report["orgs"][team.id]["trace_retention_days"] == 30
    assert report["totals"]["traces"] >= 3
    assert "never deleted" in report["audit"]["note"] and report["audit"]["deleted"] == 0


def test_sweeping_twice_is_a_no_op(db: Database, owner: Owner, client: TestClient) -> None:
    org = Org(client)
    owner.trace(org.id, day(90), n=4)
    assert (
        retention.sweep(db, default_catalogue(), now=NOW)["orgs"][org.id]["deleted"]["traces"] == 4
    )
    again = retention.sweep(db, default_catalogue(), now=NOW)
    assert again["orgs"][org.id]["deleted"] == dict.fromkeys(
        ("traces", "sessions", "sso_states", "idempotency_keys"), 0
    )


def test_batching_deletes_everything_without_one_big_statement(
    db: Database, owner: Owner, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(client)
    owner.trace(org.id, day(90), n=9)  # an exact multiple of the batch
    owner.trace(org.id, day(100), n=1)
    owner.trace(org.id, day(1), n=2)
    statements: list[int] = []
    original = retention._delete_batched

    def spy(*args: Any) -> int:
        deleted = original(*args)
        statements.append(deleted)
        return deleted

    monkeypatch.setattr(retention, "_delete_batched", spy)
    report = retention.sweep(db, default_catalogue(), now=NOW, batch_size=3)
    assert report["orgs"][org.id]["deleted"]["traces"] == 10
    assert owner.count(org.id, "traces") == 2
    # each batch is its own transaction: count the DELETEs for this org's traces
    txs: list[str] = []
    real_tx = db.tx

    def counting_tx(org_id: str | None = None, **kw: str) -> Any:
        txs.append(str(org_id))
        return real_tx(org_id, **kw)

    owner.trace(org.id, day(90), n=7)
    monkeypatch.setattr(db, "tx", counting_tx)
    retention._delete_batched(db, org.id, retention._TRACES, NOW - day(30), 3)
    assert txs.count(org.id) == 3  # 3 + 3 + 1: the short batch ends the loop


def test_sessions_sso_states_and_idempotency_keys(
    db: Database, owner: Owner, client: TestClient
) -> None:
    org = Org(client)
    client.post("/api/login", json={"email": org.email, "password": PASSWORD})  # 2nd session
    total = owner.count(org.id, "sessions")
    assert total == 2
    owner.run(org.id, "UPDATE sessions SET expires_at = %s", (NOW + day(1),))
    owner.run(
        org.id,
        "UPDATE sessions SET expires_at = %s WHERE token_hash = "
        "(SELECT token_hash FROM sessions ORDER BY created_at LIMIT 1)",
        (NOW - timedelta(minutes=1),),
    )
    for name, age in (("old", timedelta(hours=2)), ("fresh", timedelta(minutes=5))):
        owner.run(
            org.id,
            "INSERT INTO sso_states (state, org_id, nonce, code_verifier, created_at) "
            "VALUES (%s, %s, 'n', 'v', %s)",
            (f"{name}-{uuid.uuid4().hex}", org.id, NOW - age),
        )
    for key, age in (("stale", day(8)), ("recent", day(6))):
        owner.run(
            org.id,
            "INSERT INTO idempotency_keys (org_id, scope, key, response, created_at) "
            "VALUES (%s, 's', %s, '{}', %s)",
            (org.id, key, NOW - age),
        )
    report = retention.sweep(db, default_catalogue(), now=NOW)
    deleted = report["orgs"][org.id]["deleted"]
    assert deleted["sessions"] == 1 and deleted["sso_states"] == 1
    assert deleted["idempotency_keys"] == 1
    assert owner.count(org.id, "sessions") == 1
    assert [r[0].split("-")[0] for r in owner.run(org.id, "SELECT state FROM sso_states")] == [
        "fresh"
    ]
    assert [r[0] for r in owner.run(org.id, "SELECT key FROM idempotency_keys")] == ["recent"]


def test_audit_records_are_never_deleted(
    db: Database, owner: Owner, client: TestClient, pg_dsns: dict[str, str]
) -> None:
    org = Org(client)
    with_plan(owner, org, "team")
    for n in range(3):
        client.post(
            "/v1/audit/events",
            json={
                "tenant_id": org.id,
                "event_type": "tool.result",
                "actor": "a",
                "payload_json": f'{{"n":{n}}}',
            },
            headers=org.key_auth,
        )
    far_future = NOW + day(36500)
    report = retention.sweep(db, default_catalogue(), now=far_future)
    assert owner.count(org.id, "audit_records") == 3
    assert report["audit"]["deleted"] == 0
    assert report["orgs"][org.id]["audit_min_retention_days"] == 30
    # and the database would refuse even if the sweeper tried
    with pytest.raises(psycopg.Error), db.tx(org.id) as conn:
        conn.execute("DELETE FROM audit_records")


def test_orgs_are_enumerated_without_opening_other_tenants_to_the_app_role(
    db: Database, client: TestClient
) -> None:
    a, b = Org(client, "Alpha"), Org(client, "Beta")
    listed = dict(retention.list_orgs(db))
    assert {a.id, b.id} <= set(listed)
    assert listed[a.id] == "team"
    with db.tx() as conn:  # no tenant set: tables are still invisible
        assert conn.execute("SELECT count(*) AS n FROM orgs").fetchone() == {"n": 0}
        assert conn.execute("SELECT count(*) AS n FROM users").fetchone() == {"n": 0}
    with db.tx(a.id) as conn:  # the index opens nothing beyond itself
        assert conn.execute("SELECT count(*) AS n FROM orgs").fetchone() == {"n": 1}
        cols = conn.execute("SELECT * FROM org_index LIMIT 1").fetchone()
        assert cols is not None
        assert set(cols) == {"org_id", "plan", "billing_status", "has_subscription"}


def test_an_unknown_plan_deletes_nothing(db: Database, owner: Owner, client: TestClient) -> None:
    org = Org(client)
    with_plan(owner, org, "mystery")
    owner.trace(org.id, day(900), n=2)
    report = retention.sweep(db, default_catalogue(), now=NOW)
    assert "skipped" in report["orgs"][org.id]
    assert owner.count(org.id, "traces") == 2


def test_cli_prints_a_summary_and_exits_zero(
    pg_dsns: dict[str, str],
    client: TestClient,
    owner: Owner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = Org(client)
    monkeypatch.setenv("KEELGATE_CP_DATABASE_URL", pg_dsns["app"])
    monkeypatch.setenv("KEELGATE_CP_SECRET_KEY", "x" * 40)
    assert retention.main(["sweep-retention"]) == 0
    out = capsys.readouterr().out
    assert org.id in out and "traces=" in out and "never deleted" in out
    assert retention.main(["bogus"]) == 2
    from keelgate_cloud.__main__ import main as cli  # noqa: PLC0415

    assert cli(["sweep-retention"]) == 0
