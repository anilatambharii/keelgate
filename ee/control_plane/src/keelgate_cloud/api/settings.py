"""Per-org settings: name and data-residency label, with the plan's retention terms."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from keelgate_cloud.api.deps import State, User, require_roles
from keelgate_cloud.errors import ValidationFailedError

if TYPE_CHECKING:
    from keelgate_cloud.state import AppState

router = APIRouter(prefix="/api/settings", tags=["settings"])

REGIONS: Final = {
    "us": "United States",
    "eu": "European Union",
    "apac": "Asia-Pacific",
}
RESIDENCY_NOTE: Final = (
    "Residency is recorded as a policy label. This deployment runs in a single region and does "
    "not yet route or store data by it."
)


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    residency: Literal["us", "eu", "apac"] | None = None


def _view(state: AppState, org_id: str) -> dict[str, Any]:
    org = state.identity.get_org(org_id)
    plan = state.catalogue.get(org["plan"])
    return {
        "name": org["name"],
        "slug": org["slug"],
        "plan": plan.id,
        "residency": org["residency"],
        "residency_enforced": False,
        "residency_note": RESIDENCY_NOTE,
        "retention": {
            "trace_retention_days": plan.trace_retention_days,
            "audit_min_retention_days": plan.audit_min_retention_days,
            "audit_immutable": plan.audit_immutable,
        },
        "features": list(plan.features),
    }


@router.get("")
def get_settings(principal: User, state: State) -> dict[str, Any]:
    """The org's settings (any member)."""
    return _view(state, principal.org_id)


@router.get("/residency-regions")
def residency_regions(_: User) -> dict[str, Any]:
    """The residency values an owner may choose."""
    return {
        "regions": [{"id": key, "label": label} for key, label in REGIONS.items()],
        "enforced": False,
        "note": RESIDENCY_NOTE,
    }


@router.patch("", dependencies=[require_roles("owner")])
def update_settings(body: SettingsIn, principal: User, state: State) -> dict[str, Any]:
    """Change the name and/or residency label (owner only). Changes are recorded in the audit
    chain as ``settings.changed`` with old and new values.
    """
    name = body.name.strip() if body.name is not None else None
    if name is not None and not name:
        raise ValidationFailedError("name cannot be empty")
    if name is None and body.residency is None:
        raise ValidationFailedError("send name and/or residency")
    org_id = principal.org_id
    with state.db.tx(org_id) as conn:
        before = conn.execute("SELECT name, residency FROM orgs").fetchone()
        assert before is not None  # noqa: S101 - the caller's own org exists
        wanted = {"name": name, "residency": body.residency}
        changes = {
            field: {"old": before[field], "new": value}
            for field, value in wanted.items()
            if value is not None and value != before[field]
        }
        if changes:
            conn.execute(
                "UPDATE orgs SET name = %s, residency = %s",
                (wanted["name"] or before["name"], wanted["residency"] or before["residency"]),
            )
    if changes:
        try:
            state.audit.append(
                tenant_id=org_id,
                event_type="settings.changed",
                actor=principal.actor,
                payload={"changes": changes},
            )
        except BaseException:
            with state.db.tx(org_id) as conn:  # an unrecorded change must not stand
                conn.execute(
                    "UPDATE orgs SET name = %s, residency = %s",
                    (before["name"], before["residency"]),
                )
            raise
    return _view(state, org_id)
