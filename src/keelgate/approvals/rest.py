"""REST surface for the approval queue (``pip install 'keelgate[server]'``).

Authentication is a static bearer-token table mapping each token to an
:class:`~keelgate.approvals._models.Approver`. The approver's tenant comes from
that table, **never** from the request, so a caller cannot name another tenant.
This is deliberately minimal: real identity (OIDC, SSO) belongs to the Cloud
control plane and is out of scope for K1.

The ``signoff_code`` is friction for human UIs, so an approver reads the
evidence before typing it. It is not a cryptographic proof of reading. The real
controls are the tier clearance, tenant scoping, separation of duties, binding
to the exact action, single use, and the audit trail.
"""

import hmac
from collections.abc import Mapping
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from keelgate.approvals._models import ApprovalRequest, Approver
from keelgate.approvals._queue import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotAuthorisedError,
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    ApprovalQueue,
    ApprovalSignoffError,
)

_STATUS: dict[type[ApprovalError], int] = {
    ApprovalNotFoundError: 404,
    ApprovalExpiredError: 410,
    ApprovalNotPendingError: 409,
    ApprovalNotAuthorisedError: 403,
    ApprovalSignoffError: 422,
}


class ApproveBody(BaseModel):
    signoff_code: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=1000)


class RejectBody(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


def create_app(queue: ApprovalQueue, tokens: Mapping[str, Approver]) -> FastAPI:
    """Build the approvals API over ``queue``, authenticating with ``tokens``."""
    if not tokens:
        raise ValueError("at least one approver token is required")
    app = FastAPI(title="Keelgate approvals", docs_url=None, redoc_url=None)

    def authenticate(authorization: str | None = Header(default=None)) -> Approver:
        presented = ""
        if authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        match: Approver | None = None
        for token, approver in tokens.items():  # compare against all: no early exit
            if hmac.compare_digest(presented.encode(), token.encode()):
                match = approver
        if match is None:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")
        return match

    @app.exception_handler(ApprovalError)
    async def _approval_error(_: Request, exc: ApprovalError) -> JSONResponse:
        status = next((s for t, s in _STATUS.items() if isinstance(exc, t)), 400)
        return JSONResponse(
            status_code=status,
            content={"error": exc.code, "detail": str(exc)},
            headers={"X-Content-Type-Options": "nosniff"},
        )

    def view(request: ApprovalRequest, *, include_code: bool) -> dict[str, Any]:
        body: dict[str, Any] = request.model_dump(mode="json")
        if include_code:
            body["signoff_code"] = request.signoff_code
        return body

    @app.get("/approvals")
    def list_pending(approver: Approver = Depends(authenticate)) -> list[dict[str, Any]]:
        return [view(r, include_code=False) for r in queue.list_pending(approver.tenant_id)]

    @app.get("/approvals/{request_id}")
    def show(request_id: str, approver: Approver = Depends(authenticate)) -> dict[str, Any]:
        return view(queue.get(approver.tenant_id, request_id), include_code=True)

    @app.post("/approvals/{request_id}/approve")
    def approve(
        request_id: str, body: ApproveBody, approver: Approver = Depends(authenticate)
    ) -> dict[str, Any]:
        done = queue.approve(
            approver.tenant_id,
            request_id,
            approver,
            signoff_code=body.signoff_code,
            note=body.note,
        )
        return view(done, include_code=False)

    @app.post("/approvals/{request_id}/reject")
    def reject(
        request_id: str, body: RejectBody, approver: Approver = Depends(authenticate)
    ) -> dict[str, Any]:
        done = queue.reject(approver.tenant_id, request_id, approver, note=body.note)
        return view(done, include_code=False)

    return app
