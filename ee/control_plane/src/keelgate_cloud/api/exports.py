"""Audit browser and exports: JSON, CSV and the Business-plan compliance bundle.

Exports stream from the database in keyset-paginated pages, so a large chain is never held in
memory. They carry the *exact* stored fields of every record, so the open-source verifier
(``keelgate.audit.verify_chain``) accepts them unchanged.

An ``audit.exported`` event is appended to the org chain *before* any data is sent, after the
exported range (``to_seq``) has been fixed. The event therefore never belongs to the export it
describes, and an export that cannot be recorded is not served.

CSV and formula injection: every column of a record is an input to its hash, so altering a cell
would stop the export from re-verifying. Only ``event_type`` and ``actor`` can hold free text that
a spreadsheet might evaluate (``seq``, ``timestamp`` and the hashes are digits and hex;
``payload_json`` is canonical JSON, which starts with ``{ [ "``, a digit, ``-`` before a number,
``t``, ``f`` or ``n``, never ``=``, ``+`` or ``@``). Those two cells are therefore prefixed with a
single quote when they begin with ``= + - @``, tab or CR, and the ``csv_escaped`` column names which
cells of that row were prefixed (comma separated, empty when none). To rebuild a record, strip the
first character of each named cell. The ``x-csv-note`` response header says the same.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from keelgate import __version__ as keelgate_version
from keelgate.audit import AuditRecord, ChainHead, verify_chain

from keelgate_cloud.api.deps import State, User, require_feature, require_roles
from keelgate_cloud.errors import ApiError, ValidationFailedError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from keelgate_cloud.identity import Principal
    from keelgate_cloud.state import AppState

router = APIRouter(prefix="/api/audit", tags=["audit"])

PAGE_SIZE: Final = 1000
MAX_COMPLIANCE_RECORDS: Final = 5_000_000
CSV_TRIGGERS: Final = ("=", "+", "-", "@", "\t", "\r")
CSV_COLUMNS: Final = (
    "tenant_id",
    "seq",
    "timestamp",
    "event_type",
    "actor",
    "payload_json",
    "prev_hash",
    "hash",
    "csv_escaped",
)
CSV_ESCAPED_FIELDS: Final = ("event_type", "actor")
CSV_NOTE: Final = (
    "cells in event_type/actor starting with = + - @ tab or CR are prefixed with a single quote; "
    "the csv_escaped column lists them; strip the first character of each listed cell to restore "
    "the stored value; payload_json, seq, timestamp and hashes are never altered"
)
_COLUMNS_SQL: Final = "seq, timestamp, event_type, actor, payload_json, prev_hash, hash"


class ExportTooLargeError(ApiError):
    """The chain is longer than the compliance bundle will build in one request."""

    status = 413
    code = "export_too_large"


# --------------------------------------------------------------------------------- shared reads


def _pages(state: AppState, org_id: str, first: int, last: int) -> Iterator[list[dict[str, Any]]]:
    """Rows ``first..last`` in order, one page (one short transaction) at a time."""
    cursor = first - 1
    while cursor < last:
        with state.db.tx(org_id) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS_SQL} FROM audit_records "
                "WHERE seq > %s AND seq <= %s ORDER BY seq LIMIT %s",
                (cursor, last, PAGE_SIZE),
            ).fetchall()
        if not rows:
            return
        yield rows
        cursor = rows[-1]["seq"]


def _records(state: AppState, org_id: str, first: int, last: int) -> Iterator[AuditRecord]:
    for page in _pages(state, org_id, first, last):
        for row in page:
            yield AuditRecord(tenant_id=org_id, **row)


def _record_dict(org_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": org_id,
        "seq": row["seq"],
        "timestamp": row["timestamp"],
        "event_type": row["event_type"],
        "actor": row["actor"],
        "payload_json": row["payload_json"],
        "prev_hash": row["prev_hash"],
        "hash": row["hash"],
    }


def _resolve_range(
    state: AppState, org_id: str, from_seq: int | None, to_seq: int | None
) -> tuple[int, int, int, dict[str, Any] | None]:
    """Fix the exported range now: ``(first, last, count, head)``; head is the last record."""
    if from_seq is not None and to_seq is not None and from_seq > to_seq:
        raise ValidationFailedError("from_seq must not be greater than to_seq")
    with state.db.tx(org_id) as conn:
        top = conn.execute("SELECT max(seq) AS seq FROM audit_records").fetchone()
        newest = int(top["seq"]) if top and top["seq"] is not None else 0
        first = from_seq or 1
        last = min(to_seq, newest) if to_seq is not None else newest
        if last < first:
            return first, max(last, 0), 0, None
        row = conn.execute("SELECT seq, hash FROM audit_records WHERE seq = %s", (last,)).fetchone()
    assert row is not None  # noqa: S101 - the chain is contiguous and append-only
    return first, last, last - first + 1, {"seq": row["seq"], "hash": row["hash"]}


def _record_export(state: AppState, principal: Principal, payload: dict[str, Any]) -> None:
    """Append the ``audit.exported`` event. Raises (and so refuses the export) if it cannot."""
    state.audit.append(
        tenant_id=principal.org_id,
        event_type="audit.exported",
        actor=principal.actor,
        payload=payload,
    )


def _download_headers(filename: str, **extra: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        **extra,
    }


# --------------------------------------------------------------------------------- browser


@router.get("")
def browse(
    principal: User,
    state: State,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    event_type: Annotated[str | None, Query(max_length=200)] = None,
    actor: Annotated[str | None, Query(max_length=500)] = None,
    newest_first: bool = False,
) -> dict[str, Any]:
    """Browse the org's chain. ``after_seq`` is a cursor: records after it (ascending) or, with
    ``newest_first``, records before it (descending; 0 means start from the newest).
    """
    if newest_first:
        seq_filter = "(%(after)s = 0 OR seq < %(after)s)"
        order = "DESC"
    else:
        seq_filter = "seq > %(after)s"
        order = "ASC"
    with state.db.tx(principal.org_id) as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS_SQL} FROM audit_records "
            f"WHERE {seq_filter} "
            "AND (%(event_type)s::text IS NULL OR event_type = %(event_type)s) "
            "AND (%(actor)s::text IS NULL OR actor = %(actor)s) "
            f"ORDER BY seq {order} LIMIT %(limit)s",
            {"after": after_seq, "event_type": event_type, "actor": actor, "limit": limit + 1},
        ).fetchall()
    has_more = len(rows) > limit
    records = []
    for row in rows[:limit]:
        try:
            payload: Any = json.loads(row["payload_json"])
        except ValueError:
            payload = row["payload_json"]
        records.append(
            {
                "seq": row["seq"],
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "payload": payload,
                "hash": row["hash"],
                "prev_hash": row["prev_hash"],
            }
        )
    return {"records": records, "has_more": has_more}


# --------------------------------------------------------------------------------- JSON / CSV


def _json_stream(
    state: AppState, org_id: str, header: dict[str, Any], first: int, last: int
) -> Iterator[bytes]:
    opening = json.dumps(header)[:-1] + ', "records": ['
    yield opening.encode()
    separator = ""
    for page in _pages(state, org_id, first, last):
        chunk = []
        for row in page:
            chunk.append(separator + json.dumps(_record_dict(org_id, row)))
            separator = ","
        yield "".join(chunk).encode()
    yield b"]}"


def _csv_cell(value: str) -> tuple[str, bool]:
    if value.startswith(CSV_TRIGGERS):
        return "'" + value, True
    return value, False


def _csv_stream(state: AppState, org_id: str, first: int, last: int) -> Iterator[bytes]:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    def drain() -> bytes:
        data = buffer.getvalue().encode()
        buffer.seek(0)
        buffer.truncate()
        return data

    writer.writerow(CSV_COLUMNS)
    yield drain()
    for page in _pages(state, org_id, first, last):
        for row in page:
            cells: dict[str, str] = {}
            escaped: list[str] = []
            for name in CSV_ESCAPED_FIELDS:
                cells[name], changed = _csv_cell(row[name])
                if changed:
                    escaped.append(name)
            writer.writerow(
                [
                    org_id,
                    row["seq"],
                    row["timestamp"],
                    cells["event_type"],
                    cells["actor"],
                    row["payload_json"],
                    row["prev_hash"],
                    row["hash"],
                    ",".join(escaped),
                ]
            )
        yield drain()


@router.get("/export", dependencies=[require_roles("owner", "admin", "approver")])
def export(
    principal: User,
    state: State,
    format: Literal["json", "csv"] = "json",
    from_seq: Annotated[int | None, Query(ge=1)] = None,
    to_seq: Annotated[int | None, Query(ge=1)] = None,
) -> StreamingResponse:
    """Stream the chain (or a range of it) as JSON or CSV. Viewers cannot export."""
    org_id = principal.org_id
    first, last, count, head = _resolve_range(state, org_id, from_seq, to_seq)
    _record_export(
        state, principal, {"format": format, "from_seq": first, "to_seq": last, "count": count}
    )
    stamp = datetime.now(UTC)
    if format == "csv":
        return StreamingResponse(
            _csv_stream(state, org_id, first, last),
            media_type="text/csv; charset=utf-8",
            headers=_download_headers(f"audit-{org_id}.csv", **{"X-CSV-Note": CSV_NOTE}),
        )
    header = {
        "org_id": org_id,
        "generated_at": stamp.isoformat(),
        "from_seq": first,
        "to_seq": last,
        "count": count,
        "head": head,
    }
    return StreamingResponse(
        _json_stream(state, org_id, header, first, last),
        media_type="application/json",
        headers=_download_headers(f"audit-{org_id}.json"),
    )


# --------------------------------------------------------------------------------- compliance

VERIFY_MD: Final = """# Verifying this audit export independently

