"""Durable SQLite implementations of the grant-side state.

The in-memory ledger and revocation list lose their contents when the process dies,
which is wrong for anything that must survive a kill and resume: a restarted agent
would see a fresh budget and could spend a grant twice. These keep the same protocols
and the same atomicity, so they are drop-in replacements.

One file may be shared by several processes on a host: writers take an immediate
transaction, so a reservation is checked and applied as one step.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_spend (
    grant_id TEXT PRIMARY KEY,
    spent    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS revoked_grants (
    grant_id TEXT PRIMARY KEY
);
"""


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(_SCHEMA)
    return conn


class SqliteBudgetLedger:
    """Atomic per-grant spend, persisted. Implements :class:`BudgetLedger`."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = _connect(path)
        self._lock = threading.RLock()

    def try_reserve(self, grant_id: str, amount: float, limit: float) -> bool:
        """Spend ``amount`` against ``limit`` in one transaction; False if it would exceed."""
        if amount < 0 or math.isnan(amount):
            return False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT spent FROM budget_spend WHERE grant_id = ?", (grant_id,)
                ).fetchone()
                current = row[0] if row else 0.0
                if current + amount > limit:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute(
                    "INSERT INTO budget_spend (grant_id, spent) VALUES (?, ?) "
                    "ON CONFLICT(grant_id) DO UPDATE SET spent = excluded.spent",
                    (grant_id, current + amount),
                )
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def release(self, grant_id: str, amount: float) -> None:
        """Return a reservation for an action that never executed."""
        with self._lock:
            self._conn.execute(
                "UPDATE budget_spend SET spent = MAX(0.0, spent - ?) WHERE grant_id = ?",
                (amount, grant_id),
            )

    def spent(self, grant_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT spent FROM budget_spend WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteRevocationList:
    """Persisted revocations. Implements :class:`RevocationList`."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = _connect(path)
        self._lock = threading.RLock()

    def revoke(self, grant_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO revoked_grants (grant_id) VALUES (?)", (grant_id,)
            )

    def is_revoked(self, grant_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM revoked_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
