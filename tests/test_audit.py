"""The audit chain: integrity, tamper detection, atomicity, tenant isolation."""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from keelgate.audit import (
    AuditError,
    AuditLog,
    AuditRecord,
    ChainHead,
    EventType,
    PostgresAuditStore,
    SqliteAuditStore,
    canonical_json,
    genesis_hash,
    verify_chain,
)
from keelgate.audit.records import build_record
from tests.conftest import Clock, skip_or_fail

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def filled(log: AuditLog, tenant: str = "t1", n: int = 5) -> list[AuditRecord]:
    return [
        log.append(tenant_id=tenant, event_type=EventType.TOOL_CALL, actor="a", payload={"n": i})
        for i in range(n)
    ]


def rewrite(rec: AuditRecord, **changes: Any) -> AuditRecord:
    return rec.model_copy(update=changes)


# --------------------------------------------------------------- format pins


def test_golden_hash_vector_pins_the_record_format() -> None:
    """If this changes, every existing chain stops verifying. Do not 'fix' it casually."""
    record = build_record(
        tenant_id="t",
        seq=1,
        prev_hash=genesis_hash("t"),
        timestamp=T0,
        event_type="tool.call",
        actor="a",
        payload={"b": 1, "a": [1, 2]},
    )
    # Public test vectors (SHA-256 of fixed inputs), not secrets.
    assert (
        genesis_hash("t")
        == (
            "a22a6c6153a91adf054f712d65f883d4c5bb317867a3ad54bddd58a2b4a3d6c4"  # pragma: allowlist secret
        )
    )
    assert record.payload_json == '{"a":[1,2],"b":1}'
    assert (
        record.hash
        == (
            "4f54a3e0b45b97d000f5207a2d42b77e9585599b54ad082512515a34dd8d19b8"  # pragma: allowlist secret
        )
    )


def test_canonical_json_is_order_independent_and_compact() -> None:
    assert canonical_json({"b": 1, "a": {"d": 1, "c": 2}}) == '{"a":{"c":2,"d":1},"b":1}'
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_canonical_json_escapes_non_ascii_so_storage_encoding_cannot_matter() -> None:
    assert canonical_json({"k": "é"}) == '{"k":"\\u00e9"}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_canonical_json_refuses_non_finite_numbers(bad: float) -> None:
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json({"x": bad})


def test_genesis_is_tenant_specific() -> None:
    assert genesis_hash("t1") != genesis_hash("t2")


# ------------------------------------------------------------------ happy path


def test_a_clean_chain_verifies() -> None:
    log = AuditLog()
    filled(log)
    result = log.verify_chain("t1")
    assert result.ok
    assert bool(result)
    assert result.records_checked == 5
    assert result.head == log.head("t1")
    assert result.error is None


def test_an_empty_chain_verifies_and_has_no_head() -> None:
    log = AuditLog()
    result = log.verify_chain("nobody")
    assert result.ok
    assert result.records_checked == 0
    assert result.head is None
    assert log.head("nobody") is None


def test_records_link_to_their_predecessor_from_genesis() -> None:
    records = filled(AuditLog(), n=3)
    assert records[0].prev_hash == genesis_hash("t1")
    assert records[1].prev_hash == records[0].hash
    assert records[2].prev_hash == records[1].hash
    assert [r.seq for r in records] == [1, 2, 3]


def test_after_seq_returns_only_later_records() -> None:
    log = AuditLog()
    filled(log, n=5)
    assert [r.seq for r in log.records("t1", after_seq=3)] == [4, 5]


def test_event_types_are_open_ended() -> None:
    log = AuditLog()
    log.append(tenant_id="t", event_type="some.future.event", actor="a", payload={})
    assert log.verify_chain("t").ok


def test_timestamps_come_from_the_injected_clock_in_utc() -> None:
    clock = Clock()
    log = AuditLog(clock=clock)
    rec = log.append(tenant_id="t", event_type="x", actor="a", payload={})
    assert rec.timestamp == clock.now.astimezone(UTC).isoformat()


# ------------------------------------------------------------- tenant isolation


def test_chains_are_independent_per_tenant() -> None:
    log = AuditLog()
    filled(log, "t1", 3)
    filled(log, "t2", 2)
    assert log.head("t1") is not None and log.head("t1").seq == 3  # type: ignore[union-attr]
    assert log.head("t2") is not None and log.head("t2").seq == 2  # type: ignore[union-attr]
    assert log.verify_chain("t1").ok and log.verify_chain("t2").ok
    assert {r.tenant_id for r in log.records("t1")} == {"t1"}


