"""Postgres access. Every query runs inside a transaction that names the org it acts for.

``Database.tx(org_id=...)`` sets ``app.org_id`` for that transaction only (``SET LOCAL``), which is
what the row-level-security policies compare against. ``Database.lookup(...)`` is for the handful
of reads that must happen before a caller is known; it admits exactly one secret-derived row.
"""

from __future__ import annotations

from contextlib import contextmanager
from importlib import resources
from typing import TYPE_CHECKING, Any, Final, cast

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    from collections.abc import Iterator

LOOKUPS: Final = frozenset({"key", "session", "email", "slug", "stripe_customer", "sso_state"})

Row = dict[str, Any]


class Database:
    """A connection pool plus the tenant-scoped transaction helpers."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._pool.wait()

    @contextmanager
    def tx(self, org_id: str | None = None, **lookups: str) -> Iterator[psycopg.Connection[Row]]:
        """A transaction scoped to ``org_id`` (or to one lookup row). Commits on success.

        With neither an org nor a lookup the transaction sees no tenant rows at all.
        """
        unknown = set(lookups) - LOOKUPS
        if unknown:
            raise ValueError(f"unknown lookup: {sorted(unknown)}")
        with self._pool.connection() as conn, conn.transaction():
            if org_id is not None:
                conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))
            for name, value in lookups.items():
                conn.execute(f"SELECT set_config('app.lookup_{name}', %s, true)", (value,))
            yield cast("psycopg.Connection[Row]", conn)

    def close(self) -> None:
        """Close the pool."""
        self._pool.close()


def migrations() -> list[tuple[str, str]]:
    """The migration files, in order, as (name, sql)."""
    root = resources.files("keelgate_cloud") / "migrations"
    files = sorted((p for p in root.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def migrate(owner_dsn: str, *, app_role: str = "kg_app") -> list[str]:
    """Apply pending migrations as the owner role and grant the application role its access.

    Returns the names applied. Safe to run repeatedly.
    """
    applied: list[str] = []
    with psycopg.connect(owner_dsn, autocommit=False) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for name, text in migrations():
            if name in done:
                continue
            role = sql.Identifier(app_role).as_string(conn)
            conn.execute(text.replace("__APP_ROLE__", role).encode())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
        conn.commit()
    return applied
