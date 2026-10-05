"""Loop state: everything a checkpoint must hold so a run can be resumed exactly.

The shape is driven by one requirement: **resuming must never repeat a completed WRITE.**
That needs more than idempotency keys, because a key is derived from the arguments, and
arguments come from a model that will not say the same thing twice. So:

* The model's *proposed actions* are saved **before** any of them runs (write-ahead). After
  a crash, a resume re-submits the *saved* arguments, never new ones from the model.
* Each action carries its own status. ``DONE`` actions are skipped outright; an action
  whose result is unknown is never retried, it waits for a human.
* The durable idempotency store covers the one window checkpoints cannot: the gap between a
  tool finishing and the checkpoint that records it.

State is plain data (Pydantic, JSON-serialisable) so any store can hold it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from keelgate.context.item import ContextItem

STATE_VERSION: Final = 1


class LoopType(StrEnum):
    TASK = "task"
    VERIFICATION = "verification"
    MONITOR = "monitor"


class Phase(StrEnum):
    PLAN = "plan"
    ACT = "act"
    OBSERVE = "observe"
    VERIFY = "verify"
    DONE = "done"


class StopReason(StrEnum):
    GOAL_REACHED = "goal_reached"
    MAX_ITERATIONS = "max_iterations"
    TOKEN_BUDGET = "token_budget"  # noqa: S105 - a stop reason, not a credential
    DOLLAR_BUDGET = "dollar_budget"
    TIMEOUT = "timeout"
    VERIFIER_REJECTIONS = "verifier_rejections"
    # The model has no known price, and a dollar budget is set: stop rather than spend unmetered.
    COST_UNKNOWN = "cost_unknown"
    # An action may or may not have taken effect. A human must reconcile it.
    OUTCOME_UNKNOWN = "outcome_unknown"
    APPROVAL_PENDING = "approval_pending"
    ERROR = "error"


# After these a resume has nothing to continue.
TERMINAL_REASONS: Final = frozenset({StopReason.GOAL_REACHED, StopReason.VERIFIER_REJECTIONS})


class ActionStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    DENIED = "denied"
    ERROR = "error"
    AWAITING_APPROVAL = "awaiting_approval"
    UNKNOWN = "unknown"
    ABANDONED = "abandoned"


# Statuses that are settled: a resume neither re-runs nor waits for them.
SETTLED_ACTIONS: Final = frozenset(
    {ActionStatus.DONE, ActionStatus.DENIED, ActionStatus.ERROR, ActionStatus.ABANDONED}
)


class ActionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    error_code: str | None = None
    message: str = ""
    hint: str = ""
    # Fixed-wording detail from the gateway, such as the policy reasons for a denial.
    details: dict[str, Any] = Field(default_factory=dict)
    # The tool output as JSON text. It is untrusted data and is only ever shown to the
    # model through a fenced, untrusted context item.
    output_json: str | None = None
    published_at: datetime | None = None
    replayed: bool = False


class PlannedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    status: ActionStatus = ActionStatus.PENDING
    approval_id: str | None = None
    outcome: ActionOutcome | None = None


class VerdictRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    iteration: int
    decision: str
    reasons: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()


class LoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_version: int = STATE_VERSION
    run_id: str
    tenant_id: str
    agent_id: str
    trace_id: str
    loop_type: LoopType = LoopType.TASK
    goal: str
    as_of: datetime

    phase: Phase = Phase.PLAN
    iteration: int = 0
    actions: list[PlannedAction] = Field(default_factory=list)
    pending_final_answer: str | None = None
    observations: list[ContextItem] = Field(default_factory=list)
    verdicts: list[VerdictRecord] = Field(default_factory=list)

    llm_calls: int = 0
    # Planner calls only, so a scripted double indexed by it is unaffected by verifier calls.
    plan_calls: int = 0
    tokens_used: int = 0
    dollars_used: float = 0.0
    cost_unknown: bool = False
    rejections: int = 0
    context_rejections: int = 0
    # Items the context builder refused, so a stale or future-dated item is audited once.
    context_rejected_ids: list[str] = Field(default_factory=list)
    active_seconds: float = 0.0

    stop_reason: StopReason | None = None
    final_answer: str | None = None
    error: str | None = None

    created_at: datetime
    updated_at: datetime
    checkpoint_seq: int = 0

    @property
    def finished(self) -> bool:
        return self.stop_reason in TERMINAL_REASONS

    def action(self, action_id: str) -> PlannedAction:
        for candidate in self.actions:
            if candidate.action_id == action_id:
                return candidate
        raise KeyError(action_id)
