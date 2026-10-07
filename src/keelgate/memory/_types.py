"""Memory records and the one interface all four tiers implement.

Four tiers, one shape:

* ``WORKING``: scratch for one run. Gone when the run is.
* ``EPISODIC``: what the agent decided and what happened. Append-only history.
* ``SEMANTIC``: facts, each with a validity window (``valid_from`` / ``valid_to``).
* ``PROCEDURAL``: versioned skills and playbooks.

Every write is a **new version**; nothing is ever updated in place. Every version carries
the agent and trace id that wrote it, and a ``recorded_at`` taken from the store's own
clock. A caller cannot supply it, so no write can claim to have been known earlier than it
was. That is what makes reads at an ``as_of`` honest: a record is visible only if it was
*recorded* by then, and (for episodes and facts) also *happened* or was *valid* by then.

Retrieved memory is **untrusted**. It may contain text planted by an earlier tool result or
document. :meth:`MemoryRecord.to_context_item` always labels it so.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keelgate.context._item import ContextItem, ItemKind

if TYPE_CHECKING:
    from collections.abc import Mapping


class MemoryTier(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryStoreError(Exception):
    """Base class for memory failures."""


class RecordNotFoundError(MemoryStoreError):
    """No such record. Also what another tenant's record looks like: absent."""


class InvalidMemoryWriteError(MemoryStoreError):
    """The write is malformed for its tier."""


class ConcurrentWriteError(MemoryStoreError):
    """Another writer created the next version first. Re-read and retry."""


class Attribution(BaseModel):
    """Who wrote a version, and under which trace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str
    version: int = Field(ge=1)
    tenant_id: str
    tier: MemoryTier
    key: str = Field(min_length=1, max_length=300)
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime
    attribution: Attribution
    occurred_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    run_id: str | None = None
    retired: bool = False

    @model_validator(mode="after")
    def _times_are_aware_and_ordered(self) -> MemoryRecord:
        for name in ("recorded_at", "occurred_at", "valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()

    def to_context_item(self, *, priority: int = 0) -> ContextItem:
        """This record as an UNTRUSTED context item, dated when it became known."""
        return ContextItem.outside(
            ItemKind.MEMORY,
            self.content,
            item_id=f"memory:{self.record_id}@{self.version}",
            published_at=self.recorded_at,
            origin=f"memory:{self.tier.value}",
            ref=f"{self.record_id}@{self.version}",
            priority=priority,
        )


@runtime_checkable
class Memory(Protocol):
    """The interface every tier implements. All reads take an explicit ``as_of``."""

    tier: MemoryTier
    tenant_id: str

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
    ) -> MemoryRecord: ...

    def revise(
        self,
        record_id: str,
        *,
        content: str,
        attribution: Attribution,
        data: Mapping[str, Any] | None = None,
        valid_to: datetime | None = None,
    ) -> MemoryRecord: ...

    def retire(self, record_id: str, *, attribution: Attribution) -> MemoryRecord: ...

    def get(self, record_id: str, *, as_of: datetime) -> MemoryRecord | None: ...

    def history(self, record_id: str, *, as_of: datetime) -> tuple[MemoryRecord, ...]: ...

    def search(
        self, query: str, *, as_of: datetime, limit: int = 5
    ) -> tuple[MemoryRecord, ...]: ...
