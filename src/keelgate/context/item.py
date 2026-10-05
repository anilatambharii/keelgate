"""Context items: what may enter a prompt, with where it came from and when it was known.

Two rules are enforced by the type, not by convention:

* **Trust.** Only harness-authored kinds (system, instruction, operator task) can be
  ``TRUSTED``. Tool output, memory, documents and summaries are always ``UNTRUSTED``, and
  a summary of untrusted text is untrusted however it was produced.
* **Time.** Every item that did not come from the harness itself must say when it was
  published. ``ContextBuilder`` refuses the rest, because "unknown" could be "after the
  cutoff", and a leak you cannot rule out is a leak.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ItemKind(StrEnum):
    SYSTEM = "system"
    INSTRUCTION = "instruction"
    TASK = "task"
    OBSERVATION = "observation"
    MEMORY = "memory"
    DOCUMENT = "document"
    SUMMARY = "summary"


class Trust(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


# Kinds the harness authors itself. Everything else carries outside text.
HARNESS_KINDS: Final = frozenset({ItemKind.SYSTEM, ItemKind.INSTRUCTION, ItemKind.TASK})


class Provenance(BaseModel):
    """Where an item came from. ``origin`` is a stable label; ``ref`` is a record id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str = Field(min_length=1, max_length=200)  # e.g. "tool:market_quote"
    ref: str = Field(default="", max_length=200)  # e.g. a call id or memory record id
    uri: str | None = Field(default=None, max_length=2048)


class ContextItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1, max_length=200)
    kind: ItemKind
    content: str
    trust: Trust = Trust.UNTRUSTED
    published_at: datetime | None = None
    provenance: Provenance
    # Higher survives longer when the budget forces a choice.
    priority: int = 0
    # For summaries: the ids of the full records this stands in for.
    pointers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _trust_follows_kind(self) -> ContextItem:
        harness = self.kind in HARNESS_KINDS
        if self.trust is Trust.TRUSTED and not harness:
            raise ValueError(f"a {self.kind.value} item carries outside text and cannot be TRUSTED")
        if self.published_at is not None and (
            self.published_at.tzinfo is None or self.published_at.utcoffset() is None
        ):
            raise ValueError("published_at must be timezone-aware")
        return self

    @property
    def is_trusted(self) -> bool:
        return self.trust is Trust.TRUSTED

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()

    @classmethod
    def harness(
        cls,
        kind: ItemKind,
        content: str,
        *,
        item_id: str,
        priority: int = 1000,
        origin: str = "harness",
    ) -> ContextItem:
        """A trusted, harness-authored item (system prompt, instructions, the operator task)."""
        return cls(
            item_id=item_id,
            kind=kind,
            content=content,
            trust=Trust.TRUSTED,
            provenance=Provenance(origin=origin),
            priority=priority,
        )

    @classmethod
    def outside(
        cls,
        kind: ItemKind,
        content: str,
        *,
        item_id: str,
        published_at: datetime | None,
        origin: str,
        ref: str = "",
        uri: str | None = None,
        priority: int = 0,
    ) -> ContextItem:
        """An untrusted item carrying outside text: tool output, memory, a document."""
        return cls(
            item_id=item_id,
            kind=kind,
            content=content,
            trust=Trust.UNTRUSTED,
            published_at=published_at,
            provenance=Provenance(origin=origin, ref=ref, uri=uri),
            priority=priority,
        )
