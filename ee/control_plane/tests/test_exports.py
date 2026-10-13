"""Audit browser, JSON/CSV exports and the compliance bundle."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import tracemalloc
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from keelgate.audit import AuditRecord, ChainHead, genesis_hash, verify_chain
from keelgate.audit._records import build_record

from keelgate_cloud.api import exports
from keelgate_cloud.db import Database
from tests.harness import Org, make_app


@pytest.fixture(scope="module")
def app(db: Database):  # type: ignore[no-untyped-def]
    return make_app(db)


@pytest.fixture(scope="module")
def client(app) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(app)


def set_plan(pg_dsns: dict[str, str], org: Org, plan: str) -> None:
    with psycopg.connect(pg_dsns["owner"]) as conn:
        conn.execute("SELECT set_config('app.org_id', %s, true)", (org.id,))
        conn.execute("UPDATE orgs SET plan = %s", (plan,))


def emit(
    client: TestClient,
    org: Org,
    *,
    actor: str = "agent-1",
    payload: str = "{}",
    event_type: str = "tool.result",
) -> dict[str, Any]:
    response = client.post(
        "/v1/audit/events",
        json={
            "tenant_id": org.id,
            "event_type": event_type,
            "actor": actor,
            "payload_json": payload,
        },
        headers=org.key_auth,
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def records_of(document: dict[str, Any]) -> list[AuditRecord]:
    return [AuditRecord(**r) for r in document["records"]]


def seed_chain(pg_dsns: dict[str, str], org: Org, count: int) -> None:
    """Bulk-write a valid chain through the owner connection (the API is too slow for 5000)."""
    stamp = datetime.now(UTC) - timedelta(days=1)
    prev, rows = genesis_hash(org.id), []
    for seq in range(1, count + 1):
        record = build_record(
            tenant_id=org.id,
            seq=seq,
            prev_hash=prev,
            timestamp=stamp + timedelta(milliseconds=seq),
            event_type="tool.result",
            actor="bulk",
            payload={"n": seq, "text": "x" * 40},
        )
        prev = record.hash
        rows.append(
            (
                org.id,
                record.seq,
                record.timestamp,
                record.event_type,
                record.actor,
                record.payload_json,
                record.prev_hash,
                record.hash,
            )
        )
    with psycopg.connect(pg_dsns["owner"]) as conn:
        conn.execute("SELECT set_config('app.org_id', %s, true)", (org.id,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO audit_records (org_id, seq, timestamp, event_type, actor, "
                "payload_json, prev_hash, hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )


def csv_records(text: str, org_id: str) -> list[AuditRecord]:
    out = []
    for row in csv.DictReader(io.StringIO(text, newline="")):
        for name in filter(None, row["csv_escaped"].split(",")):
            row[name] = row[name][1:]
        out.append(
            AuditRecord(
                tenant_id=row["tenant_id"],
                seq=int(row["seq"]),
                timestamp=row["timestamp"],
                event_type=row["event_type"],
                actor=row["actor"],
                payload_json=row["payload_json"],
                prev_hash=row["prev_hash"],
                hash=row["hash"],
            )
        )
    assert all(r.tenant_id == org_id for r in out)
    return out


# ------------------------------------------------------------------------------------ browser


def test_browse_filters_and_paginates(client: TestClient) -> None:
    org = Org(client)
    for n in range(7):
        emit(client, org, actor="a" if n % 2 else "b", payload=json.dumps({"n": n}))
    page = client.get("/api/audit?limit=3", headers=org.auth).json()
    assert [r["seq"] for r in page["records"]] == [1, 2, 3]
    assert page["has_more"] is True
    assert page["records"][0]["payload"] == {"n": 0}
    assert set(page["records"][0]) == {
        "seq",
        "timestamp",
        "event_type",
        "actor",
        "payload",
        "hash",
        "prev_hash",
    }
    rest = client.get("/api/audit?limit=10&after_seq=3", headers=org.auth).json()
    assert [r["seq"] for r in rest["records"]] == [4, 5, 6, 7]
    assert rest["has_more"] is False
    newest = client.get("/api/audit?limit=3&newest_first=true", headers=org.auth).json()
    assert [r["seq"] for r in newest["records"]] == [7, 6, 5]
    older = client.get("/api/audit?limit=3&newest_first=true&after_seq=5", headers=org.auth).json()
    assert [r["seq"] for r in older["records"]] == [4, 3, 2]
    assert older["has_more"] is True
    only_a = client.get("/api/audit?actor=a", headers=org.auth).json()
    assert {r["actor"] for r in only_a["records"]} == {"a"}
    assert client.get("/api/audit?event_type=nope", headers=org.auth).json()["records"] == []
    assert client.get("/api/audit?limit=501", headers=org.auth).status_code == 422
    assert client.get("/api/audit?limit=0", headers=org.auth).status_code == 422


def test_browse_is_open_to_every_role_and_needs_a_session(client: TestClient) -> None:
    org = Org(client)
    emit(client, org)
    for role in ("owner", "admin", "approver", "viewer"):
        headers = org.auth if role == "owner" else org.invite(role)
        response = client.get("/api/audit", headers=headers)
        assert response.status_code == 200, role
        assert len(response.json()["records"]) == 1
    assert client.get("/api/audit").status_code == 401
    assert client.get("/api/audit", headers=org.key_auth).status_code == 401  # keys are not users


def test_browse_never_shows_another_orgs_records(client: TestClient) -> None:
    a, b = Org(client, "A"), Org(client, "B")
    emit(client, a, actor="secret-agent-of-a")
    emit(client, b, actor="agent-of-b")
    seen = client.get("/api/audit?limit=500", headers=b.auth).json()["records"]
    assert [r["actor"] for r in seen] == ["agent-of-b"]
    assert client.get("/api/audit?actor=secret-agent-of-a", headers=b.auth).json()["records"] == []


# ------------------------------------------------------------------------------------ JSON


def test_json_export_verifies_with_the_open_source_verifier(client: TestClient) -> None:
    org = Org(client)
    for n in range(12):
        emit(client, org, payload=json.dumps({"n": n, "note": 'café, "quoted"\nline'}))
    response = client.get("/api/audit/export?format=json", headers=org.auth)
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    document = response.json()
    assert document["org_id"] == org.id
    assert document["count"] == 12 and document["from_seq"] == 1 and document["to_seq"] == 12
    records = records_of(document)
    assert document["head"] == {"seq": 12, "hash": records[-1].hash}
    anchor = ChainHead(org.id, document["head"]["seq"], document["head"]["hash"])
    assert verify_chain(records, tenant_id=org.id, expected_head=anchor)
    assert {r["tenant_id"] for r in document["records"]} == {org.id}


def test_a_one_byte_edit_to_the_export_breaks_verification(client: TestClient) -> None:
    org = Org(client)
    for n in range(5):
        emit(client, org, payload=json.dumps({"amount": 100 + n}))
    raw = client.get("/api/audit/export", headers=org.auth).text
    assert verify_chain(records_of(json.loads(raw)), tenant_id=org.id)
    edited = raw.replace('\\"amount\\":102', '\\"amount\\":192', 1)
    assert edited != raw and len(edited) == len(raw)
    result = verify_chain(records_of(json.loads(edited)), tenant_id=org.id)
    assert not result and result.bad_seq == 3
    # dropping the newest record is caught against the anchored head
    document = json.loads(raw)
    anchor = ChainHead(org.id, document["head"]["seq"], document["head"]["hash"])
    document["records"].pop()
    assert not verify_chain(records_of(document), tenant_id=org.id, expected_head=anchor)


def test_export_records_itself_after_fixing_the_range(client: TestClient) -> None:
    org = Org(client)
    emit(client, org)
    emit(client, org)
    first = client.get("/api/audit/export?format=json", headers=org.auth).json()
    assert first["to_seq"] == 2 and first["count"] == 2  # its own event is not in it
    events = client.get("/api/audit?event_type=audit.exported", headers=org.auth).json()
    assert len(events["records"]) == 1
    event = events["records"][0]
    assert event["seq"] == 3
    assert event["actor"].startswith("user:")
    assert event["payload"] == {"format": "json", "from_seq": 1, "to_seq": 2, "count": 2}
    second = client.get("/api/audit/export?format=csv", headers=org.auth)
    assert second.status_code == 200
    third = client.get("/api/audit/export", headers=org.auth).json()
    assert third["count"] == 4  # the first export's event and the csv export's event
    assert verify_chain(records_of(third), tenant_id=org.id)


def test_range_exports_and_validation(client: TestClient) -> None:
    org = Org(client)
    for _ in range(6):
        emit(client, org)
    part = client.get("/api/audit/export?from_seq=3&to_seq=5", headers=org.auth).json()
    assert (part["from_seq"], part["to_seq"], part["count"]) == (3, 5, 3)
    assert [r["seq"] for r in part["records"]] == [3, 4, 5]
    beyond = client.get("/api/audit/export?to_seq=999", headers=org.auth).json()
    assert beyond["to_seq"] == 7 and beyond["count"] == 7  # 6 + the range export's own event
    nothing = client.get("/api/audit/export?from_seq=50", headers=org.auth).json()
    assert nothing["count"] == 0 and nothing["records"] == [] and nothing["head"] is None
    assert client.get("/api/audit/export?from_seq=5&to_seq=2", headers=org.auth).status_code == 422
    assert client.get("/api/audit/export?from_seq=0", headers=org.auth).status_code == 422
    assert client.get("/api/audit/export?format=xml", headers=org.auth).status_code == 422


def test_empty_chain_exports_cleanly(client: TestClient) -> None:
    org = Org(client)
    document = client.get("/api/audit/export", headers=org.auth).json()
    assert document["count"] == 0 and document["head"] is None and document["records"] == []
    assert verify_chain(records_of(document), tenant_id=org.id)
    table = list(
        csv.reader(io.StringIO(client.get("/api/audit/export?format=csv", headers=org.auth).text))
    )
    assert len(table) == 2  # header + the earlier export's own event; nothing else


def test_export_roles(client: TestClient) -> None:
    org = Org(client)
    emit(client, org)
    assert client.get("/api/audit/export", headers=org.invite("viewer")).status_code == 403
    for role in ("admin", "approver"):
        assert client.get("/api/audit/export", headers=org.invite(role)).status_code == 200
    assert client.get("/api/audit/export", headers=org.auth).status_code == 200
    assert client.get("/api/audit/export").status_code == 401
    assert client.get("/api/audit/export", headers=org.key_auth).status_code == 401
    viewer = org.invite("viewer")
    assert client.get("/api/audit/export?format=csv", headers=viewer).status_code == 403
    assert client.get("/api/audit/export/compliance", headers=viewer).status_code == 403
    # a refused export leaves no event behind
    events = client.get("/api/audit?event_type=audit.exported", headers=org.auth).json()
    assert len(events["records"]) == 3


def test_export_contains_only_the_callers_org(client: TestClient) -> None:
    a, b = Org(client, "A"), Org(client, "B")
    emit(client, a, actor="secret-agent-of-a", payload='{"k":"secret-of-a"}')
    emit(client, b, actor="agent-of-b")
    for fmt in ("json", "csv"):
        body = client.get(f"/api/audit/export?format={fmt}", headers=b.auth).text
        assert "secret-of-a" not in body and "secret-agent-of-a" not in body
        assert a.id not in body
    # another org's seq range is just this org's range: nothing to guess at
    only_b = client.get("/api/audit/export?from_seq=1&to_seq=1", headers=b.auth).json()
    assert only_b["records"][0]["actor"] == "agent-of-b"
    # the exporting event lands in the exporter's chain only
    assert (
        client.get("/api/audit?event_type=audit.exported", headers=a.auth).json()["records"] == []
    )


# ------------------------------------------------------------------------------------ CSV


def test_csv_round_trips_awkward_content_and_neutralises_formulas(client: TestClient) -> None:
    org = Org(client)
    emit(client, org, actor='=HYPERLINK("http://evil","x")', payload='{"a":"x,y"}')
    emit(client, org, actor="+1+1", payload='{"line":"one\\ntwo","q":"say \\"hi\\""}')
    emit(client, org, actor="-2", payload="-5", event_type="tool.error")
    emit(client, org, actor="@SUM(A1)", payload='"=1+1"')
    emit(client, org, actor="plain, with comma", payload='[1,"a\\r\\nb"]')
    response = client.get("/api/audit/export?format=csv", headers=org.auth)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "x-csv-note" in response.headers
    assert response.content.startswith(b"tenant_id,seq,")
    rows = list(csv.DictReader(io.StringIO(response.text, newline="")))
    assert len(rows) == 5
    # the spreadsheet-visible cell no longer starts with a formula character
    assert rows[0]["actor"].startswith("'=") and rows[0]["csv_escaped"] == "actor"
    assert rows[3]["actor"].startswith("'@")
    assert rows[4]["actor"] == "plain, with comma" and rows[4]["csv_escaped"] == ""
    # the hashed payload column is untouched, and stripping the marker restores the record
    assert rows[2]["payload_json"] == "-5"
    restored = csv_records(response.text, org.id)
    assert restored[0].actor == '=HYPERLINK("http://evil","x")'
    assert verify_chain(restored, tenant_id=org.id)
    for row in rows:
        assert not row["payload_json"].startswith(("=", "+", "@"))


def test_csv_matches_json_record_for_record(client: TestClient) -> None:
    org = Org(client)
    for n in range(30):
        emit(client, org, payload=json.dumps({"n": n}))
    from_json = records_of(client.get("/api/audit/export", headers=org.auth).json())
    from_csv = csv_records(
        client.get("/api/audit/export?format=csv", headers=org.auth).text, org.id
    )
    assert [r for r in from_csv if r.event_type != "audit.exported"] == from_json


# ------------------------------------------------------------------------------------ compliance


def read_bundle(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        assert bundle.testzip() is None
        return {name: bundle.read(name) for name in bundle.namelist()}


def run_verify_snippet(files: dict[str, bytes], where: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the python block from VERIFY.md against the unzipped files, as a customer would."""
    for name, data in files.items():
        (where / name).write_bytes(data)
    code = re.search(r"```python\n(.*?)```", files["VERIFY.md"].decode(), re.S)
    assert code is not None
    monkeypatch.chdir(where)
    captured: list[object] = []
    namespace: dict[str, Any] = {"print": lambda *a: captured.extend(a)}
    exec(compile(code.group(1), "VERIFY.md", "exec"), namespace)  # noqa: S102
    return str(captured[0])


