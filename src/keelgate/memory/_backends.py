"""Storage backends for memory: SQLite for local use, Postgres + pgvector for shared use.

Both store **every version as its own row** and never update or delete (triggers enforce
it). A read at ``as_of`` takes, per record, the latest version *recorded* by then, drops it
if that version is a tombstone, and keeps it only if the version is also valid, and has
happened, by then. Tenants are separated in every query; there is no query without one.

Timestamps are stored as fixed-width UTC text in SQLite (so string order is time order) and
as ``timestamptz`` in Postgres.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from keelgate.memory._embedder import cosine
from keelgate.memory._types import (
    Attribution,
    ConcurrentWriteError,
    MemoryRecord,
    MemoryStoreError,
    MemoryTier,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class MemoryBackend(Protocol):
    """Storage for memory records.

    Append-only and bitemporal: a write is a new version, never an edit, and a read honours both
    when a fact was true and when it was recorded.
    """

    def append(self, record: MemoryRecord, embedding: Sequence[float] | None) -> None: ...

    def latest(self, tenant_id: str, record_id: str) -> MemoryRecord | None:
        """The newest version regardless of time. Writers use this; readers use ``as_of``."""
        ...

    def versions(
        self, tenant_id: str, record_id: str, *, as_of: datetime
    ) -> list[MemoryRecord]: ...

    def visible(
        self,
        tenant_id: str,
        tier: MemoryTier,
        *,
        as_of: datetime,
        run_id: str | None = None,
        key: str | None = None,
    ) -> list[MemoryRecord]: ...

    def nearest(
        self,
        tenant_id: str,
        tier: MemoryTier,
        embedding: Sequence[float],
        *,
        as_of: datetime,
        limit: int,
    ) -> list[tuple[MemoryRecord, float]]: ...

    def close(self) -> None: ...


_COLUMNS = (
    "tenant_id, record_id, version, tier, key, content, data_json, recorded_at, occurred_at, "
    "valid_from, valid_to, agent_id, trace_id, run_id, retired"
)

_INSERT_FIELDS = (
    "tenant_id, record_id, version, tier, key, content, data_json, recorded_at, "
    "occurred_at, valid_from, valid_to, agent_id, trace_id, run_id, retired"
)
_INSERT_SQLITE = (
    f"INSERT INTO memory_records ({_INSERT_FIELDS}, embedding_json) "  # noqa: S608
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_INSERT_PG = (
    f"INSERT INTO memory_records ({_INSERT_FIELDS}, embedding) "  # noqa: S608
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)"
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_record(row: tuple[Any, ...]) -> MemoryRecord:
    return MemoryRecord(
        tenant_id=row[0],
        record_id=row[1],
        version=row[2],
        tier=MemoryTier(row[3]),
        key=row[4],
        content=row[5],
        data=json.loads(row[6]),
        recorded_at=_parse(row[7]),  # type: ignore[arg-type]
        occurred_at=_parse(row[8]),
        valid_from=_parse(row[9]),
        valid_to=_parse(row[10]),
        attribution=Attribution(agent_id=row[11], trace_id=row[12]),
        run_id=row[13],
        retired=bool(row[14]),
    )


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_records (
    tenant_id      TEXT    NOT NULL,
    record_id      TEXT    NOT NULL,
    version        INTEGER NOT NULL,
    tier           TEXT    NOT NULL,
    key            TEXT    NOT NULL,
    content        TEXT    NOT NULL,
    data_json      TEXT    NOT NULL,
    recorded_at    TEXT    NOT NULL,
    occurred_at    TEXT,
    valid_from     TEXT,
    valid_to       TEXT,
    agent_id       TEXT    NOT NULL,
    trace_id       TEXT    NOT NULL,
    run_id         TEXT,
    retired        INTEGER NOT NULL DEFAULT 0,
    embedding_json TEXT,
    PRIMARY KEY (tenant_id, record_id, version)
);
CREATE INDEX IF NOT EXISTS memory_tier_time ON memory_records (tenant_id, tier, recorded_at);
CREATE TRIGGER IF NOT EXISTS memory_records_no_update
BEFORE UPDATE ON memory_records
BEGIN SELECT RAISE(ABORT, 'memory_records is append-only'); END;
CREATE TRIGGER IF NOT EXISTS memory_records_no_delete
BEFORE DELETE ON memory_records
BEGIN SELECT RAISE(ABORT, 'memory_records is append-only'); END;
"""

