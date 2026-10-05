"""A small, fully wired, in-memory governed stack for downstream tests.

Everything here is real Keelgate (gateway, grant verification, audit chain, approvals,
idempotency, budget ledger). Only two things are test doubles: a movable clock, and a
:class:`StaticPolicyEngine` whose decisions you script. Use the real :class:`RegoEngine` with a
policy pack when the test is *about* policy; use the static engine when it is about something else.

``StaticPolicyEngine`` is DENY by default, like the real engine. ``allow_all()`` exists for tests
that need an unobstructed path and is named so it cannot be mistaken for a production default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from keelgate.approvals import ApprovalQueue, ApprovalTier
from keelgate.audit import AuditLog
from keelgate.capabilities import GrantSigner, GrantVerifier, InMemoryRevocationList, issue_grant
from keelgate.policy import Decision, PolicyContext, PolicyDecision, PolicyEngine, PolicyInput
from keelgate.tools import CallContext, ToolGateway, ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from keelgate.tools import Tool
    from keelgate.tools.outcomes import ToolOutcome

DEFAULT_START: Final = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)


class ManualClock:
    """A clock that only moves when told to. Callable, so it plugs in wherever a clock does."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        if start.tzinfo is None:
            raise ValueError("a clock needs a timezone-aware start")
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class StaticPolicyEngine:
    """A scripted :class:`~keelgate.policy.PolicyEngine`. Unlisted tools are DENIED.

    ``rules`` maps a tool name to a :class:`Decision`. Every input it sees is kept in
    ``inputs`` so a test can assert what the policy was asked. As in production, the gateway
    consults policy for PROPOSE and WRITE tools only; READ tools are gated by the grant alone.
    """

    name = "static-test-policy"

    def __init__(
        self,
        rules: Mapping[str, Decision] | None = None,
        *,
        default: Decision = Decision.DENY,
        approval_tier: ApprovalTier = ApprovalTier.ONE_CLICK,
    ) -> None:
        self._rules = dict(rules or {})
        self._default = default
        self._tier = approval_tier
        self.inputs: list[PolicyInput] = []

    @classmethod
    def allow_all(cls) -> StaticPolicyEngine:
        """Allow every action. For tests only; never a production policy."""
        return cls(default=Decision.ALLOW)

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        self.inputs.append(policy_input)
        effect = self._rules.get(policy_input.action.tool, self._default)
        return PolicyDecision(
            effect=effect,
            reasons=(f"scripted: {effect.value}",),
            approval_tier=self._tier if effect is Decision.REQUIRE_APPROVAL else None,
            policy_version="static",
            engine=self.name,
        )


@dataclass
class GovernedHarness:
    """A wired gateway plus the handles a test needs to drive and inspect it."""

    clock: ManualClock
    signer: GrantSigner
    audit: AuditLog
    approvals: ApprovalQueue
    registry: ToolRegistry
    gateway: ToolGateway
    engine: PolicyEngine
    revocations: InMemoryRevocationList
    tenant_id: str = "tenant-1"
    agent_id: str = "agent-1"
    extra: dict[str, Any] = field(default_factory=dict)

    def grant(
        self,
        capabilities: Iterable[str] | None = None,
        *,
        tenant: str | None = None,
        max_cost: float = 100.0,
        ttl: timedelta = timedelta(hours=1),
    ) -> str:
        """A signed grant token. Default: every capability of every registered tool."""
        if capabilities is None:
            tools = (self.registry.get(n) for n in self.registry.names())
            capabilities = sorted({t.spec.capability for t in tools if t is not None})
        caps = tuple(capabilities)
        return issue_grant(
            self.signer,
            agent_id=self.agent_id,
            tenant_id=tenant or self.tenant_id,
            capabilities=caps,
            max_cost=max_cost,
            ttl=ttl,
            clock=self.clock,
        ).token

    def context(self, tenant: str | None = None, **overrides: Any) -> CallContext:
        values: dict[str, Any] = {
            "tenant_id": tenant or self.tenant_id,
            "policy_context": PolicyContext(as_of=self.clock(), execution_mode="paper"),
        }
        values.update(overrides)
        return CallContext(**values)

    async def call(
        self, tool: str, arguments: dict[str, Any], *, token: str | None = None, **ctx: Any
    ) -> ToolOutcome:
        return await self.gateway.call(
            tool_name=tool,
            arguments=arguments,
            grant_token=token if token is not None else self.grant(),
            context=self.context(**ctx),
        )


def build_governed_harness(
    tools: Iterable[Tool] = (),
    *,
    engine: PolicyEngine | None = None,
    clock: ManualClock | None = None,
    tenant_id: str = "tenant-1",
    agent_id: str = "agent-1",
) -> GovernedHarness:
    """Build the stack. ``engine`` defaults to a DENY-everything static policy."""
    clock = clock or ManualClock()
    signer = GrantSigner.generate()
    revocations = InMemoryRevocationList()
    verifier = GrantVerifier(
        {signer.key_id: signer.public_key_pem()}, clock=clock, revocations=revocations
    )
    audit = AuditLog(clock=clock)
    approvals = ApprovalQueue(audit=audit, clock=clock)
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    chosen: PolicyEngine = engine or StaticPolicyEngine()
    gateway = ToolGateway(
        registry=registry, verifier=verifier, engine=chosen, audit=audit, approvals=approvals
    )
    return GovernedHarness(
        clock=clock,
        signer=signer,
        audit=audit,
        approvals=approvals,
        registry=registry,
        gateway=gateway,
        engine=chosen,
        revocations=revocations,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
