"""Approval request models and the evidence bundle.

Everything inside an :class:`EvidenceBundle` that came from the model (rationale,
source titles, verifier flags) is untrusted text that a human will read. Render
it as text only, never as markup or terminal control sequences; see
:func:`sanitize_for_display`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from keelgate.approvals._tiers import ApprovalTier

MAX_RATIONALE: Final = 4000
SIGNOFF_CODE_LENGTH: Final = 8

# C0/C1 controls except tab and newline. Stripping ESC alone is not enough: a
# bare CSI (0x9B) is an escape introducer on many terminals.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_for_display(text: str) -> str:
    """Strip control characters so model-derived text cannot drive a terminal."""
    return _CONTROL_RE.sub("", text)


class ApprovalStatus(StrEnum):
    """Where an approval request is: pending, approved, rejected, expired or consumed."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class EvidenceSource(BaseModel):
    """A source cited in an approval's evidence: where it came from, when it was retrieved, and a
    hash of its content.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str = Field(max_length=2048)
    title: str | None = Field(default=None, max_length=300)
    retrieved_at: datetime | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EvidenceBundle(BaseModel):
    """What an approver needs to decide, and what their decision is bound to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    args: dict[str, Any]
    rationale: str = Field(max_length=MAX_RATIONALE)
    sources: tuple[EvidenceSource, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    verifier_flags: tuple[str, ...] = ()
    policy_reasons: tuple[str, ...] = ()
    policy_version: str = ""

    def digest(self) -> str:
        document = self.model_dump(mode="json")
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


class Approver(BaseModel):
    """A human cleared to decide requests in one tenant up to ``max_tier``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approver_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    max_tier: ApprovalTier = ApprovalTier.ONE_CLICK


class ApprovalRequest(BaseModel):
    """A call parked for a human: the exact arguments' hash, the tier required, the evidence bundle,
    and the request's status and decision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    tenant_id: str
    agent_id: str
    tool_name: str
    args_hash: str
    tier: ApprovalTier
    evidence: EvidenceBundle
    evidence_hash: str
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None

    @property
    def signoff_code(self) -> str:
        """Short code derived from the evidence, echoed back for EXPLICIT_SIGNOFF."""
        return self.evidence_hash[:SIGNOFF_CODE_LENGTH]


def action_hash(tool: str, args: dict[str, Any]) -> str:
    """Hash of exactly what will run. An approval is bound to this value."""
    encoded = json.dumps(
        {"tool": tool, "args": args},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
