"""The optional Cedar backend: same decisions, and Cedar's fail-open quirks neutralised."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

cedarpy = pytest.importorskip("cedarpy")

from keelgate.policy import Decision, PolicyEngine  # noqa: E402
from keelgate.policy.cedar import CedarEngine, load_cedar_sources  # noqa: E402
from tests.conftest import LIMITS, run  # noqa: E402

PACK = Path(__file__).resolve().parents[1] / "policies" / "cedar_minimal"


@pytest.fixture(scope="module")
def engine() -> CedarEngine:
    return CedarEngine(PACK)


def doc(**resource: Any) -> dict[str, Any]:
    return {
        "action": {
            "tool": "trade_paper_execute",
            "side_effect": "WRITE",
            "capability": "trade:paper_execute",
            "args": {},
        },
        "actor": {"agent_id": "agent-1", "tenant_id": "tenant-1", "grant_id": "g-1"},
        "resource": {"symbol": "AAPL", "notional": 5000, **resource},
        "context": {
            "as_of": "2026-10-05T14:30:00+00:00",
            "execution_mode": "paper",
            "exposure": {"daily_notional": 0},
            "limits": copy.deepcopy(LIMITS),
        },
    }


def decide(engine: CedarEngine, document: dict[str, Any]) -> Any:
    return run(engine.decide_document(document))


def test_cedar_engine_satisfies_the_policy_protocol(engine: CedarEngine) -> None:
    assert isinstance(engine, PolicyEngine)
    assert engine.name == "cedar"


def test_allow_with_a_policy_label_as_the_reason(engine: CedarEngine) -> None:
    d = decide(engine, doc())
    assert d.effect is Decision.ALLOW
    assert d.reasons == ("permit_paper_trades_within_limits",)
    assert d.engine == "cedar"
    assert d.policy_version.startswith("sha256:")


@pytest.mark.parametrize("symbol", ["TSLA", "tsla", "gme", "Gme"])
def test_restricted_symbols_are_denied_case_insensitively(engine: CedarEngine, symbol: str) -> None:
    d = decide(engine, doc(symbol=symbol))
    assert d.effect is Decision.DENY
    assert d.reasons == ("denied by policy forbid_restricted_symbols",)


@pytest.mark.parametrize(
    ("notional", "tier"),
    [
        (10_001, "ONE_CLICK"),
        (25_000, "ONE_CLICK"),
        (25_001, "EXPLICIT_SIGNOFF"),
        (50_000, "EXPLICIT_SIGNOFF"),
    ],
)
def test_approval_tiers(engine: CedarEngine, notional: float, tier: str) -> None:
    d = decide(engine, doc(notional=notional))
    assert d.effect is Decision.REQUIRE_APPROVAL
    assert d.approval_tier is not None and d.approval_tier.value == tier


def test_no_approval_at_or_below_the_threshold(engine: CedarEngine) -> None:
    assert decide(engine, doc(notional=10_000)).effect is Decision.ALLOW


@pytest.mark.parametrize("notional", [50_001, 0, -5])
def test_limits_are_enforced(engine: CedarEngine, notional: float) -> None:
    d = decide(engine, doc(notional=notional))
    assert d.effect is Decision.DENY
    assert d.reasons == ("no Cedar policy permits this action",)


def test_fractional_amounts_round_up_so_a_cap_is_never_missed(engine: CedarEngine) -> None:
    assert decide(engine, doc(notional=50_000.00)).effect is Decision.REQUIRE_APPROVAL
    assert decide(engine, doc(notional=50_000.01)).effect is Decision.DENY


def test_daily_exposure_cap(engine: CedarEngine) -> None:
    d = doc()
    d["context"]["exposure"]["daily_notional"] = 96_000
    assert decide(engine, d).effect is Decision.DENY
    d["context"]["exposure"]["daily_notional"] = 95_000
    assert decide(engine, d).effect is Decision.ALLOW


@pytest.mark.parametrize("mode", ["live", "production", "", "PAPER"])
def test_only_paper_and_simulation_modes_are_permitted(engine: CedarEngine, mode: str) -> None:
    d = doc()
    d["context"]["execution_mode"] = mode
    assert decide(engine, d).effect is Decision.DENY


def test_simulation_mode_is_permitted(engine: CedarEngine) -> None:
    d = doc()
    d["context"]["execution_mode"] = "simulation"
    assert decide(engine, d).effect is Decision.ALLOW


def test_propose_is_permitted_and_ignores_exposure(engine: CedarEngine) -> None:
    d = doc()
    d["action"].update(capability="trade:propose", side_effect="PROPOSE")
    d["context"]["exposure"]["daily_notional"] = 99_999_999
    assert decide(engine, d).effect is Decision.ALLOW


@pytest.mark.parametrize(
    "action",
    [
        {"capability": "wire:transfer"},
        {"capability": "trade:paper_execute", "side_effect": "READ"},
        {"capability": "trade:propose", "side_effect": "WRITE"},
    ],
)
def test_unknown_or_mismatched_actions_are_denied(
    engine: CedarEngine, action: dict[str, str]
) -> None:
    d = doc()
    d["action"].update(action)
    assert decide(engine, d).effect is Decision.DENY


# --------------------------------------------------------- the fail-open quirk


def test_cedar_itself_fails_open_when_a_forbid_cannot_evaluate() -> None:
    """Documenting the hazard: the raw library returns Allow while reporting an error.

    If this ever stops being true upstream, the adapter's extra check can be
    revisited. Until then it is load-bearing.
    """
    policies = (
        'permit(principal, action == Action::"invoke", resource);\n'
        'forbid(principal, action == Action::"invoke", resource) when { resource.restricted == true };'
    )
    request = {
        "principal": {"type": "Agent", "id": "a"},
        "action": {"type": "Action", "id": "invoke"},
        "resource": {"type": "Resource", "id": "r"},
        "context": {},
    }
    entities = [
        {"uid": {"type": "Agent", "id": "a"}, "attrs": {}, "parents": []},
        {"uid": {"type": "Resource", "id": "r"}, "attrs": {}, "parents": []},  # no `restricted`
    ]
    raw = cedarpy.is_authorized(request, policies, entities)
    assert raw.allowed, "upstream behaviour changed; revisit CedarEngine._evaluate"
    assert list(raw.diagnostics.errors), "Cedar should have reported the evaluation error"


def test_the_adapter_denies_when_a_policy_errors(tmp_path: Path) -> None:
    """A forbid that errors is skipped by Cedar; the adapter must not accept that Allow."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "p.cedar").write_text(
        '@id("allow_all")\npermit(principal, action == Action::"invoke", resource);\n'
        '@id("forbid_flagged")\n'
        'forbid(principal, action == Action::"invoke", resource) when { resource.flagged == true };\n'
    )
    broken = CedarEngine(pack)  # resource has no `flagged` attribute
    d = decide(broken, doc())
    assert d.effect is Decision.DENY
    assert "evaluation failed" in d.reasons[0]


