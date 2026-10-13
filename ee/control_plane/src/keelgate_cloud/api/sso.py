"""OIDC single sign-on: configuration (owner/admin) and the browser login flow (no auth)."""

from __future__ import annotations

import contextlib
import json
import logging
import urllib.parse
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from keelgate_cloud import security, sso
from keelgate_cloud.api.deps import State, User, require_feature, require_roles
from keelgate_cloud.errors import NotFoundError, ValidationFailedError
from keelgate_cloud.limits import throttle

router = APIRouter(prefix="/api/sso", tags=["sso"])
log = logging.getLogger("keelgate_cloud.sso")

_MAX_BODY = 32_768
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
STATE_COOKIE = "kg_sso_state"
_ADMIN = require_roles("owner")  # who controls the IdP controls who can sign in: owner only


class SsoConfigIn(BaseModel):
    """The body of ``PUT /api/sso``. ``client_secret`` may be omitted to keep the stored one."""

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str | None = Field(default=None, min_length=1, max_length=2048, repr=False)
    allowed_domains: list[str] = Field(max_length=50)
    default_role: Literal["approver", "viewer"] = "viewer"
    enabled: bool = True


def _view(state: Any, row: dict[str, Any]) -> dict[str, Any]:
    """The config as the API shows it: never the secret, not even sealed."""
    return {
        "issuer": row["issuer"],
        "client_id": row["client_id"],
        "client_secret_set": True,
        "allowed_domains": list(row["allowed_domains"]),
        "default_role": row["default_role"],
        "enabled": row["enabled"],
        "updated_at": row["updated_at"],
        "redirect_uri": sso.redirect_uri(state),
    }


async def _parse(request: Request) -> SsoConfigIn:
    """Parse the body by hand: FastAPI's 422 echoes the request body, which holds the secret."""
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise ValidationFailedError("request body too large")
    try:
        return SsoConfigIn.model_validate(json.loads(raw))
    except ValueError as exc:
        if isinstance(exc, ValidationError):
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise ValidationFailedError(detail) from None
        raise ValidationFailedError("the body is not valid JSON") from None


@router.put("", dependencies=[_ADMIN])
async def put_config(request: Request, state: State, principal: User) -> dict[str, Any]:
    """Create or replace the org's SSO configuration."""
    body = await _parse(request)

    def work() -> dict[str, Any]:
        require_feature(state, principal.org_id, "sso")
        issuer = sso.validate_issuer(body.issuer, state)
        domains = sso.normalise_domains(body.allowed_domains)
        with state.db.tx(principal.org_id) as conn:
            current = conn.execute("SELECT client_secret_enc FROM sso_configs").fetchone()
            if body.client_secret is not None:
                sealed = state.secrets.seal(body.client_secret)
            elif current is not None:
                sealed = current["client_secret_enc"]
            else:
                raise ValidationFailedError("client_secret is required", code="secret_required")
            row = conn.execute(
                "INSERT INTO sso_configs (org_id, issuer, client_id, client_secret_enc, "
                "allowed_domains, default_role, enabled) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (org_id) DO UPDATE SET issuer = EXCLUDED.issuer, "
                "client_id = EXCLUDED.client_id, client_secret_enc = EXCLUDED.client_secret_enc, "
                "allowed_domains = EXCLUDED.allowed_domains, "
                "default_role = EXCLUDED.default_role, enabled = EXCLUDED.enabled, "
                "updated_at = now() RETURNING *",
                (
                    principal.org_id,
                    issuer,
                    body.client_id,
                    sealed,
                    domains,
                    body.default_role,
                    body.enabled,
                ),
            ).fetchone()
        assert row is not None  # noqa: S101
        sso.audit(
            state,
            principal.org_id,
            "sso.configured",
            principal.actor,
            {
                "action": "set",
                "issuer": issuer,
                "client_id": body.client_id,
                "allowed_domains": domains,
                "default_role": body.default_role,
                "enabled": body.enabled,
            },
        )
        return _view(state, row)

    return await run_in_threadpool(work)


