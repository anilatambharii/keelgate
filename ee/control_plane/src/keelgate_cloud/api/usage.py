"""Usage and plan limits, and the trace explorer (dashboard API)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from keelgate_cloud import metering
from keelgate_cloud.api.deps import State, User
from keelgate_cloud.errors import NotFoundError, ValidationFailedError

router = APIRouter(prefix="/api", tags=["usage"])


@router.get("/usage")
def usage(
    principal: User,
    state: State,
    period: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
) -> dict[str, Any]:
    """This period usage against the plan limits (default: the current month)."""
    period = period or metering.period_of(metering.today())
    plan = state.plan_of(principal.org_id)
    with state.db.tx(principal.org_id) as conn:
        counts = metering.usage_summary(conn, period)
        agents = conn.execute("SELECT count(*) AS n FROM agents").fetchone()
        daily = conn.execute(
            "SELECT day, metric, quantity FROM usage_daily WHERE day >= %s AND day < %s "
            "ORDER BY day",
            metering.period_bounds(period),
        ).fetchall()
    cap = plan.included_gated_actions
    return {
        "period": period,
        "plan": plan.id,
        "usage": counts,
        "agents": agents["n"] if agents else 0,
        "limits": {
            "gated_actions": cap,
            "max_agents": plan.max_agents,
            "trace_retention_days": plan.trace_retention_days,
            "audit_min_retention_days": plan.audit_min_retention_days,
            "rate_limit_per_minute": plan.rate_limit_per_minute,
        },
        "gated_actions_remaining": None if cap is None else max(cap - counts["gated_actions"], 0),
        "daily": [
            {"day": d["day"].isoformat(), "metric": d["metric"], "quantity": d["quantity"]}
            for d in daily
        ],
    }


@router.get("/traces")
def list_traces(
    principal: User,
    state: State,
    q: Annotated[str | None, Query(max_length=100)] = None,
    errors_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Traces, newest first, each summarised by its root span."""
    with state.db.tx(principal.org_id) as conn:
        rows = conn.execute(
            "SELECT trace_id, count(*) AS spans, min(start_ns) AS start_ns, max(end_ns) AS end_ns, "
            "bool_or(status = 'ERROR') AS has_error, max(ingested_at) AS ingested_at, "
            "(array_agg(name ORDER BY (parent_span_id IS NOT NULL), start_ns))[1] AS root_name "
            "FROM traces GROUP BY trace_id "
            "HAVING (%(q)s::text IS NULL OR bool_or(name ILIKE '%%' || %(q)s || '%%')) "
            "AND (NOT %(errs)s OR bool_or(status = 'ERROR')) "
            "ORDER BY max(ingested_at) DESC, trace_id LIMIT %(limit)s OFFSET %(offset)s",
            {"q": q, "errs": errors_only, "limit": limit, "offset": offset},
        ).fetchall()
    return {"items": rows}


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, principal: User, state: State) -> dict[str, Any]:
    """Every span of one trace, in start order."""
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id):
        raise ValidationFailedError("trace ids are 32 lowercase hex characters")
    with state.db.tx(principal.org_id) as conn:
        spans = conn.execute(
            "SELECT trace_id, span_id, parent_span_id, name, start_ns, end_ns, status, attributes "
            "FROM traces WHERE trace_id = %s ORDER BY start_ns NULLS LAST, span_id",
            (trace_id,),
        ).fetchall()
    if not spans:
        raise NotFoundError("no such trace")
    return {"trace_id": trace_id, "spans": spans}
