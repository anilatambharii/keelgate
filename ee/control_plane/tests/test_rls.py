"""Row-level security: the database, not the application, keeps tenants apart."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from keelgate_cloud.db import Database

TENANT_TABLES = [
    "orgs", "users", "sessions", "api_keys", "sso_configs", "sso_states", "audit_records",
    "agents", "idempotency_keys", "approvals", "approval_routes", "policy_packs",
    "policy_versions", "policy_active", "usage_daily", "usage_reports", "traces",
]  # fmt: skip


def make_org(db: Database, slug: str | None = None) -> str:
    org_id = str(uuid.uuid4())
    with db.tx(org_id) as conn:
        conn.execute(
            "INSERT INTO orgs (id, name, slug) VALUES (%s, %s, %s)",
            (org_id, "Org", slug or f"org-{org_id[:8]}"),
        )
    return org_id


def seed(db: Database, org_id: str) -> dict[str, Any]:
    """One row in every tenant table that the app role may write."""
    user_id, key_id = str(uuid.uuid4()), str(uuid.uuid4())
    email = f"u-{user_id[:8]}@example.com"
    key_hash = f"hash-{key_id}"
    session_hash = f"sess-{user_id}"
    with db.tx(org_id) as conn:
        conn.execute(
            "INSERT INTO users (id, org_id, email, role) VALUES (%s, %s, %s, 'owner')",
            (user_id, org_id, email),
        )
        conn.execute(
            "INSERT INTO api_keys (id, org_id, name, key_prefix, key_hash) "
            "VALUES (%s, %s, 'k', 'kg_live_abcd', %s)",
            (key_id, org_id, key_hash),
        )
        conn.execute(
            "INSERT INTO sessions (token_hash, org_id, user_id, expires_at) "
            "VALUES (%s, %s, %s, now() + interval '1 hour')",
            (session_hash, org_id, user_id),
        )
        conn.execute(
            "INSERT INTO audit_records (org_id, seq, timestamp, event_type, actor, payload_json,"
            " prev_hash, hash) VALUES (%s, 1, 't', 'e', 'a', '{}', 'p', 'h')",
            (org_id,),
        )
        conn.execute("INSERT INTO agents (org_id, agent_id) VALUES (%s, 'agent-1')", (org_id,))
        conn.execute(
            "INSERT INTO approvals (request_id, org_id, status, expires_at, body) "
            "VALUES (%s, %s, 'PENDING', now() + interval '1 hour', '{}')",
            (f"req-{org_id}", org_id),
        )
        conn.execute(
            "INSERT INTO usage_daily (org_id, day, metric, quantity) "
            "VALUES (%s, current_date, 'gated_actions', 5)",
            (org_id,),
        )
        conn.execute(
            "INSERT INTO traces (org_id, trace_id, span_id, name, status) "
            "VALUES (%s, 't1', 's1', 'n', 'OK')",
            (org_id,),
        )
    return {"user": user_id, "email": email, "key_hash": key_hash, "session": session_hash}


@pytest.fixture(scope="module")
def two_orgs(db: Database) -> dict[str, Any]:
    a, b = make_org(db), make_org(db)
    return {"a": a, "b": b, "a_rows": seed(db, a), "b_rows": seed(db, b)}


def test_the_application_role_has_no_way_around_row_level_security(db: Database) -> None:
    with db.tx() as conn:
        role = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert role == {"rolsuper": False, "rolbypassrls": False}
        owned = conn.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public' "
            "AND tableowner = current_user"
        ).fetchone()
        assert owned == {"n": 0}


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_every_tenant_table_has_forced_row_level_security(db: Database, table: str) -> None:
    with db.tx() as conn:
        row = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", (table,)
        ).fetchone()
    assert row == {"relrowsecurity": True, "relforcerowsecurity": True}


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_a_transaction_that_names_no_org_sees_nothing(
    db: Database, two_orgs: dict[str, Any], table: str
) -> None:
    with db.tx() as conn:
        assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone() == {"n": 0}


@pytest.mark.parametrize(
    "table",
    [
        "users",
        "api_keys",
        "sessions",
        "audit_records",
        "agents",
        "approvals",
        "usage_daily",
        "traces",
    ],
)
def test_an_org_sees_only_its_own_rows(db: Database, two_orgs: dict[str, Any], table: str) -> None:
    for mine, theirs in (("a", "b"), ("b", "a")):
        with db.tx(two_orgs[mine]) as conn:
            rows = conn.execute(f"SELECT org_id FROM {table}").fetchall()
        assert rows, "seed data is missing"
        assert {str(r["org_id"]) for r in rows} == {two_orgs[mine]}
        assert two_orgs[theirs] not in {str(r["org_id"]) for r in rows}


def test_an_org_cannot_see_another_org_by_id(db: Database, two_orgs: dict[str, Any]) -> None:
    with db.tx(two_orgs["a"]) as conn:
        assert conn.execute("SELECT 1 FROM orgs WHERE id = %s", (two_orgs["b"],)).fetchall() == []
        assert (
            conn.execute(
                "SELECT 1 FROM users WHERE id = %s", (two_orgs["b_rows"]["user"],)
            ).fetchall()
            == []
        )


def test_an_org_cannot_modify_or_delete_another_orgs_rows(
    db: Database, two_orgs: dict[str, Any]
) -> None:
    with db.tx(two_orgs["a"]) as conn:
        updated = conn.execute(
            "UPDATE users SET display_name = 'pwned' WHERE id = %s", (two_orgs["b_rows"]["user"],)
        )
        assert updated.rowcount == 0
        deleted = conn.execute(
            "DELETE FROM api_keys WHERE id IS NOT NULL AND org_id = %s", (two_orgs["b"],)
        )
        assert deleted.rowcount == 0
    with db.tx(two_orgs["b"]) as conn:
        row = conn.execute("SELECT display_name FROM users").fetchone()
        assert row == {"display_name": ""}


@pytest.mark.parametrize("table", ["agents", "usage_daily", "approval_routes", "policy_packs"])
def test_an_org_cannot_write_a_row_into_another_org(
    db: Database, two_orgs: dict[str, Any], table: str
) -> None:
    inserts = {
        "agents": ("INSERT INTO agents (org_id, agent_id) VALUES (%s, 'x')", 1),
        "usage_daily": (
            "INSERT INTO usage_daily (org_id, day, metric, quantity) "
            "VALUES (%s, current_date, 'approvals', 1)",
            1,
        ),
        "approval_routes": ("INSERT INTO approval_routes (org_id) VALUES (%s)", 1),
        "policy_packs": ("INSERT INTO policy_packs (org_id, name) VALUES (%s, 'p')", 1),
    }
    statement, _ = inserts[table]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.tx(two_orgs["a"]) as conn:
        conn.execute(statement, (two_orgs["b"],))


def test_an_org_cannot_move_its_own_row_into_another_org(
    db: Database, two_orgs: dict[str, Any]
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.tx(two_orgs["a"]) as conn:
        conn.execute("UPDATE agents SET org_id = %s", (two_orgs["b"],))


def test_an_org_row_cannot_be_inserted_under_another_orgs_context(db: Database) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.tx(str(uuid.uuid4())) as conn:
        conn.execute(
            "INSERT INTO orgs (id, name, slug) VALUES (%s, 'x', 'sneaky-org')", (uuid.uuid4(),)
        )


# --------------------------------------------------------------------------- append-only audit


def test_the_application_cannot_change_or_remove_audit_records(
    db: Database, two_orgs: dict[str, Any]
) -> None:
    for statement in (
        "UPDATE audit_records SET actor = 'x'",
        "DELETE FROM audit_records",
        "TRUNCATE audit_records",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.tx(two_orgs["a"]) as conn:
            conn.execute(statement)
    with db.tx(two_orgs["a"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit_records").fetchone() == {"n": 1}


def test_even_the_owner_cannot_rewrite_the_audit_log(
    pg_dsns: dict[str, str], two_orgs: dict[str, Any]
) -> None:
    """The append-only trigger holds for the table owner too (defence in depth)."""
    with psycopg.connect(pg_dsns["owner"]) as conn:
        conn.execute("SELECT set_config('app.org_id', %s, false)", (two_orgs["a"],))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE audit_records SET actor = 'x'")
        conn.rollback()
        conn.execute("SELECT set_config('app.org_id', %s, false)", (two_orgs["a"],))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM audit_records")


def test_the_owner_without_an_org_context_also_sees_nothing(pg_dsns: dict[str, str]) -> None:
    """FORCE ROW LEVEL SECURITY: ownership is not a way around the policy."""
    with psycopg.connect(pg_dsns["owner"]) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (0,)


# ------------------------------------------------------------------------------ pre-auth lookups


def test_a_key_lookup_finds_exactly_one_row(db: Database, two_orgs: dict[str, Any]) -> None:
    with db.tx(key=two_orgs["a_rows"]["key_hash"]) as conn:
        rows = conn.execute("SELECT org_id FROM api_keys").fetchall()
    assert [str(r["org_id"]) for r in rows] == [two_orgs["a"]]


def test_a_lookup_reveals_nothing_else(db: Database, two_orgs: dict[str, Any]) -> None:
    with db.tx(key=two_orgs["a_rows"]["key_hash"]) as conn:
        for table in ("users", "audit_records", "approvals", "traces", "agents", "sessions"):
            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone() == {"n": 0}


def test_a_lookup_for_an_unknown_secret_finds_nothing(db: Database) -> None:
    with db.tx(key="not-a-key-hash") as conn:
        assert conn.execute("SELECT count(*) AS n FROM api_keys").fetchone() == {"n": 0}


def test_session_email_and_slug_lookups_each_find_one_row(
    db: Database, two_orgs: dict[str, Any]
) -> None:
    rows = two_orgs["a_rows"]
    with db.tx(session=rows["session"]) as conn:
        assert [
            str(r["org_id"]) for r in conn.execute("SELECT org_id FROM sessions").fetchall()
        ] == [two_orgs["a"]]
    with db.tx(email=rows["email"]) as conn:
        assert [str(r["org_id"]) for r in conn.execute("SELECT org_id FROM users").fetchall()] == [
            two_orgs["a"]
        ]
    with db.tx(slug=f"org-{two_orgs['a'][:8]}") as conn:
        assert [str(r["id"]) for r in conn.execute("SELECT id FROM orgs").fetchall()] == [
            two_orgs["a"]
        ]


def test_a_lookup_cannot_write(db: Database, two_orgs: dict[str, Any]) -> None:
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        db.tx(key=two_orgs["a_rows"]["key_hash"]) as conn,
    ):
        conn.execute("UPDATE api_keys SET revoked_at = now()")


def test_an_unknown_lookup_name_is_refused(db: Database) -> None:
    with pytest.raises(ValueError), db.tx(everything="1"):
        pass


def test_the_org_context_does_not_leak_to_the_next_transaction_on_a_reused_connection(
    pg_dsns: dict[str, str], two_orgs: dict[str, Any]
) -> None:
    single = Database(pg_dsns["app"], min_size=1, max_size=1)
    try:
        with single.tx(two_orgs["a"]) as conn:
            assert conn.execute("SELECT count(*) AS n FROM users").fetchone() == {"n": 1}
        with single.tx() as conn:  # same physical connection, no org named
            assert conn.execute("SELECT count(*) AS n FROM users").fetchone() == {"n": 0}
    finally:
        single.close()
