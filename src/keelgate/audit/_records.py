"""Audit record format and the pure chain verifier.

Each record commits to its predecessor: ``hash = SHA-256(canonical(record fields
+ prev_hash))``. Chains are **per tenant** and start from a tenant-specific
genesis hash, so a tenant can verify its own history without seeing anyone
else's, and a record spliced in from another tenant's chain fails verification.

Payloads are stored and hashed as the exact canonical JSON *text*, never as
parsed values, so a storage layer that reorders keys (JSONB does) or rewrites
whitespace cannot silently pass as unmodified.

Limits worth stating plainly: a hash chain proves records were not altered or
reordered *relative to each other*. It cannot, alone, detect that the newest
records were deleted, and anyone able to rewrite the whole chain can recompute
it. Anchor :class:`ChainHead` somewhere the writer cannot reach and pass it as
``expected_head`` to close the truncation gap.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

RECORD_VERSION: Final = 1
MAX_PAYLOAD_BYTES: Final = 256 * 1024


class AuditError(Exception):
    """The audit log could not be written or read correctly."""


class EventType(StrEnum):
    """Event kinds Keelgate itself emits. Stored as plain strings, so a chain
    containing a newer event kind still verifies under an older verifier."""

    TOOL_CALL = "tool.call"
    GRANT_REJECTED = "grant.rejected"
    POLICY_DECISION = "policy.decision"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_CONSUMED = "approval.consumed"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"
    TOOL_REPLAY = "tool.replay"
    LOOP_TRANSITION = "loop.transition"
    MEMORY_WRITE = "memory.write"
    CONTEXT_REJECTED = "context.rejected"
    OUTCOME_RECONCILED = "loop.reconciled"


def canonical_json(value: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ASCII, no NaN or infinity."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def genesis_hash(tenant_id: str) -> str:
    """The ``prev_hash`` of a tenant's first record."""
    return hashlib.sha256(b"keelgate.audit.genesis.v1\0" + tenant_id.encode()).hexdigest()


class AuditRecord(BaseModel):
    """One hash-chained entry in a tenant's audit log: sequence, time, event type, actor, payload
    and the hashes that link it to the one before.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    timestamp: str
    event_type: str = Field(min_length=1)
    actor: str
    payload_json: str
    prev_hash: str
    hash: str

    @property
    def payload(self) -> Any:
        return json.loads(self.payload_json)


def compute_hash(
    *,
    tenant_id: str,
    seq: int,
    timestamp: str,
    event_type: str,
    actor: str,
    payload_json: str,
    prev_hash: str,
) -> str:
    body = canonical_json(
        {
            "v": RECORD_VERSION,
            "tenant_id": tenant_id,
            "seq": seq,
            "timestamp": timestamp,
            "event_type": event_type,
            "actor": actor,
            "payload": payload_json,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(body.encode()).hexdigest()


def build_record(
    *,
    tenant_id: str,
    seq: int,
    prev_hash: str,
    timestamp: datetime,
    event_type: str,
    actor: str,
    payload: object,
) -> AuditRecord:
    """Build the next record in a chain. Raises :class:`AuditError` on bad input."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise AuditError("audit timestamps must be timezone-aware")
    try:
        payload_json = canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise AuditError(f"audit payload is not canonical-JSON serialisable: {exc}") from exc
    if len(payload_json.encode()) > MAX_PAYLOAD_BYTES:
        raise AuditError(f"audit payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    ts = timestamp.astimezone(UTC).isoformat()
    return AuditRecord(
        tenant_id=tenant_id,
        seq=seq,
        timestamp=ts,
        event_type=str(event_type),
        actor=actor,
        payload_json=payload_json,
        prev_hash=prev_hash,
        hash=compute_hash(
            tenant_id=tenant_id,
            seq=seq,
            timestamp=ts,
            event_type=str(event_type),
            actor=actor,
            payload_json=payload_json,
            prev_hash=prev_hash,
        ),
    )


@dataclass(frozen=True)
class ChainHead:
    """The newest record of a tenant's chain. Anchor this outside the writer."""

    tenant_id: str
    seq: int
    hash: str


@dataclass(frozen=True)
class ChainVerification:
    """The result of verifying one tenant's audit chain.

    Whether it is intact, how many records were checked, the head, and where it broke if it did.
    Truthy when the chain is intact.
    """

    ok: bool
    tenant_id: str
    records_checked: int
    head: ChainHead | None
    bad_seq: int | None = None
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def verify_chain(  # noqa: PLR0911 - one early return per distinct tamper signal
    records: Iterable[AuditRecord],
    *,
    tenant_id: str,
    expected_head: ChainHead | None = None,
) -> ChainVerification:
    """Verify one tenant's records, in order, from genesis.

    Pure and storage-independent: feed it records exported from anywhere. It
    checks tenant, contiguous sequence numbers, each ``prev_hash`` link, every
    recomputed hash and timestamp ordering. With ``expected_head`` it also
    detects truncation and rollback of the tail.
    """
    previous = genesis_hash(tenant_id)
    expected_seq = 1
    checked = 0
    last_time: datetime | None = None
    head: ChainHead | None = None
    anchor_seen = False

    def fail(seq: int | None, error: str) -> ChainVerification:
        return ChainVerification(False, tenant_id, checked, head, bad_seq=seq, error=error)

    for record in records:
        if record.tenant_id != tenant_id:
            return fail(record.seq, f"record belongs to tenant {record.tenant_id!r}")
        if record.seq != expected_seq:
            return fail(record.seq, f"expected seq {expected_seq}, found {record.seq}")
        if not hmac.compare_digest(record.prev_hash, previous):
            return fail(record.seq, "prev_hash does not match the preceding record")
        recomputed = compute_hash(
            tenant_id=record.tenant_id,
            seq=record.seq,
            timestamp=record.timestamp,
            event_type=record.event_type,
            actor=record.actor,
            payload_json=record.payload_json,
            prev_hash=record.prev_hash,
        )
        if not hmac.compare_digest(record.hash, recomputed):
            return fail(record.seq, "record hash does not match its contents")
        try:
            moment = datetime.fromisoformat(record.timestamp)
        except ValueError:
            return fail(record.seq, "timestamp is not ISO-8601")
        if last_time is not None and moment < last_time:
            return fail(record.seq, "timestamp moves backwards")
        last_time = moment
        previous = record.hash
        expected_seq += 1
        checked += 1
        head = ChainHead(tenant_id, record.seq, record.hash)
        if (
            expected_head is not None
            and record.seq == expected_head.seq
            and expected_head.tenant_id == tenant_id
        ):
            if not hmac.compare_digest(record.hash, expected_head.hash):
                return fail(
                    record.seq, "record differs from the anchored head: chain was rewritten"
                )
            anchor_seen = True

    if expected_head is not None:
        if expected_head.tenant_id != tenant_id:
            return fail(None, "expected head belongs to a different tenant")
        if not anchor_seen:
            return fail(None, "chain is shorter than the anchored head: records were removed")
    return ChainVerification(True, tenant_id, checked, head)
