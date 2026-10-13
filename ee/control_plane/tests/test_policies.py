"""The hosted policy registry: testing, simulation, deploy gating, rollback and isolation."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelgate.audit import AuditError, verify_chain
from keelgate.cloud import CloudClient
from keelgate.cloud._policy import CloudPolicyEngine
from keelgate.policy import PolicyInput, RegoEngine
from psycopg.types.json import Jsonb

from keelgate_cloud import policy_registry as reg
from keelgate_cloud.db import Database
from tests.harness import Org, live_server, make_app

QUERY = "data.acme.mini.decision"
MINI = """package acme.mini

import rego.v1

default decision := {
	"effect": "DENY",
	"reasons": ["no rule allows this action"],
	"approval_tier": null,
}

allow_read if {
	input.action.side_effect == "READ"
	input.context.execution_mode == "paper"
}

decision := {"effect": "ALLOW", "reasons": ["read"], "approval_tier": null} if allow_read
"""
LOOSE = MINI.replace('\tinput.context.execution_mode == "paper"\n', "")


def doc(side_effect: str = "READ", mode: str | None = "paper") -> dict[str, Any]:
    context: dict[str, Any] = {"as_of": "2026-10-05T14:30:00+00:00"}
    if mode is not None:
        context["execution_mode"] = mode
    return {
        "action": {"tool": "t", "side_effect": side_effect, "capability": "x:y", "args": {}},
        "actor": {"agent_id": "a", "tenant_id": "t", "grant_id": "g"},
        "resource": {},
        "context": context,
    }


CASES = [
    {"name": "read in paper is allowed", "input": doc(), "expect": {"effect": "ALLOW"}},
    {"name": "write is denied", "input": doc("WRITE"), "expect": {"effect": "DENY"}},
    {"name": "missing mode is denied", "input": doc(mode=None), "expect": {"effect": "DENY"}},
]


def pack(files: dict[str, str] | None = None, cases: list[Any] | None = None) -> dict[str, Any]:
    return {
        "files": files or {"mini.rego": MINI},
        "query": QUERY,
        "cases": CASES if cases is None else cases,
    }


@pytest.fixture(scope="module")
def server(db: Database):  # type: ignore[no-untyped-def]
    app = make_app(db)
    with live_server(app) as base:
        yield {"base": base, "app": app, "http": TestClient(app), "db": db}


@pytest.fixture
def http(server: dict[str, Any]) -> TestClient:
    return server["http"]  # type: ignore[no-any-return]


def create(http: TestClient, org: Org, name: str, body: dict[str, Any] | None = None) -> Any:
    return http.post("/api/policies", json={"name": name, **(body or pack())}, headers=org.auth)


def new_version(http: TestClient, org: Org, name: str, body: dict[str, Any] | None = None) -> Any:
    return http.post(f"/api/policies/{name}/versions", json=body or pack(), headers=org.auth)


def deploy(http: TestClient, org: Org, name: str, n: int) -> Any:
    return http.post(f"/api/policies/{name}/versions/{n}/deploy", headers=org.auth)


# --------------------------------------------------------------------------- compile and cases


def test_a_pack_that_does_not_compile_is_a_422_with_the_compiler_message(http: TestClient) -> None:
    org = Org(http)
    broken = pack({"mini.rego": "package acme.mini\n\ndecision := {{{ nope\n"})
    response = create(http, org, "broken", broken)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "compile_error"
    assert "does not compile" in error["message"]
    assert len(error["message"]) < 3000
    assert http.get("/api/policies", headers=org.auth).json() == {"items": []}  # nothing stored


def test_a_huge_compiler_message_is_truncated(http: TestClient) -> None:
    org = Org(http)
    body = pack({"mini.rego": "package acme.mini\n" + "x := [" * 20_000})
    error = create(http, org, "huge", body).json()["error"]
    assert error["code"] == "compile_error"
    assert len(error["message"]) < 3000


def test_simulate_reports_a_compile_error_without_failing_the_request(http: TestClient) -> None:
    org = Org(http)
    body = {"files": {"mini.rego": "package x\n\n{{{"}, "query": "data.x.decision", "input": doc()}
    response = http.post("/api/policies/simulate", json=body, headers=org.auth)
    assert response.status_code == 200
    out = response.json()
    assert out["compiled"] is False
    assert "Error" in out["compile_error"] or out["compile_error"]
    assert out["results"] == []


def test_create_runs_the_cases_stores_the_report_and_does_not_deploy(http: TestClient) -> None:
    org = Org(http)
    response = create(http, org, "mini")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["tests_passed"] is True
    assert body["active"] is False
    assert body["source_hash"].startswith("sha256:")
    report = body["test_report"]
    assert report["total"] == 3
    assert report["passed_count"] == 3
    assert [c["passed"] for c in report["cases"]] == [True, True, True]
    assert report["cases"][0]["actual"]["effect"] == "ALLOW"
    listing = http.get("/api/policies", headers=org.auth).json()["items"]
    assert [(i["name"], i["active_version"], i["latest_version"]) for i in listing] == [
        ("mini", None, 1)
    ]
    full = http.get("/api/policies/mini/versions/1", headers=org.auth).json()
    assert full["files"] == {"mini.rego": MINI}
    assert full["query"] == QUERY
    assert full["cases"][0]["name"] == "read in paper is allowed"
    assert full["test_report"]["source_hash"] == full["source_hash"]


def test_the_version_hash_is_the_open_source_source_hash(http: TestClient) -> None:
    from keelgate.policy._engine import hash_sources  # noqa: PLC0415

    org = Org(http)
    body = create(http, org, "h").json()
    assert body["source_hash"] == hash_sources({"mini.rego": MINI})


def test_failing_cases_are_recorded_and_block_deploy(http: TestClient) -> None:
    org = Org(http)
    wrong = [
        *CASES,
        {"name": "wrongly expects allow", "input": doc("WRITE"), "expect": {"effect": "ALLOW"}},
    ]
    body = create(http, org, "bad", pack(cases=wrong)).json()
    assert body["tests_passed"] is False
    failed = [c for c in body["test_report"]["cases"] if not c["passed"]]
    assert [c["name"] for c in failed] == ["wrongly expects allow"]
    assert failed[0]["actual"]["effect"] == "DENY"
    blocked = deploy(http, org, "bad", 1)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "tests_failed"
    assert blocked.json()["report"]["passed_count"] == 3
    assert http.get("/v1/policies/bad/active", headers=org.key_auth).status_code == 404


def test_a_missing_input_field_must_deny_and_a_loose_policy_is_caught(http: TestClient) -> None:
    """policies/README rule 5: every rule needs a test for its input being missing."""
    org = Org(http)
    ok = create(http, org, "strict").json()
    assert ok["tests_passed"] is True  # the strict pack denies when the mode is absent
    loose = create(http, org, "loose", pack({"mini.rego": LOOSE})).json()
    assert loose["tests_passed"] is False  # allows with the mode missing, so the case fails
    case = next(c for c in loose["test_report"]["cases"] if c["name"] == "missing mode is denied")
    assert case["actual"]["effect"] == "ALLOW"


def test_approval_tier_is_compared_only_when_the_case_states_it(http: TestClient) -> None:
    org = Org(http)
    files = {
        "t.rego": 'package acme.t\n\nimport rego.v1\n\ndecision := {"effect": "REQUIRE_APPROVAL", '
        '"reasons": ["big"], "approval_tier": "ONE_CLICK"}\n'
    }
    base = {"files": files, "query": "data.acme.t.decision"}
    cases = [
        {"name": "unstated", "input": doc(), "expect": {"effect": "REQUIRE_APPROVAL"}},
        {
            "name": "right tier",
            "input": doc(),
            "expect": {"effect": "REQUIRE_APPROVAL", "approval_tier": "ONE_CLICK"},
        },
        {
            "name": "wrong tier",
            "input": doc(),
            "expect": {"effect": "REQUIRE_APPROVAL", "approval_tier": "EXPLICIT_SIGNOFF"},
        },
        {
            "name": "expects no tier",
            "input": doc(),
            "expect": {"effect": "REQUIRE_APPROVAL", "approval_tier": None},
        },
    ]
    out = http.post(
        "/api/policies/simulate", json={**base, "cases": cases}, headers=org.auth
    ).json()
    assert [r["passed"] for r in out["results"]] == [True, True, False, False]


def test_simulate_adhoc_input_persists_nothing_and_shows_the_decision(http: TestClient) -> None:
    org = Org(http)
    body = {"files": {"mini.rego": MINI}, "query": QUERY, "input": doc()}
    out = http.post("/api/policies/simulate", json=body, headers=org.auth).json()
    assert out["compile_error"] is None
    assert out["results"][0]["passed"] is None
    assert out["results"][0]["actual"] == {
        "effect": "ALLOW",
        "reasons": ["read"],
        "approval_tier": None,
    }
    assert http.get("/api/policies", headers=org.auth).json() == {"items": []}
    both = http.post("/api/policies/simulate", json={**body, "cases": CASES}, headers=org.auth)
    assert both.status_code == 422
    neither = http.post(
        "/api/policies/simulate", json={"files": body["files"], "query": QUERY}, headers=org.auth
    )
    assert neither.status_code == 422


def test_simulate_cases_return_per_case_pass_fail(http: TestClient) -> None:
    org = Org(http)
    cases = [*CASES, {"name": "bad", "input": doc(), "expect": {"effect": "DENY"}}]
    out = http.post(
        "/api/policies/simulate",
        json={"files": {"mini.rego": MINI}, "query": QUERY, "cases": cases},
        headers=org.auth,
    ).json()
    assert out["total"] == 4
    assert out["passed_count"] == 3
    assert out["all_passed"] is False
    assert [r["passed"] for r in out["results"]] == [True, True, True, False]
    assert out["results"][3]["expected"] == {"effect": "DENY"}


def test_a_wrong_query_is_an_engine_error_not_a_pass(http: TestClient) -> None:
    org = Org(http)
    body = {**pack(), "query": "data.acme.nothing.decision"}
    report = create(http, org, "noq", body).json()["test_report"]
    # An undefined rule is a policy DENY, so deny-cases pass; the allow case shows the mistake.
    assert report["cases"][0]["passed"] is False


# ------------------------------------------------------------------------------- templates


def test_templates_give_the_editor_something_to_start_from(http: TestClient) -> None:
    org = Org(http)
    items = http.get("/api/policies/templates", headers=org.auth).json()["items"]
    names = {i["name"] for i in items}
    assert {"starter", "finance_basic", "transfer_limits"} <= names
    for item in items:
        if not item["cases"]:
            continue
        out = http.post(
            "/api/policies/simulate",
            json={"files": item["files"], "query": item["query"], "cases": item["cases"]},
            headers=org.auth,
        ).json()
        assert out["compile_error"] is None, item["name"]
        assert out["all_passed"] is True, (item["name"], out["results"])
    finance = next(i for i in items if i["name"] == "finance_basic")
    assert not any(n.endswith("_test.rego") for n in finance["files"])
    assert http.get("/api/policies/templates").status_code == 401


# ---------------------------------------------------------------------------------- versions


def test_pack_names_are_unique_per_org_and_validated(http: TestClient) -> None:
    a, b = Org(http), Org(http)
    assert create(http, a, "same").status_code == 201
    assert create(http, a, "same").status_code == 409
    assert create(http, b, "same").status_code == 201  # another org may reuse the name
    for bad in ("Upper", "-lead", "has space", "x" * 64, "a/b", ""):
        assert create(http, a, bad).status_code == 422, bad


def test_versions_are_numbered_listed_and_immutable(http: TestClient) -> None:
    org = Org(http)
    create(http, org, "v")
    second = new_version(http, org, "v", pack({"mini.rego": MINI + "\n# v2\n"}))
    assert second.status_code == 201
    assert second.json()["version"] == 2
    first = http.get("/api/policies/v/versions/1", headers=org.auth).json()
    assert first["files"] == {"mini.rego": MINI}  # creating v2 did not touch v1
    items = http.get("/api/policies/v/versions", headers=org.auth).json()["items"]
    assert [i["version"] for i in items] == [2, 1]
    assert all(i["active"] is False and i["tests_passed"] for i in items)
    assert items[0]["source_hash"] != items[1]["source_hash"]
    for verb in (http.put, http.patch, http.delete):
        assert verb("/api/policies/v/versions/1", headers=org.auth).status_code in (404, 405)
    assert http.get("/api/policies/v/versions/9", headers=org.auth).status_code == 404
    assert http.get("/api/policies/nope/versions", headers=org.auth).status_code == 404
    assert new_version(http, org, "nope").status_code == 404


def test_version_numbering_is_race_safe(server: dict[str, Any], http: TestClient) -> None:
    org = Org(http)
    create(http, org, "race")
    statuses: list[tuple[int, int | None]] = []
    gate = threading.Barrier(8)

    def worker(i: int) -> None:
        client = TestClient(server["app"])
        gate.wait()
        # The per-org evaluation cap answers 429 when too many run at once: back off and retry.
        for _ in range(200):
            response = new_version(client, org, "race", pack({"mini.rego": MINI + f"\n# {i}\n"}))
            if response.status_code != 429:
                break
            time.sleep(0.05)
        statuses.append((response.status_code, response.json().get("version")))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [s for s, _ in statuses] == [201] * 8
    assert sorted(v for _, v in statuses if v is not None) == list(range(2, 10))
    items = http.get("/api/policies/race/versions", headers=org.auth).json()["items"]
    assert [i["version"] for i in items] == list(range(9, 0, -1))


# ------------------------------------------------------------------------------------ deploy


def test_deploy_activates_records_who_and_writes_a_verifiable_audit_event(
    http: TestClient,
) -> None:
    org = Org(http)
    create(http, org, "dep")
    response = deploy(http, org, "dep", 1)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["version"] == 1
    assert out["deployed_by"]
    assert out["deployed_at"]
    listing = http.get("/api/policies", headers=org.auth).json()["items"][0]
    assert listing["active_version"] == 1
    assert http.get("/api/policies/dep/versions", headers=org.auth).json()["items"][0]["active"]
    assert http.get("/api/policies/dep/versions/1", headers=org.auth).json()["active"] is True

    records = list(http.app.state.kg.audit.records(org.id))  # type: ignore[attr-defined]
    events = [r for r in records if r.event_type == "policy.deployed"]
    assert len(events) == 1
    assert json.loads(events[0].payload_json) == {
        "pack": "dep",
        "version": 1,
        "source_hash": out["source_hash"],
    }
    assert events[0].actor.startswith("user:")
    head = http.app.state.kg.audit.head(org.id)  # type: ignore[attr-defined]
    assert verify_chain(records, tenant_id=org.id, expected_head=head)


def test_deploy_is_refused_without_cases(http: TestClient) -> None:
    org = Org(http)
    create(http, org, "empty", pack(cases=[]))
    response = deploy(http, org, "empty", 1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_tests"
    assert http.get("/v1/policies/empty/active", headers=org.key_auth).status_code == 404


def test_deploy_reruns_the_cases_so_a_stale_report_cannot_deploy(
    server: dict[str, Any], http: TestClient
) -> None:
    org = Org(http)
    create(http, org, "stale")
    stale = copy.deepcopy(CASES)
    stale[0]["expect"] = {"effect": "DENY"}  # no longer true of the stored sources
    with server["db"].tx(org.id) as conn:  # tamper: stored report still says "passed"
        conn.execute("UPDATE policy_versions SET cases = %s", (Jsonb(stale),))
    response = deploy(http, org, "stale", 1)
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "tests_failed"
    assert body["report"]["cases"][0]["passed"] is False
    assert http.get("/v1/policies/stale/active", headers=org.key_auth).status_code == 404


def test_deploy_refuses_sources_that_no_longer_match_their_hash(
    server: dict[str, Any], http: TestClient
) -> None:
    org = Org(http)
    create(http, org, "tamper")
    with server["db"].tx(org.id) as conn:
        conn.execute("UPDATE policy_versions SET files = %s", (Jsonb({"mini.rego": LOOSE}),))
    assert deploy(http, org, "tamper", 1).status_code == 500
    deployed_elsewhere = http.get("/v1/policies/tamper/active", headers=org.key_auth)
    assert deployed_elsewhere.status_code == 404


def test_deploy_is_refused_when_the_audit_record_cannot_be_written(
    server: dict[str, Any], http: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(http)
    create(http, org, "aud")

    def refuse(**_: Any) -> None:
        raise AuditError("quota")

    monkeypatch.setattr(server["app"].state.kg.audit, "append", refuse)
    response = deploy(http, org, "aud", 1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "audit_rejected"
    assert http.get("/api/policies", headers=org.auth).json()["items"][0]["active_version"] is None


def test_redeploy_and_rollback(http: TestClient) -> None:
    org = Org(http)
    create(http, org, "rb")
    new_version(http, org, "rb", pack({"mini.rego": MINI + "\n# two\n"}))
    new_version(http, org, "rb", pack({"mini.rego": MINI + "\n# three\n"}))
    assert http.post("/api/policies/rb/rollback", headers=org.auth).json()["error"]["code"] == (
        "nothing_active"
    )
    deploy(http, org, "rb", 1)
    assert http.post("/api/policies/rb/rollback", headers=org.auth).status_code == 409
    deploy(http, org, "rb", 2)
    deploy(http, org, "rb", 3)
    first = http.post("/api/policies/rb/rollback", headers=org.auth)
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2
    bundle = http.get("/v1/policies/rb/active", headers=org.key_auth).json()
    assert bundle["version"] == 2
    assert bundle["files"] == {"mini.rego": MINI + "\n# two\n"}
    listing = http.get("/api/policies", headers=org.auth).json()["items"][0]
    assert (listing["active_version"], listing["latest_version"]) == (2, 3)
    records = list(http.app.state.kg.audit.records(org.id))  # type: ignore[attr-defined]
    types = [r.event_type for r in records if r.event_type.startswith("policy.")]
    assert types == ["policy.deployed"] * 3 + ["policy.rolled_back"]
    rolled = json.loads(
        next(r for r in records if r.event_type == "policy.rolled_back").payload_json
    )
    assert rolled["version"] == 2
    assert rolled["from_version"] == 3


# ---------------------------------------------------------------------------------- agent API


def test_active_bundle_shape_and_404_when_nothing_is_deployed(http: TestClient) -> None:
    org = Org(http)
    assert http.get("/v1/policies/nothing/active", headers=org.key_auth).status_code == 404
    create(http, org, "shape")
    assert http.get("/v1/policies/shape/active", headers=org.key_auth).status_code == 404
    deploy(http, org, "shape", 1)
    bundle = http.get("/v1/policies/shape/active", headers=org.key_auth).json()
    assert set(bundle) == {"name", "version", "files", "query", "source_hash", "deployed_at"}
    assert bundle["name"] == "shape"
    assert bundle["query"] == QUERY
    assert http.get("/v1/policies/shape/active").status_code == 401
    assert http.get("/v1/policies/shape/active", headers=org.auth).status_code == 401  # not a key


def test_the_real_client_gets_the_same_decisions_as_the_local_engine(
    server: dict[str, Any], http: TestClient
) -> None:
    org = Org(http)
    template = next(t for t in reg.templates() if t["name"] == "transfer_limits")
    body = {"files": template["files"], "query": template["query"], "cases": template["cases"]}
    assert create(http, org, "transfer-limits", body).json()["tests_passed"] is True
    assert deploy(http, org, "transfer-limits", 1).status_code == 200

    remote = CloudPolicyEngine.from_registry(
        CloudClient(server["base"], org.api_key), "transfer-limits"
    )
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        for name, text in template["files"].items():
            (Path(tmp) / name).write_text(text, encoding="utf-8")
        local = RegoEngine(Path(tmp), query=template["query"])
    assert remote.policy_version == local.policy_version

    async def both(case: dict[str, Any]) -> tuple[Any, Any]:
        document = case["input"]
        policy_input = PolicyInput.model_validate(
            {
                **document,
                "context": {**document["context"], "as_of": datetime(2026, 10, 5, tzinfo=UTC)},
            }
        )
        return await remote.decide(policy_input), await local.decide(policy_input)

    for case in template["cases"]:
        got, want = asyncio.run(both(case))
        assert got.effect == want.effect, case["name"]
        assert got.effect.value == case["expect"]["effect"], case["name"]
        assert got.approval_tier == want.approval_tier
        assert got.policy_version == want.policy_version


# ---------------------------------------------------------------------------- tenant isolation


def test_one_org_cannot_see_use_or_deploy_anothers_packs(http: TestClient) -> None:
    a, b = Org(http, "A"), Org(http, "B")
    create(http, a, "secret")
    deploy(http, a, "secret", 1)
    assert http.get("/api/policies", headers=b.auth).json() == {"items": []}
    for response in (
        http.get("/api/policies/secret/versions", headers=b.auth),
        http.get("/api/policies/secret/versions/1", headers=b.auth),
        new_version(http, b, "secret"),
        deploy(http, b, "secret", 1),
        http.post("/api/policies/secret/rollback", headers=b.auth),
        http.get("/v1/policies/secret/active", headers=b.key_auth),
    ):
        assert response.status_code == 404, response.text
    # B may create its own pack under the same name; B's key then sees B's, never A's.
    other = MINI + "\n# b\n"
    assert create(http, b, "secret", pack({"mini.rego": other})).status_code == 201
    assert deploy(http, b, "secret", 1).status_code == 200
    mine = http.get("/v1/policies/secret/active", headers=b.key_auth).json()
    theirs = http.get("/v1/policies/secret/active", headers=a.key_auth).json()
    assert mine["files"] == {"mini.rego": other}
    assert theirs["files"] == {"mini.rego": MINI}
    # B's activity left A's chain and active version alone.
    assert http.get("/api/policies", headers=a.auth).json()["items"][0]["active_version"] == 1


def test_simulate_runs_only_on_the_callers_own_sources(http: TestClient) -> None:
    a, b = Org(http), Org(http)
    create(http, a, "secret")
    # There is no way to name another org's pack in a simulation: only inline sources are used.
    out = http.post(
        "/api/policies/simulate",
        json={"files": {"mini.rego": LOOSE}, "query": QUERY, "input": doc(mode=None)},
        headers=b.auth,
    ).json()
    assert out["results"][0]["actual"]["effect"] == "ALLOW"
    assert http.get("/api/policies", headers=b.auth).json() == {"items": []}


# ----------------------------------------------------------------------------------- roles


def test_roles(http: TestClient) -> None:
    org = Org(http)
    create(http, org, "r")
    admin, approver, viewer = org.invite("admin"), org.invite("approver"), org.invite("viewer")
    for headers in (approver, viewer):
        assert http.get("/api/policies", headers=headers).status_code == 200
        assert http.get("/api/policies/r/versions", headers=headers).status_code == 200
        assert http.get("/api/policies/r/versions/1", headers=headers).status_code == 200
        assert http.get("/api/policies/templates", headers=headers).status_code == 200
        assert (
            http.post("/api/policies", json={"name": "x", **pack()}, headers=headers).status_code
            == 403
        )
        assert (
            http.post("/api/policies/r/versions", json=pack(), headers=headers).status_code == 403
        )
        assert http.post("/api/policies/r/versions/1/deploy", headers=headers).status_code == 403
        assert http.post("/api/policies/r/rollback", headers=headers).status_code == 403
        sim = {"files": pack()["files"], "query": QUERY, "input": doc()}
        assert http.post("/api/policies/simulate", json=sim, headers=headers).status_code == 403
    assert (
        http.post("/api/policies", json={"name": "x", **pack()}, headers=admin).status_code == 201
    )
    assert http.post("/api/policies/r/versions/1/deploy", headers=admin).status_code == 200
    assert http.get("/api/policies").status_code == 401
    assert http.post("/api/policies", json={"name": "x", **pack()}).status_code == 401


# ------------------------------------------------------------------------------------ limits


def test_size_and_shape_limits(http: TestClient) -> None:
    org = Org(http)

    def post(files: dict[str, str], cases: list[Any] | None = None, query: str = QUERY) -> Any:
        body = {"name": "lim", "files": files, "query": query, "cases": cases or []}
        return http.post("/api/policies", json=body, headers=org.auth)

    ok = {"mini.rego": MINI}
    assert post({}).json()["error"]["code"] == "no_files"
    assert post({f"f{i}.rego": MINI for i in range(21)}).json()["error"]["code"] == "too_big"
    big = "# " + "x" * (200 * 1024) + "\n"
    assert post({"a.rego": MINI + big}).json()["error"]["code"] == "too_big"
    chunk = MINI + "# " + "y" * (190 * 1024) + "\n"
    assert post({f"c{i}.rego": chunk for i in range(3)}).json()["error"]["code"] == "too_big"
    for bad in ("sub/a.rego", "..\\a.rego", "a.txt", "../a.rego", ".rego", "a b.rego"):
        assert post({bad: MINI}).json()["error"]["code"] == "invalid_file_name", bad
    assert post({"mini_test.rego": MINI}).json()["error"]["code"] == "test_file_not_allowed"
    assert post({"a.rego": "package x\x00"}).status_code == 422
    for query in ("mini.decision", "data", "data.a b", "data.x;drop", "data..x"):
        assert post(ok, query=query).json()["error"]["code"] == "invalid_query", query
    many = [{"name": f"c{i}", "input": {}, "expect": {"effect": "DENY"}} for i in range(201)]
    assert post(ok, many).json()["error"]["code"] == "too_big"
    huge = [{"name": "h", "input": {"blob": "z" * (65 * 1024)}, "expect": {"effect": "DENY"}}]
    assert post(ok, huge).json()["error"]["code"] == "too_big"
    unknown_effect = [{"name": "u", "input": {}, "expect": {"effect": "MAYBE"}}]
    assert post(ok, unknown_effect).status_code == 422
    assert post(ok, [{"name": "n", "input": {}}]).status_code == 422  # a stored case needs expect
    # 200 cases and 20 files are the inclusive maxima.
    limit_cases = [{"name": f"c{i}", "input": {}, "expect": {"effect": "DENY"}} for i in range(200)]
    assert (
        post(
            {f"f{i}.rego": MINI.replace("acme.mini", f"acme.m{i}") for i in range(20)}, limit_cases
        ).status_code
        == 201
    )
    assert (
        http.post("/api/policies", json={"name": "lim", **pack()}, headers=org.auth).status_code
        == 409
    )


# ---------------------------------------------------------------------------------- timeouts


def _slow_engine(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    original = RegoEngine.decide_document

    async def slow(self: RegoEngine, document: Any) -> Any:
        if document.get("slow"):
            time.sleep(seconds)
        return await original(self, document)

    monkeypatch.setattr(RegoEngine, "decide_document", slow)


def test_a_hung_evaluation_fails_its_case_and_the_request_still_returns(
    http: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(http)
    monkeypatch.setattr(reg, "CASE_TIMEOUT_SECONDS", 0.3)
    _slow_engine(monkeypatch, 2.0)
    cases = [
        {"name": "hangs", "input": {**doc(), "slow": True}, "expect": {"effect": "ALLOW"}},
        {"name": "fine", "input": doc(), "expect": {"effect": "ALLOW"}},
    ]
    started = time.monotonic()
    out = http.post(
        "/api/policies/simulate",
        json={"files": {"mini.rego": MINI}, "query": QUERY, "cases": cases},
        headers=org.auth,
    ).json()
    assert time.monotonic() - started < 1.9
    hung, fine = out["results"]
    assert hung["passed"] is False
    assert "exceeded" in hung["error"]
    assert fine["passed"] is True  # the engine was rebuilt; later cases are unaffected
    stored = create(http, org, "slowpack", pack(cases=cases)).json()
    assert stored["tests_passed"] is False
    assert deploy(http, org, "slowpack", 1).status_code == 409
    time.sleep(2.0)  # let the abandoned worker finish so it cannot leak into other tests


def test_too_many_abandoned_evaluations_make_the_evaluator_refuse_work(
    http: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(http)
    monkeypatch.setattr(reg, "MAX_STUCK_WORKERS", 0)
    out = http.post(
        "/api/policies/simulate",
        json={"files": {"mini.rego": MINI}, "query": QUERY, "input": doc()},
        headers=org.auth,
    )
    assert out.status_code == 503
    assert out.json()["error"]["code"] == "evaluator_busy"


def test_the_request_time_budget_fails_the_remaining_cases(
    http: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(http)
    monkeypatch.setattr(reg, "REQUEST_BUDGET_SECONDS", -1.0)
    out = http.post(
        "/api/policies/simulate",
        json={"files": {"mini.rego": MINI}, "query": QUERY, "cases": CASES},
        headers=org.auth,
    ).json()
    assert all(r["passed"] is False and "budget" in r["error"] for r in out["results"])
