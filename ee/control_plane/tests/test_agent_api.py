"""The agent-facing API through the real open-source client: audit, approvals, traces, limits."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelgate.approvals import (
    ApprovalNotFoundError,
    ApprovalNotUsableError,
    ApprovalStatus,
    ApprovalTier,
    EvidenceBundle,
    action_hash,
)
from keelgate.audit import AuditLog, EventType, verify_chain
from keelgate.cloud import (
    CloudApprovalQueue,
    CloudAuditError,
    CloudAuditStore,
    CloudClient,
    CloudQuotaError,
)

from keelgate_cloud.db import Database
from tests.harness import Org, live_server, make_app

ARGS = {"symbol": "AAPL", "qty": 10}


@pytest.fixture(scope="module")
def server(db: Database):  # type: ignore[no-untyped-def]
    app = make_app(db)
    with live_server(app) as base:
        yield {"base": base, "app": app, "http": TestClient(app)}


def cloud(server: dict[str, Any], org: Org) -> CloudClient:
    return CloudClient(server["base"], org.api_key)


def evidence(tool: str = "place_order") -> EvidenceBundle:
    return EvidenceBundle(tool=tool, args=ARGS, rationale="rebalance")


def submit(queue: CloudApprovalQueue, org: Org, *, tier: ApprovalTier = ApprovalTier.ONE_CLICK):  # type: ignore[no-untyped-def]
    return queue.submit(
        tenant_id=org.id,
        agent_id="agent-1",
        tool_name="place_order",
        args_hash=action_hash("place_order", ARGS),
        tier=tier,
        evidence=evidence(),
    )


def usage(server: dict[str, Any], org: Org) -> dict[str, Any]:
    return server["http"].get("/api/usage", headers=org.auth).json()  # type: ignore[no-any-return]


# ------------------------------------------------------------------------------------ audit


def test_audit_written_through_the_client_verifies_with_the_open_source_verifier(
    server: dict[str, Any],
) -> None:
    org = Org(server["http"])
    log = AuditLog(CloudAuditStore(cloud(server, org)))
    for n in range(5):
        log.append(
            tenant_id=org.id, event_type=EventType.TOOL_RESULT, actor="agent-1", payload={"n": n}
        )
    records = list(log.records(org.id))
    assert [r.seq for r in records] == [1, 2, 3, 4, 5]
    head = log.head(org.id)
    assert head is not None
    assert verify_chain(records, tenant_id=org.id, expected_head=head)


def test_the_server_assigns_the_chain_so_the_client_cannot_forge_it(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    forged = server["http"].post(
        "/v1/audit/events",
        json={
            "tenant_id": org.id,
            "event_type": "tool.result",
            "actor": "a",
            "payload_json": "{}",
            "seq": 99,
            "hash": "f" * 64,
        },
        headers=org.key_auth,
    )
    assert forged.status_code == 422  # extra fields are refused, not ignored
    ok = (
        server["http"]
        .post(
            "/v1/audit/events",
            json={
                "tenant_id": org.id,
                "event_type": "tool.result",
                "actor": "a",
                "payload_json": "{}",
            },
            headers=org.key_auth,
        )
        .json()
    )
    assert ok["seq"] == 1


def test_agents_cannot_write_approval_events(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    for kind in ("approval.requested", "approval.decided", "approval.consumed", "made.up"):
        response = server["http"].post(
            "/v1/audit/events",
            json={"tenant_id": org.id, "event_type": kind, "actor": "a", "payload_json": "{}"},
            headers=org.key_auth,
        )
        assert response.status_code == 422, kind
        assert response.json()["error"]["code"] == "event_not_accepted"


def test_a_retried_append_with_the_same_idempotency_key_is_recorded_once(
    server: dict[str, Any],
) -> None:
    org = Org(server["http"])
    body = {"tenant_id": org.id, "event_type": "tool.call", "actor": "a", "payload_json": "{}"}
    headers = {**org.key_auth, "Idempotency-Key": "once"}
    first = server["http"].post("/v1/audit/events", json=body, headers=headers).json()
    again = server["http"].post("/v1/audit/events", json=body, headers=headers).json()
    assert first == again
    assert usage(server, org)["usage"]["gated_actions"] == 1
    assert server["http"].get("/v1/audit/head", headers=org.key_auth).json()["seq"] == 1


def test_a_malformed_payload_is_refused(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    response = server["http"].post(
        "/v1/audit/events",
        json={"tenant_id": org.id, "event_type": "tool.call", "actor": "a", "payload_json": "{no"},
        headers=org.key_auth,
    )
    assert response.status_code == 422


def test_records_page_through_the_http_api(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    log = AuditLog(CloudAuditStore(cloud(server, org)))
    for n in range(7):
        log.append(tenant_id=org.id, event_type="tool.result", actor="a", payload={"n": n})
    page = (
        server["http"]
        .get("/v1/audit/records", params={"limit": 3, "after_seq": 2}, headers=org.key_auth)
        .json()
    )
    assert [r["seq"] for r in page["records"]] == [3, 4, 5]
    assert page["has_more"] is True


# ----------------------------------------------------------------------------- cross-tenant


@pytest.fixture
def two(server: dict[str, Any]) -> tuple[Org, Org]:
    a, b = Org(server["http"], "A"), Org(server["http"], "B")
    AuditLog(CloudAuditStore(cloud(server, b))).append(
        tenant_id=b.id, event_type="tool.result", actor="b-agent", payload={"secret": "b-only"}
    )
    return a, b


def test_a_key_cannot_name_another_tenant_on_any_agent_endpoint(
    server: dict[str, Any], two: tuple[Org, Org]
) -> None:
    a, b = two
    http = server["http"]
    expected = "tenant_mismatch"
    calls = [
        http.post(
            "/v1/audit/events",
            json={"tenant_id": b.id, "event_type": "tool.call", "actor": "x", "payload_json": "{}"},
            headers=a.key_auth,
        ),
        http.get("/v1/audit/records", params={"tenant_id": b.id}, headers=a.key_auth),
        http.get("/v1/audit/head", params={"tenant_id": b.id}, headers=a.key_auth),
        http.get("/v1/approvals", params={"tenant_id": b.id}, headers=a.key_auth),
        http.post(
            "/v1/approvals",
            json={
                "tenant_id": b.id, "agent_id": "x", "tool_name": "t", "args_hash": "0" * 64,
                "tier": "ONE_CLICK", "evidence": {},
            },
            headers=a.key_auth,
        ),
    ]  # fmt: skip
    for response in calls:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == expected
    assert http.get("/v1/audit/head", headers=b.key_auth).json()["seq"] == 1  # untouched


def test_one_tenants_approvals_are_invisible_to_another(
    server: dict[str, Any], two: tuple[Org, Org]
) -> None:
    a, b = two
    queue_b = CloudApprovalQueue(cloud(server, b))
    request = submit(queue_b, b)
    queue_a = CloudApprovalQueue(cloud(server, a))
    with pytest.raises(ApprovalNotFoundError):
        queue_a.get(a.id, request.request_id)
    with pytest.raises(ApprovalNotFoundError):
        queue_a.consume(
            tenant_id=a.id, request_id=request.request_id, agent_id="agent-1",
            tool_name="place_order", args_hash=request.args_hash,
        )  # fmt: skip
    assert queue_a.list_pending(a.id) == []
    inbox_a = server["http"].get("/api/approvals", headers=a.auth).json()["items"]
    assert inbox_a == []
    # And the other tenant's user cannot decide it either: it does not exist for them.
    decide = server["http"].post(
        f"/api/approvals/{request.request_id}/approve", json={}, headers=a.auth
    )
    assert decide.status_code == 404


def test_a_dashboard_user_sees_only_their_own_orgs_audit_and_traces(
    server: dict[str, Any], two: tuple[Org, Org]
) -> None:
    a, _b = two
    http = server["http"]
    assert http.get("/api/traces", headers=a.auth).json()["items"] == []
    assert json.dumps(http.get("/v1/audit/records", headers=a.key_auth).json()).count("b-only") == 0


# --------------------------------------------------------------------------------- metering


def call_event(log: AuditLog, org: Org, **payload: Any) -> None:
    log.append(
        tenant_id=org.id, event_type=EventType.TOOL_CALL, actor="unauthenticated", payload=payload
    )


def test_gated_actions_are_counted_exactly(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    log = AuditLog(CloudAuditStore(cloud(server, org)))
    for n in range(7):
        call_event(log, org, n=n)
    for n in range(4):  # other events are not gated actions
        log.append(tenant_id=org.id, event_type=EventType.TOOL_RESULT, actor="a", payload={"n": n})
    assert usage(server, org)["usage"]["gated_actions"] == 7


def test_concurrent_writers_are_counted_exactly_and_the_chain_stays_valid(
    server: dict[str, Any],
) -> None:
    org = Org(server["http"])
    errors: list[Exception] = []

    def writer(worker: int) -> None:
        log = AuditLog(CloudAuditStore(cloud(server, org)))
        try:
            for n in range(6):
                call_event(log, org, worker=worker, n=n)
        except Exception as exc:  # pragma: no cover - failure is reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert usage(server, org)["usage"]["gated_actions"] == 48
    records = list(AuditLog(CloudAuditStore(cloud(server, org))).records(org.id))
    assert [r.seq for r in records] == list(range(1, 49))
    assert verify_chain(records, tenant_id=org.id)


def test_usage_is_per_org(server: dict[str, Any]) -> None:
    a, b = Org(server["http"]), Org(server["http"])
    call_event(AuditLog(CloudAuditStore(cloud(server, a))), a)
    assert usage(server, a)["usage"]["gated_actions"] == 1
    assert usage(server, b)["usage"]["gated_actions"] == 0


def test_traces_are_stored_and_counted_once_per_trace(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    span = {
        "trace_id": "a" * 32, "span_id": "b" * 16, "name": "keelgate.tool.call", "status": "OK",
        "start_ns": 1, "end_ns": 2, "attributes": {"keelgate.tenant_id": org.id},
    }  # fmt: skip
    child = {**span, "span_id": "c" * 16, "parent_span_id": "b" * 16, "name": "child"}
    other = {**span, "trace_id": "d" * 32, "name": "second trace", "status": "ERROR"}
    http = server["http"]
    first = http.post("/v1/traces", json={"spans": [span, child, other]}, headers=org.key_auth)
    assert first.json() == {"accepted": 3}
    replay = http.post("/v1/traces", json={"spans": [span, child]}, headers=org.key_auth)
    assert replay.json() == {"accepted": 0}
    assert usage(server, org)["usage"]["traces_stored"] == 2
    listed = http.get("/api/traces", headers=org.auth).json()["items"]
    assert {t["trace_id"] for t in listed} == {"a" * 32, "d" * 32}
    assert next(t for t in listed if t["trace_id"] == "a" * 32)["spans"] == 2
    assert (
        http.get("/api/traces", params={"errors_only": True}, headers=org.auth).json()["items"][0][
            "trace_id"
        ]
        == "d" * 32
    )
    detail = http.get(f"/api/traces/{'a' * 32}", headers=org.auth).json()
    assert [s["name"] for s in detail["spans"]] == ["keelgate.tool.call", "child"]
    assert http.get(f"/api/traces/{'e' * 32}", headers=org.auth).status_code == 404


def test_span_batches_are_validated(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    bad = {"trace_id": "xyz", "span_id": "b" * 16, "name": "n", "status": "OK"}
    assert (
        server["http"].post("/v1/traces", json={"spans": [bad]}, headers=org.key_auth).status_code
        == 422
    )


# ------------------------------------------------------------------------------ limits


def test_the_gated_action_quota_is_a_hard_stop_and_refused_calls_are_not_counted(
    db: Database,
) -> None:
    app = make_app(db, plan={"included_gated_actions": 3})
    http = TestClient(app)
    with live_server(app) as base:
        org = Org(http)
        log = AuditLog(CloudAuditStore(CloudClient(base, org.api_key)))
        for n in range(3):
            call_event(log, org, n=n)
        with pytest.raises(CloudAuditError) as raised:
            call_event(log, org, n=3)
        assert raised.value.code == "quota_exceeded"
        # Other events (a call already in flight finishing) are still recorded.
        log.append(tenant_id=org.id, event_type=EventType.TOOL_RESULT, actor="a", payload={})
        summary = http.get("/api/usage", headers=org.auth).json()
        assert summary["usage"]["gated_actions"] == 3
        assert summary["gated_actions_remaining"] == 0
        head = log.head(org.id)
        assert head is not None
        assert head.seq == 4  # three calls + one result; the refused call left no record
        assert verify_chain(log.records(org.id), tenant_id=org.id)
        client = CloudClient(base, org.api_key)
        with pytest.raises(CloudQuotaError):
            client.request(
                "POST",
                "/v1/audit/events",
                json={
                    "tenant_id": org.id,
                    "event_type": "tool.call",
                    "actor": "a",
                    "payload_json": "{}",
                },
            )


def test_a_plan_without_a_cap_never_blocks(db: Database) -> None:
    app = make_app(db, plan={"included_gated_actions": None})
    http = TestClient(app)
    org = Org(http)
    body = {"tenant_id": org.id, "event_type": "tool.call", "actor": "a", "payload_json": "{}"}
    for _ in range(5):
        assert http.post("/v1/audit/events", json=body, headers=org.key_auth).status_code == 200


def test_the_agent_cap_refuses_a_new_agent_but_not_a_known_one(db: Database) -> None:
    http = TestClient(make_app(db, plan={"max_agents": 2}))
    org = Org(http)

    def decision(agent: str) -> int:
        return http.post(
            "/v1/audit/events",
            json={
                "tenant_id": org.id,
                "event_type": "policy.decision",
                "actor": agent,
                "payload_json": "{}",
            },
            headers=org.key_auth,
        ).status_code

    assert decision("a1") == 200
    assert decision("a2") == 200
    assert decision("a3") == 429
    assert decision("a1") == 200  # already known
    assert usage_agents(http, org) == 2


def usage_agents(http: TestClient, org: Org) -> int:
    return int(http.get("/api/usage", headers=org.auth).json()["agents"])


def test_the_rate_limit_is_per_key(db: Database) -> None:
    http = TestClient(make_app(db, plan={"rate_limit_per_minute": 5}))
    org = Org(http)
    statuses = [http.get("/v1/audit/head", headers=org.key_auth).status_code for _ in range(9)]
    assert statuses.count(200) == 5
    assert statuses[-1] == 429
    other = Org(http)  # another tenant has its own allowance
    assert http.get("/v1/audit/head", headers=other.key_auth).status_code == 200


# ---------------------------------------------------------------------------------- approvals


def test_the_approval_lifecycle_across_the_agent_and_the_dashboard(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    http = server["http"]
    queue = CloudApprovalQueue(cloud(server, org))
    request = submit(queue, org)
    assert request.status is ApprovalStatus.PENDING

    pending = http.get("/api/approvals", headers=org.auth).json()["items"]
    assert [p["request_id"] for p in pending] == [request.request_id]
    assert pending[0]["evidence"]["rationale"] == "rebalance"

    # not yet usable
    with pytest.raises(ApprovalNotUsableError):
        queue.consume(
            tenant_id=org.id, request_id=request.request_id, agent_id="agent-1",
            tool_name="place_order", args_hash=request.args_hash,
        )  # fmt: skip

    decided = http.post(
        f"/api/approvals/{request.request_id}/approve", json={"note": "ok"}, headers=org.auth
    )
    assert decided.status_code == 200, decided.text
    assert queue.get(org.id, request.request_id).status is ApprovalStatus.APPROVED

    spent = queue.consume(
        tenant_id=org.id, request_id=request.request_id, agent_id="agent-1",
        tool_name="place_order", args_hash=request.args_hash,
    )  # fmt: skip
    assert spent.status is ApprovalStatus.CONSUMED
    with pytest.raises(ApprovalNotUsableError):  # single use
        queue.consume(
            tenant_id=org.id, request_id=request.request_id, agent_id="agent-1",
            tool_name="place_order", args_hash=request.args_hash,
        )  # fmt: skip

    chain = [r.event_type for r in AuditLog(CloudAuditStore(cloud(server, org))).records(org.id)]
    assert chain == ["approval.requested", "approval.decided", "approval.consumed"]
    assert usage(server, org)["usage"]["approvals"] == 1


def test_an_approval_is_bound_to_the_exact_action(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    queue = CloudApprovalQueue(cloud(server, org))
    request = submit(queue, org)
    server["http"].post(f"/api/approvals/{request.request_id}/approve", json={}, headers=org.auth)
    with pytest.raises(ApprovalNotUsableError):
        queue.consume(
            tenant_id=org.id, request_id=request.request_id, agent_id="agent-1",
            tool_name="place_order", args_hash=action_hash("place_order", {**ARGS, "qty": 10_000}),
        )  # fmt: skip
    with pytest.raises(ApprovalNotUsableError):
        queue.consume(
            tenant_id=org.id, request_id=request.request_id, agent_id="agent-2",
            tool_name="place_order", args_hash=request.args_hash,
        )  # fmt: skip


def test_a_rejected_request_cannot_be_used(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    queue = CloudApprovalQueue(cloud(server, org))
    request = submit(queue, org)
    rejected = server["http"].post(
        f"/api/approvals/{request.request_id}/reject", json={"note": "no"}, headers=org.auth
    )
    assert rejected.json()["status"] == "REJECTED"
    with pytest.raises(ApprovalNotUsableError):
        queue.consume(
            tenant_id=org.id, request_id=request.request_id, agent_id="agent-1",
            tool_name="place_order", args_hash=request.args_hash,
        )  # fmt: skip
    again = server["http"].post(
        f"/api/approvals/{request.request_id}/approve", json={}, headers=org.auth
    )
    assert again.status_code == 409


def test_only_approvers_can_decide_and_clearance_is_enforced(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    http = server["http"]
    queue = CloudApprovalQueue(cloud(server, org))
    viewer = org.invite("viewer")
    junior = org.invite("approver")  # cleared for ONE_CLICK only
    senior = org.invite("approver", tier="EXPLICIT_SIGNOFF")

    one_click = submit(queue, org)
    assert (
        http.post(
            f"/api/approvals/{one_click.request_id}/approve", json={}, headers=viewer
        ).status_code
        == 403
    )
    assert (
        http.post(
            f"/api/approvals/{one_click.request_id}/approve", json={}, headers=junior
        ).status_code
        == 200
    )

    strict = submit(queue, org, tier=ApprovalTier.EXPLICIT_SIGNOFF)
    assert (
        http.post(
            f"/api/approvals/{strict.request_id}/approve", json={}, headers=junior
        ).status_code
        == 403
    )
    no_code = http.post(f"/api/approvals/{strict.request_id}/approve", json={}, headers=senior)
    assert no_code.status_code == 400
    assert no_code.json()["error"]["code"] == "approval_signoff_invalid"
    shown = http.get(f"/api/approvals/{strict.request_id}", headers=senior).json()
    assert shown["signoff_code_required"] is True
    code = strict.signoff_code
    assert (
        http.post(
            f"/api/approvals/{strict.request_id}/approve",
            json={"signoff_code": code},
            headers=senior,
        ).status_code
        == 200
    )


def test_routing_rules_restrict_who_decides_and_can_raise_the_tier(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    http = server["http"]
    queue = CloudApprovalQueue(cloud(server, org))
    senior = org.invite("approver", tier="EXPLICIT_SIGNOFF")
    owner_id = http.get("/api/me", headers=org.auth).json()["user"]["id"]
    assert (
        http.patch(
            f"/api/users/{owner_id}", json={"approval_tier": "EXPLICIT_SIGNOFF"}, headers=org.auth
        ).status_code
        == 204
    )
    http.post(
        "/api/approval-routes",
        json={"priority": 1, "tool_pattern": "place_*", "min_tier": "ONE_CLICK",
              "allowed_roles": ["owner"], "escalate_to": "EXPLICIT_SIGNOFF"},
        headers=org.auth,
    )  # fmt: skip
    request = submit(queue, org)  # filed as ONE_CLICK, raised by the rule
    assert request.tier is ApprovalTier.EXPLICIT_SIGNOFF
    denied = http.post(f"/api/approvals/{request.request_id}/approve", json={}, headers=senior)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "routing_forbidden"
    owner = http.post(
        f"/api/approvals/{request.request_id}/approve",
        json={"signoff_code": request.signoff_code},
        headers=org.auth,
    )
    assert owner.status_code == 200, owner.text


def test_only_an_owner_can_change_clearance(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    admin = org.invite("admin")
    me = server["http"].get("/api/me", headers=admin).json()["user"]["id"]
    denied = server["http"].patch(
        f"/api/users/{me}", json={"approval_tier": "EXPLICIT_SIGNOFF"}, headers=admin
    )
    assert denied.status_code == 403


def test_routing_rules_are_validated_and_scoped_to_the_org(server: dict[str, Any]) -> None:
    a, b = Org(server["http"]), Org(server["http"])
    http = server["http"]
    bad = http.post("/api/approval-routes", json={"allowed_roles": ["wizard"]}, headers=a.auth)
    assert bad.status_code == 422
    created = http.post("/api/approval-routes", json={"tool_pattern": "x*"}, headers=a.auth).json()
    assert http.get("/api/approval-routes", headers=b.auth).json()["items"] == []
    assert http.delete(f"/api/approval-routes/{created['id']}", headers=b.auth).status_code == 404
    assert http.delete(f"/api/approval-routes/{created['id']}", headers=a.auth).status_code == 204


def test_submit_with_a_retry_key_files_one_request(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    body = {
        "tenant_id": org.id, "agent_id": "agent-1", "tool_name": "place_order",
        "args_hash": action_hash("place_order", ARGS), "tier": "ONE_CLICK",
        "evidence": evidence().model_dump(mode="json"),
    }  # fmt: skip
    headers = {**org.key_auth, "Idempotency-Key": "k-1"}
    first = server["http"].post("/v1/approvals", json=body, headers=headers).json()
    second = server["http"].post("/v1/approvals", json=body, headers=headers).json()
    assert first["request_id"] == second["request_id"]
    assert len(server["http"].get("/v1/approvals", headers=org.key_auth).json()["items"]) == 1
    assert usage(server, org)["usage"]["approvals"] == 1


def test_an_auto_tier_and_a_bad_evidence_bundle_are_refused(server: dict[str, Any]) -> None:
    org = Org(server["http"])
    base = {
        "tenant_id": org.id, "agent_id": "a", "tool_name": "t", "args_hash": "0" * 64,
        "tier": "AUTO", "evidence": {},
    }  # fmt: skip
    assert server["http"].post("/v1/approvals", json=base, headers=org.key_auth).status_code == 422
    base["tier"] = "ONE_CLICK"
    assert server["http"].post("/v1/approvals", json=base, headers=org.key_auth).status_code == 422


def test_expired_requests_leave_the_inbox(server: dict[str, Any], db: Database) -> None:
    org = Org(server["http"])
    queue = CloudApprovalQueue(cloud(server, org))
    request = queue.submit(
        tenant_id=org.id, agent_id="a", tool_name="place_order",
        args_hash=action_hash("place_order", ARGS), tier=ApprovalTier.ONE_CLICK,
        evidence=evidence(), ttl=timedelta(seconds=1),
    )  # fmt: skip
    with db.tx(org.id) as conn:
        conn.execute(
            "UPDATE approvals SET body = jsonb_set(body, '{expires_at}', "
            "to_jsonb(to_char(now() at time zone 'utc' - interval '1 minute', "
            '\'YYYY-MM-DD"T"HH24:MI:SS"Z"\')))'
        )
    assert server["http"].get("/api/approvals", headers=org.auth).json()["items"] == []
    gone = server["http"].post(
        f"/api/approvals/{request.request_id}/approve", json={}, headers=org.auth
    )
    assert gone.status_code in (400, 409)
