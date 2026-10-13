"""Fixtures: a throwaway Postgres database, migrated as the owner and used as the app role.

Needs the dev Postgres (``make up`` in the repo root). Tests that need it fail loudly, not skip,
when ``KEELGATE_REQUIRE_INTEGRATION=1``; otherwise they skip with a reason.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql

from keelgate_cloud.db import Database, migrate

ADMIN_DSN = os.environ.get(
    "KEELGATE_TEST_PG_ADMIN_DSN",
    "postgresql://keelgate:keelgate@localhost:5432/postgres",  # pragma: allowlist secret
)
OWNER_ROLE = "kgcp_test_owner"
APP_ROLE = "kgcp_test_app"
ROLE_PASSWORD = "kgcp-test-only"  # pragma: allowlist secret


def _dsn(user: str, database: str) -> str:
    base = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
    return psycopg.conninfo.make_conninfo(
        user=user,
        password=ROLE_PASSWORD,
        dbname=database,
        host=base.get("host"),
        port=base.get("port"),
    )


def _ensure_role(admin: psycopg.Connection[Any], name: str) -> None:
    exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
    if exists is None:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB").format(
                sql.Identifier(name), sql.Literal(ROLE_PASSWORD)
            )
        )


@pytest.fixture(scope="session")
def pg_dsns() -> Iterator[dict[str, str]]:
    """A fresh database with the schema applied; yields the owner and app DSNs."""
    database = f"kgcp_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        if os.environ.get("KEELGATE_REQUIRE_INTEGRATION") == "1":
            raise
        pytest.skip(f"no Postgres for the control-plane tests: {exc}")
    with admin:
        _ensure_role(admin, OWNER_ROLE)
        _ensure_role(admin, APP_ROLE)
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(OWNER_ROLE)
            )
        )
    owner_dsn = _dsn(OWNER_ROLE, database)
    migrate(owner_dsn, app_role=APP_ROLE)
    try:
        yield {"owner": owner_dsn, "app": _dsn(APP_ROLE, database), "database": database}
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )


@pytest.fixture(scope="session")
def db(pg_dsns: dict[str, str]) -> Iterator[Database]:
    """The application's pool: connects as the unprivileged role, so RLS applies."""
    database = Database(pg_dsns["app"])
    yield database
    database.close()