def test_a_tenants_records_do_not_verify_as_another_tenants() -> None:
    log = AuditLog()
    records = filled(log, "t1", 3)
    result = verify_chain(records, tenant_id="t2")
    assert not result.ok
    assert "tenant" in (result.error or "")


def test_a_record_spliced_in_from_another_tenant_is_detected() -> None:
    log = AuditLog()
    mine = filled(log, "t1", 3)
    theirs = filled(log, "t2", 3)
    spliced = [mine[0], theirs[1], mine[2]]
    assert not verify_chain(spliced, tenant_id="t1").ok


def test_same_content_in_two_tenants_has_different_hashes() -> None:
    log = AuditLog(clock=Clock())
    a = log.append(tenant_id="t1", event_type="x", actor="a", payload={"v": 1})
    b = log.append(tenant_id="t2", event_type="x", actor="a", payload={"v": 1})
    assert a.hash != b.hash


# ------------------------------------------------------------ tamper detection


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload_json", '{"n":999}'),
        ("payload_json", '{"n": 1}'),  # whitespace-only rewrite is still a rewrite
        ("actor", "mallory"),
        ("event_type", "tool.result"),
        ("timestamp", "2026-01-01T00:00:00+00:00"),
    ],
)
def test_altering_any_field_is_detected(field: str, value: str) -> None:
    records = filled(AuditLog(clock=Clock()), n=5)
    forged = list(records)
    forged[2] = rewrite(forged[2], **{field: value})
    result = verify_chain(forged, tenant_id="t1")
    assert not result.ok
    assert result.bad_seq == 3
    assert "hash does not match" in (result.error or "")


def test_recomputing_one_hash_is_not_enough_because_the_next_link_breaks() -> None:
    records = filled(AuditLog(clock=Clock()), n=5)
    original = records[2]
    forged_body = build_record(
        tenant_id="t1",
        seq=3,
        prev_hash=original.prev_hash,
        timestamp=datetime.fromisoformat(original.timestamp),
        event_type=original.event_type,
        actor=original.actor,
        payload={"n": 999},
    )
    forged = [*records[:2], forged_body, *records[3:]]
    result = verify_chain(forged, tenant_id="t1")
    assert not result.ok
    assert result.bad_seq == 4
    assert "prev_hash" in (result.error or "")


def test_deleting_a_middle_record_is_detected() -> None:
    records = filled(AuditLog(), n=5)
    result = verify_chain([*records[:2], *records[3:]], tenant_id="t1")
    assert not result.ok
    assert "expected seq 3" in (result.error or "")


def test_reordering_records_is_detected() -> None:
    records = filled(AuditLog(), n=4)
    result = verify_chain([records[0], records[2], records[1], records[3]], tenant_id="t1")
    assert not result.ok


def test_duplicating_a_record_is_detected() -> None:
    records = filled(AuditLog(), n=3)
    assert not verify_chain([records[0], records[1], records[1], records[2]], tenant_id="t1").ok


def test_dropping_the_first_record_is_detected() -> None:
    records = filled(AuditLog(), n=3)
    assert not verify_chain(records[1:], tenant_id="t1").ok


def test_verification_rejects_a_chain_whose_time_runs_backwards() -> None:
    first = build_record(
        tenant_id="t",
        seq=1,
        prev_hash=genesis_hash("t"),
        timestamp=T0 + timedelta(hours=1),
        event_type="x",
        actor="a",
        payload={},
    )
    second = build_record(
        tenant_id="t",
        seq=2,
        prev_hash=first.hash,
        timestamp=T0,
        event_type="x",
        actor="a",
        payload={},
    )
    result = verify_chain([first, second], tenant_id="t")
    assert not result.ok
    assert "backwards" in (result.error or "")


def test_a_clock_stepped_backwards_is_clamped_not_flagged() -> None:
    """An NTP step must not turn an honest chain into a false tamper alarm."""
    clock = Clock()
    log = AuditLog(clock=clock)
    first = log.append(tenant_id="t", event_type="x", actor="a", payload={})
    clock.advance(timedelta(minutes=-5))
    second = log.append(tenant_id="t", event_type="x", actor="a", payload={})
    assert second.timestamp == first.timestamp
    assert log.verify_chain("t").ok


def test_a_non_iso_timestamp_is_detected() -> None:
    records = filled(AuditLog(), n=2)
    broken = rewrite(records[1], timestamp="yesterday")
    assert not verify_chain([records[0], broken], tenant_id="t1").ok


def test_truncating_the_tail_is_invisible_without_an_anchor_and_caught_with_one() -> None:
    """The documented limit of a bare hash chain, and its remedy."""
    log = AuditLog()
    records = filled(log, n=5)
    anchor = log.head("t1")
    assert anchor is not None
    truncated = records[:3]

    assert verify_chain(truncated, tenant_id="t1").ok  # a prefix is a valid chain
    result = verify_chain(truncated, tenant_id="t1", expected_head=anchor)
    assert not result.ok
    assert "removed" in (result.error or "")