_SQLITE_VISIBLE = f"""
SELECT {_COLUMNS}, embedding_json FROM memory_records m
WHERE tenant_id = ? AND tier = ? AND recorded_at <= ?
  AND version = (
      SELECT MAX(version) FROM memory_records s
      WHERE s.tenant_id = m.tenant_id AND s.record_id = m.record_id AND s.recorded_at <= ?)
  AND retired = 0
  AND (valid_from IS NULL OR valid_from <= ?)
  AND (valid_to IS NULL OR ? < valid_to)
  AND (occurred_at IS NULL OR occurred_at <= ?)
"""  # noqa: S608 - the interpolated text is a constant column list, never input


class SqliteMemoryBackend:
    """SQLite storage for memory: append-only, bitemporal, tenant-scoped, with exact vector search.

    Good for local use and tests.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SQLITE_SCHEMA)
        self._lock = threading.RLock()

    def append(self, record: MemoryRecord, embedding: Sequence[float] | None) -> None:
        values = (
            record.tenant_id,
            record.record_id,
            record.version,
            record.tier.value,
            record.key,
            record.content,
            json.dumps(record.data, sort_keys=True, allow_nan=False),
            _iso(record.recorded_at),
            _iso(record.occurred_at),
            _iso(record.valid_from),
            _iso(record.valid_to),
            record.attribution.agent_id,
            record.attribution.trace_id,
            record.run_id,
            int(record.retired),
            json.dumps(list(embedding)) if embedding is not None else None,
        )
        with self._lock:
            try:
                self._conn.execute(_INSERT_SQLITE, values)
            except sqlite3.IntegrityError as exc:
                raise ConcurrentWriteError(
                    f"{record.record_id} version {record.version} already exists"
                ) from exc

    def latest(self, tenant_id: str, record_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM memory_records WHERE tenant_id = ? AND record_id = ? "  # noqa: S608
                "ORDER BY version DESC LIMIT 1",
                (tenant_id, record_id),
            ).fetchone()
        return _row_to_record(row) if row else None

    def versions(self, tenant_id: str, record_id: str, *, as_of: datetime) -> list[MemoryRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM memory_records WHERE tenant_id = ? AND record_id = ? "  # noqa: S608
                "AND recorded_at <= ? ORDER BY version",
                (tenant_id, record_id, _iso(as_of)),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def _visible_rows(
        self, tenant_id: str, tier: MemoryTier, as_of: datetime, run_id: str | None, key: str | None
    ) -> list[tuple[Any, ...]]:
        stamp = _iso(as_of)
        sql = _SQLITE_VISIBLE
        params: list[Any] = [tenant_id, tier.value, stamp, stamp, stamp, stamp, stamp]
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if key is not None:
            sql += " AND key = ?"
            params.append(key)
        sql += " ORDER BY recorded_at, record_id"
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def visible(
        self,
        tenant_id: str,
        tier: MemoryTier,
        *,
        as_of: datetime,
        run_id: str | None = None,
        key: str | None = None,
    ) -> list[MemoryRecord]:
        return [_row_to_record(r) for r in self._visible_rows(tenant_id, tier, as_of, run_id, key)]

    def nearest(
        self,
        tenant_id: str,
        tier: MemoryTier,
        embedding: Sequence[float],
        *,
        as_of: datetime,
        limit: int,
    ) -> list[tuple[MemoryRecord, float]]:
        scored: list[tuple[MemoryRecord, float]] = []
        for row in self._visible_rows(tenant_id, tier, as_of, None, None):
            if row[-1] is None:
                continue
            scored.append((_row_to_record(row), cosine(embedding, json.loads(row[-1]))))
        scored.sort(key=lambda pair: (-pair[1], pair[0].record_id))
        return scored[:limit]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresMemoryBackend:
    """Shared memory on Postgres with the ``pgvector`` extension.

    Needs ``psycopg`` (extra ``keelgate[server]``) and a database where
    ``CREATE EXTENSION vector`` has been run, which the dev stack does on first start.
    The Python ``pgvector`` package is not needed: vectors travel as text and are cast.
    """

    def __init__(self, dsn: str, *, dim: int) -> None:
        try:
            import psycopg  # noqa: PLC0415 - optional dependency, imported lazily
            from psycopg import sql  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise MemoryStoreError(
                "PostgresMemoryBackend needs psycopg: pip install 'keelgate[server]'"
            ) from exc
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._psycopg = psycopg
        self._dsn = dsn
        self._dim = dim
        ddl = sql.SQL(
            """
            CREATE EXTENSION IF NOT EXISTS vector;
            CREATE TABLE IF NOT EXISTS memory_records (
                tenant_id   TEXT NOT NULL,
                record_id   TEXT NOT NULL,
                version     INTEGER NOT NULL,
                tier        TEXT NOT NULL,
                key         TEXT NOT NULL,
                content     TEXT NOT NULL,
                data_json   TEXT NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL,
                occurred_at TIMESTAMPTZ,
                valid_from  TIMESTAMPTZ,
                valid_to    TIMESTAMPTZ,
                agent_id    TEXT NOT NULL,
                trace_id    TEXT NOT NULL,
                run_id      TEXT,
                retired     BOOLEAN NOT NULL DEFAULT FALSE,
                embedding   vector({dim}),
                PRIMARY KEY (tenant_id, record_id, version)
            );
            CREATE INDEX IF NOT EXISTS memory_tier_time
                ON memory_records (tenant_id, tier, recorded_at);
            CREATE OR REPLACE FUNCTION memory_records_append_only() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'memory_records is append-only'; END;
            $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS memory_records_no_change ON memory_records;
            CREATE TRIGGER memory_records_no_change
                BEFORE UPDATE OR DELETE ON memory_records
                FOR EACH ROW EXECUTE FUNCTION memory_records_append_only();
            """
        ).format(dim=sql.SQL(str(int(dim))))
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(ddl)

    _VISIBLE = f"""
        SELECT {_COLUMNS}, embedding::text FROM memory_records m
        WHERE tenant_id = %s AND tier = %s AND recorded_at <= %s
          AND version = (
              SELECT MAX(version) FROM memory_records s
              WHERE s.tenant_id = m.tenant_id AND s.record_id = m.record_id
                AND s.recorded_at <= %s)
          AND retired = FALSE
          AND (valid_from IS NULL OR valid_from <= %s)
          AND (valid_to IS NULL OR %s < valid_to)
          AND (occurred_at IS NULL OR occurred_at <= %s)
    """  # noqa: S608 - constant column list

    @staticmethod
    def _vec(embedding: Sequence[float]) -> str:
        return "[" + ",".join(repr(float(x)) for x in embedding) + "]"

    def append(self, record: MemoryRecord, embedding: Sequence[float] | None) -> None:
        if embedding is not None and len(embedding) != self._dim:
            raise MemoryStoreError(
                f"embedding has {len(embedding)} dimensions, expected {self._dim}"
            )
        try:
            with self._psycopg.connect(self._dsn) as conn:
                conn.execute(
                    _INSERT_PG,
                    (
                        record.tenant_id,
                        record.record_id,
                        record.version,
                        record.tier.value,
                        record.key,
                        record.content,
                        json.dumps(record.data, sort_keys=True, allow_nan=False),
                        record.recorded_at,
                        record.occurred_at,
                        record.valid_from,
                        record.valid_to,
                        record.attribution.agent_id,
                        record.attribution.trace_id,
                        record.run_id,
                        record.retired,
                        self._vec(embedding) if embedding is not None else None,
                    ),
                )
        except self._psycopg.errors.UniqueViolation as exc:
            raise ConcurrentWriteError(
                f"{record.record_id} version {record.version} already exists"
            ) from exc

    @staticmethod
    def _to_record(row: tuple[Any, ...]) -> MemoryRecord:
        return MemoryRecord(
            tenant_id=row[0],
            record_id=row[1],
            version=row[2],
            tier=MemoryTier(row[3]),
            key=row[4],
            content=row[5],
            data=json.loads(row[6]),
            recorded_at=row[7],
            occurred_at=row[8],
            valid_from=row[9],
            valid_to=row[10],
            attribution=Attribution(agent_id=row[11], trace_id=row[12]),
            run_id=row[13],
            retired=bool(row[14]),
        )

    def latest(self, tenant_id: str, record_id: str) -> MemoryRecord | None:
        with self._psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM memory_records WHERE tenant_id = %s AND record_id = %s "  # noqa: S608
                "ORDER BY version DESC LIMIT 1",
                (tenant_id, record_id),
            ).fetchone()
        return self._to_record(row) if row else None

    def versions(self, tenant_id: str, record_id: str, *, as_of: datetime) -> list[MemoryRecord]:
        with self._psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM memory_records WHERE tenant_id = %s AND record_id = %s "  # noqa: S608
                "AND recorded_at <= %s ORDER BY version",
                (tenant_id, record_id, as_of),
            ).fetchall()
        return [self._to_record(r) for r in rows]

    def _rows(
        self,
        tenant_id: str,
        tier: MemoryTier,
        as_of: datetime,
        run_id: str | None,
        key: str | None,
    ) -> list[tuple[Any, ...]]:
        sql = self._VISIBLE
        params: list[Any] = [tenant_id, tier.value, as_of, as_of, as_of, as_of, as_of]
        if run_id is not None:
            sql += " AND run_id = %s"
            params.append(run_id)
        if key is not None:
            sql += " AND key = %s"
            params.append(key)
        sql += " ORDER BY recorded_at, record_id"
        with self._psycopg.connect(self._dsn) as conn:
            return conn.execute(sql, params).fetchall()

    def visible(
        self,
        tenant_id: str,
        tier: MemoryTier,
        *,
        as_of: datetime,
        run_id: str | None = None,
        key: str | None = None,
    ) -> list[MemoryRecord]:
        return [self._to_record(r) for r in self._rows(tenant_id, tier, as_of, run_id, key)]

    def nearest(
        self,
        tenant_id: str,
        tier: MemoryTier,
        embedding: Sequence[float],
        *,
        as_of: datetime,
        limit: int,
    ) -> list[tuple[MemoryRecord, float]]:
        # Rank inside the database with pgvector's cosine distance, over exactly the rows
        # visible at as_of: a nearer neighbour that was not yet known must not displace
        # one that was.
        vector = self._vec(embedding)
        wrapped = (
            f"SELECT {_COLUMNS}, 1 - (embedding <=> %s::vector) AS score FROM memory_records m "  # noqa: S608
            "WHERE tenant_id = %s AND tier = %s AND recorded_at <= %s "
            "AND version = (SELECT MAX(version) FROM memory_records s "
            "WHERE s.tenant_id = m.tenant_id AND s.record_id = m.record_id "
            "AND s.recorded_at <= %s) "
            "AND retired = FALSE AND embedding IS NOT NULL "
            "AND (valid_from IS NULL OR valid_from <= %s) AND (valid_to IS NULL OR %s < valid_to) "
            "AND (occurred_at IS NULL OR occurred_at <= %s) "
            "ORDER BY embedding <=> %s::vector, record_id LIMIT %s"
        )
        params = [vector, tenant_id, tier.value, as_of, as_of, as_of, as_of, as_of, vector, limit]
        with self._psycopg.connect(self._dsn) as conn:
            rows = conn.execute(wrapped, params).fetchall()
        return [(self._to_record(r[:15]), float(r[15])) for r in rows]

    def close(self) -> None:
        """Connections are per-operation; nothing is held open."""
