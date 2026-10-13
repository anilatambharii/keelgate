"""Sign-up, sign-in, API keys and users: the dashboard-facing account API (``/api``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from keelgate_cloud.api.deps import State, User, bearer_token, require_roles
from keelgate_cloud.errors import PermissionDeniedError
from keelgate_cloud.limits import throttle

router = APIRouter(prefix="/api", tags=["accounts"])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupIn(_Body):
    org_name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class LoginIn(_Body):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class KeyIn(_Body):
    name: str = Field(min_length=1, max_length=100)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class UserIn(_Body):
    email: str = Field(max_length=254)
    role: str
    password: str | None = Field(default=None, max_length=256)
    approval_tier: str = "ONE_CLICK"


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if k.endswith("id") or k == "id" else v) for k, v in row.items()}


def _org_view(state: Any, org: dict[str, Any]) -> dict[str, Any]:
    plan = state.catalogue.get(org["plan"])
    return {
        "id": str(org["id"]),
        "name": org["name"],
        "slug": org["slug"],
        "plan": plan.id,
        "plan_name": plan.name,
        "features": list(plan.features),
        "billing_status": org["billing_status"],
        "trial_ends_at": org["trial_ends_at"],
        "residency": org["residency"],
    }


@router.post("/signup", status_code=201)
def signup(body: SignupIn, request: Request, state: State) -> dict[str, Any]:
    """Create an organisation (on a trial) and its owner. Returns a session token."""
    throttle(state, request, "signup", state.settings.signup_per_minute)
    org, token = state.identity.signup(
        org_name=body.org_name, email=body.email, password=body.password
    )
    return {"token": token, "org": _org_view(state, org)}


@router.post("/login")
def login(body: LoginIn, request: Request, state: State) -> dict[str, str]:
    """Open a session with an e-mail and password."""
    throttle(state, request, "login", state.settings.login_per_minute, body.email.strip().lower())
    return {"token": state.identity.login(email=body.email, password=body.password)}


@router.post("/logout", status_code=204)
def logout(request: Request, state: State, _: User) -> Response:
    """End the current session."""
    state.identity.logout(bearer_token(request))
    return Response(status_code=204)


@router.get("/me")
def me(principal: User, state: State) -> dict[str, Any]:
    """The signed-in user and their organisation."""
    org = state.identity.get_org(principal.org_id)
    return {
        "user": {
            "id": principal.user_id,
            "email": principal.email,
            "role": principal.role,
            "approval_tier": principal.approval_tier,
        },
        "org": _org_view(state, org),
    }


# ------------------------------------------------------------------------------------ keys


@router.get("/keys", dependencies=[require_roles("owner", "admin")])
def list_keys(principal: User, state: State) -> dict[str, Any]:
    """The org's API keys. The secret itself is shown only when a key is created."""
    return {"items": [_jsonable(k) for k in state.identity.list_api_keys(principal.org_id)]}


@router.post("/keys", status_code=201, dependencies=[require_roles("owner", "admin")])
def create_key(body: KeyIn, principal: User, state: State) -> dict[str, Any]:
    """Mint an API key. The response is the only time the full key is visible."""
    row, key = state.identity.create_api_key(
        principal.org_id,
        name=body.name,
        created_by=principal.user_id,
        expires_in_days=body.expires_in_days,
    )
    return {**_jsonable(row), "key": key}


@router.delete("/keys/{key_id}", status_code=204, dependencies=[require_roles("owner", "admin")])
def revoke_key(key_id: str, principal: User, state: State) -> Response:
    """Revoke a key immediately."""
    state.identity.revoke_api_key(principal.org_id, key_id)
    return Response(status_code=204)


# ----------------------------------------------------------------------------------- users


@router.get("/users", dependencies=[require_roles("owner", "admin")])
def list_users(principal: User, state: State) -> dict[str, Any]:
    """The org's users."""
    return {"items": [_jsonable(u) for u in state.identity.list_users(principal.org_id)]}


@router.post("/users", status_code=201, dependencies=[require_roles("owner", "admin")])
def invite_user(body: UserIn, principal: User, state: State) -> dict[str, Any]:
    """Add a user. Only an owner may add an admin."""
    if body.role == "admin" and principal.role != "owner":
        raise PermissionDeniedError("only an owner can add an admin")
    row = state.identity.invite_user(
        principal.org_id,
        email=body.email,
        role=body.role,
        password=body.password,
        approval_tier=body.approval_tier,
        granted_by_owner=principal.role == "owner",
    )
    return _jsonable(row)


class TierIn(_Body):
    approval_tier: str


@router.patch("/users/{user_id}", status_code=204, dependencies=[require_roles("owner")])
def set_user_tier(user_id: str, body: TierIn, principal: User, state: State) -> Response:
    """Set the approval tier a user is cleared for. Owners only, so clearance cannot be self-granted
    by an admin.
    """
    state.identity.set_approval_tier(principal.org_id, user_id, body.approval_tier)
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=204, dependencies=[require_roles("owner", "admin")])
def disable_user(user_id: str, principal: User, state: State) -> Response:
    """Disable a user and end their sessions."""
    state.identity.disable_user(principal.org_id, user_id)
    return Response(status_code=204)
