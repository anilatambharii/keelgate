"""The tool gateway: the only place a tool body is ever executed.

Every call walks the same gauntlet, and each step fails closed:

1. verify the signed grant, then check it belongs to this tenant
2. look the tool up, then check the grant names its capability
3. refuse any execution mode but paper or simulation (v1 has no live path)
4. validate the arguments against the tool's input model
5. reserve the budget
6. for WRITE: answer an exact repeat from the idempotency store
7. for PROPOSE and WRITE: ask the policy engine
   ALLOW runs, DENY stops, REQUIRE_APPROVAL parks the call for a human
8. run the tool under its timeout, validate its output, audit the result

Audit is written **before** anything with a side effect, so an action can never
happen without a durable record that it was authorised. Nothing downstream of
the model can alter this order: the policy context is supplied by trusted harness
code, and tool output is returned wrapped as untrusted data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ValidationError

from keelgate.approvals import (
    ApprovalError,
    ApprovalQueue,
    EvidenceBundle,
    EvidenceSource,
    action_hash,
)
from keelgate.approvals.models import MAX_RATIONALE
from keelgate.audit import AuditError, AuditLog, EventType, canonical_json
from keelgate.capabilities import (
    BudgetLedger,
    CapabilityGrant,
    GrantError,
    GrantVerifier,
    InMemoryBudgetLedger,
)
from keelgate.policy import (
    Decision,
    PolicyAction,
    PolicyActor,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
)
from keelgate.policy.engine import deny as policy_deny
from keelgate.tools.idempotency import (
    ClaimState,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)
from keelgate.tools.outcomes import (
    ErrorCode,
    OutcomeStatus,
    ToolError,
    ToolOutcome,
    Untrusted,
)
from keelgate.tools.spec import SideEffect, Tool, ToolRefusedError, ToolRegistry

if TYPE_CHECKING:
    from datetime import timedelta

# v1 has no live brokerage or real-money path. This is not configurable upward.
PAPER_MODES: Final = frozenset({"paper", "simulation"})
MAX_ARGUMENT_BYTES: Final = 64 * 1024
MAX_AUDITED_OUTPUT_BYTES: Final = 32 * 1024
MAX_IDEMPOTENCY_KEY: Final = 200


@dataclass(frozen=True)
class CallContext:
    """Per-call facts, split by trust.

    ``tenant_id`` and ``policy_context`` come from the harness and are trusted:
    they feed the policy engine. ``rationale``, ``sources``, ``confidence`` and
    ``verifier_flags`` come from the model or its tooling and are only ever shown
    to a human approver; they never influence a decision.
    """

    tenant_id: str
    policy_context: PolicyContext
    rationale: str = ""
    sources: tuple[EvidenceSource, ...] = ()
    confidence: float | None = None
    verifier_flags: tuple[str, ...] = ()
    approval_id: str | None = None
    approval_ttl: timedelta | None = None
    # Restricts this call to the listed kinds of tool, whatever the grant allows. A
    # read-only monitor passes {READ}; ``None`` means no extra restriction.
    allowed_side_effects: frozenset[SideEffect] | None = None
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class _State:
    ctx: CallContext
    tool_name: str
    actor: str = "unauthenticated"
    grant: CapabilityGrant | None = None
    tool: Tool | None = None
    validated: BaseModel | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    args_hash: str = ""
    idempotency_key: str | None = None
    reserved: float = 0.0
    committed: bool = False
    cleared: bool = False
    policy: PolicyDecision | None = None
    seq: int | None = None


class ToolGateway:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        verifier: GrantVerifier,
        engine: PolicyEngine,
        audit: AuditLog,
        approvals: ApprovalQueue | None = None,
        ledger: BudgetLedger | None = None,
        idempotency: IdempotencyStore | None = None,
        allowed_modes: frozenset[str] = PAPER_MODES,
    ) -> None:
        if not allowed_modes or not allowed_modes <= PAPER_MODES:
            raise ValueError("v1 permits only the paper and simulation execution modes")
        registry.freeze()  # no tool may appear after the gateway is built
        self._registry = registry
        self._verifier = verifier
        self._engine = engine
        self._audit = audit
        self._approvals = approvals
        self._ledger: BudgetLedger = ledger or InMemoryBudgetLedger()
        self._idempotency: IdempotencyStore = idempotency or InMemoryIdempotencyStore()
        self._allowed_modes = allowed_modes

    # ------------------------------------------------------------------ entry

    async def call(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        grant_token: str,
        context: CallContext,
    ) -> ToolOutcome:
        """Run one proposed tool call through every gate. Never raises for a refusal."""
        state = _State(ctx=context, tool_name=tool_name)
        try:
            refusal = await self._gate(state, arguments, grant_token)
        except AuditError:
            self._release(state)
            return self._bare(state, ErrorCode.AUDIT_UNAVAILABLE, "The audit log is unavailable.")
        except Exception:  # fail closed on any bug before execution
            self._release(state)
            return self._bare(state, ErrorCode.INTERNAL, "The call could not be processed.")
        if refusal is not None:
            self._release(state)
            return refusal
        return await self._execute(state)

    # ------------------------------------------------------------------- gate

    async def _gate(  # noqa: PLR0911 - each early return is one distinct gate refusal
        self, st: _State, arguments: Mapping[str, Any], grant_token: str
    ) -> ToolOutcome | None:
        ctx = st.ctx
        cleaned = _normalise_arguments(arguments)
        self._append(
            st,
            EventType.TOOL_CALL,
            {
                "call_id": ctx.call_id,
                "tool": st.tool_name,
                "arguments": cleaned if cleaned is not None else "<unserialisable>",
            },
        )
        if cleaned is None:
            return self._refuse(
                st,
                status=OutcomeStatus.ERROR,
                code=ErrorCode.INVALID_ARGUMENTS,
                message="The arguments must be a JSON object of reasonable size.",
                hint="Send plain JSON values only: no NaN, no infinity, no binary data.",
            )

        # 1. grant, tenant
        try:
            grant = self._verifier.verify(grant_token)
        except GrantError as exc:
            self._append(st, EventType.GRANT_REJECTED, {"call_id": ctx.call_id, "reason": exc.code})
            return self._not_authorised(st)
        st.grant, st.actor = grant, grant.agent_id
        if grant.tenant_id != ctx.tenant_id:
            self._append(
                st,
                EventType.GRANT_REJECTED,
                {"call_id": ctx.call_id, "reason": "tenant_mismatch", "grant_id": grant.grant_id},
            )
            return self._not_authorised(st)

        # 2. tool, capability
        tool = self._registry.get(st.tool_name)
        if tool is None:
            return self._refuse(
                st,
                status=OutcomeStatus.ERROR,
                code=ErrorCode.UNKNOWN_TOOL,
                message="There is no tool with that name.",
                hint="Use only the tools you were offered.",
            )
        st.tool = tool
        if not grant.allows(tool.spec.capability):
            self._append(
                st,
                EventType.GRANT_REJECTED,
                {
                    "call_id": ctx.call_id,
                    "reason": "capability_missing",
                    "capability": str(tool.spec.capability),
                    "grant_id": grant.grant_id,
                },
            )
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.CAPABILITY_DENIED,
                message="You are not permitted to use this tool.",
                hint="Do not retry. Choose a different approach or ask the operator.",
                log=False,
            )

        allowed = ctx.allowed_side_effects
        if allowed is not None and tool.spec.side_effect not in allowed:
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.SIDE_EFFECT_NOT_PERMITTED,
                message="This run is not permitted to use that kind of tool.",
                hint="This run is restricted, for example to read-only tools. Do not retry.",
                reason=f"{tool.spec.side_effect.value} is outside this run's allowed side effects",
            )

        # 3. paper only
        mode = ctx.policy_context.execution_mode
        if mode not in self._allowed_modes:
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.EXECUTION_MODE_FORBIDDEN,
                message="Only paper and simulation execution are permitted.",
                hint="There is no live execution path. Do not retry.",
            )

        # 4. arguments
        try:
            validated = tool.spec.input_model.model_validate(cleaned)
        except ValidationError as exc:
            return self._refuse(
                st,
                status=OutcomeStatus.ERROR,
                code=ErrorCode.INVALID_ARGUMENTS,
                message="The arguments do not match the tool's input schema.",
                hint="Fix the listed fields and call again.",
                retryable=True,
                details={"fields": _field_errors(exc)},
            )
        st.validated = validated
        st.arguments = validated.model_dump(mode="json")
        st.args_hash = action_hash(tool.name, st.arguments)

        # 5. budget
        cost = tool.spec.cost_estimate
        if not self._ledger.try_reserve(grant.grant_id, cost, grant.budget.max_cost):
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.BUDGET_EXCEEDED,
                message="The budget for this grant is exhausted.",
                hint="Do not retry. Report that the budget is spent.",
            )
        st.reserved = cost

        # 6. idempotent replay
        if tool.spec.side_effect is SideEffect.WRITE:
            replay = self._idempotency_gate(st, tool, validated)
            if replay is not None:
                return replay

        # 7. policy
        if tool.spec.side_effect is SideEffect.READ:
            st.cleared = True
            return None
        return await self._policy_gate(st, tool, validated, grant)

    def _idempotency_gate(self, st: _State, tool: Tool, validated: BaseModel) -> ToolOutcome | None:
        derive = tool.spec.idempotency_key
        try:
            if derive is None:  # @tool forbids this for WRITE; fail closed if it ever happens
                raise ValueError("WRITE tool has no idempotency key")
            key: object = derive(validated)
            if not isinstance(key, str) or not key or len(key) > MAX_IDEMPOTENCY_KEY:
                raise ValueError("idempotency key must be a short non-empty string")
        except Exception:
            return self._refuse(
                st,
                status=OutcomeStatus.ERROR,
                code=ErrorCode.INVALID_ARGUMENTS,
                message="An idempotency key could not be derived from the arguments.",
            )
        st.idempotency_key = key
        claim = self._idempotency.peek(st.ctx.tenant_id, tool.name, key, st.args_hash)
        if claim.state is ClaimState.NEW:
            return None
        if claim.state is ClaimState.DONE and claim.output is not None:
            self._append(
                st,
                EventType.TOOL_REPLAY,
                {"call_id": st.ctx.call_id, "tool": tool.name, "idempotency_key": key},
            )
            output = tool.spec.output_model.model_validate(claim.output)
            return ToolOutcome(
                OutcomeStatus.OK,
                tool.name,
                st.ctx.call_id,
                output=Untrusted(output),
                replayed=True,
                audit_seq=st.seq,
            )
        if claim.state is ClaimState.CONFLICT:
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.IDEMPOTENCY_CONFLICT,
                message="That idempotency key was already used with different arguments.",
                hint="Do not retry. A new request needs new arguments and a new key.",
            )
        return self._refuse(
            st,
            status=OutcomeStatus.ERROR,
            code=ErrorCode.OUTCOME_UNKNOWN,
            message="An earlier attempt may or may not have taken effect.",
            hint="Do not retry. A human must reconcile the outcome first.",
        )

    async def _policy_gate(
        self, st: _State, tool: Tool, validated: BaseModel, grant: CapabilityGrant
    ) -> ToolOutcome | None:
        decision = await self._decide(st, tool, validated, grant)
        st.policy = decision
        self._append(
            st,
            EventType.POLICY_DECISION,
            {
                "call_id": st.ctx.call_id,
                "tool": tool.name,
                "effect": decision.effect.value,
                "reasons": list(decision.reasons),
                "approval_tier": decision.approval_tier.value if decision.approval_tier else None,
                "policy_version": decision.policy_version,
                "engine": decision.engine,
                "args_hash": st.args_hash,
            },
        )
        if decision.effect is Decision.ALLOW:
            st.cleared = True
            return None
        if decision.effect is Decision.DENY:
            unavailable = decision.policy_version == "unavailable"
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.POLICY_UNAVAILABLE if unavailable else ErrorCode.POLICY_DENIED,
                message=(
                    "The policy engine could not decide, so the action was refused."
                    if unavailable
                    else "The action was denied by policy."
                ),
                hint="Do not retry the same action. Choose a different approach.",
                details={} if unavailable else {"reasons": list(decision.reasons)},
                log=False,
                policy=decision,
            )
        return self._approval_gate(st, tool, decision, grant)

    def _approval_gate(
        self, st: _State, tool: Tool, decision: PolicyDecision, grant: CapabilityGrant
    ) -> ToolOutcome | None:
        ctx = st.ctx
        tier = decision.approval_tier
        if self._approvals is None or tier is None:
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.APPROVAL_UNAVAILABLE,
                message="This action needs human approval, and none is configured.",
                hint="Do not retry.",
                policy=decision,
            )
        if ctx.approval_id is None:
            evidence = EvidenceBundle(
                tool=tool.name,
                args=st.arguments,
                rationale=ctx.rationale[:MAX_RATIONALE],
                sources=ctx.sources,
                confidence=ctx.confidence,
                verifier_flags=ctx.verifier_flags,
                policy_reasons=decision.reasons,
                policy_version=decision.policy_version,
            )
            request = self._approvals.submit(
                tenant_id=ctx.tenant_id,
                agent_id=grant.agent_id,
                tool_name=tool.name,
                args_hash=st.args_hash,
                tier=tier,
                evidence=evidence,
                ttl=ctx.approval_ttl,
            )
            return ToolOutcome(
                OutcomeStatus.APPROVAL_REQUIRED,
                tool.name,
                ctx.call_id,
                approval_id=request.request_id,
                approval_tier=tier,
                policy=decision,
                audit_seq=st.seq,
            )
        try:
            existing = self._approvals.get(ctx.tenant_id, ctx.approval_id)
            if not existing.tier.covers(tier):
                raise ApprovalError("approval tier is below what policy now requires")
            self._approvals.consume(
                tenant_id=ctx.tenant_id,
                request_id=ctx.approval_id,
                agent_id=grant.agent_id,
                tool_name=tool.name,
                args_hash=st.args_hash,
            )
        except ApprovalError as exc:
            return self._refuse(
                st,
                status=OutcomeStatus.DENIED,
                code=ErrorCode.APPROVAL_INVALID,
                message="That approval cannot be used for this action.",
                hint="Do not retry. Request approval again if it is still needed.",
                reason=getattr(exc, "code", "approval_error"),
                policy=decision,
            )
        st.cleared = True
        return None

    async def _decide(
        self, st: _State, tool: Tool, validated: BaseModel, grant: CapabilityGrant
    ) -> PolicyDecision:
        try:
            resource = dict(tool.spec.resource(validated)) if tool.spec.resource else {}
            policy_input = PolicyInput(
                action=PolicyAction(
                    tool=tool.name,
                    side_effect=tool.spec.side_effect.value,
                    capability=str(tool.spec.capability),
                    args=st.arguments,
                ),
                actor=PolicyActor(
                    agent_id=grant.agent_id, tenant_id=grant.tenant_id, grant_id=grant.grant_id
                ),
                resource=resource,
                context=st.ctx.policy_context,
            )
            decision = await self._engine.decide(policy_input)
            if not isinstance(decision, PolicyDecision):
                raise TypeError("engine returned a non-decision")
            return decision
        except Exception as exc:  # the gate fails closed on any engine or input error
            return policy_deny(
                f"policy evaluation failed: {type(exc).__name__}",
                engine=getattr(self._engine, "name", "unknown"),
            )

    # ---------------------------------------------------------------- execute

    async def _execute(  # noqa: PLR0911, PLR0912 - linear run/validate/record sequence
        self, st: _State
    ) -> ToolOutcome:
        tool, ctx = st.tool, st.ctx
        # The one invariant that matters. Reaching here without having been
        # cleared by ALLOW, an approval, or a READ tool means a bug above.
        if not st.cleared or tool is None or st.validated is None or st.grant is None:
            self._release(st)
            return self._bare(st, ErrorCode.INTERNAL, "The call was not cleared to run.")

        is_write = tool.spec.side_effect is SideEffect.WRITE
        if is_write:
            if st.idempotency_key is None:
                self._release(st)
                return self._bare(st, ErrorCode.INTERNAL, "A WRITE call had no idempotency key.")
            claim = self._idempotency.claim(
                ctx.tenant_id, tool.name, st.idempotency_key, st.args_hash
            )
            if claim.state is not ClaimState.NEW:
                self._release(st)
                return self._bare(
                    st,
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "An identical call is already in progress or was already made.",
                    retryable=claim.state is ClaimState.IN_FLIGHT,
                )

        st.committed = True  # cost is spent from here on, whatever happens
        try:
            raw = await asyncio.wait_for(self._invoke(tool, st.validated), tool.spec.timeout_s)
        except asyncio.CancelledError:
            # Cancelled mid-flight: the effect may or may not have happened.
            if is_write and st.idempotency_key:
                self._idempotency.mark_unknown(ctx.tenant_id, tool.name, st.idempotency_key)
            raise
        except Exception as exc:
            return self._tool_failure(st, tool, exc, is_write=is_write)

        try:
            output = tool.spec.output_model.model_validate(raw)
        except ValidationError:
            if is_write and st.idempotency_key:
                self._idempotency.mark_unknown(ctx.tenant_id, tool.name, st.idempotency_key)
            return self._after(
                st,
                EventType.TOOL_ERROR,
                {"code": ErrorCode.INVALID_TOOL_OUTPUT.value},
                ToolError(
                    code=ErrorCode.INVALID_TOOL_OUTPUT,
                    message="The tool returned data that does not match its output schema.",
                    hint="Do not retry. This is a defect in the tool.",
                ),
            )

        dumped = output.model_dump(mode="json")
        if is_write and st.idempotency_key:
            self._idempotency.complete(ctx.tenant_id, tool.name, st.idempotency_key, dumped)
        encoded = canonical_json(dumped)
        payload: dict[str, Any] = {
            "call_id": ctx.call_id,
            "tool": tool.name,
            "cost": tool.spec.cost_estimate,
            "output_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }
        if len(encoded.encode()) <= MAX_AUDITED_OUTPUT_BYTES:
            payload["output"] = dumped
        else:
            payload["output_truncated"] = True
        try:
            self._append(st, EventType.TOOL_RESULT, payload)
        except AuditError:
            return self._bare(
                st,
                ErrorCode.AUDIT_FAILED_AFTER_EXECUTION,
                "The action ran but could not be recorded. Stop and reconcile.",
            )
        return ToolOutcome(
            OutcomeStatus.OK,
            tool.name,
            ctx.call_id,
            output=Untrusted(output),
            policy=st.policy,
            audit_seq=st.seq,
        )

    def _tool_failure(
        self, st: _State, tool: Tool, exc: Exception, *, is_write: bool
    ) -> ToolOutcome:
        """Turn a tool-body failure into an outcome, settling the idempotency claim.

        ``ToolRefusedError`` means the tool did nothing, so a retry is safe. A
        timeout or any other exception from a WRITE is treated as possibly
        partial: the key is parked as UNKNOWN and never retried automatically.
        """
        key = st.idempotency_key if is_write else None
        tenant = st.ctx.tenant_id
        if isinstance(exc, ToolRefusedError):
            if key:
                self._idempotency.release(tenant, tool.name, key)
            return self._after(
                st,
                EventType.TOOL_ERROR,
                {"code": ErrorCode.TOOL_FAILED.value, "refused_before_effect": True},
                ToolError(
                    code=ErrorCode.TOOL_FAILED,
                    message="The tool declined to run.",
                    retryable=True,
                    hint="Nothing was changed. Adjust the request and try again.",
                ),
            )
        timed_out = isinstance(exc, TimeoutError)
        if key:
            self._idempotency.mark_unknown(tenant, tool.name, key)
        recorded: dict[str, Any] = (
            {"code": ErrorCode.TOOL_TIMEOUT.value, "write_outcome_unknown": is_write}
            if timed_out
            else {"code": ErrorCode.TOOL_FAILED.value, "exception": type(exc).__name__}
        )
        if is_write:
            error = ToolError(
                code=ErrorCode.OUTCOME_UNKNOWN,
                message="The tool did not finish in time." if timed_out else "The tool failed.",
                hint=("Do not retry. The action may have taken effect; a human must reconcile."),
            )
        else:
            error = ToolError(
                code=ErrorCode.TOOL_TIMEOUT if timed_out else ErrorCode.TOOL_FAILED,
                message="The tool did not finish in time." if timed_out else "The tool failed.",
                retryable=True,
                hint="You may retry once.",
            )
        return self._after(st, EventType.TOOL_ERROR, recorded, error)

    @staticmethod
    async def _invoke(tool: Tool, validated: BaseModel) -> Any:
        if tool.is_async:
            return await tool._fn(validated)
        return await asyncio.to_thread(tool._fn, validated)

    # ---------------------------------------------------------------- helpers

    def _append(self, st: _State, event: EventType, payload: dict[str, Any]) -> None:
        record = self._audit.append(
            tenant_id=st.ctx.tenant_id, event_type=event, actor=st.actor, payload=payload
        )
        st.seq = record.seq

    def _release(self, st: _State) -> None:
        """Hand back the budget reservation for a call that never ran."""
        if st.reserved and not st.committed and st.grant is not None:
            self._ledger.release(st.grant.grant_id, st.reserved)
            st.reserved = 0.0

    def _refuse(
        self,
        st: _State,
        *,
        status: OutcomeStatus,
        code: ErrorCode,
        message: str,
        hint: str = "",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        reason: str = "",
        log: bool = True,
        policy: PolicyDecision | None = None,
    ) -> ToolOutcome:
        if log:
            self._append(
                st,
                EventType.TOOL_ERROR,
                {"call_id": st.ctx.call_id, "code": code.value, "reason": reason or message},
            )
        return ToolOutcome(
            status,
            st.tool_name,
            st.ctx.call_id,
            error=ToolError(
                code=code, message=message, retryable=retryable, hint=hint, details=details or {}
            ),
            policy=policy or st.policy,
            audit_seq=st.seq,
        )

    def _not_authorised(self, st: _State) -> ToolOutcome:
        return self._refuse(
            st,
            status=OutcomeStatus.DENIED,
            code=ErrorCode.NOT_AUTHORISED,
            message="Authorisation failed.",
            hint="Do not retry. Ask the operator.",
            log=False,
        )

    def _after(
        self, st: _State, event: EventType, extra: dict[str, Any], error: ToolError
    ) -> ToolOutcome:
        payload = {"call_id": st.ctx.call_id, "tool": st.tool_name, **extra}
        try:
            self._append(st, event, payload)
        except AuditError:
            error = ToolError(
                code=ErrorCode.AUDIT_FAILED_AFTER_EXECUTION,
                message="The tool failed and the failure could not be recorded.",
                hint="Stop and reconcile.",
            )
        return ToolOutcome(
            OutcomeStatus.ERROR, st.tool_name, st.ctx.call_id, error=error, audit_seq=st.seq
        )

    def _bare(
        self, st: _State, code: ErrorCode, message: str, *, retryable: bool = False
    ) -> ToolOutcome:
        """An error result that does not depend on the audit log working."""
        return ToolOutcome(
            OutcomeStatus.ERROR,
            st.tool_name,
            st.ctx.call_id,
            error=ToolError(code=code, message=message, retryable=retryable),
            audit_seq=st.seq,
        )


def _normalise_arguments(arguments: object) -> dict[str, Any] | None:
    """Round-trip through strict JSON; None if the model sent something unusable."""
    if not isinstance(arguments, Mapping):
        return None
    try:
        encoded = canonical_json(dict(arguments))
    except (TypeError, ValueError):
        return None
    if len(encoded.encode()) > MAX_ARGUMENT_BYTES:
        return None
    result: dict[str, Any] = json.loads(encoded)
    return result


def _field_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Field-level problems without echoing the offending input back."""
    return [
        {"field": ".".join(str(p) for p in err["loc"]) or "(root)", "problem": err["msg"][:200]}
        for err in exc.errors(include_input=False, include_url=False)
    ]
