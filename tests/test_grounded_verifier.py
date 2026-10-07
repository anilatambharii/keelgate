"""The deterministic grounding verifier: figures and claims must be supported by tool results."""

from __future__ import annotations

import json
from typing import Any

import pytest

from keelgate.context import ContextItem, ItemKind
from keelgate.loop import GroundedAnswerVerifier, VerdictDecision
from keelgate.loop.roles import VerifyRequest
from tests.conftest import MARKET_OPEN, run


def observation(tool: str, status: str, **output: Any) -> ContextItem:
    body = {"tool": tool, "arguments": {}, "status": status, "output": output}
    return ContextItem.outside(
        ItemKind.OBSERVATION,
        json.dumps(body, sort_keys=True),
        item_id=f"obs:{tool}",
        published_at=MARKET_OPEN,
        origin=f"tool:{tool}",
    )


def verify(answer: str | None, *obs: ContextItem, goal: str = "Research AAPL", **kw: Any) -> Any:
    request = VerifyRequest(
        run_id="r",
        tenant_id="t",
        iteration=1,
        goal=goal,
        final_answer=answer,
        actions=(),
        observations=obs,
    )
    return run(GroundedAnswerVerifier(**kw).verify(request))


QUOTE = observation("market_quote", "done", symbol="AAPL", price=187.25)


def test_figures_that_appear_in_a_tool_result_are_accepted() -> None:
    assert verify("AAPL last traded at 187.25.", QUOTE).decision is VerdictDecision.ACCEPT


@pytest.mark.parametrize("answer", ["AAPL is at 999.99.", "AAPL is at 178.25.", "Up 12.5% today."])
def test_an_invented_or_transposed_figure_is_rejected(answer: str) -> None:
    verdict = verify(answer, QUOTE)
    assert verdict.decision is VerdictDecision.REJECT and "ungrounded_number" in verdict.flags


def test_thousands_separators_and_the_goal_are_understood() -> None:
    order = observation("paper_order", "done", notional=5000)
    assert verify("Placed 5,000 of AAPL.", order).decision is VerdictDecision.ACCEPT
    assert (
        verify("Looked at 250 shares.", goal="Review 250 shares").decision is VerdictDecision.ACCEPT
    )


def test_small_whole_numbers_are_not_treated_as_claims() -> None:
    assert verify("I took 3 steps and found 2 sources.", QUOTE).decision is VerdictDecision.ACCEPT


def test_no_answer_yet_is_not_judged() -> None:
    assert verify(None).decision is VerdictDecision.ACCEPT


def test_a_claimed_effect_needs_a_completed_write_tool() -> None:
    done = observation("paper_order", "done")
    denied = observation("paper_order", "denied")
    kw = {"write_tools": {"paper_order"}}
    assert verify("I bought AAPL.", done, **kw).decision is VerdictDecision.ACCEPT
    rejected = verify("I bought AAPL.", denied, **kw)
    assert rejected.decision is VerdictDecision.REJECT and "unsupported_claim" in rejected.flags
    assert verify("I bought AAPL.", QUOTE, **kw).decision is VerdictDecision.REJECT


@pytest.mark.parametrize(
    "answer",
    ["The order was not executed.", "I could not place the order.", "It never filled."],
)
def test_denying_an_effect_is_not_claiming_it(answer: str) -> None:
    assert verify(
        answer, observation("paper_order", "denied"), write_tools={"paper_order"}
    ).decision is (VerdictDecision.ACCEPT)


def test_the_claim_rule_is_off_unless_write_tools_are_named() -> None:
    assert verify("I bought AAPL.", QUOTE).decision is VerdictDecision.ACCEPT


def test_a_malformed_observation_does_not_crash_the_verifier() -> None:
    bad = ContextItem.outside(
        ItemKind.OBSERVATION,
        "not json at all 42",
        item_id="obs:x",
        published_at=MARKET_OPEN,
        origin="t",
    )
    assert verify("The value is 42.", bad).decision is VerdictDecision.ACCEPT


@pytest.mark.parametrize(
    "answer",
    [
        "AAPL last traded at 187.25.",  # describes the market, not an act of ours
        "The stock has traded sideways near 187.25.",
        "Volume traded was heavy, around 187.25 per share.",
    ],
)
def test_the_word_traded_alone_is_not_a_claim_of_action(answer: str) -> None:
    got = verify(answer, QUOTE, write_tools={"paper_order"})
    assert got.decision is VerdictDecision.ACCEPT


@pytest.mark.parametrize(
    "answer",
    ["I bought AAPL.", "We've placed the order.", "Bought some AAPL.", "The order was filled."],
)
def test_first_person_imperative_and_passive_claims_are_recognised(answer: str) -> None:
    got = verify(answer, QUOTE, write_tools={"paper_order"})
    assert got.decision is VerdictDecision.REJECT and "unsupported_claim" in got.flags
