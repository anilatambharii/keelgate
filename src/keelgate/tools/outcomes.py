"""Structured, model-actionable results from the tool gateway.

A refusal is a normal, expected result, not an exception: the loop feeds it back
to the model so it can change course. Each error carries a stable ``code``, whether
a retry could help, and a ``hint`` telling the model what to do instead.

Error messages are fixed wording. They never echo model-supplied strings, and
authorisation failures are deliberately uninformative; the precise reason goes to
the audit log, not back to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from keelgate.approvals.tiers import ApprovalTier
    from keelgate.policy.types import PolicyDecision

T = TypeVar("T")


class OutcomeStatus(StrEnum):
    OK = "OK"
    DENIED = "DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    ERROR = "ERROR"


class ErrorCode(StrEnum):
    NOT_AUTHORISED = "not_authorised"
    CAPABILITY_DENIED = "capability_denied"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    EXECUTION_MODE_FORBIDDEN = "execution_mode_forbidden"
    BUDGET_EXCEEDED = "budget_exceeded"
    POLICY_DENIED = "policy_denied"
    POLICY_UNAVAILABLE = "policy_unavailable"
    APPROVAL_UNAVAILABLE = "approval_unavailable"
    APPROVAL_INVALID = "approval_invalid"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OUTCOME_UNKNOWN = "outcome_unknown"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_FAILED = "tool_failed"
    INVALID_TOOL_OUTPUT = "invalid_tool_output"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    AUDIT_FAILED_AFTER_EXECUTION = "audit_failed_after_execution"
    INTERNAL = "internal_error"


class ToolError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False
    hint: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class Untrusted(Generic[T]):
    """Marks data that came from outside the harness.

    Tool output, retrieved documents and web content may contain text written by
    an attacker. Wrapping it makes the trust boundary visible in the types: code
    must call :meth:`unwrap_untrusted` by name to read it, and a reviewer can grep
    for every place that does.
    """

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def unwrap_untrusted(self) -> T:
        return self._value

    def __repr__(self) -> str:
        return "Untrusted(<redacted>)"


@dataclass(frozen=True)
class ToolOutcome:
    status: OutcomeStatus
    tool: str
    call_id: str
    output: Untrusted[BaseModel] | None = None
    error: ToolError | None = None
    approval_id: str | None = None
    approval_tier: ApprovalTier | None = None
    policy: PolicyDecision | None = None
    replayed: bool = False
    audit_seq: int | None = field(default=None, compare=False)

    @property
    def ok(self) -> bool:
        return self.status is OutcomeStatus.OK

    def for_model(self) -> dict[str, Any]:
        """The control-plane part of the result, safe to show a model.

        Deliberately excludes ``output``: that is untrusted data, and the caller
        must decide explicitly how to present it.
        """
        view: dict[str, Any] = {"status": self.status.value, "tool": self.tool}
        if self.error is not None:
            view["error"] = self.error.model_dump(mode="json")
        if self.approval_id is not None:
            view["approval_id"] = self.approval_id
            view["approval_tier"] = self.approval_tier.value if self.approval_tier else None
            view["hint"] = (
                "A human must approve this action. Stop and wait; do not retry or "
                "attempt a different route to the same effect."
            )
        return view