# ------------------------------------------------------------------ fail-closed


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["context"].pop("limits"),
        lambda d: d["context"].pop("exposure"),
        lambda d: d["context"].pop("as_of"),
        lambda d: d["context"].pop("execution_mode"),
        lambda d: d["context"]["limits"].pop("restricted_symbols"),
        lambda d: d["context"]["limits"].update(max_notional_per_action="lots"),
        lambda d: d["context"]["limits"].update(max_notional_per_action=float("nan")),
        lambda d: d["resource"].update(notional="5000"),
        lambda d: d["resource"].update(notional=True),
        lambda d: d["resource"].pop("symbol"),
        lambda d: d["resource"].pop("notional"),
        lambda d: d["context"].update(as_of="not a time"),
        lambda d: d.pop("actor"),
        lambda d: d.pop("action"),
        lambda d: d.clear(),
    ],
)
def test_malformed_input_is_denied_never_allowed(engine: CedarEngine, mutate: Any) -> None:
    d = doc()
    mutate(d)
    assert decide(engine, d).effect is Decision.DENY


def test_the_typed_path_rejects_nan(engine: CedarEngine) -> None:
    from keelgate.policy import PolicyAction, PolicyActor, PolicyInput
    from tests.conftest import policy_context

    bad = PolicyInput(
        action=PolicyAction(tool="t", side_effect="WRITE", capability="trade:paper_execute"),
        actor=PolicyActor(agent_id="a", tenant_id="t", grant_id="g"),
        resource={"symbol": "AAPL", "notional": float("nan")},
        context=policy_context(),
    )
    assert run(engine.decide(bad)).effect is Decision.DENY


def test_the_typed_path_decides(engine: CedarEngine) -> None:
    from keelgate.policy import PolicyAction, PolicyActor, PolicyInput
    from tests.conftest import policy_context

    ok = PolicyInput(
        action=PolicyAction(tool="t", side_effect="WRITE", capability="trade:paper_execute"),
        actor=PolicyActor(agent_id="a", tenant_id="t", grant_id="g"),
        resource={"symbol": "AAPL", "notional": 100},
        context=policy_context(),
    )
    assert run(engine.decide(ok)).effect is Decision.ALLOW


# --------------------------------------------------------------------- packs


def test_a_pack_that_does_not_parse_fails_at_startup(tmp_path: Path) -> None:
    (tmp_path / "bad.cedar").write_text("this is not cedar {{{")
    with pytest.raises(Exception):  # noqa: B017 - any parse failure is acceptable
        CedarEngine(tmp_path)


def test_missing_or_empty_pack_directories_are_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CedarEngine(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="no .cedar sources"):
        load_cedar_sources(tmp_path / "empty")


def test_version_hash_follows_the_policy_text(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "p.cedar").write_text('permit(principal, action == Action::"invoke", resource);')
    first = CedarEngine(pack).policy_version
    (pack / "p.cedar").write_text('forbid(principal, action == Action::"invoke", resource);')
    assert CedarEngine(pack).policy_version != first


def test_an_unlabelled_policy_falls_back_to_its_cedar_id(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "p.cedar").write_text(
        'permit(principal, action == Action::"invoke", resource);\n'
        'forbid(principal, action == Action::"invoke", resource);\n'
    )
    d = decide(CedarEngine(pack), doc())
    assert d.effect is Decision.DENY
    assert d.reasons == ("denied by policy policy1",)