def test_rolling_the_chain_back_to_an_empty_log_is_caught_by_an_anchor() -> None:
    log = AuditLog()
    filled(log, n=3)
    result = verify_chain([], tenant_id="t1", expected_head=log.head("t1"))
    assert not result.ok


def test_a_fully_rewritten_chain_passes_alone_but_not_against_an_anchor() -> None:
    """An attacker who can rewrite the whole table and recompute every hash."""
    log = AuditLog(clock=Clock())
    filled(log, n=4)
    anchor = log.head("t1")

    forged: list[AuditRecord] = []
    prev = genesis_hash("t1")
    for i in range(4):
        rec = build_record(
            tenant_id="t1",
            seq=i + 1,
            prev_hash=prev,
            timestamp=T0,
            event_type="tool.call",
            actor="a",
            payload={"n": i, "forged": True},
        )
        forged.append(rec)
        prev = rec.hash

    assert verify_chain(forged, tenant_id="t1").ok
    result = verify_chain(forged, tenant_id="t1", expected_head=anchor)
    assert not result.ok
    assert "rewritten" in (result.error or "")


def test_a_longer_chain_must_still_contain_the_anchored_record() -> None:
    log = AuditLog(clock=Clock())
    records = filled(log, n=3)
    anchor = log.head("t1")
    extended = [
        *records,
        build_record(
            tenant_id="t1",
            seq=4,
            prev_hash=records[-1].hash,
            timestamp=datetime.fromisoformat(records[-1].timestamp) + timedelta(days=1),
            event_type="x",
            actor="a",
            payload={},
        ),
    ]
    assert verify_chain(extended, tenant_id="t1", expected_head=anchor).ok


def test_an_anchor_for_another_tenant_is_refused() -> None:
    log = AuditLog()
    filled(log, "t1", 2)
    other = ChainHead("t2", 1, "0" * 64)
    assert not verify_chain(log.records("t1"), tenant_id="t1", expected_head=other).ok


# ------------------------------------------------------- storage-level tampering


def test_sqlite_triggers_block_update_and_delete_through_the_connection() -> None:
    store = SqliteAuditStore()
    log = AuditLog(store)
    filled(log, n=2)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE audit_records SET actor = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM audit_records")
    assert log.verify_chain("t1").ok


def test_tampering_in_the_database_file_is_detected_by_verification(tmp_path: Path) -> None:
    """Someone with file access drops the trigger and edits a row directly."""
    path = tmp_path / "audit.sqlite"
    log = AuditLog(SqliteAuditStore(path), clock=Clock())
    filled(log, n=5)
    log.close()

    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER audit_records_no_update")
    raw.execute("UPDATE audit_records SET payload_json = '{\"n\":999}' WHERE seq = 3")
    raw.commit()
    raw.close()

    reopened = AuditLog(SqliteAuditStore(path))
    result = reopened.verify_chain("t1")
    assert not result.ok
    assert result.bad_seq == 3


def test_deleting_rows_in_the_database_file_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    log = AuditLog(SqliteAuditStore(path))
    filled(log, n=5)
    anchor = log.head("t1")
    log.close()

    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER audit_records_no_delete")
    raw.execute("DELETE FROM audit_records WHERE seq = 2")
    raw.commit()
    raw.close()

    assert not AuditLog(SqliteAuditStore(path)).verify_chain("t1").ok

    # Reopening a store re-installs the append-only triggers, so an attacker has to
    # drop them again before truncating the tail.
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER IF EXISTS audit_records_no_delete")
    raw.execute("DELETE FROM audit_records WHERE seq >= 2")
    raw.commit()
    raw.close()
    reopened = AuditLog(SqliteAuditStore(path))
    assert reopened.verify_chain("t1").ok  # a clean prefix...
    assert not reopened.verify_chain("t1", expected_head=anchor).ok  # ...but not the anchored one


def test_a_file_backed_log_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    first = AuditLog(SqliteAuditStore(path))
    filled(first, n=3)
    first.close()
    second = AuditLog(SqliteAuditStore(path))
    more = second.append(tenant_id="t1", event_type="x", actor="a", payload={})
    assert more.seq == 4
    assert second.verify_chain("t1").records_checked == 4


# --------------------------------------------------------------- write errors


