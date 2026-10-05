"""The four memory tiers over one shared, append-only, bitemporal core."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from keelgate.audit.records import EventType
from keelgate.memory.embedder import Embedder, HashEmbedder
from keelgate.memory.types import (
    Attribution,
    InvalidMemoryWriteError,
    MemoryRecord,
    MemoryTier,
    RecordNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from keelgate.audit.log import AuditLog
    from keelgate.memory.backends import MemoryBackend

_WORD = re.compile(r"\w+")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _overlap(query: str, text: str) -> float:
    q, t = set(_WORD.findall(query.lower())), set(_WORD.findall(text.lower()))
    return len(q & t) / len(q) if q else 0.0


class _TierMemory:
    """Shared machinery. A subclass sets ``tier`` and overrides the hooks it needs."""

    tier: ClassVar[MemoryTier]

    def __init__(
        self,
        backend: MemoryBackend,
        *,
        tenant_id: str,
        audit: AuditLog | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required: memory is always tenant-scoped")
        self._backend = backend
        self.tenant_id = tenant_id
        self._audit = audit
        self._clock = clock

    # ------------------------------------------------------------------- hooks

    def _check_write(
        self,
        *,
        key: str,
        occurred_at: datetime | None,
        valid_from: datetime | None,
        valid_to: datetime | None,
        now: datetime,
    ) -> tuple[datetime | None, datetime | None, datetime | None]:
        """Validate tier-specific fields and return ``(occurred_at, valid_from, valid_to)``."""
        if occurred_at or valid_from or valid_to:
            raise InvalidMemoryWriteError(f"{self.tier.value} memory takes no time fields")
        return None, None, None

    def _embedding_for(self, content: str) -> list[float] | None:
        return None

    def _record_id_for(self, key: str) -> str:
        return uuid.uuid4().hex

    def _run_id(self) -> str | None:
        return None

    # ------------------------------------------------------------------- writes

    def write(
        self,
        *,
        key: str,
        content: str,
        attribution: Attribution,
        data: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> MemoryRecord:
        now = self._stamp(None)
        occurred, start, end = self._check_write(
            key=key, occurred_at=occurred_at, valid_from=valid_from, valid_to=valid_to, now=now
        )
        record_id = self._record_id_for(key)
        existing = self._backend.latest(self.tenant_id, record_id)
        if existing is not None:
            if existing.retired:
                raise InvalidMemoryWriteError(f"{record_id} was retired and cannot be rewritten")
            return self._append(
                existing,
                content=content,
                data=data,
                attribution=attribution,
                valid_to=end if end is not None else existing.valid_to,
                retired=False,
            )
        return self._first_version(
            record_id,
            key=key,
            content=content,
            data=dict(data or {}),
            attribution=attribution,
            now=now,
            occurred_at=occurred,
            valid_from=start,
            valid_to=end,
        )

    def revise(
        self,
        record_id: str,
        *,
        content: str,
        attribution: Attribution,
        data: Mapping[str, Any] | None = None,
        valid_to: datetime | None = None,
    ) -> MemoryRecord:
        previous = self._require_latest(record_id)
        if previous.retired:
            raise InvalidMemoryWriteError(f"{record_id} was retired and cannot be revised")
        return self._append(
            previous,
            content=content,
            data=data,
            attribution=attribution,
            valid_to=valid_to if valid_to is not None else previous.valid_to,
            retired=False,
        )

    def retire(self, record_id: str, *, attribution: Attribution) -> MemoryRecord:
        """Hide a record from every read recorded after this moment. History is kept."""
        previous = self._require_latest(record_id)
        return self._append(
            previous,
            content=previous.content,
            data=previous.data,
            attribution=attribution,
            valid_to=previous.valid_to,
            retired=True,
        )

    # -------------------------------------------------------------------- reads

    def get(self, record_id: str, *, as_of: datetime) -> MemoryRecord | None:
        known = self._backend.versions(self.tenant_id, record_id, as_of=as_of)
        if not known or known[-1].tier is not self.tier:
            return None
        latest = known[-1]
        return latest if self._visible_at(latest, as_of) else None

    def history(self, record_id: str, *, as_of: datetime) -> tuple[MemoryRecord, ...]:
        versions = self._backend.versions(self.tenant_id, record_id, as_of=as_of)
        if versions and versions[0].tier is not self.tier:
            return ()
        return tuple(versions)

    def search(self, query: str, *, as_of: datetime, limit: int = 5) -> tuple[MemoryRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        candidates = self._backend.visible(
            self.tenant_id, self.tier, as_of=as_of, run_id=self._run_id()
        )
        ranked = sorted(
            candidates,
            key=lambda r: (-_overlap(query, f"{r.key} {r.content}"), -r.recorded_at.timestamp()),
        )
        return tuple(ranked[:limit])

    # ----------------------------------------------------------------- internals

    def _visible_at(self, record: MemoryRecord, as_of: datetime) -> bool:
        return not (
            record.retired
            or (record.valid_from and record.valid_from > as_of)
            or (record.valid_to and as_of >= record.valid_to)
            or (record.occurred_at and record.occurred_at > as_of)
        )

    def _require_latest(self, record_id: str) -> MemoryRecord:
        # A record in another tenant is simply absent: the backend query is tenant-scoped.
        latest = self._backend.latest(self.tenant_id, record_id)
        if latest is None or latest.tier is not self.tier:
            raise RecordNotFoundError(f"no {self.tier.value} record {record_id!r}")
        return latest

    def _stamp(self, previous: datetime | None) -> datetime:
        """The store's clock, never the caller's, clamped so a record cannot be known earlier
        than the version it supersedes."""
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidMemoryWriteError("the memory clock must be timezone-aware")
        return max(now, previous) if previous is not None else now

    def _first_version(
        self,
        record_id: str,
        *,
        key: str,
        content: str,
        data: dict[str, Any],
        attribution: Attribution,
        now: datetime,
        occurred_at: datetime | None,
        valid_from: datetime | None,
        valid_to: datetime | None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            record_id=record_id,
            version=1,
            tenant_id=self.tenant_id,
            tier=self.tier,
            key=key,
            content=content,
            data=data,
            recorded_at=now,
            attribution=attribution,
            occurred_at=occurred_at,
            valid_from=valid_from,
            valid_to=valid_to,
            run_id=self._run_id(),
        )
        self._backend.append(record, self._embedding_for(content))
        self._audit_write(record)
        return record

    def _append(
        self,
        previous: MemoryRecord,
        *,
        content: str,
        data: Mapping[str, Any] | None,
        attribution: Attribution,
        valid_to: datetime | None,
        retired: bool,
    ) -> MemoryRecord:
        record = previous.model_copy(
            update={
                "version": previous.version + 1,
                "content": content,
                "data": dict(data) if data is not None else previous.data,
                "recorded_at": self._stamp(previous.recorded_at),
                "attribution": attribution,
                "valid_to": valid_to,
                "retired": retired,
            }
        )
        # Re-validate: model_copy skips validators, and valid_to must still follow valid_from.
        record = MemoryRecord.model_validate(record.model_dump())
        self._backend.append(record, self._embedding_for(content))
        self._audit_write(record)
        return record

    def _audit_write(self, record: MemoryRecord) -> None:
        if self._audit is None:
            return
        self._audit.append(
            tenant_id=record.tenant_id,
            event_type=EventType.MEMORY_WRITE,
            actor=record.attribution.agent_id,
            payload={
                "tier": record.tier.value,
                "record_id": record.record_id,
                "version": record.version,
                "key": record.key,
                "retired": record.retired,
                "content_sha256": record.content_sha256,
                "trace_id": record.attribution.trace_id,
            },
        )


class WorkingMemory(_TierMemory):
    """Scratch space for one run. Keyed; writing a key again creates a new version."""

    tier: ClassVar[MemoryTier] = MemoryTier.WORKING

    def __init__(
        self, backend: MemoryBackend, *, tenant_id: str, run_id: str, **kwargs: Any
    ) -> None:
        super().__init__(backend, tenant_id=tenant_id, **kwargs)
        if not run_id:
            raise ValueError("working memory is scoped to a run: run_id is required")
        self.run_id = run_id

    def _run_id(self) -> str | None:
        return self.run_id

    def _record_id_for(self, key: str) -> str:
        return f"{self.run_id}:{key}"

    def put(self, key: str, value: str, *, attribution: Attribution) -> MemoryRecord:
        return self.write(key=key, content=value, attribution=attribution)

    def value(self, key: str, *, as_of: datetime) -> str | None:
        record = self.get(self._record_id_for(key), as_of=as_of)
        return record.content if record else None


class EpisodicMemory(_TierMemory):
    """What the agent decided and what came of it. ``occurred_at`` cannot be in the future."""

    tier: ClassVar[MemoryTier] = MemoryTier.EPISODIC

    def _check_write(
        self,
        *,
        key: str,
        occurred_at: datetime | None,
        valid_from: datetime | None,
        valid_to: datetime | None,
        now: datetime,
    ) -> tuple[datetime | None, datetime | None, datetime | None]:
        if valid_from or valid_to:
            raise InvalidMemoryWriteError("episodes have no validity window; use semantic memory")
        when = occurred_at or now
        if when.tzinfo is None or when.utcoffset() is None:
            raise InvalidMemoryWriteError("occurred_at must be timezone-aware")
        if when > now:
            raise InvalidMemoryWriteError("an episode cannot have occurred in the future")
        return when, None, None

    def record_episode(
        self,
        decision: str,
        *,
        attribution: Attribution,
        occurred_at: datetime | None = None,
        outcome: str | None = None,
        key: str = "decision",
    ) -> MemoryRecord:
        data = {"outcome": outcome} if outcome is not None else {}
        return self.write(
            key=key, content=decision, attribution=attribution, data=data, occurred_at=occurred_at
        )

    def record_outcome(
        self, record_id: str, outcome: str, *, attribution: Attribution
    ) -> MemoryRecord:
        previous = self._require_latest(record_id)
        return self.revise(
            record_id,
            content=previous.content,
            attribution=attribution,
            data={**previous.data, "outcome": outcome},
        )

    def recall(self, query: str, *, as_of: datetime, limit: int = 5) -> tuple[MemoryRecord, ...]:
        return self.search(query, as_of=as_of, limit=limit)


class SemanticMemory(_TierMemory):
    """Facts with validity windows, retrieved by embedding similarity.

    A fact is returned at ``as_of`` only if it was *recorded* by then and ``as_of`` falls
    inside ``[valid_from, valid_to)``. A correction recorded today does not rewrite what
    was known last week.
    """

    tier: ClassVar[MemoryTier] = MemoryTier.SEMANTIC

    def __init__(
        self,
        backend: MemoryBackend,
        *,
        tenant_id: str,
        embedder: Embedder | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(backend, tenant_id=tenant_id, **kwargs)
        self._embedder: Embedder = embedder or HashEmbedder()

    def _check_write(
        self,
        *,
        key: str,
        occurred_at: datetime | None,
        valid_from: datetime | None,
        valid_to: datetime | None,
        now: datetime,
    ) -> tuple[datetime | None, datetime | None, datetime | None]:
        if occurred_at:
            raise InvalidMemoryWriteError("facts have a validity window, not an occurred_at")
        if valid_from is None:
            raise InvalidMemoryWriteError("a fact needs valid_from: when does it start holding?")
        if valid_to is not None and valid_to <= valid_from:
            raise InvalidMemoryWriteError("valid_to must be after valid_from")
        return None, valid_from, valid_to

    def _embedding_for(self, content: str) -> list[float] | None:
        return self._embedder.embed([content])[0]

    def assert_fact(
        self,
        subject: str,
        text: str,
        *,
        valid_from: datetime,
        attribution: Attribution,
        valid_to: datetime | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> MemoryRecord:
        return self.write(
            key=subject,
            content=text,
            attribution=attribution,
            data=data,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def end_fact(
        self, record_id: str, valid_to: datetime, *, attribution: Attribution
    ) -> MemoryRecord:
        """Say a fact stopped holding at ``valid_to``. Earlier reads are unaffected."""
        previous = self._require_latest(record_id)
        return self.revise(
            record_id, content=previous.content, attribution=attribution, valid_to=valid_to
        )

    def search(self, query: str, *, as_of: datetime, limit: int = 5) -> tuple[MemoryRecord, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        vector = self._embedder.embed([query])[0]
        hits = self._backend.nearest(self.tenant_id, self.tier, vector, as_of=as_of, limit=limit)
        return tuple(record for record, _score in hits)

    def search_scored(
        self, query: str, *, as_of: datetime, limit: int = 5
    ) -> tuple[tuple[MemoryRecord, float], ...]:
        vector = self._embedder.embed([query])[0]
        return tuple(
            self._backend.nearest(self.tenant_id, self.tier, vector, as_of=as_of, limit=limit)
        )


class ProceduralMemory(_TierMemory):
    """Versioned skills and playbooks, addressed by name."""

    tier: ClassVar[MemoryTier] = MemoryTier.PROCEDURAL

    def _record_id_for(self, key: str) -> str:
        return f"skill:{key}"

    def publish(self, name: str, body: str, *, attribution: Attribution) -> MemoryRecord:
        """Publish a new version of the skill ``name`` (the first, if it is new)."""
        return self.write(key=name, content=body, attribution=attribution)

    def skill(self, name: str, *, as_of: datetime) -> MemoryRecord | None:
        """The version of ``name`` that was current at ``as_of``."""
        return self.get(self._record_id_for(name), as_of=as_of)

    def skills(self, *, as_of: datetime) -> tuple[MemoryRecord, ...]:
        return tuple(self._backend.visible(self.tenant_id, self.tier, as_of=as_of))
