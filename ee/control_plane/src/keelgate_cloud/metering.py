"""Usage metering and quota enforcement.

Three metrics are counted, per org and UTC day:

* ``gated_actions``: one per governed tool call, counted when its ``tool.call`` audit event is
  recorded. The count and the audit record commit in the same transaction, so a recorded call is
  always counted exactly once and an unrecorded call is never counted.
* ``traces_stored``: distinct traces accepted (a trace is counted the first time one of its spans
  is stored).
* ``approvals``: approval requests created.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from keelgate_cloud.errors import QuotaExceededError

if TYPE_CHECKING:
    import psycopg

    from keelgate_cloud.plans import Plan

GATED_ACTIONS: Final = "gated_actions"
TRACES_STORED: Final = "traces_stored"
APPROVALS: Final = "approvals"
METRICS: Final = (GATED_ACTIONS, TRACES_STORED, APPROVALS)
GATED_EVENT: Final = "tool.call"
AGENT_EVENT: Final = "policy.decision"


def period_of(moment: datetime | date) -> str:
    """The billing period (calendar month, UTC) as ``YYYY-MM``."""
    return f"{moment.year:04d}-{moment.month:02d}"


def period_bounds(period: str) -> tuple[date, date]:
    """First day of the period and first day of the next one."""
    year, month = int(period[:4]), int(period[5:7])
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def today() -> date:
    """Today in UTC."""
    return datetime.now(UTC).date()


def record(
    conn: psycopg.Connection[Any],
    org_id: str,
    metric: str,
    quantity: int = 1,
    *,
    day: date | None = None,
) -> None:
    """Add to a metric. Must run inside the org's own transaction."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    if quantity < 0:
        raise ValueError("usage cannot go down")
    conn.execute(
        "INSERT INTO usage_daily (org_id, day, metric, quantity) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (org_id, day, metric) DO UPDATE "
        "SET quantity = usage_daily.quantity + EXCLUDED.quantity",
        (org_id, day or today(), metric, quantity),
    )


def used(conn: psycopg.Connection[Any], period: str, metric: str) -> int:
    """Total of a metric in a period for the transaction's org."""
    start, end = period_bounds(period)
    row = conn.execute(
        "SELECT COALESCE(sum(quantity), 0)::bigint AS n FROM usage_daily "
        "WHERE metric = %s AND day >= %s AND day < %s",
        (metric, start, end),
    ).fetchone()
    return int(row["n"]) if row else 0


def usage_summary(conn: psycopg.Connection[Any], period: str) -> dict[str, int]:
    """All metrics for a period."""
    return {metric: used(conn, period, metric) for metric in METRICS}


def enforce_action_quota(conn: psycopg.Connection[Any], plan: Plan, period: str) -> None:
    """Refuse a new gated action once the plan's cap is used up.

    Callers hold the org's audit lock, so concurrent calls cannot overshoot the cap.
    """
    cap = plan.included_gated_actions
    if cap is not None and used(conn, period, GATED_ACTIONS) >= cap:
        raise QuotaExceededError(
            f"the {plan.name} plan includes {cap:,} gated actions a month and they are used up"
        )


def enforce_agent_cap(
    conn: psycopg.Connection[Any], org_id: str, plan: Plan, agent_id: str
) -> None:
    """Register an agent id, refusing a new one past the plan's agent cap."""
    seen = conn.execute("SELECT 1 FROM agents WHERE agent_id = %s", (agent_id,)).fetchone()
    if seen:
        return
    if plan.max_agents is not None:
        count = conn.execute("SELECT count(*) AS n FROM agents").fetchone()
        if count is not None and count["n"] >= plan.max_agents:
            raise QuotaExceededError(
                f"the {plan.name} plan allows {plan.max_agents} agents", code="agent_cap_reached"
            )
    conn.execute(
        "INSERT INTO agents (org_id, agent_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (org_id, agent_id),
    )