@pytest.mark.parametrize(
    "payload",
    [{"x": float("nan")}, {"x": object()}, {"x": {1, 2}}, {"x": "y" * (300 * 1024)}],
)
def test_unwritable_payloads_raise_and_write_nothing(payload: dict[str, Any]) -> None:
    log = AuditLog()
    with pytest.raises(AuditError):
        log.append(tenant_id="t", event_type="x", actor="a", payload=payload)
    assert log.head("t") is None
    ok = log.append(tenant_id="t", event_type="x", actor="a", payload={})
    assert ok.seq == 1  # the failed attempt did not consume a sequence number


def test_naive_timestamps_are_refused() -> None:
    log = AuditLog(clock=lambda: datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(AuditError, match="timezone-aware"):
        log.append(tenant_id="t", event_type="x", actor="a", payload={})


def test_a_builder_that_does_not_extend_the_head_is_refused() -> None:
    store = SqliteAuditStore()
    wrong = build_record(
        tenant_id="t",
        seq=7,
        prev_hash="0" * 64,
        timestamp=T0,
        event_type="x",
        actor="a",
        payload={},
    )
    with pytest.raises(AuditError, match="does not extend"):
        store.append("t", lambda _seq, _prev, _ts: wrong)
    assert store.head("t") is None


# ------------------------------------------------------------------ concurrency


def test_concurrent_writers_produce_one_contiguous_valid_chain() -> None:
    log = AuditLog()
    errors: list[BaseException] = []

    def writer(worker: int) -> None:
        try:
            for i in range(25):
                log.append(tenant_id="t", event_type="x", actor=f"w{worker}", payload={"i": i})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    result = log.verify_chain("t")
    assert result.ok
    assert result.records_checked == 200
    assert [r.seq for r in log.records("t")] == list(range(1, 201))


# --------------------------------------------------------------------- postgres

PG_DSN = os.environ.get(
    "KEELGATE_TEST_POSTGRES_DSN",
    "postgresql://keelgate:keelgate@localhost:5432/keelgate",  # pragma: allowlist secret
)


@pytest.fixture
def pg_log() -> AuditLog:
    psycopg = pytest.importorskip("psycopg")
    try:
        psycopg.connect(PG_DSN, connect_timeout=2).close()
    except Exception as exc:
        skip_or_fail(f"no Postgres at the test DSN ({type(exc).__name__}); run `make up`")
    return AuditLog(PostgresAuditStore(PG_DSN))


@pytest.mark.integration
def test_postgres_chain_verifies_and_isolates_tenants(pg_log: AuditLog) -> None:
    tenant_a, tenant_b = f"pg-{uuid.uuid4().hex[:12]}", f"pg-{uuid.uuid4().hex[:12]}"
    filled(pg_log, tenant_a, 4)
    filled(pg_log, tenant_b, 2)
    assert pg_log.verify_chain(tenant_a).records_checked == 4
    assert pg_log.verify_chain(tenant_b).records_checked == 2
    assert {r.tenant_id for r in pg_log.records(tenant_a)} == {tenant_a}


@pytest.mark.integration
def test_postgres_blocks_update_and_delete(pg_log: AuditLog) -> None:
    psycopg = pytest.importorskip("psycopg")
    tenant = f"pg-{uuid.uuid4().hex[:12]}"
    filled(pg_log, tenant, 2)
    with psycopg.connect(PG_DSN) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE audit_records SET actor = 'x' WHERE tenant_id = %s", (tenant,))
    with psycopg.connect(PG_DSN) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM audit_records WHERE tenant_id = %s", (tenant,))
    assert pg_log.verify_chain(tenant).ok


@pytest.mark.integration
def test_postgres_detects_tampering_by_someone_who_drops_the_trigger(pg_log: AuditLog) -> None:
    psycopg = pytest.importorskip("psycopg")
    tenant = f"pg-{uuid.uuid4().hex[:12]}"
    filled(pg_log, tenant, 4)
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("ALTER TABLE audit_records DISABLE TRIGGER audit_records_no_change")
        try:
            conn.execute(
                "UPDATE audit_records SET payload_json = '{\"n\":999}' WHERE tenant_id = %s AND seq = 2",
                (tenant,),
            )
        finally:
            conn.execute("ALTER TABLE audit_records ENABLE TRIGGER audit_records_no_change")
    result = pg_log.verify_chain(tenant)
    assert not result.ok
    assert result.bad_seq == 2


@pytest.mark.integration
def test_postgres_concurrent_writers_keep_the_chain_valid(pg_log: AuditLog) -> None:
    tenant = f"pg-{uuid.uuid4().hex[:12]}"
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for i in range(10):
                pg_log.append(tenant_id=tenant, event_type="x", actor="w", payload={"i": i})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    result = pg_log.verify_chain(tenant)
    assert result.ok
    assert result.records_checked == 40
