"""The objects an app instance is built from, created once and shared by the routers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from keelgate.approvals import ApprovalQueue
from keelgate.audit import AuditLog

from keelgate_cloud import metering
from keelgate_cloud.identity import Identity
from keelgate_cloud.plans import PlanCatalogue, default_catalogue
from keelgate_cloud.ratelimit import RateLimiter
from keelgate_cloud.security import SecretBox
from keelgate_cloud.stores import RlsApprovalStore, RlsAuditStore

if TYPE_CHECKING:
    import psycopg
    from keelgate.audit import AuditRecord

    from keelgate_cloud.config import Settings
    from keelgate_cloud.db import Database


@dataclass
class AppState:
    """Everything a request handler needs."""

    settings: Settings
    db: Database
    catalogue: PlanCatalogue
    identity: Identity
    audit_store: RlsAuditStore
    audit: AuditLog
    approval_store: RlsApprovalStore
    approvals: ApprovalQueue
    limiter: RateLimiter
    secrets: SecretBox
    extras: dict[str, Any] = field(default_factory=dict)

    def plan_of(self, org_id: str):  # type: ignore[no-untyped-def]
        """The org's current plan."""
        with self.db.tx(org_id) as conn:
            row = conn.execute("SELECT plan FROM orgs").fetchone()
        return self.catalogue.get(row["plan"] if row else "community")


def build_state(
    settings: Settings, db: Database, catalogue: PlanCatalogue | None = None
) -> AppState:
    """Wire the stores, the audit log and the approval queue together."""
    catalogue = catalogue or default_catalogue()

    def meter(conn: psycopg.Connection[Any], record: AuditRecord) -> None:
        # Runs inside the audit-append transaction: the count commits with the record or not at all.
        org_id = record.tenant_id
        row = conn.execute("SELECT plan FROM orgs").fetchone()
        plan = catalogue.get(row["plan"] if row else "community")
        if record.event_type == metering.AGENT_EVENT:
            # The first event with a verified agent id: the tool.call event precedes grant
            # verification, so its actor is still "unauthenticated".
            metering.enforce_agent_cap(conn, org_id, plan, record.actor)
        elif record.event_type == metering.GATED_EVENT:
            metering.enforce_action_quota(conn, plan, metering.period_of(metering.today()))
            metering.record(conn, org_id, metering.GATED_ACTIONS)
        elif record.event_type == "approval.requested":
            metering.record(conn, org_id, metering.APPROVALS)

    audit_store = RlsAuditStore(db, on_append=meter)
    audit = AuditLog(audit_store)
    approval_store = RlsApprovalStore(db)
    approvals = ApprovalQueue(store=approval_store, audit=audit)
    return AppState(
        settings=settings,
        db=db,
        catalogue=catalogue,
        identity=Identity(db, catalogue, session_ttl_hours=settings.session_ttl_hours),
        audit_store=audit_store,
        audit=audit,
        approval_store=approval_store,
        approvals=approvals,
        limiter=RateLimiter(),
        secrets=SecretBox(settings.secret_key),
    )
