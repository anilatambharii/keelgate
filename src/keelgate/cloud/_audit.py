"""An :class:`~keelgate.audit.AuditStore` that writes to a control plane."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from keelgate.audit import AuditError, AuditRecord, ChainHead
from keelgate.cloud._client import CloudClient, CloudError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from keelgate.audit._stores import BuildRecord

_PAGE = 500


class CloudAuditError(AuditError):
    """The control plane could not durably record an audit event.

    A subclass of :class:`~keelgate.audit.AuditError`, so the gateway treats it like any audit
    failure and refuses the action rather than acting unrecorded.
    """

    def __init__(self, message: str, *, code: str = "cloud_error") -> None:
        super().__init__(message)
        self.code = code


class CloudAuditStore:
    """Append audit events on the control plane, which chains and stores them.

    The server, not the caller, assigns the sequence number, timestamp and hashes, so a
    compromised agent process cannot forge or reorder its own history. The chain a tenant
    exports can be checked with :func:`keelgate.audit.verify_chain`, which needs nothing from the
    server.
    """

    def __init__(self, client: CloudClient) -> None:
        self._client = client

    def append(self, tenant_id: str, build: BuildRecord) -> AuditRecord:
        # ``build`` is only used to validate the event and read its canonical fields; the sequence
        # number, previous hash and time it is given are placeholders the server replaces.
        draft = build(1, "0" * 64, None)
        try:
            body = self._client.request(
                "POST",
                "/v1/audit/events",
                json={
                    "tenant_id": tenant_id,
                    "event_type": draft.event_type,
                    "actor": draft.actor,
                    "payload_json": draft.payload_json,
                },
                idempotency_key=uuid.uuid4().hex,
            )
            return AuditRecord.model_validate(body)
        except CloudError as exc:
            raise CloudAuditError(f"audit event was not recorded: {exc}", code=exc.code) from exc
        except ValueError as exc:
            raise CloudAuditError("control plane returned a malformed audit record") from exc

    def iter_records(self, tenant_id: str, *, after_seq: int = 0) -> Iterator[AuditRecord]:
        cursor = after_seq
        while True:
            page = self._client.request(
                "GET",
                "/v1/audit/records",
                params={"tenant_id": tenant_id, "after_seq": cursor, "limit": _PAGE},
            )
            records = [AuditRecord.model_validate(r) for r in page["records"]]
            yield from records
            if not records or not page.get("has_more"):
                return
            cursor = records[-1].seq

    def head(self, tenant_id: str) -> ChainHead | None:
        body = self._client.request("GET", "/v1/audit/head", params={"tenant_id": tenant_id})
        if body is None:
            return None
        return ChainHead(tenant_id=tenant_id, seq=int(body["seq"]), hash=str(body["hash"]))

    def close(self) -> None:
        self._client.close()
