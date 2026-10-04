"""The ``AuditLog`` front end: stamps, chains and stores records."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from keelgate.audit.records import (
    AuditRecord,
    ChainHead,
    ChainVerification,
    EventType,
    build_record,
    verify_chain,
)
from keelgate.audit.stores import AuditStore, SqliteAuditStore

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLog:
    """Append-only, hash-chained, per-tenant audit log.

    Records every tool call, policy decision, approval and loop transition.
    Writing is synchronous and durable before the call returns: a side effect
    must never happen without its audit record having been committed first.
    """

    def __init__(self, store: AuditStore | None = None, *, clock: Clock = _utc_now) -> None:
        self._store: AuditStore = store if store is not None else SqliteAuditStore()
        self._clock = clock

    def append(
        self,
        *,
        tenant_id: str,
        event_type: EventType | str,
        actor: str,
        payload: object,
    ) -> AuditRecord:
        def build(seq: int, prev_hash: str, prev_timestamp: str | None) -> AuditRecord:
            # Stamped inside the store's critical section, so the order records are
            # stamped is the order they are chained, and clamped to the previous
            # record so a clock stepped backwards cannot make an honest chain look
            # tampered. Verification still rejects a chain whose time runs backwards.
            stamp = self._clock()
            if prev_timestamp is not None and stamp.tzinfo is not None:
                stamp = max(stamp, datetime.fromisoformat(prev_timestamp))
            return build_record(
                tenant_id=tenant_id,
                seq=seq,
                prev_hash=prev_hash,
                timestamp=stamp,
                event_type=str(event_type),
                actor=actor,
                payload=payload,
            )

        return self._store.append(tenant_id, build)

    def records(self, tenant_id: str, *, after_seq: int = 0) -> Iterator[AuditRecord]:
        return self._store.iter_records(tenant_id, after_seq=after_seq)

    def head(self, tenant_id: str) -> ChainHead | None:
        return self._store.head(tenant_id)

    def verify_chain(
        self, tenant_id: str, *, expected_head: ChainHead | None = None
    ) -> ChainVerification:
        """Verify this tenant's chain as stored. See :func:`verify_chain`."""
        return verify_chain(
            self.records(tenant_id), tenant_id=tenant_id, expected_head=expected_head
        )

    def close(self) -> None:
        self._store.close()
