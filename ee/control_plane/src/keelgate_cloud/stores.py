"""Postgres implementations of the open-source storage protocols, with tenant isolation by RLS.

Both stores implement protocols from ``keelgate`` (``AuditStore``, ``ApprovalStore``), so the
control plane runs the *same* audit-chaining and approval-rule code as the open-source harness; it
only supplies where the rows live. Every method opens a transaction scoped to one org.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from keelgate.approvals import ApprovalRequest, ApprovalStatus
from keelgate.audit import AuditError, AuditRecord, ChainHead, genesis_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import psycopg
    from keelgate.audit._stores import BuildRecord

    from keelgate_cloud.db import Database


def _lock_key(org_id: str) -> int:
    """A stable 63-bit advisory-lock key for an org's audit chain."""
    return int.from_bytes(hashlib.sha256(org_id.encode()).digest()[:8], "big") >> 1


class RlsAuditStore:
    """The audit chain of every org, in one table protected by row-level security.

    ``on_append`` runs inside the append transaction, after the record is built and before it is
    stored. It may raise to refuse the record (quota, plan) and may write to other tables (usage),
    and either way it commits or rolls back together with the record.
    """

    def __init__(
        self,
        db: Database,
        *,
        on_append: Callable[[psycopg.Connection[Any], AuditRecord], None] | None = None,
    ) -> None:
        self._db = db
        self._on_append = on_append

    def append(
        self, tenant_id: str, build: BuildRecord, *, idempotency_key: str | None = None
    ) -> AuditRecord:
        with self._db.tx(tenant_id) as conn:
            # Serialise writers for this org so sequence numbers and prev_hash links stay linear.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(tenant_id),))
            if idempotency_key is not None:
                stored = conn.execute(
                    "SELECT response FROM idempotency_keys WHERE scope = 'audit' AND key = %s",
                    (idempotency_key,),
                ).fetchone()
                if stored is not None:
                    return AuditRecord.model_validate(stored["response"])
            last = conn.execute(
                "SELECT seq, timestamp, hash FROM audit_records ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if last is None:
                seq, prev_hash, prev_ts = 1, genesis_hash(tenant_id), None
            else:
                seq, prev_hash, prev_ts = last["seq"] + 1, last["hash"], last["timestamp"]
            record = build(seq, prev_hash, prev_ts)
            if record.tenant_id != tenant_id or record.seq != seq:
                raise AuditError("built record does not match the chain position")
            if self._on_append is not None:
                self._on_append(conn, record)
            conn.execute(
                "INSERT INTO audit_records (org_id, seq, timestamp, event_type, actor, "
                "payload_json, prev_hash, hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    tenant_id,
                    record.seq,
                    record.timestamp,
                    record.event_type,
                    record.actor,
                    record.payload_json,
                    record.prev_hash,
                    record.hash,
                ),
            )
            if idempotency_key is not None:
                conn.execute(
                    "INSERT INTO idempotency_keys (org_id, scope, key, response) "
                    "VALUES (%s, 'audit', %s, %s)",
                    (tenant_id, idempotency_key, record.model_dump_json()),
                )
            return record

    def iter_records(self, tenant_id: str, *, after_seq: int = 0) -> Iterator[AuditRecord]:
        cursor = after_seq
        while True:
            with self._db.tx(tenant_id) as conn:
                rows = conn.execute(
                    "SELECT seq, timestamp, event_type, actor, payload_json, prev_hash, hash "
                    "FROM audit_records WHERE seq > %s ORDER BY seq LIMIT 1000",
                    (cursor,),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield AuditRecord(tenant_id=tenant_id, **row)
            cursor = rows[-1]["seq"]

    def head(self, tenant_id: str) -> ChainHead | None:
        with self._db.tx(tenant_id) as conn:
            row = conn.execute(
                "SELECT seq, hash FROM audit_records ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return None if row is None else ChainHead(tenant_id, row["seq"], row["hash"])

    def close(self) -> None:
        """The pool belongs to the application, not to this store."""


class RlsApprovalStore:
    """Approval requests for every org, in one table protected by row-level security."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(self, request: ApprovalRequest) -> None:
        with self._db.tx(request.tenant_id) as conn:
            conn.execute(
                "INSERT INTO approvals (request_id, org_id, status, expires_at, body) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    request.request_id,
                    request.tenant_id,
                    request.status.value,
                    request.expires_at,
                    request.model_dump_json(),
                ),
            )

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest | None:
        with self._db.tx(tenant_id) as conn:
            row = conn.execute(
                "SELECT body FROM approvals WHERE request_id = %s", (request_id,)
            ).fetchone()
        return None if row is None else ApprovalRequest.model_validate(row["body"])

    def swap(self, request: ApprovalRequest, *, expect: ApprovalStatus) -> bool:
        with self._db.tx(request.tenant_id) as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status = %s, body = %s WHERE request_id = %s AND status = %s",
                (request.status.value, request.model_dump_json(), request.request_id, expect.value),
            )
            return cursor.rowcount == 1

    def list_pending(self, tenant_id: str) -> list[ApprovalRequest]:
        with self._db.tx(tenant_id) as conn:
            rows = conn.execute(
                "SELECT body FROM approvals WHERE status = 'PENDING' "
                "ORDER BY created_at, request_id"
            ).fetchall()
        return [ApprovalRequest.model_validate(r["body"]) for r in rows]

    def list_all(
        self, tenant_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[ApprovalRequest]:
        """Requests in any state, newest first (for the dashboard history)."""
        with self._db.tx(tenant_id) as conn:
            rows = conn.execute(
                "SELECT body FROM approvals WHERE (%s::text IS NULL OR status = %s) "
                "ORDER BY created_at DESC, request_id LIMIT %s",
                (status, status, limit),
            ).fetchall()
        return [ApprovalRequest.model_validate(r["body"]) for r in rows]

    def close(self) -> None:
        """The pool belongs to the application, not to this store."""
