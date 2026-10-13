"""Retention sweeping: ``python -m keelgate_cloud sweep-retention``.

Deleted, per org and in small batches:

* traces older than the org plan's ``trace_retention_days`` (null keeps them);
* expired sessions;
* SSO login states older than an hour;
* idempotency keys older than seven days.

**Audit records are never deleted.** The table is append-only (a trigger forbids UPDATE, DELETE
and TRUNCATE) and ``keelgate.audit.verify_chain`` must keep working from genesis. A plan's
``audit_min_retention_days`` is therefore a guaranteed *minimum*: records are kept at least that
long, in practice indefinitely.

Org enumeration: the application role cannot read other orgs (row-level security), so the sweeper
reads ``org_index`` (migration 0002), a PII-free table of org ids and plans kept current by a
trigger, so no policy on ``orgs`` is loosened. Every delete then runs inside ``db.tx(org_id)``,
under that org's own RLS scope.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from keelgate_cloud.plans import default_catalogue

if TYPE_CHECKING:
    from keelgate_cloud.db import Database
    from keelgate_cloud.plans import PlanCatalogue

BATCH_SIZE: Final = 5000
SSO_STATE_MAX_AGE: Final = timedelta(hours=1)
IDEMPOTENCY_MAX_AGE: Final = timedelta(days=7)
AUDIT_NOTE: Final = (
    "audit records are never deleted by the sweeper: audit_min_retention_days is a guaranteed "
    "minimum, and the append-only chain stays verifiable from genesis"
)

# (report key, table, WHERE clause with %(cutoff)s). Table names are constants, never user input.
_Rule = tuple[str, str, str]
_SESSIONS: Final[_Rule] = ("sessions", "sessions", "expires_at < %(cutoff)s")
_SSO: Final[_Rule] = ("sso_states", "sso_states", "created_at < %(cutoff)s")
_IDEM: Final[_Rule] = ("idempotency_keys", "idempotency_keys", "created_at < %(cutoff)s")
_TRACES: Final[_Rule] = ("traces", "traces", "ingested_at < %(cutoff)s")


def _delete_batched(db: Database, org_id: str, rule: _Rule, cutoff: datetime, batch: int) -> int:
    """Delete matching rows ``batch`` at a time, one short transaction per batch."""
    _, table, where = rule
    total = 0
    while True:
        with db.tx(org_id) as conn:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE ctid IN "
                f"(SELECT ctid FROM {table} WHERE {where} LIMIT %(batch)s)",
                {"cutoff": cutoff, "batch": batch},
            )
            deleted = cursor.rowcount
        total += deleted
        if deleted < batch:
            return total


def list_orgs(db: Database) -> list[tuple[str, str]]:
    """``(org_id, plan)`` for every org, from the PII-free ``org_index``."""
    with db.tx() as conn:
        rows = conn.execute("SELECT org_id, plan FROM org_index ORDER BY org_id").fetchall()
    return [(str(r["org_id"]), r["plan"]) for r in rows]


def sweep(
    db: Database,
    catalogue: PlanCatalogue,
    now: datetime | None = None,
    *,
    batch_size: int = BATCH_SIZE,
) -> dict[str, Any]:
    """Run one sweep over every org and return a report."""
    now = now or datetime.now(UTC)
    orgs: dict[str, dict[str, Any]] = {}
    totals = {"traces": 0, "sessions": 0, "sso_states": 0, "idempotency_keys": 0}
    for org_id, plan_id in list_orgs(db):
        try:
            plan = catalogue.get(plan_id)
        except KeyError:
            # An unknown plan must never cause deletion: keep everything, say so.
            orgs[org_id] = {"plan": plan_id, "skipped": "unknown plan; nothing deleted"}
            continue
        rules: list[tuple[_Rule, datetime]] = [
            (_SESSIONS, now),
            (_SSO, now - SSO_STATE_MAX_AGE),
            (_IDEM, now - IDEMPOTENCY_MAX_AGE),
        ]
        if plan.trace_retention_days is not None:
            rules.insert(0, (_TRACES, now - timedelta(days=plan.trace_retention_days)))
        deleted = dict.fromkeys(totals, 0)
        for rule, cutoff in rules:
            deleted[rule[0]] = _delete_batched(db, org_id, rule, cutoff, batch_size)
        for key, count in deleted.items():
            totals[key] += count
        orgs[org_id] = {
            "plan": plan.id,
            "trace_retention_days": plan.trace_retention_days,
            "audit_min_retention_days": plan.audit_min_retention_days,
            "deleted": deleted,
        }
    return {
        "swept_at": now.isoformat(),
        "orgs": orgs,
        "totals": totals,
        "audit": {"deleted": 0, "note": AUDIT_NOTE},
    }


def main(args: list[str]) -> int:
    """CLI entry: ``sweep-retention``. Prints a per-org summary and exits 0."""
    from keelgate_cloud.config import Settings  # noqa: PLC0415
    from keelgate_cloud.db import Database  # noqa: PLC0415
    from keelgate_cloud.devtools import load_env  # noqa: PLC0415

    if args and args[0] != "sweep-retention":
        print(f"unknown command {args[0]!r}", file=sys.stderr)
        return 2
    load_env()
    config = Settings.from_env()
    db = Database(config.database_url, min_size=1, max_size=2)
    try:
        report = sweep(db, default_catalogue())
    finally:
        db.close()
    print(f"retention sweep at {report['swept_at']}")
    for org_id, info in report["orgs"].items():
        if "skipped" in info:
            print(f"  {org_id} [{info['plan']}] skipped: {info['skipped']}")
            continue
        d = info["deleted"]
        print(
            f"  {org_id} [{info['plan']}] traces={d['traces']} sessions={d['sessions']} "
            f"sso_states={d['sso_states']} idempotency_keys={d['idempotency_keys']}"
        )
    print(f"total: {report['totals']}")
    print(f"note: {AUDIT_NOTE}")
    return 0
