"""The ``keelgate.cloud`` client against a scripted control plane (httpx MockTransport)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from keelgate.approvals import (
    ApprovalNotFoundError,
    ApprovalNotUsableError,
    ApprovalTier,
    EvidenceBundle,
    action_hash,
)
from keelgate.audit import AuditLog, EventType, verify_chain
from keelgate.cloud import (
    CloudApprovalError,
    CloudApprovalQueue,
    CloudAuditError,
    CloudAuditStore,
    CloudAuthError,
    CloudClient,
    CloudError,
    CloudPolicyEngine,
    CloudQuotaError,
    CloudSpanExporter,
    PolicyPackError,
)
from keelgate.policy import Decision, PolicyInput
from keelgate.policy._engine import hash_sources
from tests.conftest import run

if TYPE_CHECKING:
    from collections.abc import Callable

ALLOW_PACK = {
    "p.rego": (
        "package demo\n\nimport rego.v1\n\n"
        'decision := {"effect": "ALLOW", "reasons": [], "approval_tier": null}\n'
    )
}


def client(handler: Callable[[httpx.Request], httpx.Response]) -> CloudClient:
    return CloudClient("https://cp.test", "kg_test_key", transport=httpx.MockTransport(handler))


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": f"{code} happened"}})


# ----------------------------------------------------------------------------- the client


def test_it_sends_the_api_key_and_never_leaks_it_in_errors() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return error(401, "invalid_api_key")

    with pytest.raises(CloudAuthError) as raised:
        client(handler).request("GET", "/v1/audit/head")
    assert seen[0].headers["authorization"] == "Bearer kg_test_key"
    assert "kg_test_key" not in str(raised.value)
    assert raised.value.code == "invalid_api_key"
    assert raised.value.status == 401


def test_quota_and_rate_limits_are_a_distinct_error() -> None:
    with pytest.raises(CloudQuotaError) as raised:
        client(lambda r: error(429, "quota_exceeded")).request("GET", "/v1/audit/head")
    assert raised.value.code == "quota_exceeded"


def test_an_empty_api_key_is_refused() -> None:
    with pytest.raises(ValueError):
        CloudClient("https://cp.test", "")


def test_reads_are_retried_on_a_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keelgate.cloud._client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    assert client(handler).request("GET", "/v1/audit/head") == {"ok": True}
    assert calls["n"] == 3


def test_a_write_without_an_idempotency_key_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("keelgate.cloud._client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(CloudError):
        client(handler).request("POST", "/v1/traces", json={})
    assert calls["n"] == 1


def test_a_write_with_an_idempotency_key_reuses_the_same_key_when_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("keelgate.cloud._client.time.sleep", lambda _s: None)
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(503) if len(keys) < 2 else httpx.Response(200, json={})

    client(handler).request("POST", "/v1/x", json={}, idempotency_key="k1")
    assert keys == ["k1", "k1"]


def test_an_unreachable_server_is_a_cloud_error_not_a_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("keelgate.cloud._client.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(CloudError) as raised:
        client(handler).request("GET", "/v1/audit/head")
    assert raised.value.code == "unreachable"


def test_a_malformed_success_body_is_a_cloud_error() -> None:
    with pytest.raises(CloudError) as raised:
        client(lambda r: httpx.Response(200, content=b"<html>")).request("GET", "/x")
    assert raised.value.code == "bad_response"


# ------------------------------------------------------------------------------ audit store


class FakeServerChain:
    """Chains events the way the control plane does, using only the public audit API."""

    def __init__(self) -> None:
        self.log = AuditLog()
        self.posts: list[dict[str, object]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/audit/events":
            body = json.loads(request.content)
            self.posts.append(body)
            record = self.log.append(
                tenant_id=body["tenant_id"],
                event_type=body["event_type"],
                actor=body["actor"],
                payload=json.loads(body["payload_json"]),
            )
            return httpx.Response(200, json=record.model_dump())
        if request.url.path == "/v1/audit/records":
            tenant = request.url.params["tenant_id"]
            after = int(request.url.params["after_seq"])
            limit = int(request.url.params["limit"])
            rows = list(self.log.records(tenant, after_seq=after))
            page = rows[:limit]
            return httpx.Response(
                200,
                json={"records": [r.model_dump() for r in page], "has_more": len(rows) > limit},
            )
        if request.url.path == "/v1/audit/head":
            head = self.log.head(request.url.params["tenant_id"])
            return httpx.Response(
                200,
                content=b"null"
                if head is None
                else json.dumps({"seq": head.seq, "hash": head.hash}).encode(),
            )
        return error(404, "not_found")


def test_audit_events_are_chained_by_the_server_and_verify_with_the_open_source_verifier() -> None:
    server = FakeServerChain()
    store = CloudAuditStore(client(server.handle))
    log = AuditLog(store)
    for n in range(3):
        record = log.append(
            tenant_id="t1", event_type=EventType.TOOL_CALL, actor="agent", payload={"n": n}
        )
        assert record.seq == n + 1
    assert verify_chain(log.records("t1"), tenant_id="t1")
    head = log.head("t1")
    assert head is not None
    assert head.seq == 3
    assert verify_chain(log.records("t1"), tenant_id="t1", expected_head=head)


def test_the_client_sends_only_the_event_fields_never_chain_values() -> None:
    server = FakeServerChain()
    AuditLog(CloudAuditStore(client(server.handle))).append(
        tenant_id="t1", event_type="x.y", actor="a", payload={"k": 1}
    )
    assert set(server.posts[0]) == {"tenant_id", "event_type", "actor", "payload_json"}


def test_records_are_paged() -> None:
    server = FakeServerChain()
    store = CloudAuditStore(client(server.handle))
    log = AuditLog(store)
    for n in range(7):
        log.append(tenant_id="t1", event_type="e", actor="a", payload={"n": n})
    monkey_page = 3

    def paged(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audit/records":
            params = dict(request.url.params)
            params["limit"] = str(monkey_page)
            request = httpx.Request("GET", request.url.copy_with(params=params))
        return server.handle(request)

    seqs = [r.seq for r in CloudAuditStore(client(paged)).iter_records("t1")]
    assert seqs == list(range(1, 8))


def test_an_empty_chain_has_no_head() -> None:
    assert CloudAuditStore(client(FakeServerChain().handle)).head("t1") is None


def test_a_failed_audit_write_raises_an_audit_error_so_the_gateway_refuses_the_action() -> None:
    log = AuditLog(CloudAuditStore(client(lambda r: error(429, "quota_exceeded"))))
    with pytest.raises(CloudAuditError) as raised:
        log.append(tenant_id="t1", event_type="e", actor="a", payload={})
    assert raised.value.code == "quota_exceeded"


def test_a_malformed_audit_record_is_refused() -> None:
    log = AuditLog(CloudAuditStore(client(lambda r: httpx.Response(200, json={"seq": 1}))))
    with pytest.raises(CloudAuditError):
        log.append(tenant_id="t1", event_type="e", actor="a", payload={})


# --------------------------------------------------------------------------------- approvals


def _evidence() -> EvidenceBundle:
    return EvidenceBundle(tool="place_order", args={"qty": 1}, rationale="r")


def _request_json(**over: object) -> dict[str, object]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    evidence = _evidence()
    base: dict[str, object] = {
        "request_id": "r1",
        "tenant_id": "t1",
        "agent_id": "agent",
        "tool_name": "place_order",
        "args_hash": action_hash("place_order", {"qty": 1}),
        "tier": "ONE_CLICK",
        "evidence": evidence.model_dump(mode="json"),
        "evidence_hash": evidence.digest(),
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
    }
    base.update(over)
    return base


def test_submit_posts_the_action_with_an_idempotency_key_and_parses_the_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_request_json())

    queue = CloudApprovalQueue(client(handler))
    got = queue.submit(
        tenant_id="t1",
        agent_id="agent",
        tool_name="place_order",
        args_hash=action_hash("place_order", {"qty": 1}),
        tier=ApprovalTier.ONE_CLICK,
        evidence=_evidence(),
        ttl=timedelta(minutes=5),
    )
    assert got.request_id == "r1"
    assert seen[0].headers["idempotency-key"]
    assert json.loads(seen[0].content)["ttl_seconds"] == 300


def test_auto_tier_needs_no_request() -> None:
    queue = CloudApprovalQueue(client(lambda r: httpx.Response(500)))
    with pytest.raises(ValueError):
        queue.submit(
            tenant_id="t1",
            agent_id="a",
            tool_name="t",
            args_hash="h",
            tier=ApprovalTier.AUTO,
            evidence=_evidence(),
        )


def test_server_error_codes_become_the_matching_local_exceptions() -> None:
    queue = CloudApprovalQueue(client(lambda r: error(404, "approval_not_found")))
    with pytest.raises(ApprovalNotFoundError):
        queue.get("t1", "nope")
    queue = CloudApprovalQueue(client(lambda r: error(409, "approval_not_usable")))
    with pytest.raises(ApprovalNotUsableError):
        queue.consume(tenant_id="t1", request_id="r1", agent_id="a", tool_name="t", args_hash="h")


def test_an_unknown_server_error_is_a_cloud_approval_error() -> None:
    queue = CloudApprovalQueue(client(lambda r: error(500, "boom")))
    with pytest.raises(CloudApprovalError):
        queue.list_pending("t1")


def test_pending_requests_are_listed() -> None:
    queue = CloudApprovalQueue(
        client(lambda r: httpx.Response(200, json={"items": [_request_json()]}))
    )
    assert [r.request_id for r in queue.list_pending("t1")] == ["r1"]


# ------------------------------------------------------------------------------------ policy


def bundle(files: dict[str, str] | None = None, **over: object) -> dict[str, object]:
    files = files or ALLOW_PACK
    base: dict[str, object] = {
        "files": files,
        "query": "data.demo.decision",
        "source_hash": hash_sources(files),
    }
    base.update(over)
    return base


def policy_input() -> PolicyInput:
    return PolicyInput.model_validate(
        {
            "action": {
                "tool": "t",
                "side_effect": "READ",
                "capability": "market_data:read",
                "args": {},
            },
            "actor": {"agent_id": "a", "tenant_id": "t1", "grant_id": "g"},
            "context": {"as_of": "2026-01-01T00:00:00+00:00", "execution_mode": "paper"},
        }
    )


def test_a_registry_pack_is_loaded_and_decides_locally() -> None:
    engine = CloudPolicyEngine.from_registry(
        client(lambda r: httpx.Response(200, json=bundle())), "demo"
    )
    decision = run(engine.decide(policy_input()))
    assert decision.effect is Decision.ALLOW
    assert engine.policy_version == hash_sources(ALLOW_PACK)


def test_before_a_pack_is_loaded_every_decision_is_a_deny() -> None:
    engine = CloudPolicyEngine(client(lambda r: httpx.Response(500)), "demo")
    assert run(engine.decide(policy_input())).effect is Decision.DENY


def test_a_bundle_that_does_not_match_its_hash_is_refused() -> None:
    with pytest.raises(PolicyPackError):
        CloudPolicyEngine.from_registry(
            client(lambda r: httpx.Response(200, json=bundle(source_hash="sha256:deadbeef"))),
            "demo",
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"files": {}, "query": "q", "source_hash": "x"},
        {"files": {"p.rego": "x"}, "query": 3, "source_hash": "x"},
        {"query": "q", "source_hash": "x"},
    ],
)
def test_a_malformed_bundle_is_refused(bad: dict[str, object]) -> None:
    with pytest.raises(PolicyPackError):
        CloudPolicyEngine.from_registry(client(lambda r: httpx.Response(200, json=bad)), "demo")


@pytest.mark.parametrize("name", ["../evil.rego", "sub/p.rego", "notrego.txt"])
def test_a_bundle_cannot_write_outside_its_own_directory(name: str) -> None:
    files = {name: ALLOW_PACK["p.rego"]}
    with pytest.raises(PolicyPackError):
        CloudPolicyEngine.from_registry(
            client(lambda r: httpx.Response(200, json=bundle(files))), "demo"
        )


def test_refresh_switches_to_the_newly_deployed_version() -> None:
    deny_pack = {
        "p.rego": (
            "package demo\n\nimport rego.v1\n\n"
            'decision := {"effect": "DENY", "reasons": ["no"], "approval_tier": null}\n'
        )
    }
    state = {"files": ALLOW_PACK}
    engine = CloudPolicyEngine.from_registry(
        client(lambda r: httpx.Response(200, json=bundle(state["files"]))), "demo"
    )
    assert run(engine.decide(policy_input())).effect is Decision.ALLOW
    state["files"] = deny_pack
    engine.refresh()
    assert run(engine.decide(policy_input())).effect is Decision.DENY


# -------------------------------------------------------------------------------------- traces


def test_the_exporter_posts_spans_and_swallows_failures() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

    posted: list[dict[str, object]] = []

    def ok(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1})

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(CloudSpanExporter(client(ok))))
    with provider.get_tracer("t").start_as_current_span("keelgate.test") as span:
        span.set_attribute("keelgate.tenant_id", "t1")
        span.set_attribute("big", "x" * 5000)
    spans = posted[0]["spans"]
    assert isinstance(spans, list)
    assert spans[0]["name"] == "keelgate.test"
    assert spans[0]["attributes"]["keelgate.tenant_id"] == "t1"
    assert len(spans[0]["attributes"]["big"]) == 2000
    assert len(spans[0]["trace_id"]) == 32

    failing = CloudSpanExporter(client(lambda r: error(500, "boom")))
    assert failing.export([]) is SpanExportResult.FAILURE
    assert failing.force_flush() is True