def test_compliance_export_needs_a_business_plan(
    client: TestClient, pg_dsns: dict[str, str]
) -> None:
    org = Org(client)
    emit(client, org)
    response = client.get("/api/audit/export/compliance", headers=org.auth)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "plan_required"
    assert (
        client.get("/api/audit?event_type=audit.exported", headers=org.auth).json()["records"] == []
    )
    set_plan(pg_dsns, org, "business")
    assert client.get("/api/audit/export/compliance", headers=org.auth).status_code == 200
    assert client.get("/api/audit/export/compliance").status_code == 401


def test_compliance_bundle_contents_and_independent_verification(
    client: TestClient, pg_dsns: dict[str, str], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(client, "Contoso, Inc.")
    set_plan(pg_dsns, org, "business")
    for n in range(20):
        emit(client, org, payload=json.dumps({"n": n}))
    response = client.get("/api/audit/export/compliance", headers=org.auth)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert int(response.headers["content-length"]) == len(response.content)
    files = read_bundle(response.content)
    assert set(files) == {"records.jsonl", "manifest.json", "VERIFY.md"}
    manifest = json.loads(files["manifest.json"])
    assert manifest["org_id"] == org.id and manifest["org_name"] == "Contoso, Inc."
    assert manifest["plan"] == "business" and manifest["record_count"] == 20
    assert manifest["retention"] == {"audit_min_retention_days": 365, "audit_immutable": True}
    assert manifest["verification"]["ok"] is True
    assert manifest["records_sha256"] == hashlib.sha256(files["records.jsonl"]).hexdigest()
    assert manifest["keelgate_version"]
    lines = files["records.jsonl"].decode().splitlines()
    assert len(lines) == 20
    assert manifest["head"]["seq"] == 20
    assert json.loads(lines[-1])["hash"] == manifest["head"]["hash"]
    assert "pip install keelgate" in files["VERIFY.md"].decode()
    # the customer's snippet, run as written, accepts the genuine bundle ...
    genuine = tmp_path / "genuine"
    genuine.mkdir()
    assert "ok=True" in run_verify_snippet(files, genuine, monkeypatch)
    # ... and refuses a tampered one (checksum first, hash chain second)
    forged = dict(files)
    forged["records.jsonl"] = files["records.jsonl"].replace(b'\\"n\\":7', b'\\"n\\":8', 1)
    assert forged["records.jsonl"] != files["records.jsonl"]
    bad = tmp_path / "bad"
    bad.mkdir()
    with pytest.raises(AssertionError, match="modified"):
        run_verify_snippet(forged, bad, monkeypatch)
    # an attacker who also fixes the checksum is still caught by the chain itself
    forged_manifest = json.loads(files["manifest.json"])
    forged_manifest["records_sha256"] = hashlib.sha256(forged["records.jsonl"]).hexdigest()
    forged["manifest.json"] = json.dumps(forged_manifest).encode()
    worse = tmp_path / "worse"
    worse.mkdir()
    assert "ok=False" in run_verify_snippet(forged, worse, monkeypatch)
    # and a bundle truncated to fewer records fails against the manifest head
    short = dict(files)
    short["records.jsonl"] = "\n".join(lines[:-1]).encode() + b"\n"
    fixed = json.loads(files["manifest.json"])
    fixed["records_sha256"] = hashlib.sha256(short["records.jsonl"]).hexdigest()
    short["manifest.json"] = json.dumps(fixed).encode()
    truncated = tmp_path / "short"
    truncated.mkdir()
    assert "ok=False" in run_verify_snippet(short, truncated, monkeypatch)
    # the export itself was recorded after the range was fixed
    events = client.get("/api/audit?event_type=audit.exported", headers=org.auth).json()["records"]
    assert events[-1]["payload"] == {
        "format": "compliance",
        "from_seq": 1,
        "to_seq": 20,
        "count": 20,
    }


def test_compliance_export_of_an_empty_chain_and_size_cap(
    client: TestClient, pg_dsns: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(client)
    set_plan(pg_dsns, org, "enterprise")
    files = read_bundle(client.get("/api/audit/export/compliance", headers=org.auth).content)
    manifest = json.loads(files["manifest.json"])
    assert manifest["record_count"] == 0 and manifest["head"] is None
    assert files["records.jsonl"] == b""
    monkeypatch.setattr(exports, "MAX_COMPLIANCE_RECORDS", 0)
    emit(client, org)
    capped = client.get("/api/audit/export/compliance", headers=org.auth)
    assert capped.status_code == 413 and capped.json()["error"]["code"] == "export_too_large"


def test_compliance_bundle_is_per_org(client: TestClient, pg_dsns: dict[str, str]) -> None:
    a, b = Org(client, "A"), Org(client, "B")
    for org in (a, b):
        set_plan(pg_dsns, org, "business")
    emit(client, a, actor="secret-agent-of-a")
    emit(client, b, actor="agent-of-b")
    files = read_bundle(client.get("/api/audit/export/compliance", headers=b.auth).content)
    assert b"secret-agent-of-a" not in files["records.jsonl"]
    assert a.id.encode() not in files["records.jsonl"] + files["manifest.json"]


# ------------------------------------------------------------------------------------ scale


def test_a_5000_record_chain_exports_fast_in_bounded_memory(
    client: TestClient, pg_dsns: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(client)
    seed_chain(pg_dsns, org, 5000)
    pages: list[int] = []
    original = exports._pages

    def counting(*args: Any) -> Any:
        for page in original(*args):
            pages.append(len(page))
            yield page

    monkeypatch.setattr(exports, "_pages", counting)
    monkeypatch.setattr(exports, "PAGE_SIZE", 500)

    started = time.perf_counter()
    tracemalloc.start()
    total = 0
    with client.stream("GET", "/api/audit/export?format=json", headers=org.auth) as response:
        assert response.status_code == 200
        chunks = []
        for chunk in response.iter_bytes():
            total += len(chunk)
            chunks.append(chunk)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    elapsed = time.perf_counter() - started
    assert pages == [500] * 10  # paged, never a whole-chain query
    assert elapsed < 15, elapsed
    assert peak < 12 * 1024 * 1024, peak
    document = json.loads(b"".join(chunks))
    assert document["count"] == 5000
    result = verify_chain(records_of(document), tenant_id=org.id)
    assert result and result.records_checked == 5000

    csv_response = client.get("/api/audit/export?format=csv", headers=org.auth)
    assert csv_response.text.count("\r\n") == 5002
    assert len(csv_records(csv_response.text, org.id)) == 5001
    # the chain a seeded org grows through the API continues from the seeded head
    assert emit(client, org)["seq"] == 5003  # 5001 and 5002 are the exports own events
