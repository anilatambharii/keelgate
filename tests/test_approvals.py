"""Approval queue, CLI and REST: tiers, tenant scope, expiry, single use."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from keelgate.approvals import (
    ApprovalExpiredError,
    ApprovalNotAuthorisedError,
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    ApprovalNotUsableError,
    ApprovalQueue,
    ApprovalRequest,
    ApprovalSignoffError,
    ApprovalStatus,
    ApprovalTier,
    Approver,
    EvidenceBundle,
    EvidenceSource,
    action_hash,
    sanitize_for_display,
)
from keelgate.approvals.cli import main as cli_main
from keelgate.audit import AuditLog, EventType
from tests.conftest import Clock

TOOL = "trade_paper_execute"
ARGS = {"symbol": "AAPL", "notional": 30000}
ARGS_HASH = action_hash(TOOL, ARGS)


def evidence(**overrides: Any) -> EvidenceBundle:
    values: dict[str, Any] = {
        "tool": TOOL,
        "args": ARGS,
        "rationale": "momentum signal",
        "policy_reasons": ("notional exceeds the auto-approval threshold",),
        "policy_version": "sha256:abc",
    }
    values.update(overrides)
    return EvidenceBundle(**values)


def human(
    approver_id: str = "alice",
    tenant: str = "t1",
    tier: ApprovalTier = ApprovalTier.EXPLICIT_SIGNOFF,
) -> Approver:
    return Approver(approver_id=approver_id, tenant_id=tenant, max_tier=tier)


def submit(
    q: ApprovalQueue,
    *,
    tier: ApprovalTier = ApprovalTier.ONE_CLICK,
    tenant: str = "t1",
    agent: str = "agent-1",
    **kwargs: Any,
) -> ApprovalRequest:
    return q.submit(
        tenant_id=tenant,
        agent_id=agent,
        tool_name=TOOL,
        args_hash=ARGS_HASH,
        tier=tier,
        evidence=evidence(),
        **kwargs,
    )


def consume(q: ApprovalQueue, request: ApprovalRequest, **overrides: Any) -> ApprovalRequest:
    values: dict[str, Any] = {
        "tenant_id": request.tenant_id,
        "request_id": request.request_id,
        "agent_id": request.agent_id,
        "tool_name": TOOL,
        "args_hash": ARGS_HASH,
    }
    values.update(overrides)
    return q.consume(**values)


# ------------------------------------------------------------------- tiers


def test_tiers_are_ordered() -> None:
    assert ApprovalTier.EXPLICIT_SIGNOFF.covers(ApprovalTier.ONE_CLICK)
    assert ApprovalTier.EXPLICIT_SIGNOFF.covers(ApprovalTier.EXPLICIT_SIGNOFF)
    assert ApprovalTier.ONE_CLICK.covers(ApprovalTier.ONE_CLICK)
    assert not ApprovalTier.ONE_CLICK.covers(ApprovalTier.EXPLICIT_SIGNOFF)
    assert not ApprovalTier.AUTO.covers(ApprovalTier.ONE_CLICK)


def test_auto_never_creates_a_request() -> None:
    with pytest.raises(ValueError, match="AUTO"):
        submit(ApprovalQueue(), tier=ApprovalTier.AUTO)


# ------------------------------------------------------------- happy path


def test_one_click_flow_from_request_to_single_use() -> None:
    q = ApprovalQueue()
    request = submit(q)
    assert request.status is ApprovalStatus.PENDING
    assert [r.request_id for r in q.list_pending("t1")] == [request.request_id]

    approved = q.approve("t1", request.request_id, human(tier=ApprovalTier.ONE_CLICK), note="ok")
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decided_by == "alice"
    assert approved.decision_note == "ok"
    assert q.list_pending("t1") == []

    assert consume(q, request).status is ApprovalStatus.CONSUMED


def test_explicit_signoff_needs_the_evidence_code() -> None:
    q = ApprovalQueue()
    request = submit(q, tier=ApprovalTier.EXPLICIT_SIGNOFF)
    for bad in (None, "", "nope", "0" * 8):
        with pytest.raises(ApprovalSignoffError):
            q.approve("t1", request.request_id, human(), signoff_code=bad)
    assert q.get("t1", request.request_id).status is ApprovalStatus.PENDING
    done = q.approve("t1", request.request_id, human(), signoff_code=request.signoff_code.upper())
    assert done.status is ApprovalStatus.APPROVED


def test_the_signoff_code_is_derived_from_the_evidence() -> None:
    q = ApprovalQueue()
    a = submit(q, tier=ApprovalTier.EXPLICIT_SIGNOFF)
    other = q.submit(
        tenant_id="t1",
        agent_id="agent-1",
        tool_name=TOOL,
        args_hash=ARGS_HASH,
        tier=ApprovalTier.EXPLICIT_SIGNOFF,
        evidence=evidence(rationale="different"),
    )
    assert a.signoff_code != other.signoff_code
    assert len(a.signoff_code) == 8


def test_reject_is_final() -> None:
    q = ApprovalQueue()
    request = submit(q)
    done = q.reject("t1", request.request_id, human(), note="too risky")
    assert done.status is ApprovalStatus.REJECTED
    with pytest.raises(ApprovalNotPendingError):
        q.approve("t1", request.request_id, human())
    with pytest.raises(ApprovalNotUsableError):
        consume(q, request)


def test_a_decision_is_final() -> None:
    q = ApprovalQueue()
    request = submit(q)
    q.approve("t1", request.request_id, human())
    with pytest.raises(ApprovalNotPendingError):
        q.reject("t1", request.request_id, human())
    with pytest.raises(ApprovalNotPendingError):
        q.approve("t1", request.request_id, human("bob"))


# ------------------------------------------------------------ authorisation


def test_a_requester_cannot_approve_its_own_request() -> None:
    q = ApprovalQueue()
    request = submit(q, agent="agent-1")
    with pytest.raises(ApprovalNotAuthorisedError, match="own request"):
        q.approve("t1", request.request_id, human("agent-1", tier=ApprovalTier.EXPLICIT_SIGNOFF))
    assert q.get("t1", request.request_id).status is ApprovalStatus.PENDING


def test_an_approver_below_the_tier_is_refused() -> None:
    q = ApprovalQueue()
    request = submit(q, tier=ApprovalTier.EXPLICIT_SIGNOFF)
    with pytest.raises(ApprovalNotAuthorisedError, match="not cleared"):
        q.approve(
            "t1",
            request.request_id,
            human(tier=ApprovalTier.ONE_CLICK),
            signoff_code=request.signoff_code,
        )


def test_an_approver_from_another_tenant_learns_nothing() -> None:
    q = ApprovalQueue()
    request = submit(q, tenant="t1")
    outsider = human("mallory", tenant="t2")
    with pytest.raises(ApprovalNotFoundError):
        q.approve("t1", request.request_id, outsider)  # approver tenant != request tenant
    with pytest.raises(ApprovalNotFoundError):
        q.approve(
            "t2", request.request_id, outsider
        )  # right tenant for them, wrong for the request
    with pytest.raises(ApprovalNotFoundError):
        q.reject("t2", request.request_id, outsider)
    assert q.get("t1", request.request_id).status is ApprovalStatus.PENDING


def test_requests_are_invisible_across_tenants() -> None:
    q = ApprovalQueue()
    request = submit(q, tenant="t1")
    assert q.list_pending("t2") == []
    with pytest.raises(ApprovalNotFoundError):
        q.get("t2", request.request_id)
    with pytest.raises(ApprovalNotFoundError):
        consume(q, request, tenant_id="t2")


def test_an_unknown_request_is_not_found() -> None:
    with pytest.raises(ApprovalNotFoundError):
        ApprovalQueue().get("t1", "does-not-exist")


# --------------------------------------------------------------- single use


def test_an_approval_can_be_spent_exactly_once() -> None:
    q = ApprovalQueue()
    request = submit(q)
    q.approve("t1", request.request_id, human())
    consume(q, request)
    with pytest.raises(ApprovalNotUsableError, match="CONSUMED"):
        consume(q, request)


def test_an_unapproved_request_cannot_be_spent() -> None:
    q = ApprovalQueue()
    request = submit(q)
    with pytest.raises(ApprovalNotUsableError, match="PENDING"):
        consume(q, request)


@pytest.mark.parametrize(
    "override",
    [
        {"args_hash": action_hash(TOOL, {"symbol": "AAPL", "notional": 30001})},
        {"args_hash": action_hash(TOOL, {"symbol": "MSFT", "notional": 30000})},
        {"tool_name": "report_write"},
        {"agent_id": "agent-2"},
    ],
)
def test_an_approval_is_bound_to_the_exact_action(override: dict[str, Any]) -> None:
    q = ApprovalQueue()
    request = submit(q)
    q.approve("t1", request.request_id, human())
    with pytest.raises(ApprovalNotUsableError, match="does not match"):
        consume(q, request, **override)
    assert consume(q, request).status is ApprovalStatus.CONSUMED  # still usable for the real one


def test_action_hash_distinguishes_tool_and_arguments() -> None:
    assert action_hash("a", {"x": 1}) != action_hash("b", {"x": 1})
    assert action_hash("a", {"x": 1}) != action_hash("a", {"x": 2})
    assert action_hash("a", {"x": 1, "y": 2}) == action_hash("a", {"y": 2, "x": 1})


# ------------------------------------------------------------------- expiry


def test_a_pending_request_expires() -> None:
    clock = Clock()
    q = ApprovalQueue(clock=clock, default_ttl=timedelta(minutes=10))
    request = submit(q)
    clock.advance(timedelta(minutes=10))
    with pytest.raises(ApprovalExpiredError):
        q.approve("t1", request.request_id, human())
    assert q.get("t1", request.request_id).status is ApprovalStatus.EXPIRED
    assert q.list_pending("t1") == []


def test_expiry_boundary_is_exact() -> None:
    clock = Clock()
    q = ApprovalQueue(clock=clock, default_ttl=timedelta(minutes=10))
    request = submit(q)
    clock.advance(timedelta(minutes=10) - timedelta(microseconds=1))
    assert q.approve("t1", request.request_id, human()).status is ApprovalStatus.APPROVED


def test_an_approval_that_is_not_used_in_time_expires() -> None:
    clock = Clock()
    q = ApprovalQueue(clock=clock, default_ttl=timedelta(minutes=10))
    request = submit(q)
    q.approve("t1", request.request_id, human())
    clock.advance(timedelta(minutes=11))
    with pytest.raises(ApprovalNotUsableError, match="EXPIRED"):
        consume(q, request)


def test_a_per_request_ttl_overrides_the_default() -> None:
    clock = Clock()
    q = ApprovalQueue(clock=clock, default_ttl=timedelta(hours=1))
    request = submit(q, ttl=timedelta(seconds=30))
    clock.advance(timedelta(seconds=31))
    assert q.get("t1", request.request_id).status is ApprovalStatus.EXPIRED


def test_default_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        ApprovalQueue(default_ttl=timedelta(0))


# -------------------------------------------------------------------- audit


def test_every_state_change_lands_in_the_audit_chain() -> None:
    log = AuditLog(clock=Clock())
    q = ApprovalQueue(audit=log, clock=log._clock)
    request = submit(q)
    q.approve("t1", request.request_id, human())
    consume(q, request)
    events = [r.event_type for r in log.records("t1")]
    assert events == [
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_DECIDED,
        EventType.APPROVAL_CONSUMED,
    ]
    assert log.verify_chain("t1").ok
    decided = next(r for r in log.records("t1") if r.event_type == EventType.APPROVAL_DECIDED)
    assert decided.actor == "alice"
    assert decided.payload["args_hash"] == ARGS_HASH


def test_expiry_is_audited_exactly_once() -> None:
    clock = Clock()
    log = AuditLog(clock=clock)
    q = ApprovalQueue(audit=log, clock=clock, default_ttl=timedelta(minutes=1))
    request = submit(q)
    clock.advance(timedelta(minutes=2))
    q.get("t1", request.request_id)
    q.get("t1", request.request_id)
    expired = [r for r in log.records("t1") if r.payload.get("decision") == "EXPIRED"]
    assert len(expired) == 1


# ---------------------------------------------------------------- persistence


def test_a_file_backed_queue_survives_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "approvals.sqlite"
    first = ApprovalQueue(db)
    request = submit(first)
    first.close()
    second = ApprovalQueue(db)
    assert [r.request_id for r in second.list_pending("t1")] == [request.request_id]


# ---------------------------------------------------------------- evidence


def test_evidence_digest_is_stable_and_content_sensitive() -> None:
    assert evidence().digest() == evidence().digest()
    assert evidence().digest() != evidence(rationale="other").digest()
    assert evidence().digest() != evidence(confidence=0.5).digest()


def test_evidence_carries_everything_an_approver_needs() -> None:
    bundle = evidence(
        sources=(EvidenceSource(uri="https://example.test/filing", title="10-K", sha256="a" * 64),),
        confidence=0.62,
        verifier_flags=("numbers-cross-checked",),
    )
    dumped = bundle.model_dump(mode="json")
    for key in (
        "tool",
        "args",
        "rationale",
        "sources",
        "confidence",
        "verifier_flags",
        "policy_reasons",
    ):
        assert key in dumped


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan")])
def test_evidence_confidence_must_be_a_probability(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence|greater|less|finite"):
        evidence(confidence=confidence)


def test_evidence_rationale_is_length_capped() -> None:
    with pytest.raises(ValueError, match="at most 4000"):
        evidence(rationale="x" * 4001)


def test_source_hash_must_be_a_sha256() -> None:
    with pytest.raises(ValueError, match="pattern"):
        EvidenceSource(uri="https://x", sha256="nothex")


# --------------------------------------------------------------- sanitising


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain text", "plain text"),
        ("tab\tand\nnewline", "tab\tand\nnewline"),
        ("\x1b[2J\x1b[Hscreen wipe", "[2J[Hscreen wipe"),
        ("bell\x07 here", "bell here"),
        ("\x9b31mc1 csi", "31mc1 csi"),
        ("nul\x00byte", "nulbyte"),
        ("del\x7fchar", "delchar"),
        ("unicode é — ✓ stays", "unicode é — ✓ stays"),
    ],
)
def test_control_characters_are_stripped_for_display(raw: str, clean: str) -> None:
    assert sanitize_for_display(raw) == clean


# --------------------------------------------------------------------- CLI


def run_cli(db: Path, *argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli_main(["--db", str(db), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_lists_shows_and_approves(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "q.sqlite"
    q = ApprovalQueue(db)
    request = submit(q, tier=ApprovalTier.EXPLICIT_SIGNOFF)
    q.close()

    code, out, _ = run_cli(db, "--tenant", "t1", "list", capsys=capsys)
    assert code == 0 and request.request_id in out and "PENDING" in out

    code, out, _ = run_cli(db, "--tenant", "t1", "show", request.request_id, capsys=capsys)
    assert code == 0
    assert "momentum signal" in out
    assert request.signoff_code in out

    code, _, err = run_cli(
        db,
        "--tenant",
        "t1",
        "approve",
        request.request_id,
        "--approver",
        "alice",
        "--max-tier",
        "EXPLICIT_SIGNOFF",
        capsys=capsys,
    )
    assert code == 1 and "approval_signoff_invalid" in err

    code, out, _ = run_cli(
        db,
        "--tenant",
        "t1",
        "approve",
        request.request_id,
        "--approver",
        "alice",
        "--max-tier",
        "EXPLICIT_SIGNOFF",
        "--signoff",
        request.signoff_code,
        capsys=capsys,
    )
    assert code == 0 and "APPROVED" in out

    code, out, _ = run_cli(db, "--tenant", "t1", "list", capsys=capsys)
    assert "no pending requests" in out


def test_cli_reject_and_error_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "q.sqlite"
    q = ApprovalQueue(db)
    request = submit(q)
    q.close()
    code, out, _ = run_cli(
        db,
        "--tenant",
        "t1",
        "reject",
        request.request_id,
        "--approver",
        "alice",
        "--note",
        "no",
        capsys=capsys,
    )
    assert code == 0 and "REJECTED" in out
    code, _, err = run_cli(
        db, "--tenant", "t1", "reject", request.request_id, "--approver", "alice", capsys=capsys
    )
    assert code == 1 and "approval_not_pending" in err
    code, _, err = run_cli(db, "--tenant", "t1", "show", "missing", capsys=capsys)
    assert code == 1 and "approval_not_found" in err


def test_cli_enforces_the_same_rules_as_the_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "q.sqlite"
    q = ApprovalQueue(db)
    request = submit(q, agent="agent-1")
    q.close()
    code, _, err = run_cli(
        db, "--tenant", "t1", "approve", request.request_id, "--approver", "agent-1", capsys=capsys
    )
    assert code == 1 and "approval_not_authorised" in err
    code, _, err = run_cli(
        db, "--tenant", "t2", "approve", request.request_id, "--approver", "bob", capsys=capsys
    )
    assert code == 1 and "approval_not_found" in err


def test_cli_strips_terminal_escapes_from_model_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "q.sqlite"
    q = ApprovalQueue(db)
    hostile = "\x1b[2J\x1b]0;pwned\x07\x9b31m harmless"
    request = q.submit(
        tenant_id="t1",
        agent_id="agent-1",
        tool_name=TOOL,
        args_hash=ARGS_HASH,
        tier=ApprovalTier.ONE_CLICK,
        evidence=evidence(rationale=hostile),
    )
    q.close()
    _, out, _ = run_cli(db, "--tenant", "t1", "show", request.request_id, capsys=capsys)
    assert "\x1b" not in out
    assert "\x9b" not in out
    assert "\x07" not in out
    assert "harmless" in out


def test_cli_creates_the_database_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "nested" / "dir" / "q.sqlite"
    code, out, _ = run_cli(db, "--tenant", "t1", "list", capsys=capsys)
    assert code == 0 and db.exists()
    assert "no pending requests" in out


# --------------------------------------------------------------------- REST

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from keelgate.approvals.rest import create_app  # noqa: E402

TOKENS = {
    "tok-alice": Approver(
        approver_id="alice", tenant_id="t1", max_tier=ApprovalTier.EXPLICIT_SIGNOFF
    ),
    "tok-bob": Approver(approver_id="bob", tenant_id="t1", max_tier=ApprovalTier.ONE_CLICK),
    "tok-eve": Approver(approver_id="eve", tenant_id="t2", max_tier=ApprovalTier.EXPLICIT_SIGNOFF),
}


def rest(q: ApprovalQueue) -> TestClient:
    return TestClient(create_app(q, TOKENS))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_rest_requires_a_valid_bearer_token() -> None:
    client = rest(ApprovalQueue())
    assert client.get("/approvals").status_code == 401
    assert client.get("/approvals", headers=auth("wrong")).status_code == 401
    assert client.get("/approvals", headers={"Authorization": "Basic tok-alice"}).status_code == 401
    assert client.get("/approvals", headers={"Authorization": "Bearer"}).status_code == 401
    assert client.get("/approvals", headers=auth("tok-alice")).status_code == 200


def test_rest_needs_at_least_one_token() -> None:
    with pytest.raises(ValueError, match="token"):
        create_app(ApprovalQueue(), {})


def test_rest_full_flow() -> None:
    q = ApprovalQueue()
    request = submit(q, tier=ApprovalTier.EXPLICIT_SIGNOFF)
    client = rest(q)

    listed = client.get("/approvals", headers=auth("tok-alice")).json()
    assert [r["request_id"] for r in listed] == [request.request_id]
    assert "signoff_code" not in listed[0]  # the code is only shown on the detail view

    detail = client.get(f"/approvals/{request.request_id}", headers=auth("tok-alice")).json()
    assert detail["signoff_code"] == request.signoff_code
    assert detail["evidence"]["rationale"] == "momentum signal"

    no_code = client.post(
        f"/approvals/{request.request_id}/approve", json={}, headers=auth("tok-alice")
    )
    assert no_code.status_code == 422
    assert no_code.json()["error"] == "approval_signoff_invalid"

    ok = client.post(
        f"/approvals/{request.request_id}/approve",
        json={"signoff_code": request.signoff_code, "note": "fine"},
        headers=auth("tok-alice"),
    )
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"

    again = client.post(
        f"/approvals/{request.request_id}/approve", json={}, headers=auth("tok-alice")
    )
    assert again.status_code == 409


def test_rest_reject() -> None:
    q = ApprovalQueue()
    request = submit(q)
    resp = rest(q).post(
        f"/approvals/{request.request_id}/reject", json={"note": "no"}, headers=auth("tok-bob")
    )
    assert resp.status_code == 200 and resp.json()["status"] == "REJECTED"


def test_rest_tenant_comes_from_the_token_not_the_request() -> None:
    q = ApprovalQueue()
    request = submit(q, tenant="t1")
    client = rest(q)
    assert client.get("/approvals", headers=auth("tok-eve")).json() == []
    assert (
        client.get(f"/approvals/{request.request_id}", headers=auth("tok-eve")).status_code == 404
    )
    resp = client.post(f"/approvals/{request.request_id}/approve", json={}, headers=auth("tok-eve"))
    assert resp.status_code == 404
    assert q.get("t1", request.request_id).status is ApprovalStatus.PENDING


def test_rest_enforces_tier_clearance_and_separation_of_duties() -> None:
    q = ApprovalQueue()
    big = submit(q, tier=ApprovalTier.EXPLICIT_SIGNOFF)
    own = submit(q, agent="alice")
    client = rest(q)
    low = client.post(
        f"/approvals/{big.request_id}/approve",
        json={"signoff_code": big.signoff_code},
        headers=auth("tok-bob"),
    )
    assert low.status_code == 403
    mine = client.post(f"/approvals/{own.request_id}/approve", json={}, headers=auth("tok-alice"))
    assert mine.status_code == 403


def test_rest_expired_request_is_gone() -> None:
    clock = Clock()
    q = ApprovalQueue(clock=clock, default_ttl=timedelta(minutes=1))
    request = submit(q)
    clock.advance(timedelta(minutes=2))
    resp = rest(q).post(
        f"/approvals/{request.request_id}/approve", json={}, headers=auth("tok-alice")
    )
    assert resp.status_code == 410


def test_rest_rejects_oversized_input() -> None:
    q = ApprovalQueue()
    request = submit(q)
    resp = rest(q).post(
        f"/approvals/{request.request_id}/approve",
        json={"note": "x" * 5000},
        headers=auth("tok-alice"),
    )
    assert resp.status_code == 422


def test_rest_error_bodies_are_json() -> None:
    resp = rest(ApprovalQueue()).get("/approvals/missing", headers=auth("tok-alice"))
    assert resp.status_code == 404
    assert json.loads(resp.text)["error"] == "approval_not_found"
