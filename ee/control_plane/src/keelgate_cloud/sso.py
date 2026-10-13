"""OpenID Connect single sign-on: authorization-code flow with PKCE, just-in-time provisioning.

Two halves. The *configuration* helpers validate what an org admin typed (issuer, domains). The
*flow* functions (:func:`start_login`, :func:`finish_login`) talk to the identity provider and
turn a verified ID token into a Keelgate session.

SSRF defence and its limits
---------------------------
The control plane makes server-side requests to a URL a tenant chose (the issuer) and to URLs that
issuer's discovery document names (token endpoint, JWKS). That is a classic SSRF surface. Defences:

* the issuer must be ``https`` with no userinfo, query or fragment; every URL fetched server-side
  (discovery, token endpoint, JWKS) is re-checked the same way;
* the host is resolved and **every** address must be globally routable: loopback, private,
  link-local (cloud metadata ``169.254.169.254``), CGNAT, multicast, reserved and unspecified
  ranges are refused, including IPv4-mapped IPv6 forms;
* redirects are never followed, responses are size capped and requests have short timeouts, so a
  hostile server cannot bounce us to an internal host or stream us unbounded data.

What this does **not** do: the check resolves the name itself, and the HTTP client resolves it
again when it connects, so a DNS-rebinding attacker who controls the zone can answer differently
the second time (time-of-check/time-of-use). Closing that needs connecting to the vetted IP
(a pinned transport) or an egress firewall; run the control plane with egress rules that deny
private ranges as the real backstop. ``Settings.oidc_allow_http`` turns the whole check off for
local development and must stay off in production.

Tokens: the ID token is verified with PyJWT against the IdP's JWKS with a fixed allow-list of
algorithms (RS256, ES256) chosen *before* any key is looked at, so ``none`` and HMAC algorithm
confusion are impossible. The client secret is only ever held in memory while a request runs and
is never logged or put in an exception message.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx
import jwt
import psycopg.errors
from keelgate.audit import AuditError

from keelgate_cloud import security
from keelgate_cloud.errors import ValidationFailedError

if TYPE_CHECKING:
    from keelgate_cloud.state import AppState

log = logging.getLogger("keelgate_cloud.sso")

ALLOWED_ALGORITHMS: Final = ("RS256", "ES256")
DEFAULT_ROLES: Final = ("admin", "approver", "viewer")
STATE_TTL_SECONDS: Final = 600
MAX_RESPONSE_BYTES: Final = 1_000_000
DISCOVERY_TTL: Final = 300.0
JWKS_TTL: Final = 300.0
JWKS_REFRESH_COOLDOWN: Final = 30.0
CLOCK_SKEW_SECONDS: Final = 60
MAX_TOKEN_AGE_SECONDS: Final = 600
MAX_STATES_PER_ORG: Final = 5000
SCOPE: Final = "openid email profile"

_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")

Resolver = Callable[[str], list[str]]
HttpFactory = Callable[[], httpx.Client]


class SsoFlowError(Exception):
    """A login failed. ``code`` is the stable machine code shown to the browser; nothing else is."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SsoUnavailableError(Exception):
    """SSO cannot be used for this org (unknown org, not configured, disabled, plan)."""


# ----------------------------------------------------------------------------------- validation


