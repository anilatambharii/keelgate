"""The human-in-the-loop approval queue.

Properties the rest of the system relies on:

* **Tenant scoped.** Every operation takes a tenant. A request in another tenant
  is reported as *not found*, never as *forbidden*, so existence does not leak.
* **Bound to the exact action.** An approval is tied to the hash of the tool and
  arguments. A different argument set cannot ride an existing approval.
* **Single use.** :meth:`ApprovalQueue.consume` flips APPROVED to CONSUMED
  atomically; a second consume fails.
* **Separation of duties.** The agent that asked can never approve its own request.
* **Expiring.** Neither a pending nor an approved request outlives ``expires_at``.
* **Audited.** Request, decision and consumption are each appended to the audit
  chain when an :class:`~keelgate.audit.AuditLog` is supplied.
"""

from __future__ import annotations

import hmac
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol

from keelgate.approvals._models import (
    ApprovalRequest,
    ApprovalStatus,
    Approver,
    EvidenceBundle,
)
from keelgate.approvals._tiers import ApprovalTier
from keelgate.audit import AuditLog, EventType
from keelgate.telemetry import attributes as attr
from keelgate.telemetry._hooks import traced

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_TTL: Final = timedelta(minutes=30)
MAX_NOTE: Final = 1000

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ApprovalError(Exception):
    """Base class. ``code`` is stable and safe to show to a model or an API client."""

    code = "approval_error"


class ApprovalNotFoundError(ApprovalError):
    """No such request for this tenant. Another tenant's request is reported the same way."""

    code = "approval_not_found"


class ApprovalNotPendingError(ApprovalError):
    """The request was already decided."""

    code = "approval_not_pending"


class ApprovalExpiredError(ApprovalError):
    """The approval request expired before it was decided or used."""

    code = "approval_expired"


class ApprovalNotAuthorisedError(ApprovalError):
    """The approver may not decide this request: wrong tenant, tier too low, or the requester
    itself.
    """

    code = "approval_not_authorised"


class ApprovalSignoffError(ApprovalError):
    """An explicit sign-off was required and the code supplied was missing or wrong."""

    code = "approval_signoff_invalid"


class ApprovalNotUsableError(ApprovalError):
    """The request exists but cannot back this execution (wrong state or action)."""

    code = "approval_not_usable"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    request_id  TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_tenant_status ON approvals (tenant_id, status);
