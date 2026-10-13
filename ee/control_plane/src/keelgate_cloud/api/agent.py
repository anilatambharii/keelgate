"""The agent-facing API (``/v1``), authenticated with an API key.

This is the protocol ``keelgate.cloud`` speaks (``docs/cloud-protocol.md``). The key names the
tenant; a request that names another tenant is refused.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Final

from fastapi import APIRouter, Header, Query
from keelgate.approvals import (
    ApprovalError,
    ApprovalNotAuthorisedError,
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    ApprovalNotUsableError,
    ApprovalTier,
    EvidenceBundle,
)
from keelgate.audit import AuditError, AuditLog, EventType
from pydantic import BaseModel, ConfigDict, Field

from keelgate_cloud import metering
from keelgate_cloud.api.deps import Agent, State, ensure_tenant
from keelgate_cloud.api.idempotency import claim, complete
from keelgate_cloud.api.routing import effective_tier
from keelgate_cloud.errors import (
    ApiError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)

if TYPE_CHECKING:
    from keelgate.audit import AuditRecord

router = APIRouter(prefix="/v1", tags=["agent"])

# Events a harness emits. Approval events are written by the server itself, never accepted here.
CLIENT_EVENTS: Final = frozenset(e.value for e in EventType) - {
    EventType.APPROVAL_REQUESTED.value,
    EventType.APPROVAL_DECIDED.value,
    EventType.APPROVAL_CONSUMED.value,
}
MAX_SPANS_PER_BATCH: Final = 1000
MAX_ATTRIBUTE_BYTES: Final = 16_384
MAX_TTL_SECONDS: Final = 7 * 24 * 3600
MAX_PAGE: Final = 1000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/whoami")
def whoami(principal: Agent, state: State) -> dict[str, Any]:
    """Which tenant and plan this API key belongs to (so a client need not be told its tenant)."""
    plan = state.plan_of(principal.org_id)
    return {"tenant_id": principal.org_id, "key_id": principal.api_key_id, "plan": plan.id}


# ------------------------------------------------------------------------------------- audit


class AuditEventIn(_Body):
    tenant_id: str
    event_type: str = Field(min_length=1, max_length=64)
    actor: str = Field(min_length=1, max_length=256)
    payload_json: str = Field(max_length=200_000)


class _KeyedStore:
    """Hands the request idempotency key to the store, so a retried append is not repeated."""

    def __init__(self, base: Any, key: str | None) -> None:
        self._base, self._key = base, key

    def append(self, tenant_id: str, build: Any) -> AuditRecord:
        record: AuditRecord = self._base.append(tenant_id, build, idempotency_key=self._key)
        return record


@router.post("/audit/events")
def append_event(
    body: AuditEventIn,
    principal: Agent,
    state: State,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> dict[str, Any]:
    """Chain and store one event. The server assigns sequence number, time and hashes."""
    org_id = ensure_tenant(principal, body.tenant_id)
    if body.event_type not in CLIENT_EVENTS:
        raise ValidationFailedError(
            f"event type {body.event_type!r} is not accepted from agents", code="event_not_accepted"
        )
    try:
        payload = json.loads(body.payload_json)
    except ValueError as exc:
        raise ValidationFailedError("payload_json is not valid JSON") from exc
    log = AuditLog(_KeyedStore(state.audit_store, idempotency_key))  # type: ignore[arg-type]
    try:
        record = log.append(
            tenant_id=org_id, event_type=body.event_type, actor=body.actor, payload=payload
        )
    except AuditError as exc:
        raise ValidationFailedError(str(exc), code="audit_rejected") from exc
    return record.model_dump()


@router.get("/audit/records")
def list_records(
    principal: Agent,
    state: State,
    tenant_id: str | None = None,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 500,
) -> dict[str, Any]:
    """A page of the tenant's chain, oldest first."""
    org_id = ensure_tenant(principal, tenant_id)
    with state.db.tx(org_id) as conn:
        rows = conn.execute(
            "SELECT seq, timestamp, event_type, actor, payload_json, prev_hash, hash "
            "FROM audit_records WHERE seq > %s ORDER BY seq LIMIT %s",
            (after_seq, limit + 1),
        ).fetchall()
    page = rows[:limit]
    return {
        "records": [{"tenant_id": org_id, **r} for r in page],
        "has_more": len(rows) > limit,
    }


@router.get("/audit/head")
def audit_head(
    principal: Agent, state: State, tenant_id: str | None = None
) -> dict[str, Any] | None:
    """The newest record's sequence number and hash, or ``null`` for an empty chain."""
    org_id = ensure_tenant(principal, tenant_id)
    head = state.audit_store.head(org_id)
    return None if head is None else {"seq": head.seq, "hash": head.hash}


# --------------------------------------------------------------------------------- approvals


class ApprovalIn(_Body):
    tenant_id: str
    agent_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    args_hash: str = Field(min_length=64, max_length=64)
    tier: ApprovalTier
    evidence: dict[str, Any]
    ttl_seconds: int | None = Field(default=None, ge=1, le=MAX_TTL_SECONDS)


class ConsumeIn(_Body):
    tenant_id: str
    agent_id: str
    tool_name: str
    args_hash: str


