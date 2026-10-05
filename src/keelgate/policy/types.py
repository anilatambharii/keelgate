"""Policy request and decision types.

Everything the engine sees is assembled by Keelgate from trusted sources. In
particular ``PolicyContext`` (limits, exposure, positions, mode, ``as_of``) is
supplied by harness code and must never be derived from model output or from any
tool result.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keelgate.approvals.tiers import ApprovalTier

MAX_REASONS = 20
MAX_REASON_LENGTH = 300


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class PolicyAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    side_effect: Literal["READ", "PROPOSE", "WRITE"]
    capability: str
    args: dict[str, Any] = Field(default_factory=dict)


class PolicyActor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str
    tenant_id: str
    grant_id: str


class PolicyContext(BaseModel):
    """Trusted facts about the world at ``as_of``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    # A plain string on purpose: the policy must be able to *see* and deny a
    # non-paper mode. The gateway also refuses anything but paper/simulation.
    execution_mode: str = "paper"
    positions: dict[str, float] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    exposure: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _as_of_is_aware(self) -> PolicyContext:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return self


class PolicyInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: PolicyAction
    actor: PolicyActor
    resource: dict[str, Any] = Field(default_factory=dict)
    context: PolicyContext

    def to_document(self) -> dict[str, Any]:
        """The JSON document handed to the engine.

        ``as_of`` is rendered as UTC with an explicit ``+00:00`` offset, the one
        form both OPA and the in-process evaluator parse. NaN and infinity are
        refused outright (``allow_nan=False``): they compare false against every
        limit, which is exactly how a limit check gets bypassed.
        """
        document = self.model_dump(mode="python")
        document["context"]["as_of"] = self.context.as_of.astimezone(UTC).isoformat()
        encoded = json.dumps(document, allow_nan=False, default=_reject_unserialisable)
        result: dict[str, Any] = json.loads(encoded)
        return result


def _reject_unserialisable(value: object) -> object:
    raise TypeError(f"policy input contains a non-JSON value of type {type(value).__name__}")


class PolicyDecision(BaseModel):
    """What the policy decided, and exactly which policy decided it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect: Decision
    reasons: tuple[str, ...] = ()
    approval_tier: ApprovalTier | None = None
    policy_version: str
    engine: str

    @model_validator(mode="after")
    def _consistent(self) -> PolicyDecision:
        if self.effect is Decision.REQUIRE_APPROVAL:
            if self.approval_tier in (None, ApprovalTier.AUTO):
                raise ValueError("REQUIRE_APPROVAL needs ONE_CLICK or EXPLICIT_SIGNOFF")
        elif self.approval_tier is not None:
            raise ValueError("only REQUIRE_APPROVAL carries an approval tier")
        return self

    @property
    def allowed(self) -> bool:
        return self.effect is Decision.ALLOW