"""


class ApprovalStore(Protocol):
    """Where approval requests are persisted (SQLite ships).

    Implement this to keep requests elsewhere, for example in a database that enforces tenant
    isolation itself. The queue enforces every approval rule; a store only keeps rows. ``swap``
    must be an atomic compare-and-swap on status, and a request of another tenant must read as
    absent.
    """

    def insert(self, request: ApprovalRequest) -> None:
        """Persist a new request."""
        ...

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest | None:
        """The tenant request, or ``None`` if there is no such request for that tenant."""
        ...

    def swap(self, request: ApprovalRequest, *, expect: ApprovalStatus) -> bool:
        """Replace the stored request only if its status is still ``expect``."""
        ...

    def list_pending(self, tenant_id: str) -> list[ApprovalRequest]:
        """The tenant requests whose stored status is PENDING, oldest first."""
        ...

    def close(self) -> None:
        """Release the store resources."""
        ...


class ApprovalBackend(Protocol):
    """What the tool gateway needs from an approval queue: submit, look up, and spend.

    :class:`ApprovalQueue` is the local implementation. A remote queue (a hosted control plane,
    for instance) implements the same three calls.
    """

    def submit(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        tool_name: str,
        args_hash: str,
        tier: ApprovalTier,
        evidence: EvidenceBundle,
        ttl: timedelta | None = None,
    ) -> ApprovalRequest:
        """Park an action for a human and return the pending request."""
        ...

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest:
        """The tenant request, or raise :class:`ApprovalNotFoundError`."""
        ...

    def consume(
        self,
        *,
        tenant_id: str,
        request_id: str,
        agent_id: str,
        tool_name: str,
        args_hash: str,
    ) -> ApprovalRequest:
        """Spend an approved request, once, for exactly this action."""
        ...


class SqliteApprovalStore:
    """The default :class:`ApprovalStore`: one SQLite database, in memory unless given a path."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def insert(self, request: ApprovalRequest) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO approvals (request_id, tenant_id, status, expires_at, body) "
                "VALUES (?,?,?,?,?)",
                (
                    request.request_id,
                    request.tenant_id,
                    request.status.value,
                    request.expires_at.isoformat(),
                    request.model_dump_json(),
                ),
            )

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM approvals WHERE request_id = ? AND tenant_id = ?",
                (request_id, tenant_id),
            ).fetchone()
        return None if row is None else ApprovalRequest.model_validate_json(row[0])

    def swap(self, request: ApprovalRequest, *, expect: ApprovalStatus) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE approvals SET status = ?, body = ? WHERE request_id = ? AND status = ?",
                (
                    request.status.value,
                    request.model_dump_json(),
                    request.request_id,
                    expect.value,
                ),
            )
        return cursor.rowcount == 1

    def list_pending(self, tenant_id: str) -> list[ApprovalRequest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT body FROM approvals WHERE tenant_id = ? AND status = ? ORDER BY rowid",
                (tenant_id, ApprovalStatus.PENDING.value),
            ).fetchall()
        return [ApprovalRequest.model_validate_json(row[0]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class ApprovalQueue:
    """The human-in-the-loop approval queue (SQLite-backed).

    Requests are submitted with an evidence bundle, decided by a human of sufficient tier (never the
    requester), and spent exactly once for exactly the arguments that were approved.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        store: ApprovalStore | None = None,
        audit: AuditLog | None = None,
        clock: Clock = _utc_now,
        default_ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        if default_ttl <= timedelta(0):
            raise ValueError("default_ttl must be positive")
        self._audit = audit
        self._clock = clock
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        self._store_: ApprovalStore = store if store is not None else SqliteApprovalStore(path)

    # ------------------------------------------------------------- submission

    @traced(
        "keelgate.approval.submit",
        pre=lambda a: {
            attr.TENANT_ID: a["tenant_id"],
            attr.APPROVAL_TIER: a["tier"].value,
            attr.GEN_AI_TOOL_NAME: a["tool_name"],
            attr.AGENT_ID: a["agent_id"],
        },
        post=lambda r: {attr.APPROVAL_ID: r.request_id, attr.APPROVAL_STATUS: r.status.value},
    )
    def submit(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        tool_name: str,
        args_hash: str,
        tier: ApprovalTier,
        evidence: EvidenceBundle,
        ttl: timedelta | None = None,
    ) -> ApprovalRequest:
        if tier is ApprovalTier.AUTO:
            raise ValueError("AUTO needs no approval request")
        now = self._clock()
        request = ApprovalRequest(
            request_id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            agent_id=agent_id,
            tool_name=tool_name,
            args_hash=args_hash,
            tier=tier,
            evidence=evidence,
            evidence_hash=evidence.digest(),
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now + (ttl or self._default_ttl),
        )
        with self._lock:
            self._store_.insert(request)
        self._record(
            EventType.APPROVAL_REQUESTED,
            tenant_id,
            agent_id,
            {
                "request_id": request.request_id,
                "tool": tool_name,
                "tier": tier.value,
                "args_hash": args_hash,
                "evidence_hash": request.evidence_hash,
                "policy_version": evidence.policy_version,
                "expires_at": request.expires_at.isoformat(),
            },
        )
        return request

    # ----------------------------------------------------------------- reads

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest:
        with self._lock:
            return self._load(tenant_id, request_id)

    def list_pending(self, tenant_id: str) -> list[ApprovalRequest]:
        with self._lock:
            pending = self._store_.list_pending(tenant_id)
            loaded = [self._load(tenant_id, r.request_id) for r in pending]
        return [r for r in loaded if r.status is ApprovalStatus.PENDING]

    # -------------------------------------------------------------- decisions

    @traced(
        "keelgate.approval.approve",
        pre=lambda a: {attr.TENANT_ID: a["tenant_id"], attr.APPROVAL_ID: a["request_id"]},
        post=lambda r: {attr.APPROVAL_STATUS: r.status.value, attr.APPROVAL_TIER: r.tier.value},
    )
    def approve(
        self,
        tenant_id: str,
        request_id: str,
        approver: Approver,
        *,
        signoff_code: str | None = None,
        note: str | None = None,
    ) -> ApprovalRequest:
        with self._lock:
            request = self._load_for_decision(tenant_id, request_id, approver)
            if request.tier is ApprovalTier.EXPLICIT_SIGNOFF and not (
                signoff_code is not None
                and hmac.compare_digest(signoff_code.strip().lower(), request.signoff_code)
            ):
                raise ApprovalSignoffError("EXPLICIT_SIGNOFF needs the evidence signoff code")
            return self._decide(request, ApprovalStatus.APPROVED, approver, note)

    @traced(
        "keelgate.approval.reject",
        pre=lambda a: {attr.TENANT_ID: a["tenant_id"], attr.APPROVAL_ID: a["request_id"]},
        post=lambda r: {attr.APPROVAL_STATUS: r.status.value, attr.APPROVAL_TIER: r.tier.value},
    )
    def reject(
        self,
        tenant_id: str,
        request_id: str,
        approver: Approver,
        *,
        note: str | None = None,
    ) -> ApprovalRequest:
        with self._lock:
            request = self._load_for_decision(tenant_id, request_id, approver)
            return self._decide(request, ApprovalStatus.REJECTED, approver, note)

    # ------------------------------------------------------------ consumption

    @traced(
        "keelgate.approval.consume",
        pre=lambda a: {
            attr.TENANT_ID: a["tenant_id"],
            attr.APPROVAL_ID: a["request_id"],
            attr.GEN_AI_TOOL_NAME: a["tool_name"],
        },
        post=lambda r: {attr.APPROVAL_STATUS: r.status.value},
    )
    def consume(
        self,
        *,
        tenant_id: str,
        request_id: str,
        agent_id: str,
        tool_name: str,
        args_hash: str,
    ) -> ApprovalRequest:
        """Spend an approval, once, for exactly this agent, tool and arguments."""
        with self._lock:
            request = self._load(tenant_id, request_id)
            if request.status is not ApprovalStatus.APPROVED:
                raise ApprovalNotUsableError(f"approval is {request.status.value}")
            if (
                request.agent_id != agent_id
                or request.tool_name != tool_name
                or not hmac.compare_digest(request.args_hash, args_hash)
            ):
                raise ApprovalNotUsableError("approval does not match this action")
            updated = request.model_copy(update={"status": ApprovalStatus.CONSUMED})
            self._store(updated, expect=ApprovalStatus.APPROVED)
        self._record(
            EventType.APPROVAL_CONSUMED,
            tenant_id,
            agent_id,
            {"request_id": request_id, "tool": tool_name, "args_hash": args_hash},
        )
        return updated

    def close(self) -> None:
        with self._lock:
            self._store_.close()

    # --------------------------------------------------------------- internals

    def _load(self, tenant_id: str, request_id: str) -> ApprovalRequest:
        request = self._store_.get(tenant_id, request_id)
        if request is None:
            raise ApprovalNotFoundError("no such approval request")
        if (
            request.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
            and self._clock() >= request.expires_at
        ):
            expired = request.model_copy(update={"status": ApprovalStatus.EXPIRED})
            self._store(expired, expect=request.status)
            self._record(
                EventType.APPROVAL_DECIDED,
                tenant_id,
                "system",
                {"request_id": request_id, "decision": "EXPIRED"},
            )
            return expired
        return request

    def _load_for_decision(
        self, tenant_id: str, request_id: str, approver: Approver
    ) -> ApprovalRequest:
        # An approver from another tenant learns nothing, not even existence.
        if approver.tenant_id != tenant_id:
            raise ApprovalNotFoundError("no such approval request")
        request = self._load(tenant_id, request_id)
        if request.status is ApprovalStatus.EXPIRED:
            raise ApprovalExpiredError("approval request has expired")
        if request.status is not ApprovalStatus.PENDING:
            raise ApprovalNotPendingError(f"approval request is {request.status.value}")
        if approver.approver_id == request.agent_id:
            raise ApprovalNotAuthorisedError("a requester cannot decide its own request")
        if not approver.max_tier.covers(request.tier):
            raise ApprovalNotAuthorisedError(
                f"approver is not cleared for {request.tier.value} requests"
            )
        return request

    def _decide(
        self,
        request: ApprovalRequest,
        status: ApprovalStatus,
        approver: Approver,
        note: str | None,
    ) -> ApprovalRequest:
        updated = request.model_copy(
            update={
                "status": status,
                "decided_by": approver.approver_id,
                "decided_at": self._clock(),
                "decision_note": (note or "")[:MAX_NOTE] or None,
            }
        )
        self._store(updated, expect=ApprovalStatus.PENDING)
        self._record(
            EventType.APPROVAL_DECIDED,
            request.tenant_id,
            approver.approver_id,
            {
                "request_id": request.request_id,
                "decision": status.value,
                "tier": request.tier.value,
                "args_hash": request.args_hash,
                "evidence_hash": request.evidence_hash,
            },
        )
        return updated

    def _store(self, request: ApprovalRequest, *, expect: ApprovalStatus) -> None:
        """Compare-and-swap on status: a stale writer changes nothing."""
        if not self._store_.swap(request, expect=expect):
            raise ApprovalNotPendingError("approval request changed concurrently")

    def _record(
        self, event: EventType, tenant_id: str, actor: str, payload: dict[str, object]
    ) -> None:
        if self._audit is not None:
            self._audit.append(tenant_id=tenant_id, event_type=event, actor=actor, payload=payload)