@router.get("", dependencies=[_ADMIN])
def get_config(state: State, principal: User) -> dict[str, Any]:
    """The org's SSO configuration, without the client secret."""
    require_feature(state, principal.org_id, "sso")
    with state.db.tx(principal.org_id) as conn:
        row = conn.execute("SELECT * FROM sso_configs").fetchone()
    if row is None:
        raise NotFoundError("SSO is not configured")
    return _view(state, row)


@router.delete("", status_code=204, dependencies=[_ADMIN])
def delete_config(state: State, principal: User) -> Response:
    """Remove the SSO configuration. Users created through SSO keep their accounts."""
    require_feature(state, principal.org_id, "sso")
    with state.db.tx(principal.org_id) as conn:
        removed = conn.execute("DELETE FROM sso_configs RETURNING org_id").fetchone()
        conn.execute("DELETE FROM sso_states")
    if removed is not None:
        sso.audit(state, principal.org_id, "sso.configured", principal.actor, {"action": "removed"})
    return Response(status_code=204)


# ------------------------------------------------------------------------- browser login flow


@router.get("/login", include_in_schema=False)
def login(
    request: Request, state: State, org: Annotated[str, Query(max_length=63)]
) -> RedirectResponse:
    """Send the browser to the IdP. Every reason SSO is unavailable looks the same.

    The state is also set as an HttpOnly cookie, and the callback requires both to match. Without
    that, an attacker could finish a login themselves and send the callback URL to a victim, who
    would then be signed in to the attacker's account (login CSRF).
    """
    throttle(state, request, "sso-login", 60)
    try:
        url = sso.start_login(state, org)
    except sso.SsoUnavailableError:
        raise NotFoundError("single sign-on is not available", code="sso_not_available") from None
    response = RedirectResponse(url, status_code=302, headers=_NO_STORE)
    issued = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("state", [""])[0]
    response.set_cookie(
        STATE_COOKIE,
        issued,
        max_age=sso.STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=state.settings.public_url.startswith("https://"),
        path="/api/sso",
    )
    return response


def _error_redirect(state: State, code: str) -> RedirectResponse:
    base = state.settings.dashboard_url.rstrip("/")
    return RedirectResponse(
        f"{base}/sso/error?code={urllib.parse.quote(code, safe='')}",
        status_code=302,
        headers=_NO_STORE,
    )


@router.get("/callback", include_in_schema=False)
def callback(
    request: Request,
    state: State,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state_param: Annotated[str | None, Query(alias="state", max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=512)] = None,
) -> RedirectResponse:
    """Finish the login. The session token travels in the URL fragment, never the query."""
    cookie = request.cookies.get(STATE_COOKIE, "")
    bound = bool(state_param and cookie) and security.constant_time_equal(cookie, state_param or "")
    try:
        if not bound:
            # Still consume the state so a replayed or stolen URL cannot be retried.
            with contextlib.suppress(sso.SsoFlowError):
                sso.finish_login(state, code=None, state_value=state_param, idp_error="unbound")
            return _clear(_error_redirect(state, "invalid_state"))
        token = sso.finish_login(state, code=code, state_value=state_param, idp_error=error)
    except sso.SsoFlowError as exc:
        return _clear(_error_redirect(state, exc.code))
    except Exception:
        log.exception("sso: unexpected failure in callback")
        return _clear(_error_redirect(state, "server_error"))
    base = state.settings.dashboard_url.rstrip("/")
    return _clear(
        RedirectResponse(f"{base}/sso/complete#token={token}", status_code=302, headers=_NO_STORE)
    )


def _clear(response: RedirectResponse) -> RedirectResponse:
    response.delete_cookie(STATE_COOKIE, path="/api/sso")
    return response
