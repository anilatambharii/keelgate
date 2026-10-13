"""Request dependencies: who is calling, and may they."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from keelgate_cloud.errors import (
    AuthenticationError,
    PermissionDeniedError,
    PlanRequiredError,
)
from keelgate_cloud.identity import Principal
from keelgate_cloud.state import AppState


def get_state(request: Request) -> AppState:
    """The app state."""
    state: AppState = request.app.state.kg
    return state


State = Annotated[AppState, Depends(get_state)]


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("send the credential as 'Authorization: Bearer <token>'")
    return token.strip()


def user_principal(request: Request, state: State) -> Principal:
    """A signed-in dashboard user (session token)."""
    principal = state.identity.authenticate_session(bearer_token(request))
    plan = state.plan_of(principal.org_id)
    state.limiter.check(principal.actor, max(plan.rate_limit_per_minute, 60))
    return principal


def agent_principal(request: Request, state: State) -> Principal:
    """An agent's API key. Checks rate limit and that the org may use the service."""
    principal = state.identity.authenticate_api_key(bearer_token(request))
    state.identity.require_active(principal.org_id)
    plan = state.plan_of(principal.org_id)
    if not plan.hosted:
        raise PlanRequiredError("this plan is self-hosted and is not served by the control plane")
    # Per org, not per key: minting more keys must not multiply the allowance.
    state.limiter.check(f"org:{principal.org_id}", plan.rate_limit_per_minute)
    return principal


User = Annotated[Principal, Depends(user_principal)]
Agent = Annotated[Principal, Depends(agent_principal)]


def require_roles(*roles: str):  # type: ignore[no-untyped-def]
    """A dependency that admits only users with one of ``roles``."""

    def check(principal: User) -> Principal:
        if principal.role not in roles:
            raise PermissionDeniedError("your role does not allow this")
        return principal

    return Depends(check)


def require_feature(state: AppState, org_id: str, feature: str) -> None:
    """Raise unless the org's plan includes ``feature``."""
    plan = state.plan_of(org_id)
    if not plan.has(feature):
        raise PlanRequiredError(f"the {plan.name} plan does not include {feature}")


def ensure_tenant(principal: Principal, tenant_id: str | None) -> str:
    """The caller's org; refuses any request that names a different tenant."""
    if tenant_id is not None and tenant_id != principal.org_id:
        raise PermissionDeniedError(
            "this credential belongs to a different tenant", code="tenant_mismatch"
        )
    return principal.org_id
