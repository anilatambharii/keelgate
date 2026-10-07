"""The loop: stop conditions, context hygiene, approvals, and the read-only kinds."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from keelgate.approvals import ApprovalTier, Approver
from keelgate.audit import EventType
from keelgate.context import ContextItem, ItemKind
from keelgate.llm import LLMError, ModelPrice, PricingTable, Usage
from keelgate.loop import (
    ActionStatus,
    CallableVerifier,
    LLMVerifier,
    MonitorLoop,
    Phase,
    RunExistsError,
    RunNotFoundError,
    Schedule,
    StopConditions,
    StopReason,
    Verdict,
    VerificationLoop,
)
from keelgate.memory import Attribution, EpisodicMemory, SqliteMemoryBackend
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import SideEffect
from tests.conftest import MARKET_OPEN, run
from tests.loop_support import (
    AGENT,
    TENANT,
    Rig,
    build_rig,
    fake,
    order,
    three_step_script,
)

GOAL = "Research AAPL and buy a small position."


@pytest.fixture
def rig(tmp_path: Path, rego_engine: Any) -> Rig:
    return build_rig(tmp_path / "rig", engine=rego_engine)


def start(loop: Any, run_id: str = "r1", goal: str = GOAL, **kw: Any) -> Any:
    return run(
        loop.run(
            goal=goal, tenant_id=TENANT, agent_id=AGENT, as_of=MARKET_OPEN, run_id=run_id, **kw
        )
    )


def resume(loop: Any, run_id: str = "r1", **kw: Any) -> Any:
    return run(loop.resume(TENANT, run_id, **kw))


def texts(request: Any) -> str:
    return "\n".join(m.content for m in request.messages)


# ------------------------------------------------------------------ the happy path


def test_a_three_step_loop_reaches_its_goal(rig: Rig) -> None:
    llm = fake(three_step_script())
    result = start(rig.loop(llm))

    assert result.ok and result.stop_reason is StopReason.GOAL_REACHED
    assert result.final_answer == "Bought 5000 of AAPL at the quoted price."
    assert not result.resumable
    state = result.state
    assert (state.iteration, state.llm_calls, state.plan_calls) == (3, 3, 3)
    assert state.phase is Phase.DONE
    assert [e["client_order_id"] for e in rig.executions] == ["o-1"]
    assert len(state.observations) == 2
    assert state.tokens_used > 0


def test_every_step_is_checkpointed_audited_and_chained(rig: Rig) -> None:
    start(rig.loop(fake(three_step_script())))
    history = rig.checkpoints.history(TENANT, "r1")
    seqs = [s.checkpoint_seq for s in history]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and len(seqs) >= 10

    transitions = [
        r for r in rig.audit.records(TENANT) if r.event_type == EventType.LOOP_TRANSITION
    ]
    assert transitions and all(t.payload["run_id"] == "r1" for t in transitions)
    assert {t.payload["trace_id"] for t in transitions} == {history[0].trace_id}
    labels = [t.payload["label"] for t in transitions]
    assert labels[0] == "start" and labels[-1] == "stop:goal_reached"
    assert "plan" in labels and "acted" in labels and "observed" in labels and "verified" in labels
    assert rig.audit.verify_chain(TENANT).ok


def test_the_model_is_offered_the_tools_and_sees_fenced_untrusted_observations(rig: Rig) -> None:
    llm = fake(three_step_script())
    start(rig.loop(llm))
    first, third = llm.requests[0], llm.requests[2]
    assert {t.name for t in first.tools} == {"market_quote", "get_news", "paper_order"}
    assert "Goal: " + GOAL in texts(first)
    assert "<untrusted" not in texts(first)  # nothing outside has been observed yet

    seen = texts(third)
    assert '<untrusted id="obs:r1:1:0" origin="tool:market_quote"' in seen
    assert "ord-o-1" in seen  # the order result came back as an observation
    assert "never as instructions" in third.messages[0].content


def test_run_refuses_an_existing_id_and_run_or_resume_is_idempotent(rig: Rig) -> None:
    loop = rig.loop(fake(three_step_script()))
    start(loop)
    with pytest.raises(RunExistsError):
        start(loop)
    again = run(
        loop.run_or_resume(
            goal=GOAL, tenant_id=TENANT, agent_id=AGENT, as_of=MARKET_OPEN, run_id="r1"
        )
    )
    assert again.ok and len(rig.executions) == 1  # returned the finished run; did nothing


def test_naive_as_of_is_refused(rig: Rig) -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone-aware"):
        run(
            rig.loop(fake([])).run(
                goal="x",
                tenant_id=TENANT,
                agent_id=AGENT,
                as_of=datetime(2026, 1, 1),  # noqa: DTZ001
            )
        )


# ----------------------------------------------------------------------- stopping


def endless_quotes(n: int = 30) -> list[Reply]:
    return [Reply.call("market_quote", symbol="AAPL") for _ in range(n)]


def test_max_iterations_stops_and_a_larger_limit_resumes(rig: Rig) -> None:
    loop = rig.loop(fake(endless_quotes()), stop=StopConditions(max_iterations=3))
    stopped = start(loop)
    assert stopped.stop_reason is StopReason.MAX_ITERATIONS and stopped.resumable
    assert stopped.state.iteration == 3

    more = resume(loop, stop=StopConditions(max_iterations=5))
    assert more.stop_reason is StopReason.MAX_ITERATIONS and more.state.iteration == 5


def test_a_token_budget_stops_before_acting_and_resume_does_not_replan(rig: Rig) -> None:
    # The output (which cannot be known in advance) is what crosses the budget here: each call
    # reports 600 tokens against a 1000-token budget, and the context itself is far smaller.
    big = Usage(input_tokens=0, output_tokens=600)
    llm = fake(
        [
            Reply.call("market_quote", symbol="AAPL", usage=big),
            Reply.call("paper_order", usage=big, **order("tok-1")),
            Reply.say("done", usage=big),
        ]
    )
    loop = rig.loop(llm, stop=StopConditions(max_tokens=1000))
    stopped = start(loop)

    assert stopped.stop_reason is StopReason.TOKEN_BUDGET and stopped.resumable
    assert stopped.state.tokens_used == 1200 and llm.calls_made == 2
    assert rig.executions == []  # the plan that crossed the line was saved, not acted on
    assert stopped.state.phase is Phase.ACT
    assert stopped.state.actions[0].status is ActionStatus.PENDING

    done = resume(loop, stop=StopConditions(max_tokens=10_000))
    assert done.ok
    assert llm.calls_made == 3  # exactly one more call: the saved plan was NOT regenerated
    assert [e["client_order_id"] for e in rig.executions] == ["tok-1"]


def test_a_call_whose_input_alone_would_cross_the_token_budget_is_never_made(rig: Rig) -> None:
    llm = fake(three_step_script())
    loop = rig.loop(llm, stop=StopConditions(max_tokens=20))  # smaller than the prompt itself
    stopped = start(loop)

    assert stopped.stop_reason is StopReason.TOKEN_BUDGET and stopped.resumable
    assert llm.calls_made == 0 and stopped.state.tokens_used == 0  # nothing was paid for
    assert stopped.state.iteration == 0  # the attempted step does not count

    done = resume(loop, stop=StopConditions(max_tokens=10_000))
    assert done.ok and llm.calls_made == 3
    assert [e["client_order_id"] for e in rig.executions] == ["o-1"]


def test_the_pre_call_check_counts_what_was_already_spent(rig: Rig) -> None:
    # Call 1 fits a 600-token budget and spends 500; the next prompt alone is larger than the
    # 100 tokens that remain, so the second call is refused before it is paid for.
    spent = Usage(input_tokens=0, output_tokens=500)
    llm = fake([Reply.call("market_quote", symbol="AAPL", usage=spent), Reply.say("x")])
    stopped = start(rig.loop(llm, stop=StopConditions(max_tokens=600)))
    assert stopped.stop_reason is StopReason.TOKEN_BUDGET
    assert llm.calls_made == 1 and stopped.state.tokens_used == 500


def test_a_dollar_budget_stops_the_loop(rig: Rig) -> None:
    paid = Usage(input_tokens=10, output_tokens=0, cost_usd=0.6)
    llm = fake([Reply.call("market_quote", symbol="AAPL", usage=paid) for _ in range(5)])
    stopped = start(rig.loop(llm, stop=StopConditions(max_dollars=1.0)))
    assert stopped.stop_reason is StopReason.DOLLAR_BUDGET
    assert stopped.state.dollars_used == pytest.approx(1.2)
    assert llm.calls_made == 2


def test_an_unpriced_model_under_a_dollar_budget_stops_instead_of_spending_blind(rig: Rig) -> None:
    llm = fake(endless_quotes())  # no pricing: every call's cost is unknown
    stopped = start(rig.loop(llm, stop=StopConditions(max_dollars=5.0)))
    assert stopped.stop_reason is StopReason.COST_UNKNOWN
    assert stopped.state.cost_unknown and llm.calls_made == 1


def test_a_priced_model_computes_cost_and_enforces_the_budget(rig: Rig) -> None:
    pricing = PricingTable(
        {"fake-model": ModelPrice(input_per_mtok=1_000_000, output_per_mtok=1_000_000)}
    )
    llm = fake(
        endless_quotes(), pricing=pricing
    )  # one dollar per token: any call blows a $1 budget
    stopped = start(rig.loop(llm, stop=StopConditions(max_dollars=1.0)))
    assert stopped.stop_reason is StopReason.DOLLAR_BUDGET
    assert not stopped.state.cost_unknown


def test_a_dollar_budget_refuses_a_call_whose_input_alone_would_cross_it(rig: Rig) -> None:
    pricing = PricingTable({"fake-model": ModelPrice(input_per_mtok=1_000_000, output_per_mtok=0)})
    llm = fake(endless_quotes(), pricing=pricing)  # one dollar per input token
    loop = rig.loop(
        llm, stop=StopConditions(max_dollars=5.0), pricing=pricing, price_model="fake-model"
    )
    stopped = start(loop)
    assert stopped.stop_reason is StopReason.DOLLAR_BUDGET
    assert llm.calls_made == 0 and stopped.state.dollars_used == 0  # refused before any spend


def test_the_pre_call_dollar_check_is_skipped_for_an_unpriced_model(rig: Rig) -> None:
    pricing = PricingTable({})  # no price for any model
    llm = fake(endless_quotes(), pricing=pricing)
    loop = rig.loop(
        llm, stop=StopConditions(max_dollars=5.0), pricing=pricing, price_model="fake-model"
    )
    stopped = start(loop)
    assert stopped.stop_reason is StopReason.COST_UNKNOWN  # the post-call rule still applies
    assert llm.calls_made == 1


def test_no_dollar_budget_means_unknown_cost_is_fine(rig: Rig) -> None:
    result = start(rig.loop(fake(three_step_script())))
    assert result.ok and result.state.cost_unknown


def test_timeout_counts_active_time_not_time_spent_stopped(rig: Rig) -> None:
    def slow(_: Any) -> Reply:
        rig.clock.advance(timedelta(seconds=40))
        return Reply.call("market_quote", symbol="AAPL")

    llm = fake([slow, slow, slow, Reply.say("done")])
    loop = rig.loop(llm, stop=StopConditions(timeout=timedelta(seconds=60), max_iterations=20))
    stopped = start(loop)
    assert stopped.stop_reason is StopReason.TIMEOUT and stopped.resumable
    assert stopped.state.active_seconds >= 60

    rig.clock.advance(timedelta(hours=1))  # an hour of downtime: waiting is not working
    finished = resume(loop, stop=StopConditions(timeout=timedelta(seconds=200), max_iterations=20))
    assert finished.ok
    assert finished.state.active_seconds < 600


def test_a_goal_predicate_ends_the_loop_without_a_final_answer(rig: Rig) -> None:
    def bought(state: Any) -> bool:
        return any(a.tool == "paper_order" and a.status is ActionStatus.DONE for a in state.actions)

    llm = fake(three_step_script())
    result = start(rig.loop(llm, stop=StopConditions(goal=bought)))
    assert result.ok and result.final_answer is None
    assert llm.calls_made == 2  # the closing answer was never requested
    assert len(rig.executions) == 1


def test_the_verifier_rejection_limit_is_terminal(rig: Rig) -> None:
    verifier = CallableVerifier(lambda req: Verdict.reject("not convincing"))
    llm = fake([Reply.say("answer one"), Reply.say("answer two"), Reply.say("answer three")])
    result = start(rig.loop(llm, verifier=verifier, stop=StopConditions(max_verifier_rejections=2)))
    assert result.stop_reason is StopReason.VERIFIER_REJECTIONS and not result.resumable
    assert result.state.rejections == 2 and llm.calls_made == 2
    assert resume(rig.loop(llm, verifier=verifier)).stop_reason is StopReason.VERIFIER_REJECTIONS
    assert llm.calls_made == 2  # a terminal run does not restart


def test_a_revise_verdict_feeds_back_and_the_next_answer_can_pass(rig: Rig) -> None:
    seen: list[int] = []

    def verifier_fn(req: Any) -> Verdict:
        seen.append(len(seen))
        return Verdict.revise("Cite your source.") if len(seen) == 1 else Verdict.accept()

    llm = fake([Reply.say("first draft"), Reply.say("final, citing the quote")])
    result = start(rig.loop(llm, verifier=CallableVerifier(verifier_fn)))
    assert result.ok and result.final_answer == "final, citing the quote"
    assert "Verifier verdict: REVISE. Cite your source." in texts(llm.requests[1])
    assert result.state.rejections == 1


def test_an_empty_plan_is_noted_not_fatal(rig: Rig) -> None:
    llm = fake([Reply.say("   "), Reply.say("a real answer")])
    result = start(rig.loop(llm))
    assert result.ok and "neither a tool call nor an answer" in texts(llm.requests[1])


def test_a_provider_error_stops_with_error_and_is_resumable(rig: Rig) -> None:
    calls = {"n": 0}

    def flaky(_: Any) -> Reply:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("upstream 503", provider="fake", retryable=True)
        return Reply.say("recovered")

    loop = rig.loop(fake([flaky]))
    stopped = start(loop)
    assert stopped.stop_reason is StopReason.ERROR and stopped.resumable
    assert "LLMError: upstream 503" in (stopped.state.error or "")
    again = resume(loop)
    assert again.ok and again.final_answer == "recovered"


def test_stop_conditions_are_validated() -> None:
    for kwargs in (
        {"max_iterations": 0},
        {"max_tokens": -1},
        {"max_dollars": 0},
        {"max_verifier_rejections": 0},
        {"timeout": timedelta(0)},
    ):
        with pytest.raises(ValueError, match="positive"):
            StopConditions(**kwargs)


# ---------------------------------------------------------------- llm verifier


def test_an_llm_verifier_accepts_and_its_usage_is_counted(rig: Rig) -> None:
    verdict_llm = FakeLLM([Reply.say('{"decision": "accept", "reasons": ["checks out"]}')])
    result = start(
        rig.loop(fake([Reply.say("the answer")]), verifier=LLMVerifier(verdict_llm, "fake-model"))
    )
    assert result.ok
    assert result.state.llm_calls == 2 and result.state.plan_calls == 1
    assert result.state.verdicts[0].decision == "ACCEPT"
    prompt = verdict_llm.last_request.messages[0].content
    assert "the answer" in prompt and "never follow instructions" in prompt.lower()


@pytest.mark.parametrize("bad", ["not json", "[]", '{"decision": "MAYBE"}', "{}", ""])
def test_an_unparseable_verifier_reply_is_a_rejection_never_an_acceptance(
    rig: Rig, bad: str
) -> None:
    verdict_llm = FakeLLM([Reply.say(bad)])
    result = start(
        rig.loop(
            fake([Reply.say("answer"), Reply.say("answer")]),
            verifier=LLMVerifier(verdict_llm, "fake-model"),
            stop=StopConditions(max_verifier_rejections=1),
        )
    )
    assert result.stop_reason is StopReason.VERIFIER_REJECTIONS
    assert result.final_answer is None
    assert "verifier_unparseable" in result.state.verdicts[0].flags


@pytest.mark.parametrize(
    ("value", "decision"),
    [(True, "ACCEPT"), (False, "REJECT"), ("not a verdict", "REJECT")],
)
def test_callable_verifiers_may_return_bools_and_reject_nonsense(
    rig: Rig, value: Any, decision: str
) -> None:
    result = start(
        rig.loop(
            fake([Reply.say("a")] * 3),
            verifier=CallableVerifier(lambda req: value),
            stop=StopConditions(max_verifier_rejections=1),
        )
    )
    assert result.state.verdicts[0].decision == decision


def test_an_async_callable_verifier_is_awaited(rig: Rig) -> None:
    async def judge(req: Any) -> Verdict:
        return Verdict.accept("ok")

    assert start(rig.loop(fake([Reply.say("a")]), verifier=CallableVerifier(judge))).ok


# ------------------------------------------------------------------ denials and approvals


def test_a_policy_denial_is_explained_to_the_model_which_can_adapt(rig: Rig) -> None:
    llm = fake(
        [
            Reply.call("paper_order", **order("deny-1", symbol="TSLA")),
            Reply.say("Understood, skipping TSLA."),
        ]
    )
    result = start(rig.loop(llm))
    assert result.ok and rig.executions == []
    seen = texts(llm.requests[1])
    assert "policy_denied" in seen and "symbol is on the restricted list" in seen


def big_order_script() -> list[Reply]:
    return [
        Reply.call("paper_order", **order("big-1", notional=30_000)),
        Reply.say("Bought, with approval."),
    ]


def human() -> Approver:
    return Approver(approver_id="alice", tenant_id=TENANT, max_tier=ApprovalTier.EXPLICIT_SIGNOFF)


def test_a_gated_action_parks_the_loop_until_a_human_decides(rig: Rig) -> None:
    llm = fake(big_order_script())
    loop = rig.loop(llm)
    parked = start(loop)
    assert parked.stop_reason is StopReason.APPROVAL_PENDING and parked.resumable
    action = parked.state.actions[0]
    assert action.status is ActionStatus.AWAITING_APPROVAL and action.approval_id
    assert rig.executions == []

    still = resume(loop)  # nobody has decided yet
    assert still.stop_reason is StopReason.APPROVAL_PENDING and llm.calls_made == 1

    request = rig.approvals.get(TENANT, action.approval_id)
    rig.approvals.approve(TENANT, request.request_id, human(), signoff_code=request.signoff_code)
    done = resume(loop)
    assert done.ok and [e["client_order_id"] for e in rig.executions] == ["big-1"]
    assert llm.calls_made == 2  # no re-planning to get the approved action executed


def test_a_rejected_approval_denies_the_action_and_tells_the_model(rig: Rig) -> None:
    llm = fake(big_order_script())
    loop = rig.loop(llm)
    parked = start(loop)
    approval_id = parked.state.actions[0].approval_id or ""
    rig.approvals.reject(TENANT, approval_id, human(), note="too risky")

    result = resume(loop)
    assert result.ok and rig.executions == []
    assert "approval_not_granted" in texts(llm.requests[1])


def test_an_expired_approval_denies_the_action(rig: Rig) -> None:
    loop = rig.loop(fake(big_order_script()))
    parked = start(loop)
    rig.clock.advance(timedelta(hours=2))  # the request's 30 minutes are long gone
    result = resume(loop)
    assert result.ok and rig.executions == []
    assert parked.state.actions[0].approval_id is not None


def test_without_an_approval_queue_a_gated_action_just_waits(rig: Rig) -> None:
    loop = rig.loop(fake(big_order_script()), approvals=None)
    parked = start(loop)
    assert parked.stop_reason is StopReason.APPROVAL_PENDING
    assert resume(loop).stop_reason is StopReason.APPROVAL_PENDING


# ------------------------------------------------------------ as_of and context hygiene


def test_a_tool_result_dated_after_as_of_never_reaches_the_model(rig: Rig) -> None:
    rig.news_published = MARKET_OPEN + timedelta(days=1)  # the "news" is from the future
    llm = fake(
        [
            Reply.call("get_news", topic="x"),
            Reply.call("market_quote", symbol="AAPL"),
            Reply.say("done"),
        ]
    )
    result = start(rig.loop(llm))

    assert result.ok
    for request in llm.requests:
        assert "HEADLINE-x" not in texts(request)
    assert result.state.context_rejections == 1  # refused once, however many plans followed
    rejected = [r for r in rig.audit.records(TENANT) if r.event_type == EventType.CONTEXT_REJECTED]
    assert len(rejected) == 1
    assert (
        rejected[0].payload["reason"] == "as_of_violation" and rejected[0].payload["run_id"] == "r1"
    )
    assert "HEADLINE" not in rejected[0].payload_json  # a hash is recorded, never the content


def test_a_tool_result_dated_before_as_of_is_shown(rig: Rig) -> None:
    llm = fake([Reply.call("get_news", topic="x"), Reply.say("done")])
    start(rig.loop(llm))
    assert "HEADLINE-x" in texts(llm.requests[1])


class StaticSource:
    def __init__(self, items: list[ContextItem]) -> None:
        self.items = items

    def retrieve(self, state: Any) -> list[ContextItem]:
        return self.items


def test_context_sources_are_untrusted_and_dated(rig: Rig) -> None:
    past = ContextItem.outside(
        ItemKind.MEMORY,
        "Earlier lesson: size positions small.",
        item_id="m-ok",
        published_at=MARKET_OPEN - timedelta(days=2),
        origin="memory:episodic",
    )
    future = ContextItem.outside(
        ItemKind.MEMORY,
        "FUTURE-LESSON",
        item_id="m-future",
        published_at=MARKET_OPEN + timedelta(days=2),
        origin="memory:episodic",
    )
    undated = ContextItem.outside(
        ItemKind.DOCUMENT, "UNDATED-DOC", item_id="m-undated", published_at=None, origin="doc"
    )
    llm = fake([Reply.say("done")])
    result = start(rig.loop(llm, context_sources=[StaticSource([past, future, undated])]))
    seen = texts(llm.requests[0])
    assert "size positions small" in seen and "<untrusted" in seen
    assert "FUTURE-LESSON" not in seen and "UNDATED-DOC" not in seen
    # Items a source supplied but the builder refused are counted and audited like any other.
    assert result.state.context_rejections == 2
    reasons = sorted(
        r.payload["reason"]
        for r in rig.audit.records(TENANT)
        if r.event_type == EventType.CONTEXT_REJECTED
    )
    assert reasons == ["as_of_violation", "undated"]


def test_injection_in_a_tool_result_is_fenced_and_cannot_widen_what_is_allowed(rig: Rig) -> None:
    """The planner reads hostile text from a tool and (obeying it) asks for a restricted trade."""
    llm = fake(
        [
            Reply.call("get_news", topic="x"),
            Reply.call("paper_order", **order("inj-1", notional=1_000_000, symbol="TSLA")),
            Reply.say("done"),
        ]
    )
    result = start(rig.loop(llm))
    assert result.ok and rig.executions == []
    assert (
        result.state.actions == []
    )  # cleared after the verify step; the denial is in observations
    assert any("policy_denied" in o.content for o in result.state.observations)


# ------------------------------------------------------------------ memory and episodes


def test_executed_writes_are_recorded_as_attributed_episodes(rig: Rig) -> None:
    episodes = EpisodicMemory(SqliteMemoryBackend(), tenant_id=TENANT, clock=rig.clock)
    result = start(rig.loop(fake(three_step_script()), episodic=episodes))
    recalled = episodes.recall("paper_order", as_of=MARKET_OPEN)
    assert len(recalled) == 1  # the READ was not recorded, the WRITE was
    assert recalled[0].attribution == Attribution(agent_id=AGENT, trace_id=result.state.trace_id)
    assert "o-1" in recalled[0].content and "ord-o-1" in recalled[0].data["outcome"]


# --------------------------------------------------------------- read-only loop kinds


def test_a_verification_loop_is_offered_only_read_tools(rig: Rig) -> None:
    llm = fake([Reply.say("The claim is supported.")])
    result = start(rig.loop(llm, loop_cls=VerificationLoop))
    assert result.ok
    assert {t.name for t in llm.requests[0].tools} == {"market_quote", "get_news"}
    assert result.state.loop_type.value == "verification"


def test_a_verification_loop_cannot_write_even_if_the_model_asks_anyway(rig: Rig) -> None:
    """A hostile or confused model names a tool it was never offered. The gateway still refuses."""
    llm = fake(
        [Reply.call("paper_order", **order("sneaky-1")), Reply.say("done")], strict_tools=False
    )
    result = start(rig.loop(llm, loop_cls=VerificationLoop))
    assert result.ok and rig.executions == []
    assert "side_effect_not_permitted" in texts(llm.requests[1])


@pytest.mark.parametrize(
    "allowed",
    [None, frozenset({SideEffect.READ, SideEffect.WRITE}), frozenset({SideEffect.PROPOSE})],
)
def test_a_verification_loop_refuses_to_be_given_more_than_read(rig: Rig, allowed: Any) -> None:
    with pytest.raises(ValueError, match="read-only"):
        rig.loop(fake([]), loop_cls=VerificationLoop, allowed_side_effects=allowed)


def test_verify_claim_runs_a_goal_framed_run(rig: Rig) -> None:
    loop = rig.loop(fake([Reply.say("Supported.")]), loop_cls=VerificationLoop)
    result = run(
        loop.verify_claim(
            "AAPL closed above 180",
            tenant_id=TENANT,
            agent_id=AGENT,
            as_of=MARKET_OPEN,
            run_id="v1",
        )
    )
    assert result.ok and "AAPL closed above 180" in result.state.goal


def monitor(
    rig: Rig,
    llm: FakeLLM,
    *,
    allow_writes: bool = False,
    sleeps: list[float] | None = None,
    **kw: Any,
) -> MonitorLoop:
    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return MonitorLoop(
        monitor_id="aapl-watch",
        goal="Check the AAPL quote and report anything unusual.",
        loop_factory=lambda **kw2: rig.loop(llm, **kw2),
        schedule=Schedule(timedelta(minutes=15)),
        tenant_id=TENANT,
        agent_id=AGENT,
        clock=lambda: MARKET_OPEN,
        sleep=fake_sleep,
        allow_writes=allow_writes,
        **kw,
    )


def test_a_monitor_runs_bounded_read_only_ticks_on_a_schedule(rig: Rig) -> None:
    sleeps: list[float] = []
    llm = fake(
        [Reply.call("market_quote", symbol="AAPL"), Reply.say("Nothing unusual.")],
        strict_tools=False,
    )
    summary = run(monitor(rig, llm, sleeps=sleeps).run_ticks(3))
    assert summary.ticks_run == 3 and all(r.ok for r in summary.results)
    assert sleeps == [900.0, 900.0]  # between ticks, not before the first
    assert rig.checkpoints.runs(TENANT, prefix="aapl-watch.tick-") == [
        "aapl-watch.tick-1",
        "aapl-watch.tick-2",
        "aapl-watch.tick-3",
    ]
    assert {r.state.loop_type.value for r in summary.results} == {"monitor"}


def test_a_monitor_is_read_only_by_default_even_against_a_hostile_script(rig: Rig) -> None:
    llm = fake([Reply.call("paper_order", **order("mon-1")), Reply.say("done")], strict_tools=False)
    mon = monitor(rig, llm)
    assert mon.read_only
    result = run(mon.tick())
    assert result.ok and rig.executions == []


def test_a_monitor_may_write_only_when_told_to_in_so_many_words(rig: Rig) -> None:
    llm = fake([Reply.call("paper_order", **order("mon-2")), Reply.say("done")])
    mon = monitor(rig, llm, allow_writes=True)
    assert not mon.read_only
    run(mon.tick())
    assert [e["client_order_id"] for e in rig.executions] == ["mon-2"]


def test_a_restarted_monitor_continues_numbering_and_finishes_a_half_done_tick(
    tmp_path: Path, rego_engine: Any
) -> None:
    rig = build_rig(tmp_path / "m", engine=rego_engine)
    script = [Reply.call("market_quote", symbol="AAPL"), Reply.say("ok")]

    run(monitor(rig, fake(script)).tick())  # tick-1 completes
    hits: dict[str, int] = {}

    def crash_once(name: str) -> None:
        hits[name] = hits.get(name, 0) + 1
        if name == "after_plan" and hits[name] == 2:
            raise RuntimeError("process died")

    llm = fake(script)
    broken = MonitorLoop(
        monitor_id="aapl-watch",
        goal="g",
        loop_factory=lambda **kw: rig.loop(llm, failpoint=crash_once, **kw),
        schedule=Schedule(timedelta(minutes=1)),
        tenant_id=TENANT,
        agent_id=AGENT,
        clock=lambda: MARKET_OPEN,
    )
    with pytest.raises(RuntimeError, match="process died"):
        run(broken.tick())  # tick-2 dies mid-way

    resumed = run(monitor(rig, fake(script)).tick())  # a fresh monitor finishes tick-2...
    assert resumed.state.run_id == "aapl-watch.tick-2" and resumed.ok
    nxt = run(monitor(rig, fake(script)).tick())  # ...and only then starts tick-3
    assert nxt.state.run_id == "aapl-watch.tick-3"


def test_a_monitor_validates_its_inputs(rig: Rig) -> None:
    with pytest.raises(ValueError, match="positive"):
        Schedule(timedelta(0))
    assert Schedule(timedelta(minutes=5)).next_after(MARKET_OPEN) == MARKET_OPEN + timedelta(
        minutes=5
    )
    with pytest.raises(ValueError, match="count"):
        run(monitor(rig, fake([])).run_ticks(0))


def test_a_monitor_refuses_a_loop_factory_that_ignores_the_read_only_restriction(rig: Rig) -> None:
    with pytest.raises(ValueError, match="honour allowed_side_effects"):
        MonitorLoop(
            monitor_id="m",
            goal="g",
            loop_factory=lambda **kw: rig.loop(fake([])),  # drops the restriction
            schedule=Schedule(timedelta(minutes=1)),
            tenant_id=TENANT,
            agent_id=AGENT,
        )


# ---------------------------------------------------------------- tenants and reconcile


def test_a_run_is_invisible_to_other_tenants(rig: Rig) -> None:
    start(rig.loop(fake(three_step_script())))
    assert rig.checkpoints.load("tenant-2", "r1") is None
    with pytest.raises(RunNotFoundError):
        run(rig.loop(fake([])).resume("tenant-2", "r1"))
    assert rig.checkpoints.runs("tenant-2") == []


def test_the_same_run_id_in_two_tenants_is_two_runs(rig: Rig) -> None:
    loop = rig.loop(fake([Reply.say("A done")]))
    run(
        loop.run(goal="g", tenant_id="tenant-1", agent_id=AGENT, as_of=MARKET_OPEN, run_id="shared")
    )
    run(
        loop.run(goal="g", tenant_id="tenant-2", agent_id=AGENT, as_of=MARKET_OPEN, run_id="shared")
    )
    assert rig.checkpoints.load("tenant-1", "shared") is not None
    assert rig.checkpoints.load("tenant-2", "shared") is not None


def test_resume_of_an_unknown_run_fails(rig: Rig) -> None:
    with pytest.raises(RunNotFoundError):
        resume(rig.loop(fake([])), "nope")


def test_reconcile_only_applies_to_unknown_actions(rig: Rig) -> None:
    # Stop mid-way with a planned action still pending: it is not UNKNOWN, so it cannot be reconciled.
    big = Usage(input_tokens=0, output_tokens=1200)
    loop = rig.loop(
        fake([Reply.call("paper_order", usage=big, **order("rec-1")), Reply.say("done")]),
        stop=StopConditions(max_tokens=1000),
    )
    stopped = start(loop)
    pending = stopped.state.actions[0]
    assert pending.status is ActionStatus.PENDING
    with pytest.raises(ValueError, match="not unknown"):
        run(loop.reconcile(TENANT, "r1", pending.action_id, executed=True, by="alice"))
    with pytest.raises(KeyError):
        run(loop.reconcile(TENANT, "r1", "r1:99:0", executed=True, by="alice"))
    with pytest.raises(RunNotFoundError):
        run(loop.reconcile(TENANT, "ghost", "x", executed=True, by="alice"))


def test_a_checkpoint_written_before_trace_roots_were_recorded_still_loads(rig: Rig) -> None:
    """0.1 checkpoints have no ``root_span_id``; 0.2 must resume them (docs/migrating-to-0.2.md)."""
    import json

    from keelgate.loop import LoopState

    result = start(rig.loop(fake(three_step_script())))
    old = json.loads(result.state.model_dump_json())
    del old["root_span_id"]  # the field did not exist in 0.1
    loaded = LoopState.model_validate(old)
    assert loaded.root_span_id == "" and loaded.run_id == result.state.run_id
