"""Audit storage backends.

The append path is atomic per tenant: read the chain head and insert the next
record inside one transaction, so two writers cannot both claim the same
sequence number. Both backends also install triggers that reject ``UPDATE`` and
``DELETE``. Those stop accidents and casual tampering through the application
connection; they do **not** stop someone with DDL rights, which is what the hash
chain and an externally anchored head are for.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, Protocol

from keelgate.audit._records import AuditError, AuditRecord, ChainHead, genesis_hash

if TYPE_CHECKING:
    from pathlib import Path

BuildRecord = Callable[[int, str, str | None], AuditRecord]


class AuditStore(Protocol):
    def append(self, tenant_id: str, build: BuildRecord) -> AuditRecord:
        """Atomically build and persist the tenant's next record.

        ``build(next_seq, prev_hash, prev_timestamp)`` runs inside the critical
        section, so the record is stamped in the same order it is chained.
        """
        ...

    def iter_records(self, tenant_id: str, *, after_seq: int = 0) -> Iterator[AuditRecord]: ...

    def head(self, tenant_id: str) -> ChainHead | None: ...

    def close(self) -> None: ...


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    tenant_id    TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    timestamp    TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    prev_hash    TEXT    NOT NULL,
    hash         TEXT    NOT NULL,
    PRIMARY KEY (tenant_id, seq)
);
CREATE TRIGGER IF NOT EXISTS audit_records_no_update
BEFORE UPDATE ON audit_records
BEGIN SELECT RAISE(ABORT, 'audit_records is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_records_no_delete
BEFORE DELETE ON audit_records
BEGIN SELECT RAISE(ABORT, 'audit_records is append-only'); END;
"""

_SQLITE_INSERT = (
    "INSERT INTO audit_records (tenant_id, seq, timestamp, event_type, actor, payload_json, "
    "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)"
)
_SQLITE_SELECT = (
    "SELECT tenant_id, seq, timestamp, event_type, actor, payload_json, prev_hash, hash "
    "FROM audit_records WHERE tenant_id = ? AND seq > ? ORDER BY seq ASC"
)
_PG_INSERT = (
    "INSERT INTO audit_records (tenant_id, seq, timestamp, event_type, actor, payload_json, "
    "prev_hash, hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
)
_PG_SELECT = (
    "SELECT tenant_id, seq, timestamp, event_type, actor, payload_json, prev_hash, hash "
    "FROM audit_records WHERE tenant_id = %s AND seq > %s ORDER BY seq ASC"
)


def _row_to_record(row: tuple[Any, ...]) -> AuditRecord:
    return AuditRecord(
        tenant_id=row[0],
        seq=row[1],
        timestamp=row[2],
        event_type=row[3],
        actor=row[4],
        payload_json=row[5],
        prev_hash=row[6],
        hash=row[7],
    )


class SqliteAuditStore:
    """Local, single-file audit store. Use ``":memory:"`` for tests."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        # isolation_level=None: we manage transactions explicitly.
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SQLITE_SCHEMA)

    def append(self, tenant_id: str, build: BuildRecord) -> AuditRecord:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT seq, hash, timestamp FROM audit_records WHERE tenant_id = ? "
                    "ORDER BY seq DESC LIMIT 1",
                    (tenant_id,),
                ).fetchone()
                seq, prev, prev_ts = (
                    (row[0] + 1, row[1], row[2]) if row else (1, genesis_hash(tenant_id), None)
                )
                record = build(seq, prev, prev_ts)
                if record.tenant_id != tenant_id or record.seq != seq or record.prev_hash != prev:
                    raise AuditError("built record does not extend the chain head")
                self._conn.execute(
                    _SQLITE_INSERT,
                    (
                        record.tenant_id,
                        record.seq,
                        record.timestamp,
                        record.event_type,
                        record.actor,
                        record.payload_json,
                        record.prev_hash,
                        record.hash,
                    ),
                )
                self._conn.execute("COMMIT")
                return record
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def iter_records(self, tenant_id: str, *, after_seq: int = 0) -> Iterator[AuditRecord]:
        with self._lock:
            rows = self._conn.execute(
                _SQLITE_SELECT,
                (tenant_id, after_seq),
            ).fetchall()
        for row in rows:
            yield _row_to_record(row)

    def head(self, tenant_id: str) -> ChainHead | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, hash FROM audit_records WHERE tenant_id = ? ORDER BY seq DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return ChainHead(tenant_id, row[0], row[1]) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    tenant_id    TEXT    NOT NULL,
    seq          BIGINT  NOT NULL,
    timestamp    TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    prev_hash    TEXT    NOT NULL,
    hash         TEXT    NOT NULL,
    PRIMARY KEY (tenant_id, seq)
);
CREATE OR REPLACE FUNCTION audit_records_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_records is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_records_no_change ON audit_records;
CREATE TRIGGER audit_records_no_change
BEFORE UPDATE OR DELETE ON audit_records
FOR EACH ROW EXECUTE FUNCTION audit_records_append_only();
"""


class PostgresAuditStore:
    """Shared audit store for multi-process deployments.

    Payloads are ``TEXT``, not ``JSONB``: JSONB normalises key order and number
    formatting, which would break the byte-exact hash. Appends serialise per
    tenant with a transaction-scoped advisory lock.
    """

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg  # noqa: PLC0415 - optional dependency, imported lazily
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise AuditError(
                "PostgresAuditStore needs psycopg: pip install 'keelgate[server]'"
            ) from exc
        self._psycopg = psycopg
        self._dsn = dsn
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(_POSTGRES_SCHEMA)

    def append(self, tenant_id: str, build: BuildRecord) -> AuditRecord:
        with self._psycopg.connect(self._dsn) as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,))
            row = conn.execute(
                "SELECT seq, hash, timestamp FROM audit_records WHERE tenant_id = %s "
                "ORDER BY seq DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            seq, prev, prev_ts = (
                (row[0] + 1, row[1], row[2]) if row else (1, genesis_hash(tenant_id), None)
            )
            record = build(seq, prev, prev_ts)
            if record.tenant_id != tenant_id or record.seq != seq or record.prev_hash != prev:
                raise AuditError("built record does not extend the chain head")
            conn.execute(
                _PG_INSERT,
                (
                    record.tenant_id,
                    record.seq,
                    record.timestamp,
                    record.event_type,
                    record.actor,
                    record.payload_json,
                    record.prev_hash,
                    record.hash,
                ),
            )
            return record

    def iter_records(self, tenant_id: str, *, after_seq: int = 0) -> Iterator[AuditRecord]:
        with self._psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                _PG_SELECT,
                (tenant_id, after_seq),
            ).fetchall()
        for row in rows:
            yield _row_to_record(row)

    def head(self, tenant_id: str) -> ChainHead | None:
        with self._psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT seq, hash FROM audit_records WHERE tenant_id = %s "
                "ORDER BY seq DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return ChainHead(tenant_id, row[0], row[1]) if row else None

    def close(self) -> None:
        """Connections are per-operation; nothing is held open."""
