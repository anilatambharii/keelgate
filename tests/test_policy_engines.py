"""Engine plumbing: every failure mode must end in DENY, never ALLOW."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from keelgate.approvals import ApprovalTier
from keelgate.policy import (
    Decision,
    OpaHttpEngine,
    PolicyAction,
    PolicyActor,
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
    RegoEngine,
    decision_from_result,
    hash_sources,
    load_pack_sources,
    pack_path,
)
from tests.conftest import LIMITS, MARKET_OPEN, policy_context, run


def make_input(**overrides: Any) -> PolicyInput:
    values: dict[str, Any] = {
        "action": PolicyAction(
            tool="t", side_effect="WRITE", capability="trade:paper_execute", args={"a": 1}
        ),
        "actor": PolicyActor(agent_id="a", tenant_id="t", grant_id="g"),
        "resource": {"symbol": "AAPL", "notional": 100},
        "context": policy_context(),
    }
    values.update(overrides)
    return PolicyInput(**values)


# ------------------------------------------------------------ result parsing


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "ALLOW",
        7,
        [],
        {},
        {"effect": "MAYBE"},
        {"effect": None},
        {"effect": 1},
        {"reasons": ["x"]},
    ],
)
def test_malformed_results_become_deny(raw: object) -> None:
    decision = decision_from_result(raw, policy_version="v", engine="e")
    assert decision.effect is Decision.DENY


def test_a_well_formed_allow_is_allowed() -> None:
    decision = decision_from_result(
        {"effect": "ALLOW", "reasons": ["ok"], "approval_tier": None},
        policy_version="v",
        engine="e",
    )
    assert decision.effect is Decision.ALLOW
    assert decision.allowed
    assert decision.approval_tier is None


@pytest.mark.parametrize("tier", [None, "AUTO", "SOMETHING", 5, ""])
def test_approval_without_a_usable_tier_gets_the_strictest_tier(tier: object) -> None:
    decision = decision_from_result(
        {"effect": "REQUIRE_APPROVAL", "reasons": [], "approval_tier": tier},
        policy_version="v",
        engine="e",
    )
    assert decision.effect is Decision.REQUIRE_APPROVAL
    assert decision.approval_tier is ApprovalTier.EXPLICIT_SIGNOFF


def test_a_tier_on_a_non_approval_decision_is_ignored() -> None:
    decision = decision_from_result(
        {"effect": "ALLOW", "reasons": [], "approval_tier": "ONE_CLICK"},
        policy_version="v",
        engine="e",
    )
    assert decision.approval_tier is None


def test_reasons_are_filtered_truncated_and_capped() -> None:
    raw = {"effect": "DENY", "reasons": ["x" * 1000, 5, None, *[f"r{i}" for i in range(50)]]}
    decision = decision_from_result(raw, policy_version="v", engine="e")
    assert len(decision.reasons) == 20
    assert len(decision.reasons[0]) == 300
    assert all(isinstance(r, str) for r in decision.reasons)


def test_non_list_reasons_are_dropped() -> None:
    decision = decision_from_result(
        {"effect": "DENY", "reasons": "not a list"}, policy_version="v", engine="e"
    )
    assert decision.reasons == ()


def test_decision_model_refuses_inconsistent_states() -> None:
    with pytest.raises(ValueError, match="needs ONE_CLICK"):
        PolicyDecision(effect=Decision.REQUIRE_APPROVAL, policy_version="v", engine="e")
    with pytest.raises(ValueError, match="only REQUIRE_APPROVAL"):
        PolicyDecision(
            effect=Decision.ALLOW,
            approval_tier=ApprovalTier.ONE_CLICK,
            policy_version="v",
            engine="e",
        )


# -------------------------------------------------------------- policy input


def test_as_of_is_rendered_as_utc_with_an_explicit_offset() -> None:
    ny = timezone(timedelta(hours=-4))
    ctx = PolicyContext(as_of=datetime(2026, 10, 5, 10, 30, tzinfo=ny), limits=LIMITS)
    document = make_input(context=ctx).to_document()
    assert document["context"]["as_of"] == "2026-10-05T14:30:00+00:00"


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PolicyContext(as_of=datetime(2026, 10, 5, 14, 30))  # noqa: DTZ001


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_never_reach_the_engine(bad: float) -> None:
    """NaN compares false against every limit, which is how a cap gets bypassed."""
    with pytest.raises(ValueError, match="Out of range|allow_nan"):
        make_input(resource={"symbol": "AAPL", "notional": bad}).to_document()


def test_non_json_values_never_reach_the_engine() -> None:
    with pytest.raises(TypeError, match="non-JSON"):
        make_input(resource={"symbol": "AAPL", "notional": object()}).to_document()


def test_engines_deny_an_unserialisable_input(rego_engine: RegoEngine) -> None:
    decision = run(rego_engine.decide(make_input(resource={"notional": float("nan")})))
    assert decision.effect is Decision.DENY
    assert "rejected" in decision.reasons[0]

    opa = OpaHttpEngine("http://opa.invalid")
    decision = run(opa.decide(make_input(resource={"notional": float("nan")})))
    assert decision.effect is Decision.DENY


# ------------------------------------------------------------------ in-process


def test_rego_engine_satisfies_the_protocol(rego_engine: RegoEngine) -> None:
    assert isinstance(rego_engine, PolicyEngine)
    assert isinstance(OpaHttpEngine("http://x"), PolicyEngine)


def test_rego_engine_decides_through_the_typed_path(rego_engine: RegoEngine) -> None:
    decision = run(rego_engine.decide(make_input()))
    assert decision.effect is Decision.ALLOW
    assert decision.engine == "rego-inprocess"


def test_policy_version_tracks_the_pack_contents(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "p.rego").write_text("package keelgate.finance_basic\n\ndecision := {}\n")
    first = RegoEngine(pack).policy_version
    assert RegoEngine(pack).policy_version == first
    (pack / "p.rego").write_text('package keelgate.finance_basic\n\ndecision := {"x": 1}\n')
    assert RegoEngine(pack).policy_version != first


def test_test_files_do_not_change_the_policy_version(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "p.rego").write_text("package keelgate.finance_basic\n\ndecision := {}\n")
    before = RegoEngine(pack).policy_version
    (pack / "p_test.rego").write_text("package keelgate.finance_basic\n\ntest_x := true\n")
    assert RegoEngine(pack).policy_version == before


def test_a_broken_pack_fails_at_startup_not_at_the_first_decision(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "p.rego").write_text("package x\n\nthis is not rego {{{\n")
    with pytest.raises(Exception):  # noqa: B017 - any compile failure is acceptable
        RegoEngine(pack)


def test_a_pack_that_defines_nothing_denies(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "p.rego").write_text("package keelgate.finance_basic\n\nunrelated := 1\n")
    decision = run(RegoEngine(pack).decide(make_input()))
    assert decision.effect is Decision.DENY
    assert decision.reasons == ("policy returned no decision",)


def test_missing_pack_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        RegoEngine(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="no .rego sources"):
        load_pack_sources(tmp_path / "empty")


def test_pack_path_finds_the_shipped_pack() -> None:
    assert (pack_path("finance_basic") / "finance_basic.rego").is_file()


def test_hash_sources_is_order_independent_and_content_sensitive() -> None:
    a = hash_sources({"x.rego": "1", "y.rego": "2"})
    assert a == hash_sources({"y.rego": "2", "x.rego": "1"})
    assert a != hash_sources({"x.rego": "1", "y.rego": "3"})
    assert a != hash_sources({"x.rego": "12", "y.rego": ""})  # no boundary ambiguity


def test_concurrent_decisions_do_not_corrupt_each_other(rego_engine: RegoEngine) -> None:
    import asyncio

    async def many() -> list[Decision]:
        inputs = [
            make_input(resource={"symbol": "TSLA" if i % 2 else "AAPL", "notional": 100})
            for i in range(40)
        ]
        results = await asyncio.gather(*(rego_engine.decide(i) for i in inputs))
        return [r.effect for r in results]

    effects = run(many())
    assert effects == [Decision.DENY if i % 2 else Decision.ALLOW for i in range(40)]


# --------------------------------------------------------------------- OPA/HTTP


def opa_engine(handler: Any, **kwargs: Any) -> OpaHttpEngine:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpaHttpEngine("http://opa.test", client=client, **kwargs)


def good_handler(result: Any = None) -> Any:
    payload = result if result is not None else {"effect": "ALLOW", "reasons": ["ok"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/policies":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": "/policies/finance_basic/finance_basic.rego", "raw": "package x"},
                        {"id": "/policies/finance_basic/finance_basic_test.rego", "raw": "tests"},
                        {"id": "/policies/other/other.rego", "raw": "unrelated"},
                    ]
                },
            )
        return httpx.Response(200, json={"result": payload})

    return handler


def test_opa_allow_round_trip_and_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return good_handler()(request)

    decision = run(opa_engine(handler).decide(make_input()))
    assert decision.effect is Decision.ALLOW
    assert decision.engine == "opa-http"
    post = next(r for r in seen if r.method == "POST")
    assert post.url.path == "/v1/data/keelgate/finance_basic/decision"
    body = json.loads(post.content)
    assert body["input"]["context"]["as_of"] == MARKET_OPEN.astimezone(UTC).isoformat()
    assert body["input"]["actor"]["tenant_id"] == "t"


def test_opa_version_hashes_only_the_pack_sources_not_tests_or_other_packs() -> None:
    decision = run(opa_engine(good_handler()).decide(make_input()))
    assert decision.policy_version == hash_sources({"finance_basic.rego": "package x"})


def test_opa_version_is_cached_then_refreshed() -> None:
    fetches = 0
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        if request.url.path == "/v1/policies":
            fetches += 1
        return good_handler()(request)

    engine = opa_engine(handler, version_ttl=5.0, monotonic=lambda: now[0])
    run(engine.decide(make_input()))
    run(engine.decide(make_input()))
    assert fetches == 1
    now[0] = 6.0
    run(engine.decide(make_input()))
    assert fetches == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(404, json={}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={}),  # OPA's answer for an undefined rule
        httpx.Response(200, json={"result": None}),
        httpx.Response(200, json={"result": {"effect": "ALLOW-ish"}}),
        httpx.Response(200, json=["unexpected", "list"]),
    ],
)
def test_opa_bad_answers_deny(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/policies":
            return good_handler()(request)
        return response

    decision = run(opa_engine(handler).decide(make_input()))
    assert decision.effect is Decision.DENY


def test_opa_unreachable_denies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    decision = run(opa_engine(handler).decide(make_input()))
    assert decision.effect is Decision.DENY
    assert "ConnectError" in decision.reasons[0]
    assert decision.policy_version == "unavailable"


def test_opa_timeout_denies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    decision = run(opa_engine(handler).decide(make_input()))
    assert decision.effect is Decision.DENY


def test_opa_without_loaded_policies_denies_because_it_cannot_attest_a_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/policies":
            return httpx.Response(200, json={"result": []})
        return httpx.Response(200, json={"result": {"effect": "ALLOW", "reasons": []}})

    decision = run(opa_engine(handler).decide(make_input()))
    assert decision.effect is Decision.DENY


def test_opa_sends_the_bearer_token_when_configured() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return good_handler()(request)

    engine = OpaHttpEngine("http://opa.test", bearer_token="s3cr3t")  # pragma: allowlist secret
    engine._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer s3cr3t"},  # pragma: allowlist secret
    )
    run(engine.decide(make_input()))
    assert seen
    assert all(h == "Bearer s3cr3t" for h in seen)  # pragma: allowlist secret


def test_opa_does_not_follow_redirects_by_default() -> None:
    engine = OpaHttpEngine("http://opa.test")
    assert engine._client.follow_redirects is False
    run(engine.aclose())


def test_opa_close_leaves_an_injected_client_open() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(good_handler()))
    run(OpaHttpEngine("http://opa.test", client=client).aclose())
    assert not client.is_closed
    run(client.aclose())