def _approval_error(exc: ApprovalError) -> ApiError:
    mapping: dict[type[ApprovalError], type[ApiError]] = {
        ApprovalNotFoundError: NotFoundError,
        ApprovalNotPendingError: ConflictError,
        ApprovalNotUsableError: ConflictError,
        ApprovalNotAuthorisedError: ApiError,
    }
    cls = mapping.get(type(exc), ApiError)
    error = cls(str(exc), code=exc.code)
    if cls is ApiError:
        error.status = 403 if isinstance(exc, ApprovalNotAuthorisedError) else 400
    return error


@router.post("/approvals")
def submit_approval(
    body: ApprovalIn,
    principal: Agent,
    state: State,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> dict[str, Any]:
    """Park an action in the tenant's inbox."""
    org_id = ensure_tenant(principal, body.tenant_id)
    if body.tier is ApprovalTier.AUTO:
        raise ValidationFailedError("AUTO needs no approval request")
    if idempotency_key is not None:
        stored = claim(state, org_id, "approval", idempotency_key)
        if stored is not None:
            return stored
    try:
        evidence = EvidenceBundle.model_validate(body.evidence)
        tier = effective_tier(state, org_id, body.tool_name, body.tier)
        request = state.approvals.submit(
            tenant_id=org_id,
            agent_id=body.agent_id,
            tool_name=body.tool_name,
            args_hash=body.args_hash,
            tier=tier,
            evidence=evidence,
            ttl=None if body.ttl_seconds is None else timedelta(seconds=body.ttl_seconds),
        )
    except ValueError as exc:
        raise ValidationFailedError(f"invalid approval request: {exc}") from exc
    except ApprovalError as exc:
        raise _approval_error(exc) from exc
    result: dict[str, Any] = request.model_dump(mode="json")
    if idempotency_key is not None:
        complete(state, org_id, "approval", idempotency_key, result)
    return result


@router.get("/approvals")
def list_approvals(
    principal: Agent, state: State, tenant_id: str | None = None, status: str = "PENDING"
) -> dict[str, Any]:
    """The tenant's requests in a state (default: pending)."""
    org_id = ensure_tenant(principal, tenant_id)
    items = state.approval_store.list_all(org_id, status=status.upper())
    return {"items": [r.model_dump(mode="json") for r in items]}


@router.get("/approvals/{request_id}")
def get_approval(
    request_id: str, principal: Agent, state: State, tenant_id: str | None = None
) -> dict[str, Any]:
    """One request. Another tenant's request is simply not found."""
    org_id = ensure_tenant(principal, tenant_id)
    try:
        return state.approvals.get(org_id, request_id).model_dump(mode="json")
    except ApprovalError as exc:
        raise _approval_error(exc) from exc


@router.post("/approvals/{request_id}/consume")
def consume_approval(
    request_id: str, body: ConsumeIn, principal: Agent, state: State
) -> dict[str, Any]:
    """Spend an approved request, once, for exactly this action."""
    org_id = ensure_tenant(principal, body.tenant_id)
    try:
        spent = state.approvals.consume(
            tenant_id=org_id,
            request_id=request_id,
            agent_id=body.agent_id,
            tool_name=body.tool_name,
            args_hash=body.args_hash,
        )
    except ApprovalError as exc:
        raise _approval_error(exc) from exc
    return spent.model_dump(mode="json")


# ------------------------------------------------------------------------------------ traces


class SpanIn(_Body):
    trace_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str = Field(min_length=1, max_length=256)
    start_ns: int | None = None
    end_ns: int | None = None
    status: str = Field(max_length=16)
    attributes: dict[str, Any] = Field(default_factory=dict)


class TracesIn(_Body):
    spans: list[SpanIn] = Field(max_length=MAX_SPANS_PER_BATCH)


@router.post("/traces")
def ingest_traces(body: TracesIn, principal: Agent, state: State) -> dict[str, int]:
    """Store spans. A trace is metered the first time one of its spans is accepted."""
    org_id = principal.org_id
    accepted = 0
    with state.db.tx(org_id) as conn:
        known = {
            r["trace_id"]
            for r in conn.execute(
                "SELECT DISTINCT trace_id FROM traces WHERE trace_id = ANY(%s)",
                ([s.trace_id for s in body.spans],),
            ).fetchall()
        }
        new_traces = set()
        for span in body.spans:
            attributes = json.dumps(span.attributes, default=str)
            if len(attributes) > MAX_ATTRIBUTE_BYTES:
                attributes = json.dumps({"truncated": True})
            inserted = conn.execute(
                "INSERT INTO traces (org_id, trace_id, span_id, parent_span_id, name, start_ns, "
                "end_ns, status, attributes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    org_id,
                    span.trace_id,
                    span.span_id,
                    span.parent_span_id,
                    span.name,
                    span.start_ns,
                    span.end_ns,
                    span.status,
                    attributes,
                ),
            )
            if inserted.rowcount:
                accepted += 1
                if span.trace_id not in known:
                    new_traces.add(span.trace_id)
        if new_traces:
            metering.record(conn, org_id, metering.TRACES_STORED, len(new_traces))
    return {"accepted": accepted}
