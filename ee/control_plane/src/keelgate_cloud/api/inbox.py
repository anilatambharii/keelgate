"""The hosted approvals inbox and the org approval routing rules (dashboard API)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from keelgate.approvals import ApprovalError, ApprovalTier, Approver
from pydantic import BaseModel, ConfigDict, Field

from keelgate_cloud.api.agent import _approval_error
from keelgate_cloud.api.deps import State, User, require_roles
from keelgate_cloud.api.routing import check_decider
from keelgate_cloud.errors import NotFoundError, ValidationFailedError
from keelgate_cloud.identity import ROLES

router = APIRouter(prefix="/api", tags=["approvals"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionIn(_Body):
    note: str | None = Field(default=None, max_length=1000)
    signoff_code: str | None = Field(default=None, max_length=64)


class RouteIn(_Body):
    priority: int = Field(default=100, ge=0, le=10_000)
    tool_pattern: str = Field(default="*", min_length=1, max_length=128)
    min_tier: str = "ONE_CLICK"
    allowed_roles: list[str] = Field(default_factory=lambda: ["owner", "admin", "approver"])
    escalate_to: str | None = None
    enabled: bool = True


def _view(request: Any, *, show_code: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = request.model_dump(mode="json")
    explicit = request.tier is ApprovalTier.EXPLICIT_SIGNOFF
    data["signoff_code_required"] = explicit
    # Shown to the signed-in approver, as the CLI does: typing it back proves they read it.
    data["signoff_code"] = request.signoff_code if explicit and show_code else None
    return data


@router.get("/approvals")
def list_inbox(
    principal: User,
    state: State,
    status: Annotated[str | None, Query(max_length=16)] = "PENDING",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Requests in the inbox, newest first. ``status=ALL`` lists every state."""
    wanted = None if status is None or status.upper() == "ALL" else status.upper()
    items = state.approval_store.list_all(principal.org_id, status=wanted, limit=limit)
    if wanted == "PENDING":
        # Expiry is applied lazily on read: refresh so the inbox never offers an expired request.
        items = [state.approvals.get(principal.org_id, r.request_id) for r in items]
        items = [r for r in items if r.status.value == "PENDING"]
    show = principal.role != "viewer"
    return {"items": [_view(r, show_code=show) for r in items]}


@router.get("/approvals/{request_id}")
def get_request(request_id: str, principal: User, state: State) -> dict[str, Any]:
    """One request with its evidence."""
    try:
        return _view(
            state.approvals.get(principal.org_id, request_id),
            show_code=principal.role != "viewer",
        )
    except ApprovalError as exc:
        raise _approval_error(exc) from exc


def _decide(
    request_id: str, principal: Any, state: Any, body: DecisionIn, *, approve: bool
) -> dict[str, Any]:
    try:
        request = state.approvals.get(principal.org_id, request_id)
        check_decider(state, principal, request.tool_name, request.tier)
        approver = Approver(
            approver_id=principal.actor,
            tenant_id=principal.org_id,
            max_tier=ApprovalTier(principal.approval_tier),
        )
        if approve:
            done = state.approvals.approve(
                principal.org_id,
                request_id,
                approver,
                signoff_code=body.signoff_code,
                note=body.note,
            )
        else:
            done = state.approvals.reject(principal.org_id, request_id, approver, note=body.note)
    except ApprovalError as exc:
        raise _approval_error(exc) from exc
    return _view(done)


@router.post("/approvals/{request_id}/approve")
def approve(request_id: str, body: DecisionIn, principal: User, state: State) -> dict[str, Any]:
    """Approve a pending request. The agent that asked can never be the approver."""
    return _decide(request_id, principal, state, body, approve=True)


@router.post("/approvals/{request_id}/reject")
def reject(request_id: str, body: DecisionIn, principal: User, state: State) -> dict[str, Any]:
    """Reject a pending request."""
    return _decide(request_id, principal, state, body, approve=False)


# ------------------------------------------------------------------------------ routing rules


def _validate_route(body: RouteIn) -> None:
    if body.min_tier not in ("ONE_CLICK", "EXPLICIT_SIGNOFF"):
        raise ValidationFailedError("unknown min_tier")
    if body.escalate_to not in (None, "ONE_CLICK", "EXPLICIT_SIGNOFF"):
        raise ValidationFailedError("unknown escalate_to")
    if not body.allowed_roles or any(r not in ROLES for r in body.allowed_roles):
        raise ValidationFailedError("allowed_roles must be a non-empty list of known roles")


def _route_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "id": str(row["id"]), "org_id": str(row["org_id"])}


@router.get("/approval-routes", dependencies=[require_roles("owner", "admin")])
def list_routes(principal: User, state: State) -> dict[str, Any]:
    """The org routing rules in evaluation order."""
    with state.db.tx(principal.org_id) as conn:
        rows = conn.execute(
            "SELECT * FROM approval_routes ORDER BY priority, created_at"
        ).fetchall()
    return {"items": [_route_view(r) for r in rows]}


@router.post("/approval-routes", status_code=201, dependencies=[require_roles("owner", "admin")])
def create_route(body: RouteIn, principal: User, state: State) -> dict[str, Any]:
    """Add a routing rule."""
    _validate_route(body)
    with state.db.tx(principal.org_id) as conn:
        row = conn.execute(
            "INSERT INTO approval_routes (org_id, priority, tool_pattern, min_tier, allowed_roles, "
            "escalate_to, enabled) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                principal.org_id,
                body.priority,
                body.tool_pattern,
                body.min_tier,
                body.allowed_roles,
                body.escalate_to,
                body.enabled,
            ),
        ).fetchone()
    assert row is not None  # noqa: S101
    return _route_view(row)


@router.delete(
    "/approval-routes/{route_id}", status_code=204, dependencies=[require_roles("owner", "admin")]
)
def delete_route(route_id: str, principal: User, state: State) -> Response:
    """Remove a routing rule."""
    with state.db.tx(principal.org_id) as conn:
        deleted = conn.execute("DELETE FROM approval_routes WHERE id = %s", (route_id,))
    if not deleted.rowcount:
        raise NotFoundError("no such routing rule")
    return Response(status_code=204)
