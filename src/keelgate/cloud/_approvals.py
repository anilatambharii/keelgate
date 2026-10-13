"""An approval queue backed by a control plane's hosted inbox."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Final

from keelgate.approvals import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotAuthorisedError,
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    ApprovalNotUsableError,
    ApprovalRequest,
    ApprovalSignoffError,
    ApprovalTier,
    EvidenceBundle,
)
from keelgate.cloud._client import CloudClient, CloudError

_ERRORS: Final[dict[str, type[ApprovalError]]] = {
    cls.code: cls
    for cls in (
        ApprovalNotFoundError,
        ApprovalNotPendingError,
        ApprovalExpiredError,
        ApprovalNotAuthorisedError,
        ApprovalSignoffError,
        ApprovalNotUsableError,
    )
}


class CloudApprovalError(ApprovalError):
    """The approval service could not be reached or refused the request."""

    code = "approval_backend_unavailable"


def _raise_for(exc: CloudError) -> ApprovalError:
    cls = _ERRORS.get(exc.code)
    return cls(str(exc)) if cls is not None else CloudApprovalError(str(exc))


class CloudApprovalQueue:
    """Park actions in the control plane's approvals inbox and spend them from there.

    It implements the three calls the tool gateway needs
    (:class:`~keelgate.approvals.ApprovalBackend`).
    Humans decide requests in the dashboard or API; the separation of duties, tier checks,
    expiry and single use are enforced by the server, with the same rules as the local queue.
    """

    def __init__(self, client: CloudClient) -> None:
        self._client = client

    def submit(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        tool_name: str,
        args_hash: str,
        tier: ApprovalTier,
        evidence: EvidenceBundle,
        ttl: timedelta | None = None,
    ) -> ApprovalRequest:
        if tier is ApprovalTier.AUTO:
            raise ValueError("AUTO needs no approval request")
        try:
            body = self._client.request(
                "POST",
                "/v1/approvals",
                json={
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "tool_name": tool_name,
                    "args_hash": args_hash,
                    "tier": tier.value,
                    "evidence": evidence.model_dump(mode="json"),
                    "ttl_seconds": None if ttl is None else int(ttl / timedelta(seconds=1)),
                },
                idempotency_key=uuid.uuid4().hex,
            )
            return ApprovalRequest.model_validate(body)
        except CloudError as exc:
            raise _raise_for(exc) from exc

    def get(self, tenant_id: str, request_id: str) -> ApprovalRequest:
        try:
            body = self._client.request(
                "GET", f"/v1/approvals/{request_id}", params={"tenant_id": tenant_id}
            )
            return ApprovalRequest.model_validate(body)
        except CloudError as exc:
            raise _raise_for(exc) from exc

    def list_pending(self, tenant_id: str) -> list[ApprovalRequest]:
        try:
            body = self._client.request(
                "GET", "/v1/approvals", params={"tenant_id": tenant_id, "status": "PENDING"}
            )
            return [ApprovalRequest.model_validate(item) for item in body["items"]]
        except CloudError as exc:
            raise _raise_for(exc) from exc

    def consume(
        self,
        *,
        tenant_id: str,
        request_id: str,
        agent_id: str,
        tool_name: str,
        args_hash: str,
    ) -> ApprovalRequest:
        try:
            body = self._client.request(
                "POST",
                f"/v1/approvals/{request_id}/consume",
                json={
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "tool_name": tool_name,
                    "args_hash": args_hash,
                },
            )
            return ApprovalRequest.model_validate(body)
        except CloudError as exc:
            raise _raise_for(exc) from exc

    def close(self) -> None:
        self._client.close()