This bundle holds your organisation's complete, hash-chained audit log, as stored.

Files:

- `records.jsonl`: one audit record per line, exactly as stored (including `payload_json`).
- `manifest.json`: the chain head, record count, the SHA-256 of `records.jsonl`, the retention
  terms and the result of the server's own verification.
- `VERIFY.md`: this file.

Re-verify without trusting Keelgate Cloud:

    pip install keelgate

then run this in the folder holding the unzipped files:

```python
import hashlib, json
from keelgate.audit import AuditRecord, ChainHead, verify_chain
m = json.load(open("manifest.json", encoding="utf-8"))
raw = open("records.jsonl", "rb").read()
assert hashlib.sha256(raw).hexdigest() == m["records_sha256"], "records.jsonl was modified"
recs = [AuditRecord(**json.loads(x)) for x in raw.decode("utf-8").splitlines() if x.strip()]
head = ChainHead(m["org_id"], m["head"]["seq"], m["head"]["hash"]) if m["head"] else None
print(verify_chain(recs, tenant_id=m["org_id"], expected_head=head))
```

`ok=True` means every record links to its predecessor from genesis, every hash recomputes, and the
chain ends at the head named in the manifest. Anchor the manifest head somewhere Keelgate cannot
write (a ticket, your own log) to detect later rewrites or truncation.

