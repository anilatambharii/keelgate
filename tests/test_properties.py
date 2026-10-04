"""Property tests: no WRITE executes without authorisation, whatever we throw at the gate.

The oracle here is deliberately independent of the gateway's internals. It watches
two things the gateway cannot fake: what the scripted policy engine actually
returned for each call, and whether a tool body actually ran. The invariant is
about those two observations only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from keelgate.approvals import ApprovalStatus, ApprovalTier, Approver, EvidenceBundle, action_hash
from keelgate.capabilities import GrantSigner, issue_grant
from keelgate.policy import Decision, PolicyDecision, PolicyInput
from keelgate.tools import OutcomeStatus
from tests.conftest import Harness, TradeIn, build_harness, policy_context, run

CAPS_FULL = ("market_data:read", "trade:propose", "trade:paper_execute", "report:write")
TOOLS = ("market_quote", "trade_propose", "trade_paper_execute", "report_write")
WRITE_TOOLS = {"trade_paper_execute", "report_write"}
TOOL_CAP = {
    "market_quote": "market_data:read",
    "trade_propose": "trade:propose",
    "trade_paper_execute": "trade:paper_execute",
    "report_write": "report:write",
}


class ScriptedEngine:
    """A policy engine that answers whatever the test tells it to, and keeps score."""

    name = "scripted"

    def __init__(self) -> None:
        self.behaviour = "ALLOW"
        self.returned: list[str] = []  # effect label per decide() call in the current call

    async def decide(self, policy_input: PolicyInput) -> Any:
        b = self.behaviour
        if b == "raise":
            self.returned.append("raise")
            raise RuntimeError("engine crashed")
        if b == "garbage":
            self.returned.append("garbage")
            return {"effect": "ALLOW", "reasons": []}  # not a PolicyDecision
        if b == "none":
            self.returned.append("none")
            return None
        if b == "ALLOW":
            self.returned.append("ALLOW")
            return PolicyDecision(effect=Decision.ALLOW, policy_version="v", engine=self.name)
        if b == "DENY":
            self.returned.append("DENY")
            return PolicyDecision(
                effect=Decision.DENY, reasons=("no",), policy_version="v", engine=self.name
            )
        self.returned.append("REQUIRE_APPROVAL")
        return PolicyDecision(
            effect=Decision.REQUIRE_APPROVAL,
            reasons=("big",),
            approval_tier=ApprovalTier.EXPLICIT_SIGNOFF,
            policy_version="v",
            engine=self.name,
        )


@dataclass(frozen=True)
class CallSpec:
    tool: str
    valid_args: bool
    grant: str  # valid | missing_cap | expired | wrong_tenant | revoked | garbage | foreign_signer
    mode: str
    behaviour: str
    approval: str  # none | approved | pending | rejected | other_args | fake
    key: int
    budget: float


call_specs = st.builds(
    CallSpec,
    tool=st.sampled_from(TOOLS),
    valid_args=st.booleans(),
    grant=st.sampled_from(
        [
            "valid",
            "valid",
            "valid",
            "missing_cap",
            "expired",
            "wrong_tenant",
            "revoked",
            "garbage",
            "foreign_signer",
        ]
    ),
    mode=st.sampled_from(["paper", "paper", "simulation", "live", "production", "", "PAPER"]),
    behaviour=st.sampled_from(
        ["ALLOW", "ALLOW", "DENY", "REQUIRE_APPROVAL", "raise", "garbage", "none"]
    ),
    approval=st.sampled_from(
        ["none", "none", "approved", "pending", "rejected", "other_args", "fake"]
    ),
    key=st.integers(min_value=0, max_value=3),  # few keys, so replays and conflicts happen
    budget=st.sampled_from([0.0, 1.0, 5.0, 100.0]),
)


def args_for(spec: CallSpec) -> dict[str, Any]:
    if spec.tool == "market_quote":
        return {"symbol": "AAPL"} if spec.valid_args else {"symbol": 5}
    if spec.tool == "report_write":
        return {"title": f"r{spec.key}"} if spec.valid_args else {}
    notional = 5000 if spec.valid_args else -1
    return {"symbol": "AAPL", "notional": notional, "client_order_id": f"o{spec.key}"}


def build_token(h: Harness, spec: CallSpec) -> str:
    caps = CAPS_FULL
    if spec.grant == "missing_cap":
        caps = tuple(c for c in CAPS_FULL if c != TOOL_CAP[spec.tool]) or ("market_data:read",)
    tenant = "tenant-2" if spec.grant == "wrong_tenant" else "tenant-1"
    if spec.grant == "garbage":
        return "v4.public.garbage"
    signer = GrantSigner.generate() if spec.grant == "foreign_signer" else h.signer
    ttl = timedelta(seconds=1) if spec.grant == "expired" else timedelta(hours=1)
    signed = issue_grant(
        signer,
        agent_id="agent-1",
        tenant_id=tenant,
        capabilities=caps,
        max_cost=spec.budget,
        ttl=ttl,
        clock=h.clock,
    )
    if spec.grant == "expired":
        h.clock.advance(timedelta(seconds=2))
    if spec.grant == "revoked":
        h.revocations.revoke(signed.grant.grant_id)
    return signed.token


def prepare_approval(h: Harness, spec: CallSpec, args: dict[str, Any]) -> str | None:
    if spec.approval == "none":
        return None
    if spec.approval == "fake":
        return "deadbeef" * 4
    if spec.tool != "trade_paper_execute" or not spec.valid_args:
        return None
    dumped = TradeIn(**args).model_dump(mode="json")
    target = (
        dumped if spec.approval != "other_args" else {**dumped, "notional": dumped["notional"] + 1}
    )
    request = h.queue.submit(
        tenant_id="tenant-1",
        agent_id="agent-1",
        tool_name=spec.tool,
        args_hash=action_hash(spec.tool, target),
        tier=ApprovalTier.EXPLICIT_SIGNOFF,
        evidence=EvidenceBundle(tool=spec.tool, args=target, rationale="r"),
    )
    human = Approver(
        approver_id="alice", tenant_id="tenant-1", max_tier=ApprovalTier.EXPLICIT_SIGNOFF
    )
    if spec.approval in ("approved", "other_args"):
        h.queue.approve("tenant-1", request.request_id, human, signoff_code=request.signoff_code)
    elif spec.approval == "rejected":
        h.queue.reject("tenant-1", request.request_id, human)
    return request.request_id


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(call_specs, min_size=1, max_size=8))
def test_no_write_executes_without_an_authorising_decision(specs: list[CallSpec]) -> None:
    engine = ScriptedEngine()
    h = build_harness(engine=engine)

    for spec in specs:
        engine.behaviour = spec.behaviour
        engine.returned.clear()
        args = args_for(spec)
        token = build_token(h, spec)
        approval_id = prepare_approval(h, spec, args)
        before = len(h.executed)

        outcome = h.call(
            spec.tool,
            args,
            token=token,
            context=h.ctx(
                policy_context=policy_context(execution_mode=spec.mode), approval_id=approval_id
            ),
        )
        ran = len(h.executed) - before

        # --- independent oracle -------------------------------------------------
        mode_ok = spec.mode in {"paper", "simulation"}
        preconditions = spec.grant == "valid" and mode_ok and spec.valid_args

        if ran:
            # A WRITE body ran. Everything below must then hold.
            assert spec.tool in WRITE_TOOLS
            assert outcome.status is OutcomeStatus.OK and not outcome.replayed
            assert preconditions, f"ran despite failed preconditions: {spec}"
            assert engine.returned, "ran without any policy decision"
            effect = engine.returned[-1]
            assert effect in ("ALLOW", "REQUIRE_APPROVAL"), f"ran after policy said {effect}"
            if effect == "REQUIRE_APPROVAL":
                assert spec.approval == "approved", f"ran without a matching approval: {spec}"
                assert approval_id is not None
                assert h.queue.get("tenant-1", approval_id).status is ApprovalStatus.CONSUMED
        # Nothing ran. If it claims success it must be a read, a proposal or a replay.
        elif outcome.status is OutcomeStatus.OK:
            assert spec.tool not in WRITE_TOOLS or outcome.replayed

        if not preconditions:
            assert outcome.status is not OutcomeStatus.OK, f"succeeded despite {spec}"

        if outcome.status is OutcomeStatus.APPROVAL_REQUIRED:
            assert ran == 0
            assert outcome.approval_id is not None

    # --- whole-run invariants ----------------------------------------------------
    assert h.audit.verify_chain("tenant-1").ok
    assert h.audit.verify_chain("tenant-2").ok
    keys = [(e["tool"], e.get("client_order_id") or e.get("title")) for e in h.executed]
    assert len(keys) == len(set(keys)), "a WRITE ran twice for the same idempotency key"

    # Every executed WRITE has a matching tool.result record, and none without a decision first.
    records = list(h.audit.records("tenant-1"))
    results = [
        r for r in records if r.event_type == "tool.result" and r.payload["tool"] in WRITE_TOOLS
    ]
    assert len(results) == len(h.executed)
    for result in results:
        call_id = result.payload["call_id"]
        decision = next(
            r
            for r in records
            if r.event_type == "policy.decision" and r.payload["call_id"] == call_id
        )
        assert decision.seq < result.seq
        assert decision.payload["effect"] in ("ALLOW", "REQUIRE_APPROVAL")


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.sampled_from(["market_quote", "trade_paper_execute", "report_write"]),
        min_size=1,
        max_size=12,
    ),
    st.sampled_from([0.0, 1.0, 3.0, 10.0, 50.0]),
)
def test_spend_never_exceeds_the_grant_budget(tools: list[str], budget: float) -> None:
    """Costs: market_quote 1, trade_paper_execute 2, report_write 0."""
    engine = ScriptedEngine()
    h = build_harness(engine=engine)
    token = h.grant(cost=budget)
    costs = {"market_quote": 1.0, "trade_paper_execute": 2.0, "report_write": 0.0}
    spent = 0.0
    for i, tool_name in enumerate(tools):
        args = (
            {"symbol": "A"}
            if tool_name == "market_quote"
            else {"title": f"r{i}"}
            if tool_name == "report_write"
            else {"symbol": "AAPL", "notional": 100, "client_order_id": f"o{i}"}
        )
        out = h.call(tool_name, args, token=token)
        if out.status is OutcomeStatus.OK:
            spent += costs[tool_name]
    assert spent <= budget


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.integers(min_value=0, max_value=2), min_size=2, max_size=10))
def test_a_repeated_idempotency_key_never_runs_twice(keys: list[int]) -> None:
    engine = ScriptedEngine()
    h = build_harness(engine=engine)
    for k in keys:
        h.call(
            "trade_paper_execute", {"symbol": "AAPL", "notional": 100, "client_order_id": f"k{k}"}
        )
    assert len(h.executed) == len(set(keys))


@settings(max_examples=100, deadline=None)
@given(
    st.sampled_from(["DENY", "raise", "garbage", "none"]),
    st.sampled_from(["trade_paper_execute", "report_write"]),
)
def test_a_non_allowing_engine_means_no_write_ever(behaviour: str, tool_name: str) -> None:
    engine = ScriptedEngine()
    engine.behaviour = behaviour
    h = build_harness(engine=engine)
    args = (
        {"symbol": "AAPL", "notional": 100, "client_order_id": "o"}
        if tool_name == "trade_paper_execute"
        else {"title": "t"}
    )
    out = h.call(tool_name, args)
    assert out.status is OutcomeStatus.DENIED
    assert h.executed == []


def test_the_property_test_would_catch_a_gateway_that_skips_the_policy_gate() -> None:
    """Mutation check: prove the oracle has teeth by breaking the gate on purpose."""
    from keelgate.tools import gateway as gateway_module

    engine = ScriptedEngine()
    engine.behaviour = "DENY"
    h = build_harness(engine=engine)

    original = gateway_module.ToolGateway._policy_gate

    async def skip_policy(self: Any, st_: Any, tool_: Any, validated: Any, grant: Any) -> None:
        st_.cleared = True  # a buggy gateway that trusts everything

    gateway_module.ToolGateway._policy_gate = skip_policy  # type: ignore[method-assign,assignment]
    try:
        out = h.call(
            "trade_paper_execute", {"symbol": "AAPL", "notional": 100, "client_order_id": "o"}
        )
    finally:
        gateway_module.ToolGateway._policy_gate = original  # type: ignore[method-assign]

    # With the gate broken the write runs although policy never said ALLOW...
    assert len(h.executed) == 1
    assert out.status is OutcomeStatus.OK
    # ...which is exactly the condition the property test asserts can never occur.
    assert engine.returned == []
    restored = run(
        h.gateway.call(
            tool_name="trade_paper_execute",
            arguments={"symbol": "AAPL", "notional": 100, "client_order_id": "o2"},
            grant_token=h.grant(),
            context=h.ctx(),
        )
    )
    assert restored.status is OutcomeStatus.DENIED  # the real gate denies again
    assert len(h.executed) == 1
