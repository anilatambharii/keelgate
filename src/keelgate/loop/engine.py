"""The loop: plan, act, observe, verify, then revise or stop.

Every transition is checkpointed, and every action goes through the tool gateway, so the loop
adds no new way to do anything: it can only ask, and the gateway still decides.

Crash safety, in the order a crash can happen:

1. *During planning.* Nothing has run. A resume plans again.
2. *After planning, before acting.* The proposed actions were saved first (write-ahead), so a
   resume runs those exact actions rather than asking the model again.
3. *While a tool runs.* The idempotency key stays ``IN_FLIGHT``, which the gateway reads as
   "outcome unknown". The loop stops and waits for a human; it never retries.
4. *After a tool finished, before the checkpoint.* The key is ``DONE``. The resume re-submits
   the saved action and the gateway replays the stored result instead of repeating it.
5. *After the checkpoint.* The action is ``DONE`` and is simply skipped.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from opentelemetry.trace import Status, StatusCode

from keelgate.approvals.models import ApprovalStatus
from keelgate.audit.records import EventType
from keelgate.context import (
    BuiltContext,
    ContextBuilder,
    ContextItem,
    ItemKind,
    Rejection,
)
from keelgate.context.tokens import ApproxTokenCounter
from keelgate.llm.types import LLMError, Message, ToolSchema, Usage
from keelgate.loop.roles import (
    AcceptAllVerifier,
    Planner,
    PlanRequest,
    Verdict,
    VerdictDecision,
    Verifier,
    VerifyRequest,
)
from keelgate.loop.state import (
    SETTLED_ACTIONS,
    TERMINAL_REASONS,
    ActionOutcome,
    ActionStatus,
    LoopState,
    LoopType,
    Phase,
    PlannedAction,
    StopReason,
    VerdictRecord,
)
from keelgate.loop.stop import StopConditions
from keelgate.telemetry import attributes as attr
from keelgate.telemetry.core import set_attributes, span
from keelgate.telemetry.hooks import run_span
from keelgate.tools.outcomes import ErrorCode, OutcomeStatus, ToolOutcome
from keelgate.tools.spec import SideEffect

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from keelgate.approvals.queue import ApprovalQueue
    from keelgate.audit.log import AuditLog
    from keelgate.context.compaction import Summarizer
    from keelgate.context.tokens import TokenCounter
    from keelgate.llm.pricing import PricingTable
    from keelgate.loop.checkpoint import CheckpointStore
    from keelgate.memory.tiers import EpisodicMemory
    from keelgate.policy.types import PolicyContext
    from keelgate.tools.spec import ToolRegistry

DEFAULT_SYSTEM_PROMPT: Final = (
    "You are an agent working toward the goal below. Use the offered tools to gather "
    "information and to act. When the goal is met, reply with a final answer in plain text "
    "and no tool call. Some actions need human approval or may be refused; if one is, do not "
    "try another route to the same effect."
)
MAX_STORED_OUTPUT: Final = 16_000
# Results that mean "stop and look", not "tell the model and carry on".
_HARD_ERRORS: Final = frozenset(
    {
        ErrorCode.AUDIT_UNAVAILABLE.value,
        ErrorCode.INTERNAL.value,
    }
)


class RunExistsError(Exception):
    """``run`` was asked to start a run id that already has a checkpoint."""


class RunNotFoundError(Exception):
    """``resume`` found no checkpoint for that tenant and run id."""


class ToolCaller(Protocol):
    """What the loop needs from a gateway: one governed call. ``ToolGateway`` is the real one;
    replay substitutes a recorded one that runs nothing."""

    async def call(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        grant_token: str,
        context: Any,
    ) -> ToolOutcome: ...


class OutcomeConfirmer(Protocol):
    """Settles an action whose outcome is unknown by asking the downstream system of record.

    Return ``True`` if the action took effect, ``False`` if it definitely did not, and ``None``
    when the answer is not certain (the loop then stops for a human, as before).
    """

    name: str

    async def confirm(self, action: PlannedAction, state: LoopState) -> bool | None: ...


class ContextSource(Protocol):
    """Supplies extra context for a plan step, such as retrieved memory.

    Items must be dated (``published_at``) and untrusted; the builder enforces both.
    """

    def retrieve(self, state: LoopState) -> Sequence[ContextItem]: ...


@dataclass(frozen=True)
class LoopResult:
    state: LoopState

    @property
    def stop_reason(self) -> StopReason | None:
        return self.state.stop_reason

    @property
    def final_answer(self) -> str | None:
        return self.state.final_answer

    @property
    def ok(self) -> bool:
        return self.state.stop_reason is StopReason.GOAL_REACHED

    @property
    def resumable(self) -> bool:
        return self.state.stop_reason is not None and self.state.stop_reason not in TERMINAL_REASONS


@dataclass
class _Run:
    state: LoopState
    stop: StopConditions
    mark: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _status(action: PlannedAction) -> ActionStatus:
    return action.status


class Loop:
    def __init__(
        self,
        *,
        gateway: ToolCaller,
        registry: ToolRegistry,
        planner: Planner,
        checkpoints: CheckpointStore,
        grant_token: str | Callable[[], str],
        policy_context: Callable[[datetime], PolicyContext],
        stop: StopConditions | None = None,
        verifier: Verifier | None = None,
        audit: AuditLog | None = None,
        approvals: ApprovalQueue | None = None,
        episodic: EpisodicMemory | None = None,
        context_sources: Sequence[ContextSource] = (),
        context_budget: int = 8000,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        counter: TokenCounter | None = None,
        pricing: PricingTable | None = None,
        price_model: str = "",
        confirmer: OutcomeConfirmer | None = None,
        summarizer: Summarizer | None = None,
        clock: Callable[[], datetime] = _utc_now,
        allowed_side_effects: frozenset[SideEffect] | None = None,
        loop_type: LoopType = LoopType.TASK,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._planner = planner
        self._checkpoints = checkpoints
        self._grant_token = grant_token if callable(grant_token) else (lambda: grant_token)
        self._policy_context = policy_context
        self._stop = stop or StopConditions()
        self._verifier: Verifier = verifier or AcceptAllVerifier()
        self._audit = audit
        self._approvals = approvals
        self._episodic = episodic
        self._sources = tuple(context_sources)
        self._context_budget = context_budget
        self._system_prompt = system_prompt
        self._counter = counter
        self._pricing = pricing
        self._confirmer = confirmer
        self._price_model = price_model
        self._summarizer = summarizer
        self._clock = clock
        self._allowed = allowed_side_effects
        self._loop_type = loop_type
        # A test seam: called with a name at each crash window. A real run leaves it None.
        self._failpoint_fn = failpoint

    # ------------------------------------------------------------------- entry points

    async def run(
        self,
        *,
        goal: str,
        tenant_id: str,
        agent_id: str,
        as_of: datetime,
        run_id: str | None = None,
        trace_id: str | None = None,
        stop: StopConditions | None = None,
    ) -> LoopResult:
        """Start a new run. Refuses an id that already has a checkpoint."""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        rid = run_id or uuid.uuid4().hex
        if self._checkpoints.load(tenant_id, rid) is not None:
            raise RunExistsError(f"run {rid!r} already exists; use resume() or run_or_resume()")
        now = self._clock()
        # One run is one trace: the root span's trace id becomes the run's trace id (unless the
        # caller supplied one), and a resume in any process parents on this same root.
        with run_span(
            tenant_id=tenant_id,
            agent_id=agent_id,
            run_id=rid,
            loop_type=self._loop_type.value,
            trace_id=trace_id,
            extra={attr.AS_OF: as_of.isoformat()},
        ) as root:
            state = LoopState(
                run_id=rid,
                tenant_id=tenant_id,
                agent_id=agent_id,
                trace_id=trace_id or root.trace_id or uuid.uuid4().hex,
                root_span_id=root.span_id or "",
                loop_type=self._loop_type,
                goal=goal,
                as_of=as_of,
                created_at=now,
                updated_at=now,
            )
            set_attributes(root.span, {"keelgate.trace_id": state.trace_id})
            run = _Run(state, stop or self._stop, now)
            self._save(run, "start")
            result = await self._drive(run)
            self._close_run_span(root.span, result.state)
            return result

    async def resume(
        self, tenant_id: str, run_id: str, *, stop: StopConditions | None = None
    ) -> LoopResult:
        """Continue a stopped or interrupted run from its latest checkpoint.

        Pass a new ``stop`` to continue after a budget stop with a larger budget.
        """
        state = self._checkpoints.load(tenant_id, run_id)
        if state is None:
            raise RunNotFoundError(f"no run {run_id!r} for tenant {tenant_id!r}")
        if state.finished:
            return LoopResult(state)
        with run_span(
            tenant_id=tenant_id,
            agent_id=state.agent_id,
            run_id=run_id,
            loop_type=state.loop_type.value,
            trace_id=state.trace_id,
            root_span_id=state.root_span_id or None,
            resumed=True,
            extra={attr.AS_OF: state.as_of.isoformat(), "keelgate.trace_id": state.trace_id},
        ) as root:
            result = await self._drive(_Run(state, stop or self._stop, self._clock()))
            self._close_run_span(root.span, result.state)
            return result

    @staticmethod
    def _close_run_span(root: Any, state: LoopState) -> None:
        set_attributes(
            root,
            {
                attr.STOP_REASON: state.stop_reason.value if state.stop_reason else None,
                attr.ITERATION: state.iteration,
                "keelgate.loop.tokens_used": state.tokens_used,
                "keelgate.loop.dollars_used": state.dollars_used,
            },
        )
        if state.stop_reason is StopReason.ERROR:
            root.set_status(Status(StatusCode.ERROR, "loop error"))

    async def run_or_resume(
        self,
        *,
        goal: str,
        tenant_id: str,
        agent_id: str,
        as_of: datetime,
        run_id: str,
        trace_id: str | None = None,
        stop: StopConditions | None = None,
    ) -> LoopResult:
        """Idempotent start: resume the run if it exists, otherwise begin it."""
        if self._checkpoints.load(tenant_id, run_id) is not None:
            return await self.resume(tenant_id, run_id, stop=stop)
        return await self.run(
            goal=goal,
            tenant_id=tenant_id,
            agent_id=agent_id,
            as_of=as_of,
            run_id=run_id,
            trace_id=trace_id,
            stop=stop,
        )

    async def reconcile(
        self,
        tenant_id: str,
        run_id: str,
        action_id: str,
        *,
        executed: bool,
        by: str,
        note: str = "",
    ) -> LoopState:
        """A human settles an action whose outcome was unknown.

        ``executed=True`` records it as done; ``executed=False`` abandons it. Either way the
        action is never run again. If it must be attempted afresh, plan a new action with a
        new idempotency key.
        """
        state = self._checkpoints.load(tenant_id, run_id)
        if state is None:
            raise RunNotFoundError(f"no run {run_id!r} for tenant {tenant_id!r}")
        action = state.action(action_id)
        if action.status is not ActionStatus.UNKNOWN:
            raise ValueError(f"action {action_id} is {action.status.value}, not unknown")
        action.status = ActionStatus.DONE if executed else ActionStatus.ABANDONED
        action.outcome = ActionOutcome(
            status="reconciled",
            message=(
                f"reconciled by {by}: {'executed' if executed else 'did not execute'}. {note}"
            ).strip(),
        )
        run = _Run(state, self._stop, self._clock())
        self._save(run, f"reconcile:{action_id}")
        self._audit_reconcile(state, action, executed=executed, by=by, source="human")
        return state

    # ------------------------------------------------------------------------- driving

    async def _drive(self, run: _Run) -> LoopResult:
        state = run.state
        state.stop_reason = None
        state.error = None
        try:
            while True:
                reason = self._stop_reason(run)
                if reason is None:
                    reason = await self._step(run)
                if reason is not None:
                    return self._finish(run, reason)
        except LLMError as exc:
            state.error = f"{type(exc).__name__}: {exc}"[:300]
            return self._finish(run, StopReason.ERROR)

    async def _step(self, run: _Run) -> StopReason | None:
        phase = run.state.phase
        if phase is Phase.DONE:
            return StopReason.GOAL_REACHED
        with span(f"keelgate.loop.{phase.value}", attributes={attr.ITERATION: run.state.iteration}):
            if phase is Phase.PLAN:
                return await self._plan(run)
            if phase is Phase.ACT:
                return await self._act(run)
            if phase is Phase.OBSERVE:
                return self._observe(run)
            return await self._verify(run)

    def _stop_reason(self, run: _Run) -> StopReason | None:
        state, stop = run.state, run.stop
        if (
            state.phase is Phase.PLAN
            and stop.max_iterations
            and state.iteration >= stop.max_iterations
        ):
            return StopReason.MAX_ITERATIONS
        if stop.timeout is not None:
            elapsed = state.active_seconds + (self._clock() - run.mark).total_seconds()
            if elapsed >= stop.timeout.total_seconds():
                return StopReason.TIMEOUT
        if stop.max_tokens is not None and state.tokens_used >= stop.max_tokens:
            return StopReason.TOKEN_BUDGET
        if stop.max_dollars is not None:
            if state.cost_unknown:
                return StopReason.COST_UNKNOWN
            if state.dollars_used >= stop.max_dollars:
                return StopReason.DOLLAR_BUDGET
        return None

    def _finish(self, run: _Run, reason: StopReason) -> LoopResult:
        state = run.state
        state.stop_reason = reason
        if reason is StopReason.GOAL_REACHED:
            state.phase = Phase.DONE
        self._save(run, f"stop:{reason.value}")
        return LoopResult(state)

    # ------------------------------------------------------------------------- phases

    async def _plan(self, run: _Run) -> StopReason | None:
        state = run.state
        state.iteration += 1
        built = await self._build_context(run)
        tools = self._tool_schemas()
        # Do not pay for a call whose *input alone* would cross the token or dollar budget. (The
        # output cannot be known in advance, so a call may still overshoot by its output.)
        refused = self._precall_stop(run, built.messages, tools)
        if refused is not None:
            state.iteration -= 1  # nothing was planned; a resume with more budget retries it
            return refused
        plan = await self._planner.plan(
            PlanRequest(
                run_id=state.run_id,
                tenant_id=state.tenant_id,
                iteration=state.iteration,
                call_index=state.plan_calls,
                messages=built.messages,
                tools=tools,
            )
        )
        self._account(state, plan.usage)
        state.llm_calls += 1
        state.plan_calls += 1
        state.actions = [
            PlannedAction(
                action_id=f"{state.run_id}:{state.iteration}:{n}",
                tool=a.tool,
                arguments=a.arguments,
                rationale=a.rationale,
            )
            for n, a in enumerate(plan.actions)
        ]
        state.pending_final_answer = plan.final_answer
        if state.actions:
            state.phase = Phase.ACT
        elif plan.final_answer is not None:
            state.phase = Phase.VERIFY
        else:
            state.observations.append(
                self._note(
                    state, "plan", "The last step produced neither a tool call nor an answer."
                )
            )
            state.phase = Phase.PLAN
        # Write-ahead: the proposed actions are on disk before any of them runs.
        self._save(run, "plan")
        self._failpoint("after_plan")
        return None

    async def _auto_reconcile(self, run: _Run, action: PlannedAction) -> bool:
        """Ask the harness-supplied confirmer whether an unknown action really happened.

        Only a definite True or False settles it; None, an error, or no confirmer leaves it for a
        human. The confirmer is trusted harness code that queries the downstream system of
        record. It is never the model, and never the tool's own claim.
        """
        if self._confirmer is None:
            return False
        try:
            verdict = await self._confirmer.confirm(action, run.state)
        except Exception:
            return False
        if not isinstance(verdict, bool):
            return False
        action.status = ActionStatus.DONE if verdict else ActionStatus.ABANDONED
        action.outcome = ActionOutcome(
            status="reconciled",
            message=(
                f"confirmed by {self._confirmer.name}: "
                f"{'executed' if verdict else 'did not execute'}."
            ),
        )
        self._save(run, f"reconcile:{action.action_id}")
        self._audit_reconcile(
            run.state, action, executed=verdict, by=self._confirmer.name, source="confirmer"
        )
        return True

    def _audit_reconcile(
        self, state: LoopState, action: PlannedAction, *, executed: bool, by: str, source: str
    ) -> None:
        """Record who settled an unknown outcome, and how, in the audit chain."""
        if self._audit is None:
            return
        self._audit.append(
            tenant_id=state.tenant_id,
            event_type=EventType.OUTCOME_RECONCILED,
            actor=by,
            payload={
                "run_id": state.run_id,
                "trace_id": state.trace_id,
                "action_id": action.action_id,
                "tool": action.tool,
                "executed": executed,
                "source": source,
            },
        )

    def _precall_stop(
        self, run: _Run, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> StopReason | None:
        stop, state = run.stop, run.state
        if stop.max_tokens is None and stop.max_dollars is None:
            return None
        counter = self._counter or ApproxTokenCounter()
        estimate = sum(counter.count(m.content) for m in messages)
        estimate += sum(counter.count(t.model_dump_json()) for t in tools)
        if stop.max_tokens is not None and state.tokens_used + estimate > stop.max_tokens:
            return StopReason.TOKEN_BUDGET
        if stop.max_dollars is not None and self._pricing is not None:
            price = self._pricing.usage(self._price_model, estimate, 0).cost_usd
            # An unpriced model is handled after the call (COST_UNKNOWN); it is not guessed here.
            if price is not None and state.dollars_used + price > stop.max_dollars:
                return StopReason.DOLLAR_BUDGET
        return None

    async def _act(self, run: _Run) -> StopReason | None:  # noqa: PLR0911 - one return per stop
        state = run.state
        for action in state.actions:
            # Special statuses first: UNKNOWN waits for a human, never for a retry.
            if action.status is ActionStatus.UNKNOWN:
                if await self._auto_reconcile(run, action):
                    continue
                return StopReason.OUTCOME_UNKNOWN
            if action.status in SETTLED_ACTIONS:
                continue
            if action.status is ActionStatus.AWAITING_APPROVAL:
                verdict = self._approval_state(state, action)
                if verdict == "pending":
                    return StopReason.APPROVAL_PENDING
                if verdict == "closed":
                    action.status = ActionStatus.DENIED
                    action.outcome = ActionOutcome(
                        status="denied",
                        error_code="approval_not_granted",
                        message="The human approver rejected the action, or it expired.",
                    )
                    self._save(run, f"act:{action.action_id}")
                    continue
            early = self._stop_reason(run)
            if early is not None:
                return early

            self._failpoint("before_action")
            outcome = await self._gateway.call(
                tool_name=action.tool,
                arguments=action.arguments,
                grant_token=self._grant_token(),
                context=self._call_context(state, action),
            )
            # The crash window checkpoints cannot cover: the tool has run, nothing is saved.
            self._failpoint("after_gateway_call")
            hard = self._apply_outcome(state, action, outcome)
            self._save(run, f"act:{action.action_id}")
            self._failpoint("after_action_checkpoint")

            after = _status(action)  # _apply_outcome changed it; read it fresh
            if after is ActionStatus.AWAITING_APPROVAL:
                return StopReason.APPROVAL_PENDING
            settled_by_confirmer = False
            if after is ActionStatus.UNKNOWN:
                if not await self._auto_reconcile(run, action):
                    return StopReason.OUTCOME_UNKNOWN
                settled_by_confirmer = True
            if hard and not settled_by_confirmer:
                state.error = (
                    f"{action.tool}: {action.outcome.error_code if action.outcome else ''}"
                )
                return StopReason.ERROR
        state.phase = Phase.OBSERVE
        self._save(run, "acted")
        return None

    def _observe(self, run: _Run) -> StopReason | None:
        state = run.state
        for action in state.actions:
            item = self._observation_item(state, action)
            if all(existing.item_id != item.item_id for existing in state.observations):
                state.observations.append(item)
        state.phase = Phase.VERIFY
        self._save(run, "observed")
        self._failpoint("after_observe")
        goal = run.stop.goal
        if goal is not None and goal(state):
            return StopReason.GOAL_REACHED
        return None

    async def _verify(self, run: _Run) -> StopReason | None:
        state = run.state
        verdict = await self._verifier.verify(
            VerifyRequest(
                run_id=state.run_id,
                tenant_id=state.tenant_id,
                iteration=state.iteration,
                goal=state.goal,
                final_answer=state.pending_final_answer,
                actions=tuple((a.tool, a.status.value) for a in state.actions),
                observations=tuple(state.observations),
            )
        )
        if verdict.usage is not None:
            self._account(state, verdict.usage)
            state.llm_calls += 1
        state.verdicts.append(
            VerdictRecord(
                iteration=state.iteration,
                decision=verdict.decision.value,
                reasons=verdict.reasons,
                flags=verdict.flags,
            )
        )
        reason: StopReason | None = None
        if verdict.decision is VerdictDecision.ACCEPT:
            if state.pending_final_answer is not None:
                state.final_answer = state.pending_final_answer
                reason = StopReason.GOAL_REACHED
        else:
            state.rejections += 1
            state.observations.append(self._feedback(state, verdict))
            limit = run.stop.max_verifier_rejections
            if limit is not None and state.rejections >= limit:
                reason = StopReason.VERIFIER_REJECTIONS

        state.phase = Phase.DONE if reason is StopReason.GOAL_REACHED else Phase.PLAN
        state.actions = []
        state.pending_final_answer = None
        self._save(run, "verified")
        self._failpoint("after_verify")
        return reason

    # ------------------------------------------------------------------------ helpers

    def _tool_schemas(self) -> tuple[ToolSchema, ...]:
        schemas = []
        for name in self._registry.names():
            tool = self._registry.get(name)
            if tool is None or (
                self._allowed is not None and tool.spec.side_effect not in self._allowed
            ):
                continue
            schemas.append(
                ToolSchema(
                    name=tool.spec.name,
                    description=tool.spec.description,
                    input_schema=tool.spec.input_schema
                    or tool.spec.input_model.model_json_schema(),
                )
            )
        return tuple(schemas)

    async def _build_context(self, run: _Run) -> BuiltContext:
        state = run.state
        builder = ContextBuilder(
            as_of=state.as_of,
            token_budget=self._context_budget,
            counter=self._counter,
            summarizer=self._summarizer,
        )
        builder.add(ContextItem.harness(ItemKind.SYSTEM, self._system_prompt, item_id="system"))
        builder.add(ContextItem.harness(ItemKind.TASK, f"Goal: {state.goal}", item_id="task"))
        for source in self._sources:
            for item in source.retrieve(state):
                builder.try_add(item)
        for item in state.observations:
            builder.try_add(item)
        built = await builder.build()
        self._record_rejections(state, built.rejections)
        return built

    def _record_rejections(self, state: LoopState, rejections: Sequence[Rejection]) -> None:
        for rejection in rejections:
            if rejection.item_id in state.context_rejected_ids:
                continue
            state.context_rejected_ids.append(rejection.item_id)
            state.context_rejections += 1
            if self._audit is not None:
                self._audit.append(
                    tenant_id=state.tenant_id,
                    event_type=EventType.CONTEXT_REJECTED,
                    actor=state.agent_id,
                    payload={
                        "run_id": state.run_id,
                        "trace_id": state.trace_id,
                        "item_id": rejection.item_id,
                        "reason": rejection.reason.value,
                        "origin": rejection.origin,
                        "published_at": rejection.published_at.isoformat()
                        if rejection.published_at
                        else None,
                        "as_of": rejection.as_of.isoformat(),
                        "content_sha256": rejection.content_sha256,
                    },
                )

    @staticmethod
    def _account(state: LoopState, usage: Usage) -> None:
        state.tokens_used += usage.total_tokens
        if usage.cost_usd is None:
            state.cost_unknown = True
        else:
            state.dollars_used += usage.cost_usd

    def _call_context(self, state: LoopState, action: PlannedAction) -> Any:
        from keelgate.tools.gateway import CallContext  # noqa: PLC0415 - avoid an import cycle

        flags = state.verdicts[-1].flags if state.verdicts else ()
        return CallContext(
            tenant_id=state.tenant_id,
            policy_context=self._policy_context(state.as_of),
            rationale=action.rationale[:4000],
            verifier_flags=flags,
            approval_id=action.approval_id,
            allowed_side_effects=self._allowed,
        )

    def _approval_state(self, state: LoopState, action: PlannedAction) -> str:
        if self._approvals is None or action.approval_id is None:
            return "pending"
        try:
            status = self._approvals.get(state.tenant_id, action.approval_id).status
        except Exception:  # an unreadable approval is never treated as granted
            return "pending"
        if status is ApprovalStatus.PENDING:
            return "pending"
        # CONSUMED means a previous attempt already used it: re-submit, and the gateway's
        # idempotency replays the stored result rather than running the action again.
        if status in (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED):
            return "approved"
        return "closed"

    def _apply_outcome(self, state: LoopState, action: PlannedAction, outcome: ToolOutcome) -> bool:
        """Record a gateway result on the action.

        Returns True for a failure that must stop the loop rather than be reported to the model.
        """
        if outcome.status is OutcomeStatus.OK:
            published: datetime | None = None
            output_json: str | None = None
            if outcome.output is not None:
                # The one place tool output is unwrapped. It stays text from here on and
                # reaches the model only inside a fenced, untrusted context item.
                payload = outcome.output.unwrap_untrusted()
                output_json = payload.model_dump_json()[:MAX_STORED_OUTPUT]
                candidate = getattr(payload, "published_at", None)
                if isinstance(candidate, datetime) and candidate.utcoffset() is not None:
                    published = candidate
            action.status = ActionStatus.DONE
            action.outcome = ActionOutcome(
                status="ok",
                output_json=output_json,
                published_at=published,
                replayed=outcome.replayed,
            )
            self._remember(state, action)
            return False
        if outcome.status is OutcomeStatus.APPROVAL_REQUIRED:
            action.status = ActionStatus.AWAITING_APPROVAL
            action.approval_id = outcome.approval_id
            action.outcome = ActionOutcome(status="approval_required")
            return False

        error = outcome.error
        code = error.code.value if error else "unknown"
        action.outcome = ActionOutcome(
            status=outcome.status.value.lower(),
            error_code=code,
            message=error.message if error else "",
            hint=error.hint if error else "",
            details=dict(error.details) if error else {},
        )
        if code in (ErrorCode.OUTCOME_UNKNOWN.value, ErrorCode.AUDIT_FAILED_AFTER_EXECUTION.value):
            action.status = ActionStatus.UNKNOWN
            return False
        action.status = (
            ActionStatus.DENIED if outcome.status is OutcomeStatus.DENIED else ActionStatus.ERROR
        )
        return code in _HARD_ERRORS

    def _remember(self, state: LoopState, action: PlannedAction) -> None:
        if self._episodic is None:
            return
        from keelgate.memory.types import Attribution  # noqa: PLC0415 - avoid an import cycle

        tool = self._registry.get(action.tool)
        if tool is None or tool.spec.side_effect is SideEffect.READ:
            return
        self._episodic.record_episode(
            f"{action.tool} {json.dumps(action.arguments, sort_keys=True)}",
            attribution=Attribution(agent_id=state.agent_id, trace_id=state.trace_id),
            outcome=(action.outcome.output_json or "ok") if action.outcome else None,
            key=action.tool,
        )

    def _observation_item(self, state: LoopState, action: PlannedAction) -> ContextItem:
        outcome = action.outcome
        body: dict[str, Any] = {
            "tool": action.tool,
            "arguments": action.arguments,
            "status": action.status.value,
        }
        if outcome is not None:
            if outcome.error_code:
                body["error"] = {
                    "code": outcome.error_code,
                    "message": outcome.message,
                    "hint": outcome.hint,
                    "details": outcome.details,
                }
            if outcome.output_json is not None:
                try:
                    body["output"] = json.loads(outcome.output_json)
                except ValueError:
                    body["output"] = outcome.output_json
        published = (outcome.published_at if outcome else None) or state.as_of
        return ContextItem.outside(
            ItemKind.OBSERVATION,
            json.dumps(body, sort_keys=True, default=str),
            item_id=f"obs:{action.action_id}",
            published_at=published,
            origin=f"tool:{action.tool}",
            ref=action.action_id,
        )

    @staticmethod
    def _feedback(state: LoopState, verdict: Verdict) -> ContextItem:
        text = f"Verifier verdict: {verdict.decision.value}. " + " ".join(verdict.reasons)
        return ContextItem.outside(
            ItemKind.OBSERVATION,
            text.strip(),
            item_id=f"verdict:{state.iteration}",
            published_at=state.as_of,
            origin="verifier",
            ref=str(state.iteration),
        )

    @staticmethod
    def _note(state: LoopState, label: str, text: str) -> ContextItem:
        return ContextItem.outside(
            ItemKind.OBSERVATION,
            text,
            item_id=f"note:{label}:{state.iteration}",
            published_at=state.as_of,
            origin="harness",
        )

    def _save(self, run: _Run, label: str) -> None:
        state = run.state
        now = self._clock()
        state.active_seconds += max(0.0, (now - run.mark).total_seconds())
        run.mark = now
        state.updated_at = now
        state.checkpoint_seq += 1
        self._checkpoints.save(state)
        if self._audit is not None:
            self._audit.append(
                tenant_id=state.tenant_id,
                event_type=EventType.LOOP_TRANSITION,
                actor=state.agent_id,
                payload={
                    "run_id": state.run_id,
                    "trace_id": state.trace_id,
                    "iteration": state.iteration,
                    "phase": state.phase.value,
                    "label": label,
                    "checkpoint_seq": state.checkpoint_seq,
                },
            )

    def _failpoint(self, name: str) -> None:
        if self._failpoint_fn is not None:
            self._failpoint_fn(name)