def default_resolver(host: str) -> list[str]:
    """Every address ``host`` resolves to."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour is not None:
        ip = ip.sixtofour
    return ip.is_global and not ip.is_multicast


def check_url(
    url: str,
    *,
    allow_http: bool,
    resolver: Resolver,
    allow_query: bool = False,
) -> urllib.parse.SplitResult:
    """Validate a URL the server will fetch; raise ``ValueError`` with a safe message."""
    if len(url) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("not a valid URL")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("not a valid URL") from exc
    allowed = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in allowed:
        raise ValueError("the URL must use https")
    host = parts.hostname
    if not host:
        raise ValueError("the URL needs a host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("the URL must not contain credentials")
    if parts.fragment or (parts.query and not allow_query):
        raise ValueError("the URL must not contain a query or fragment")
    if port == 0:
        raise ValueError("not a valid URL")
    if allow_http:
        return parts
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = resolver(host)
        except OSError as exc:
            raise ValueError("the host does not resolve") from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        raise ValueError("the host is not a public internet address")
    return parts


def validate_issuer(issuer: str, state: AppState) -> str:
    """The issuer, or raise a 422 saying why it is unacceptable."""
    try:
        check_url(
            issuer,
            allow_http=state.settings.oidc_allow_http,
            resolver=_resolver(state),
        )
    except ValueError as exc:
        raise ValidationFailedError(f"issuer: {exc}", code="invalid_issuer") from exc
    return issuer


def normalise_domains(domains: list[str]) -> list[str]:
    """Validated, de-duplicated allowed e-mail domains. Must be lowercase already."""
    if not 1 <= len(domains) <= 50:
        raise ValidationFailedError("allowed_domains needs 1 to 50 domains")
    out: list[str] = []
    for domain in domains:
        if domain != domain.strip().lower() or not _DOMAIN.match(domain):
            raise ValidationFailedError(
                "allowed_domains must be lowercase domain names such as example.com",
                code="invalid_domain",
            )
        if domain not in out:
            out.append(domain)
    return out


def _resolver(state: AppState) -> Resolver:
    resolver: Resolver = state.extras.get("oidc_resolver") or default_resolver
    return resolver


# --------------------------------------------------------------------------------------- HTTP


def default_http_client() -> httpx.Client:
    """Short timeouts, no redirects."""
    return httpx.Client(
        timeout=httpx.Timeout(5.0, connect=3.0),
        follow_redirects=False,
        headers={"User-Agent": "keelgate-cloud-sso/1", "Accept": "application/json"},
        limits=httpx.Limits(max_connections=10),
    )


@dataclass
class _Cache:
    """Process-local discovery and JWKS cache. ``refresh_cooldown`` bounds forced JWKS refetches."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    discovery: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    jwks: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    refresh_cooldown: float = JWKS_REFRESH_COOLDOWN
    clock: Callable[[], float] = time.monotonic


_cache_guard = threading.Lock()


def _cache(state: AppState) -> _Cache:
    with _cache_guard:
        cache = state.extras.get("oidc_cache")
        if cache is None:
            cache = state.extras["oidc_cache"] = _Cache()
    assert isinstance(cache, _Cache)  # noqa: S101
    return cache