Audit records are never deleted by retention sweeps: the retention figure in the manifest is a
guaranteed minimum, not a deletion schedule.
"""


def _build_bundle(
    state: AppState, principal: Principal, last: int, head: dict[str, Any] | None
) -> tuple[str, int]:
    """Write records.jsonl, verify the chain over the whole range, build the zip. Returns the
    zip's path and size. Everything goes to temp files, one record in memory at a time.
    """
    org_id = principal.org_id
    plan = state.plan_of(org_id)
    org = state.identity.get_org(org_id)
    digest = hashlib.sha256()
    fd, jsonl_path = tempfile.mkstemp(suffix=".jsonl")
    zip_fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(zip_fd)
    try:
        with os.fdopen(fd, "wb") as out:

            def tee() -> Iterator[AuditRecord]:
                for record in _records(state, org_id, 1, last):
                    line = (json.dumps(record.model_dump()) + "\n").encode()
                    out.write(line)
                    digest.update(line)
                    yield record

            anchor = ChainHead(org_id, head["seq"], head["hash"]) if head else None
            result = verify_chain(tee(), tenant_id=org_id, expected_head=anchor)
        manifest = {
            "org_id": org_id,
            "org_name": org["name"],
            "generated_at": datetime.now(UTC).isoformat(),
            "plan": plan.id,
            "record_count": result.records_checked,
            "head": head,
            "retention": {
                "audit_min_retention_days": plan.audit_min_retention_days,
                "audit_immutable": plan.audit_immutable,
            },
            "verification": {
                "ok": result.ok,
                "records_checked": result.records_checked,
                "bad_seq": result.bad_seq,
                "error": result.error,
            },
            "records_sha256": digest.hexdigest(),
            "keelgate_version": keelgate_version,
        }
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(jsonl_path, "records.jsonl")
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            bundle.writestr("VERIFY.md", VERIFY_MD)
        return zip_path, os.path.getsize(zip_path)
    except BaseException:
        os.unlink(zip_path)
        raise
    finally:
        os.unlink(jsonl_path)


def _file_stream(path: str) -> Iterator[bytes]:
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(64 * 1024):
                yield chunk
    finally:
        os.unlink(path)


@router.get("/export/compliance", dependencies=[require_roles("owner", "admin", "approver")])
def export_compliance(principal: User, state: State) -> StreamingResponse:
    """A ZIP with the whole chain, a manifest and independent-verification instructions."""
    org_id = principal.org_id
    require_feature(state, org_id, "compliance_exports")
    first, last, count, head = _resolve_range(state, org_id, None, None)
    if count > MAX_COMPLIANCE_RECORDS:
        raise ExportTooLargeError(
            f"the chain has {count:,} records; export it in ranges with /api/audit/export"
        )
    path, size = _build_bundle(state, principal, last, head)
    try:
        _record_export(
            state,
            principal,
            {"format": "compliance", "from_seq": first, "to_seq": last, "count": count},
        )
    except BaseException:
        os.unlink(path)
        raise
    return StreamingResponse(
        _file_stream(path),
        media_type="application/zip",
        headers=_download_headers(
            f"audit-compliance-{org_id}.zip", **{"Content-Length": str(size)}
        ),
    )
