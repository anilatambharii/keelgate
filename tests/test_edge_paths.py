"""Narrow paths that the main suites reach only indirectly.

Each test names the exact branch it exists for. Several guard behaviour that is
defensive by design and would otherwise only be exercised by a real failure.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

import keelgate.policy._engine as engine_module
from keelgate.approvals import ApprovalNotPendingError, ApprovalQueue, ApprovalStatus, ApprovalTier
from keelgate.audit import AuditError, AuditLog, PostgresAuditStore, genesis_hash, verify_chain
from keelgate.audit._records import AuditRecord, compute_hash
from keelgate.capabilities import GrantInvalidError, GrantSigner, GrantVerifier
from keelgate.policy._engine import first_expression, pack_path
from tests.conftest import Clock, skip_or_fail
from tests.test_approvals import human, submit
from tests.test_audit import PG_DSN, T0, filled


def forged_record(**overrides: Any) -> AuditRecord:
    """A record whose hash is *consistent* with its (bad) contents."""
    fields: dict[str, Any] = {
        "tenant_id": "t",
        "seq": 1,
        "timestamp": T0.isoformat(),
        "event_type": "x",
        "actor": "a",
        "payload_json": "{}",
        "prev_hash": genesis_hash("t"),
    }
    fields.update(overrides)
    return AuditRecord(**fields, hash=compute_hash(**fields))


# --------------------------------------------------------------------- audit


def test_a_self_consistent_record_with_a_non_iso_timestamp_is_still_rejected() -> None:
    result = verify_chain([forged_record(timestamp="yesterday")], tenant_id="t")
    assert not result.ok
    assert result.error == "timestamp is not ISO-8601"


def test_a_self_consistent_record_is_otherwise_accepted() -> None:
    assert verify_chain([forged_record()], tenant_id="t").ok


def test_an_empty_store_has_no_records_to_iterate(tmp_path: Path) -> None:
    log = AuditLog()
    assert list(log.records("nobody")) == []


# ----------------------------------------------------------------- postgres


@pytest.fixture
def pg() -> AuditLog:
    psycopg = pytest.importorskip("psycopg")
    try:
        psycopg.connect(PG_DSN, connect_timeout=2).close()
    except Exception as exc:
        skip_or_fail(f"no Postgres at the test DSN ({type(exc).__name__})")
    return AuditLog(PostgresAuditStore(PG_DSN))


@pytest.mark.integration
def test_postgres_head_matches_the_last_record(pg: AuditLog) -> None:
    tenant = f"pg-{uuid.uuid4().hex[:12]}"
    assert pg.head(tenant) is None
    records = filled(pg, tenant, 3)
    head = pg.head(tenant)
    assert head is not None
    assert (head.seq, head.hash) == (3, records[-1].hash)
    assert pg.verify_chain(tenant, expected_head=head).ok


@pytest.mark.integration
def test_postgres_refuses_a_record_that_does_not_extend_the_chain(pg: AuditLog) -> None:
    tenant = f"pg-{uuid.uuid4().hex[:12]}"
    wrong = forged_record(tenant_id=tenant, seq=9, prev_hash="0" * 64)
    store = PostgresAuditStore(PG_DSN)
    with pytest.raises(AuditError, match="does not extend"):
        store.append(tenant, lambda _seq, _prev, _ts: wrong)
    assert pg.head(tenant) is None


# ------------------------------------------------------------------ approvals


def test_a_stale_writer_cannot_overwrite_a_decision_made_elsewhere(tmp_path: Path) -> None:
    """Compare-and-swap: two handles on one database race to decide the same request."""
    db = tmp_path / "q.sqlite"
    first, second = ApprovalQueue(db), ApprovalQueue(db)
    request = submit(first, tier=ApprovalTier.ONE_CLICK)
    stale = first.get("t1", request.request_id)  # read while still PENDING

    second.approve("t1", request.request_id, human(tier=ApprovalTier.ONE_CLICK))
    with pytest.raises(ApprovalNotPendingError, match="changed concurrently"):
        first._store(
            stale.model_copy(update={"status": ApprovalStatus.REJECTED}),
            expect=ApprovalStatus.PENDING,
        )
    assert second.get("t1", request.request_id).status is ApprovalStatus.APPROVED


def test_expiry_time_is_respected_across_handles(tmp_path: Path) -> None:
    clock = Clock()
    db = tmp_path / "q.sqlite"
    q = ApprovalQueue(db, clock=clock, default_ttl=timedelta(minutes=1))
    request = submit(q)
    clock.advance(timedelta(minutes=2))
    other = ApprovalQueue(db, clock=clock)
    assert other.get("t1", request.request_id).status is ApprovalStatus.EXPIRED


# --------------------------------------------------------------------- grants


def test_a_payload_that_decodes_to_a_non_byte_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyseto

    class Decoded:
        payload: ClassVar[dict[str, str]] = {"already": "parsed"}
        footer = b'{"kid":"k1"}'

    signer = GrantSigner.generate()
    verifier = GrantVerifier({"k1": signer.public_key_pem()})
    token = signer.sign({"typ": "x"})
    monkeypatch.setattr(pyseto, "decode", lambda *_a, **_k: Decoded())
    with pytest.raises(GrantInvalidError, match="byte string"):
        verifier.verify(token)


@pytest.mark.parametrize("bad_time", [1234567890, None, ["2026-01-01"], {"t": 1}])
def test_non_string_timestamps_in_a_signed_grant_are_rejected(bad_time: object) -> None:
    clock = Clock()
    signer = GrantSigner.generate()
    verifier = GrantVerifier({"k1": signer.public_key_pem()}, clock=clock)
    now = clock.now
    token = signer.sign(
        {
            "typ": "keelgate.grant.v1",
            "jti": "j",
            "iss": "keelgate",
            "kid": "k1",
            "sub": "a",
            "tenant": "t",
            "caps": ["a:b"],
            "budget": {"max_cost": 1},
            "iat": now.isoformat(),
            "nbf": now.isoformat(),
            "exp": bad_time,
        }
    )
    with pytest.raises(GrantInvalidError):
        verifier.verify(token)


# --------------------------------------------------------------------- policy


def test_first_expression_handles_an_undefined_result() -> None:
    assert first_expression([]) is None
    assert first_expression(None) is None

    class NoExpressions:
        expressions: ClassVar[list[object]] = []

    assert first_expression([NoExpressions()]) is None


def test_pack_path_prefers_a_pack_bundled_in_the_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "packs" / "demo"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(engine_module, "__file__", str(tmp_path / "engine.py"))
    assert pack_path("demo") == bundled


def test_pack_path_falls_back_to_the_source_checkout() -> None:
    path = pack_path("finance_basic")
    assert path.name == "finance_basic"
    assert (path / "finance_basic.rego").is_file()