class _Http:
    """The outbound calls to one IdP, through the injectable client."""

    def __init__(self, state: AppState) -> None:
        self._state = state
        self._factory: HttpFactory = state.extras.get("oidc_http") or default_http_client
        self._owns_client = "oidc_http" not in state.extras

    def _checked(self, url: str) -> None:
        try:
            check_url(
                url,
                allow_http=self._state.settings.oidc_allow_http,
                resolver=_resolver(self._state),
                allow_query=True,
            )
        except ValueError as exc:
            raise SsoFlowError("idp_unreachable") from exc

    def request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One request; a JSON object back, or :class:`SsoFlowError` (code chosen by caller)."""
        self._checked(url)
        client = self._factory()
        try:
            with client.stream(method, url, data=data, headers=headers) as response:
                if response.status_code != 200:
                    raise SsoFlowError("idp_error")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    raise SsoFlowError("idp_unreachable")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise SsoFlowError("idp_unreachable")
        except httpx.HTTPError as exc:
            log.warning("sso: idp request failed (%s)", type(exc).__name__)
            raise SsoFlowError("idp_unreachable") from exc
        finally:
            if self._owns_client:
                client.close()
        try:
            parsed = json.loads(bytes(body))
        except ValueError as exc:
            raise SsoFlowError("idp_unreachable") from exc
        if not isinstance(parsed, dict):
            raise SsoFlowError("idp_unreachable")
        return parsed


def _discovery(state: AppState, http: _Http, issuer: str) -> dict[str, Any]:
    cache = _cache(state)
    now = cache.clock()
    with cache.lock:
        hit = cache.discovery.get(issuer)
    if hit is not None and now - hit[0] < DISCOVERY_TTL:
        return hit[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = http.request("GET", url)
    if doc.get("issuer") != issuer:
        raise SsoFlowError("idp_unreachable")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(doc.get(key), str):
            raise SsoFlowError("idp_unreachable")
    methods = doc.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise SsoFlowError("idp_unreachable")
    with cache.lock:
        cache.discovery[issuer] = (now, doc)
    return doc


def _signing_key(state: AppState, http: _Http, jwks_uri: str, kid: str | None, alg: str) -> Any:
    """The verification key for ``kid``; refetches the JWKS once when the kid is unknown."""
    cache = _cache(state)

    def find(jwks: dict[str, Any]) -> Any:
        keys = [k for k in jwks.get("keys", []) if isinstance(k, dict)]
        keys = [k for k in keys if k.get("use") in (None, "sig") and k.get("alg") in (None, alg)]
        if kid is not None:
            keys = [k for k in keys if k.get("kid") == kid]
        elif len(keys) != 1:
            return None
        if len(keys) != 1:
            return None
        try:
            return jwt.PyJWK(keys[0]).key
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return None

    now = cache.clock()
    with cache.lock:
        hit = cache.jwks.get(jwks_uri)
    if hit is not None and now - hit[0] < JWKS_TTL:
        key = find(hit[1])
        if key is not None:
            return key
        if now - hit[0] < cache.refresh_cooldown:
            return None  # fetched moments ago: an unknown kid is not a reason to fetch again
    fresh = http.request("GET", jwks_uri)
    with cache.lock:
        cache.jwks[jwks_uri] = (now, fresh)
    return find(fresh)


# ------------------------------------------------------------------------------------- the flow


@dataclass(frozen=True)
class _Config:
    org_id: str
    issuer: str
    client_id: str
    client_secret_enc: str
    allowed_domains: list[str]
    default_role: str


def _load_config(state: AppState, slug: str) -> _Config:
    """The enabled config for the org with this slug; one error for every reason it is missing."""
    if not 1 <= len(slug) <= 63:
        raise SsoUnavailableError
    with state.db.tx(slug=slug) as conn:
        org = conn.execute("SELECT id FROM orgs WHERE slug = %s", (slug,)).fetchone()
        cfg = conn.execute("SELECT * FROM sso_configs WHERE enabled").fetchone()
    return _config_checked(state, org, cfg)


def _load_config_for_org(state: AppState, org_id: str) -> _Config:
    with state.db.tx(org_id) as conn:
        cfg = conn.execute("SELECT * FROM sso_configs WHERE enabled").fetchone()
    return _config_checked(state, {"id": org_id}, cfg)


def _config_checked(state: AppState, org: Any, cfg: Any) -> _Config:
    if org is None or cfg is None:
        raise SsoUnavailableError
    org_id = str(org["id"])
    if not state.plan_of(org_id).has("sso"):
        raise SsoUnavailableError
    try:
        state.identity.require_active(org_id)
    except Exception as exc:  # lapsed subscription: indistinguishable from "not configured"
        raise SsoUnavailableError from exc
    return _Config(
        org_id=org_id,
        issuer=cfg["issuer"],
        client_id=cfg["client_id"],
        client_secret_enc=cfg["client_secret_enc"],
        allowed_domains=list(cfg["allowed_domains"]),
        default_role=cfg["default_role"],
    )


def redirect_uri(state: AppState) -> str:
    """Where the IdP sends the browser back to."""
    return state.settings.public_url.rstrip("/") + "/api/sso/callback"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def start_login(state: AppState, slug: str) -> str:
    """Record a fresh state and return the IdP authorization URL to redirect the browser to."""
    cfg = _load_config(state, slug)
    state.limiter.check(f"sso-login:{cfg.org_id}", 120)
    http = _Http(state)
    try:
        doc = _discovery(state, http, cfg.issuer)
        endpoint = check_url(
            doc["authorization_endpoint"],
            allow_http=state.settings.oidc_allow_http,
            resolver=lambda _h: ["93.184.216.34"],  # browser-facing: scheme/shape checks only
            allow_query=True,
        )
    except ValueError as exc:
        raise SsoUnavailableError from exc
    except SsoFlowError as exc:
        raise SsoUnavailableError from exc
    st = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    with state.db.tx(cfg.org_id) as conn:
        conn.execute(
            "DELETE FROM sso_states WHERE created_at < now() - make_interval(secs => %s)",
            (STATE_TTL_SECONDS,),
        )
        pending = conn.execute("SELECT count(*) AS n FROM sso_states").fetchone()
        if pending is not None and pending["n"] >= MAX_STATES_PER_ORG:
            raise SsoUnavailableError
        conn.execute(
            "INSERT INTO sso_states (state, org_id, nonce, code_verifier) VALUES (%s,%s,%s,%s)",
            (st, cfg.org_id, nonce, verifier),
        )
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": redirect_uri(state),
        "scope": SCOPE,
        "state": st,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    joined = f"{endpoint.query}&{query}" if endpoint.query else query
    return urllib.parse.urlunsplit(endpoint._replace(query=joined))


def finish_login(
    state: AppState, *, code: str | None, state_value: str | None, idp_error: str | None
) -> str:
    """Complete a login; returns a session token or raises :class:`SsoFlowError`."""
    if not state_value or len(state_value) > 256:
        raise SsoFlowError("invalid_state")
    with state.db.tx(sso_state=state_value) as conn:
        # Single use: consumed here whatever happens next. Fresh-ness is judged by the database.
        row = conn.execute(
            "DELETE FROM sso_states WHERE state = %s "
            "RETURNING org_id, nonce, code_verifier, "
            "created_at > now() - make_interval(secs => %s) AS fresh",
            (state_value, STATE_TTL_SECONDS),
        ).fetchone()
    if row is None or not row["fresh"]:
        raise SsoFlowError("invalid_state")
    if idp_error:
        raise SsoFlowError("idp_error")
    if not code or len(code) > 2048:
        raise SsoFlowError("invalid_request")
    org_id = str(row["org_id"])
    try:
        cfg = _load_config_for_org(state, org_id)
    except SsoUnavailableError as exc:
        raise SsoFlowError("not_available") from exc
    claims = _exchange_and_verify(state, cfg, code, row["code_verifier"], row["nonce"])
    return _sign_in(state, cfg, claims)


def _exchange_and_verify(
    state: AppState, cfg: _Config, code: str, verifier: str, nonce: str
) -> dict[str, Any]:
    http = _Http(state)
    doc = _discovery(state, http, cfg.issuer)
    try:
        secret = state.secrets.open(cfg.client_secret_enc)
    except ValueError as exc:
        raise SsoFlowError("not_available") from exc
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(state),
        "code_verifier": verifier,
    }
    headers: dict[str, str] = {"Accept": "application/json"}
    methods = doc.get("token_endpoint_auth_methods_supported")
    use_basic = not isinstance(methods, list) or "client_secret_basic" in methods
    if not use_basic and "client_secret_post" not in methods:  # type: ignore[operator]
        raise SsoFlowError("token_exchange_failed")
    if use_basic:
        quote = urllib.parse.quote
        pair = f"{quote(cfg.client_id, safe='')}:{quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    else:
        form["client_id"] = cfg.client_id
        form["client_secret"] = secret
    try:
        tokens = http.request("POST", doc["token_endpoint"], data=form, headers=headers)
    except SsoFlowError as exc:
        raise SsoFlowError("token_exchange_failed") from exc
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or len(id_token) > 16384:
        raise SsoFlowError("token_invalid")
    return _verify_id_token(state, http, doc["jwks_uri"], cfg, id_token, nonce)


def _verify_id_token(
    state: AppState, http: _Http, jwks_uri: str, cfg: _Config, token: str, nonce: str
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise SsoFlowError("token_invalid") from exc
    alg = header.get("alg")
    kid = header.get("kid")
    if alg not in ALLOWED_ALGORITHMS or not (kid is None or isinstance(kid, str)):
        raise SsoFlowError("token_invalid")
    try:
        key = _signing_key(state, http, jwks_uri, kid, alg)
    except SsoFlowError as exc:
        raise SsoFlowError("token_invalid") from exc
    if key is None:
        raise SsoFlowError("token_invalid")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=cfg.client_id,
            issuer=cfg.issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise SsoFlowError("token_invalid") from exc
    now = time.time()
    iat = claims["iat"]
    if (
        not isinstance(iat, int | float)
        or iat > now + CLOCK_SKEW_SECONDS
        or now - iat > MAX_TOKEN_AGE_SECONDS + CLOCK_SKEW_SECONDS
    ):
        raise SsoFlowError("token_invalid")
    aud = claims["aud"]
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != cfg.client_id:
        raise SsoFlowError("token_invalid")
    got_nonce = claims.get("nonce")
    if not isinstance(got_nonce, str) or not security.constant_time_equal(got_nonce, nonce):
        raise SsoFlowError("token_invalid")
    sub = claims["sub"]
    if not isinstance(sub, str) or not 1 <= len(sub) <= 255:
        raise SsoFlowError("token_invalid")
    email = claims.get("email")
    if not isinstance(email, str):
        raise SsoFlowError("email_not_verified")
    verified = claims.get("email_verified")
    if not (verified is True or (isinstance(verified, str) and verified.lower() == "true")):
        raise SsoFlowError("email_not_verified")
    return claims


def _sign_in(state: AppState, cfg: _Config, claims: dict[str, Any]) -> str:
    email = str(claims["email"]).strip().lower()
    if not _EMAIL.match(email):
        raise SsoFlowError("token_invalid")
    if email.rsplit("@", 1)[1] not in cfg.allowed_domains:
        raise SsoFlowError("domain_not_allowed")
    sub = str(claims["sub"])
    with state.db.tx(email=email) as conn:
        existing = conn.execute(
            "SELECT id, org_id, disabled_at, oidc_subject FROM users WHERE email = %s", (email,)
        ).fetchone()
    provisioned = existing is None
    # One code for "belongs to another org", "disabled" and "different subject": no oracle.
    if existing is not None:
        if str(existing["org_id"]) != cfg.org_id or existing["disabled_at"] is not None:
            raise SsoFlowError("account_unavailable")
        user_id = str(existing["id"])
        if existing["oidc_subject"] is None:
            with state.db.tx(cfg.org_id) as conn:
                claimed = conn.execute(
                    "UPDATE users SET oidc_subject = %s "
                    "WHERE id = %s AND oidc_subject IS NULL AND disabled_at IS NULL RETURNING id",
                    (sub, user_id),
                ).fetchone()
            if claimed is None:
                raise SsoFlowError("account_unavailable")
        elif not security.constant_time_equal(existing["oidc_subject"], sub):
            raise SsoFlowError("account_unavailable")
    else:
        name = claims.get("name")
        display = name.strip()[:200] if isinstance(name, str) else ""
        try:
            with state.db.tx(cfg.org_id) as conn:
                created = conn.execute(
                    "INSERT INTO users (org_id, email, display_name, role, oidc_subject) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (cfg.org_id, email, display, cfg.default_role, sub),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise SsoFlowError("account_unavailable") from exc
        assert created is not None  # noqa: S101
        user_id = str(created["id"])
    token = state.identity.open_session_for(cfg.org_id, user_id)
    audit(
        state, cfg.org_id, "sso.login", f"user:{user_id}", {"user_id": user_id, "new": provisioned}
    )
    return token


def audit(state: AppState, org_id: str, event: str, actor: str, payload: dict[str, Any]) -> None:
    """Append to the org chain; a failure to audit must not break sign-in or configuration."""
    try:
        state.audit.append(tenant_id=org_id, event_type=event, actor=actor, payload=payload)
    except AuditError:
        log.warning("sso: could not write %s audit event", event)
