"""Per-org approval routing rules.

A rule says, for tools matching a glob and requests at or above a tier, which roles may decide
them and whether the request is raised to a stricter tier. The first matching enabled rule (lowest
priority number) wins. Rules can only make approval *harder*: they never lower a tier, and a
request that matches no rule falls back to the default (owner, admin and approver may decide).
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Final

from keelgate.approvals import ApprovalTier

from keelgate_cloud.errors import PermissionDeniedError

if TYPE_CHECKING:
    from keelgate_cloud.identity import Principal
    from keelgate_cloud.state import AppState

DEFAULT_DECIDERS: Final = frozenset({"owner", "admin", "approver"})
_ORDER: Final = {"ONE_CLICK": 0, "EXPLICIT_SIGNOFF": 1}


def _matching_rule(
    state: AppState, org_id: str, tool_name: str, tier: ApprovalTier
) -> dict[str, Any] | None:
    with state.db.tx(org_id) as conn:
        rules = conn.execute(
            "SELECT * FROM approval_routes WHERE enabled ORDER BY priority, created_at"
        ).fetchall()
    for rule in rules:
        if (
            fnmatchcase(tool_name, rule["tool_pattern"])
            and _ORDER[tier.value] >= _ORDER[rule["min_tier"]]
        ):
            return rule
    return None


def effective_tier(
    state: AppState, org_id: str, tool_name: str, requested: ApprovalTier
) -> ApprovalTier:
    """The tier a request is filed at: the requested one, raised by a matching rule."""
    rule = _matching_rule(state, org_id, tool_name, requested)
    if rule is not None and rule["escalate_to"] is not None:
        raised = ApprovalTier(rule["escalate_to"])
        if _ORDER[raised.value] > _ORDER[requested.value]:
            return raised
    return requested


def check_decider(
    state: AppState, principal: Principal, tool_name: str, tier: ApprovalTier
) -> None:
    """Raise unless the principal's role may decide this request under the org's routing rules."""
    rule = _matching_rule(state, principal.org_id, tool_name, tier)
    allowed = frozenset(rule["allowed_roles"]) if rule is not None else DEFAULT_DECIDERS
    if principal.role not in allowed:
        raise PermissionDeniedError(
            "your role is not allowed to decide this request", code="routing_forbidden"
        )
