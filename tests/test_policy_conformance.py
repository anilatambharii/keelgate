"""finance_basic, evaluated by both Rego implementations.

Every case runs against the in-process engine and, when an OPA server is
reachable, against real OPA too. The two are separate implementations of Rego;
running one table through both is what stops them drifting apart unnoticed. The
OPA leg is skipped (loudly, with a reason) when no server answers; CI always
provides one.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from keelgate.policy import Decision, OpaHttpEngine, RegoEngine
from tests.conftest import LIMITS, run, skip_or_fail

OPA_URL = os.environ.get("KEELGATE_TEST_OPA_URL", "http://localhost:8181")

Doc = dict[str, Any]
Mutate = Callable[[Doc], None]


def base_doc() -> Doc:
    return {
        "action": {
            "tool": "trade.paper_execute",
            "side_effect": "WRITE",
            "capability": "trade:paper_execute",
            "args": {},
        },
        "actor": {"agent_id": "agent-1", "tenant_id": "tenant-1", "grant_id": "g-1"},
        "resource": {"symbol": "AAPL", "notional": 5000},
        "context": {
            "as_of": "2026-10-05T14:30:00+00:00",
            "execution_mode": "paper",
            "exposure": {"daily_notional": 0},
            "limits": copy.deepcopy(LIMITS),
            "positions": {},
        },
    }


def at(as_of: str) -> Mutate:
    return lambda d: d["context"].__setitem__("as_of", as_of)


def resource(**values: Any) -> Mutate:
    return lambda d: d["resource"].update(values)


def context(**values: Any) -> Mutate:
    return lambda d: d["context"].update(values)


def action(**values: Any) -> Mutate:
    return lambda d: d["action"].update(values)


def drop(*path: str) -> Mutate:
    def mutate(d: Doc) -> None:
        node = d
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return mutate


def both(*mutations: Mutate) -> Mutate:
    def mutate(d: Doc) -> None:
        for m in mutations:
            m(d)

    return mutate


def propose() -> Mutate:
    return action(capability="trade:propose", side_effect="PROPOSE")


# (id, mutation, effect, tier, reason fragment expected among the reasons)
CASES: list[tuple[str, Mutate, str, str | None, str | None]] = [
    # allow
    ("baseline", lambda d: None, "ALLOW", None, None),
    ("exact-open", at("2026-10-05T13:30:00+00:00"), "ALLOW", None, None),
    ("just-before-close", at("2026-10-05T19:59:00+00:00"), "ALLOW", None, None),
    ("open-in-winter", at("2026-01-05T14:30:00+00:00"), "ALLOW", None, None),
    ("simulation-mode", context(execution_mode="simulation"), "ALLOW", None, None),
    (
        "report-write",
        both(action(capability="report:write"), drop("resource")),
        "ALLOW",
        None,
        None,
    ),
    (
        "read-market-data",
        action(capability="market_data:read", side_effect="READ"),
        "ALLOW",
        None,
        None,
    ),
    ("propose-saturday", both(propose(), at("2026-10-10T03:00:00+00:00")), "ALLOW", None, None),
    (
        "propose-ignores-daily-exposure",
        both(propose(), context(exposure={"daily_notional": 99_999_999})),
        "ALLOW",
        None,
        None,
    ),
    (
        "exposure-exactly-at-limit",
        context(exposure={"daily_notional": 95_000}),
        "ALLOW",
        None,
        None,
    ),
    ("no-approval-at-threshold", resource(notional=10_000), "ALLOW", None, None),
    # A2A task intake is a PROPOSE under the same paper-only rule
    (
        "a2a-intake",
        both(action(capability="a2a:task_submit", side_effect="PROPOSE"), drop("resource")),
        "ALLOW",
        None,
        None,
    ),
    (
        "a2a-intake-as-write",
        both(action(capability="a2a:task_submit", side_effect="WRITE"), drop("resource")),
        "DENY",
        None,
        "not permitted",
    ),
    (
        "a2a-intake-live",
        both(
            action(capability="a2a:task_submit", side_effect="PROPOSE"),
            drop("resource"),
            context(execution_mode="live"),
        ),
        "DENY",
        None,
        "live execution is forbidden",
    ),
    # trading hours
    ("before-open", at("2026-10-05T13:29:00+00:00"), "DENY", None, "outside trading hours"),
    ("at-close", at("2026-10-05T20:00:00+00:00"), "DENY", None, "outside trading hours"),
    ("before-open-winter", at("2026-01-05T14:29:00+00:00"), "DENY", None, "outside trading hours"),
    ("saturday", at("2026-10-10T15:00:00+00:00"), "DENY", None, "outside trading hours"),
    ("sunday", at("2026-10-11T15:00:00+00:00"), "DENY", None, "outside trading hours"),
    ("as-of-missing", drop("context", "as_of"), "DENY", None, "outside trading hours"),
    ("as-of-gibberish", at("next tuesday"), "DENY", None, "outside trading hours"),
    ("as-of-no-offset", at("2026-10-05T14:30:00"), "DENY", None, "outside trading hours"),
    (
        "hours-missing",
        drop("context", "limits", "trading_hours"),
        "DENY",
        None,
        "outside trading hours",
    ),
    (
        "timezone-unknown",
        lambda d: d["context"]["limits"]["trading_hours"].update(tz="Mars/Olympus"),
        "DENY",
        None,
        None,  # OPA says "outside trading hours"; the in-process engine errors. Both deny.
    ),
    # paper only
    ("live-mode", context(execution_mode="live"), "DENY", None, "live execution is forbidden"),
    # The case that failed open in the first draft: an absent mode must DENY.
    (
        "mode-missing",
        drop("context", "execution_mode"),
        "DENY",
        None,
        "live execution is forbidden",
    ),
    ("mode-null", context(execution_mode=None), "DENY", None, "live execution is forbidden"),
    (
        "mode-wrong-case",
        context(execution_mode="PAPER"),
        "DENY",
        None,
        "live execution is forbidden",
    ),
    # restricted list
    ("restricted", resource(symbol="TSLA"), "DENY", None, "restricted list"),
    ("restricted-lowercase", resource(symbol="tsla"), "DENY", None, "restricted list"),
    ("restricted-list-entry-lowercase", resource(symbol="GME"), "DENY", None, "restricted list"),
    (
        "restricted-propose",
        both(propose(), resource(symbol="TSLA")),
        "DENY",
        None,
        "restricted list",
    ),
    # notional
    ("over-per-action", resource(notional=50_001), "DENY", None, "per-action limit"),
    ("zero-notional", resource(notional=0), "DENY", None, "positive number"),
    ("negative-notional", resource(notional=-100), "DENY", None, "positive number"),
    ("string-notional", resource(notional="5000"), "DENY", None, "positive number"),
    ("notional-missing", drop("resource", "notional"), "DENY", None, "positive number"),
    ("symbol-missing", drop("resource", "symbol"), "DENY", None, "symbol is missing"),
    ("symbol-not-a-string", resource(symbol=123), "DENY", None, "symbol is missing"),
    # daily exposure
    (
        "daily-exceeded",
        context(exposure={"daily_notional": 96_000}),
        "DENY",
        None,
        "daily exposure",
    ),
    ("exposure-missing", drop("context", "exposure"), "DENY", None, "daily exposure"),
    ("exposure-negative", context(exposure={"daily_notional": -1}), "DENY", None, "daily exposure"),
    ("exposure-string", context(exposure={"daily_notional": "0"}), "DENY", None, "daily exposure"),
    # approval tiers
    ("one-click", resource(notional=10_001), "REQUIRE_APPROVAL", "ONE_CLICK", "auto-approval"),
    (
        "explicit",
        resource(notional=25_001),
        "REQUIRE_APPROVAL",
        "EXPLICIT_SIGNOFF",
        "explicit sign-off",
    ),
    (
        "one-click-at-explicit-threshold",
        resource(notional=25_000),
        "REQUIRE_APPROVAL",
        "ONE_CLICK",
        None,
    ),
    (
        "at-the-per-action-cap",
        resource(notional=50_000),
        "REQUIRE_APPROVAL",
        "EXPLICIT_SIGNOFF",
        None,
    ),
    (
        "deny-beats-approval",
        resource(symbol="TSLA", notional=30_000),
        "DENY",
        None,
        "restricted list",
    ),
    # deny by default
    ("unknown-capability", action(capability="wire:transfer"), "DENY", None, "not permitted"),
    ("wrong-side-effect", action(side_effect="READ"), "DENY", None, "not permitted"),
    ("empty-input", lambda d: (d.clear(), None)[1], "DENY", None, "not permitted"),
    ("limits-missing", drop("context", "limits"), "DENY", None, "limits are missing"),
    (
        "limits-malformed",
        context(limits={"max_notional_per_action": "lots"}),
        "DENY",
        None,
        "limits are missing",
    ),
    (
        "thresholds-out-of-order",
        lambda d: d["context"]["limits"].update(approval_one_click_notional=30_000),
        "DENY",
        None,
        "limits are missing",
    ),
    (
        "restricted-list-not-an-array",
        lambda d: d["context"]["limits"].update(restricted_symbols="TSLA"),
        "DENY",
        None,
        "limits are missing",
    ),
]


class _FreshOpa:
    """An OPA engine built per call.

    ``run()`` starts a new event loop for every test and an ``httpx.AsyncClient``
    cannot be shared across loops, so each decision gets its own client. The
    engine under test is the same class production uses.
    """

    async def decide_document(self, document: Any) -> Any:
        engine = OpaHttpEngine(OPA_URL)
        try:
            return await engine.decide_document(document)
        finally:
            await engine.aclose()


@pytest.fixture(scope="module", params=["rego", "opa"])
def engine(request: pytest.FixtureRequest, rego_engine: RegoEngine) -> Any:
    if request.param == "rego":
        return rego_engine
    try:
        httpx.get(f"{OPA_URL}/health", timeout=1.0).raise_for_status()
    except Exception as exc:
        skip_or_fail(f"no OPA server at {OPA_URL} ({type(exc).__name__}); run `make up`")
    return _FreshOpa()


@pytest.mark.parametrize(
    ("case", "mutate", "effect", "tier", "fragment"), CASES, ids=[c[0] for c in CASES]
)
def test_finance_basic(
    engine: Any, case: str, mutate: Mutate, effect: str, tier: str | None, fragment: str | None
) -> None:
    doc = base_doc()
    mutate(doc)
    decision = run(engine.decide_document(doc))
    assert decision.effect is Decision(effect), (case, decision.reasons)
    assert (decision.approval_tier.value if decision.approval_tier else None) == tier
    if fragment is not None:
        assert any(fragment in r for r in decision.reasons), decision.reasons
    assert decision.reasons == tuple(sorted(decision.reasons)) or effect != "DENY"


def test_decision_carries_the_policy_version(engine: Any, rego_engine: RegoEngine) -> None:
    decision = run(engine.decide_document(base_doc()))
    assert decision.policy_version.startswith("sha256:")
    assert decision.engine in {"rego-inprocess", "opa-http"}


def test_both_engines_hash_the_same_policy_sources(engine: Any, rego_engine: RegoEngine) -> None:
    """The version recorded in the audit log is the hash of what actually decided."""
    decision = run(engine.decide_document(base_doc()))
    assert decision.policy_version == rego_engine.policy_version


def test_reasons_never_echo_model_supplied_strings(engine: Any) -> None:
    marker = "IGNORE-ALL-PREVIOUS-INSTRUCTIONS"
    doc = base_doc()
    doc["resource"]["symbol"] = marker
    doc["resource"]["notional"] = 999_999_999
    doc["context"]["limits"]["restricted_symbols"] = [marker]
    decision = run(engine.decide_document(doc))
    assert decision.effect is Decision.DENY
    assert all(marker.lower() not in r.lower() for r in decision.reasons)


def test_every_case_id_is_unique() -> None:
    ids = [c[0] for c in CASES]
    assert len(ids) == len(set(ids))
