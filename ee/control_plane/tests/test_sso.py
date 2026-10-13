"""OIDC single sign-on against a fake identity provider (httpx.MockTransport, no network)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from keelgate_cloud.db import Database
from tests.harness import Org, make_app, unique_email

if TYPE_CHECKING:
    from fastapi import FastAPI

ISSUER = "https://idp.example.test"
CLIENT_ID = "kg-client-id"
CLIENT_SECRET = "idp-client-secret-DO-NOT-LEAK-12345"  # pragma: allowlist secret
PUBLIC_URL = "https://cp.example.test"
DASH = "https://dash.example.test"
PUBLIC_IP = "93.184.216.34"

Mint = Callable[[str], str]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Keypair:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.public_pem = self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def jwk(self) -> dict[str, Any]:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        return {**public, "kid": self.kid, "use": "sig", "alg": "RS256"}


class FakeIdp:
    """Discovery, JWKS and token endpoints, and a way to mint real signed ID tokens."""

    def __init__(self) -> None:
        self.key = Keypair("key-1")
        self.published = [self.key]
        self.requests: list[httpx.Request] = []
        self.codes: dict[str, dict[str, Any]] = {}
        self.auth_methods: list[str] | None = ["client_secret_basic", "client_secret_post"]
        self.discovery_issuer = ISSUER
        self.token_status = 200
        self.token_body_size = 0

    def discovery(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "issuer": self.discovery_issuer,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
            "code_challenge_methods_supported": ["S256"],
        }
        if self.auth_methods is not None:
            doc["token_endpoint_auth_methods_supported"] = self.auth_methods
        return doc

    def claims(self, nonce: str, /, **over: Any) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "subject-1",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "email": unique_email("sso"),
            "email_verified": True,
            "name": "Sso Person",
        }
        claims.update(over)
        return {k: v for k, v in claims.items() if v is not None}

    def sign(self, claims: dict[str, Any], key: Keypair | None = None) -> str:
        key = key or self.key
        return jwt.encode(claims, key.private_pem, algorithm="RS256", headers={"kid": key.kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [k.jwk() for k in self.published]})
        if path == "/token" and request.method == "POST":
            return self._token(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "server_error"})
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        basic = request.headers.get("authorization", "")
        if basic.startswith("Basic "):
            cid, _, secret = base64.b64decode(basic[6:]).decode().partition(":")
            cid, secret = urllib.parse.unquote(cid), urllib.parse.unquote(secret)
        else:
            cid, secret = form.get("client_id", ""), form.get("client_secret", "")
        if cid != CLIENT_ID or secret != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        entry = self.codes.pop(form.get("code", ""), None)
        if entry is None or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        if _b64(hashlib.sha256(verifier.encode()).digest()) != entry["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != f"{PUBLIC_URL}/api/sso/callback":
            return httpx.Response(400, json={"error": "invalid_grant"})
        body: dict[str, Any] = {"access_token": "at", "token_type": "Bearer"}
        body["id_token"] = entry["mint"](entry["nonce"])
        if self.token_body_size:
            body["padding"] = "x" * self.token_body_size
        return httpx.Response(200, json=body)

    def issue_code(self, nonce: str, challenge: str, mint: Mint) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"nonce": nonce, "challenge": challenge, "mint": mint}
        return code


class Env:
    def __init__(self, app: FastAPI, idp: FakeIdp, db: Database) -> None:
        self.app = app
        self.idp = idp
        self.db = db
        self.client = TestClient(app, base_url="https://testserver", follow_redirects=False)

    def org(self, name: str = "Acme") -> Org:
        return Org(self.client, name)

    def slug(self, org: Org) -> str:
        return str(self.client.get("/api/me", headers=org.auth).json()["org"]["slug"])

    def configure(self, org: Org, **over: Any) -> httpx.Response:
        body: dict[str, Any] = {
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "allowed_domains": ["example.com"],
            "default_role": "viewer",
            "enabled": True,
        }
        body.update(over)
        return self.client.put("/api/sso", json=body, headers=org.auth)

    def start(self, org: Org) -> tuple[str, dict[str, str]]:
        """Hit /login; returns (state, authorization-request params)."""
        response = self.client.get("/api/sso/login", params={"org": self.slug(org)})
        assert response.status_code == 302, response.text
        params = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(response.headers["location"]).query)
        )
        return params["state"], params

    def login(
        self,
        org: Org,
        *,
        mint: Mint | None = None,
        **claims: Any,
    ) -> httpx.Response:
        """A full login through the real endpoints; returns the callback response."""
        state, params = self.start(org)
        nonce = params["nonce"]
        make = mint or (lambda n: self.idp.sign(self.idp.claims(n, **claims)))
        code = self.idp.issue_code(nonce, params["code_challenge"], make)
        return self.client.get("/api/sso/callback", params={"code": code, "state": state})


@pytest.fixture
def env(db: Database) -> Env:
    idp = FakeIdp()
    app = make_app(db, public_url=PUBLIC_URL, dashboard_url=DASH)
    extras = app.state.kg.extras
    extras["oidc_http"] = lambda: httpx.Client(transport=httpx.MockTransport(idp.handler))
    extras["oidc_resolver"] = lambda host: {"rebind.example.test": ["10.0.0.5"]}.get(
        host, [PUBLIC_IP]
    )
    return Env(app, idp, db)


def error_code(response: httpx.Response) -> str:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{DASH}/sso/error?code="), location
    return location.split("code=", 1)[1]


def session_headers(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{DASH}/sso/complete#token="), location
    assert "?" not in location
    return {"Authorization": "Bearer " + location.split("#token=", 1)[1]}


@pytest.fixture
def org(env: Env) -> Org:
    org = env.org()
    assert env.configure(org).status_code == 200
    return org


# ------------------------------------------------------------------------------- happy path


def test_full_login_flow_provisions_a_user_and_opens_a_session(env: Env, org: Org) -> None:
    assert env.configure(org, default_role="approver").status_code == 200
    first = env.client.get("/api/sso/login", params={"org": env.slug(org)})
    assert first.headers["location"].startswith(f"{ISSUER}/authorize?")
    assert first.headers["cache-control"] == "no-store"
    cookie = first.headers["set-cookie"]
    assert "kg_sso_state=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    state, params = env.start(org)
    assert params["response_type"] == "code"
    assert params["scope"] == "openid email profile"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == f"{PUBLIC_URL}/api/sso/callback"
    assert params["code_challenge_method"] == "S256"
    assert params["nonce"] and len(state) >= 32

    email = unique_email("newbie")
    code = env.idp.issue_code(
        params["nonce"],
        params["code_challenge"],
        lambda n: env.idp.sign(env.idp.claims(n, email=email.upper(), sub="abc")),
    )
    response = env.client.get("/api/sso/callback", params={"code": code, "state": state})
    headers = session_headers(response)
    me = env.client.get("/api/me", headers=headers).json()
    assert me["user"]["email"] == email
    assert me["user"]["role"] == "approver"
    assert me["org"]["id"] == org.id
    # The token endpoint saw basic auth and the verifier whose S256 matched the challenge.
    token_calls = [r for r in env.idp.requests if r.url.path == "/token"]
    assert len(token_calls) == 1
    assert token_calls[0].headers["authorization"].startswith("Basic ")
    with env.db.tx(org.id) as conn:
        user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        audit = conn.execute(
            "SELECT actor, payload_json FROM audit_records WHERE event_type = 'sso.login'"
        ).fetchall()
        states = conn.execute("SELECT count(*) AS n FROM sso_states WHERE state = %s", (state,))
        assert states.fetchone()["n"] == 0  # type: ignore[index]
    assert user is not None and user["password_hash"] is None and user["oidc_subject"] == "abc"
    assert audit and audit[-1]["actor"] == f"user:{user['id']}"
    assert email not in audit[-1]["payload_json"]


def test_client_secret_post_is_used_when_basic_is_not_offered(env: Env, org: Org) -> None:
    env.idp.auth_methods = ["client_secret_post"]
    headers = session_headers(env.login(org))
    assert env.client.get("/api/me", headers=headers).status_code == 200
    token_call = next(r for r in env.idp.requests if r.url.path == "/token")
    assert "authorization" not in token_call.headers
    assert b"client_secret=" in token_call.content


def test_es256_tokens_are_accepted(env: Env, org: Org) -> None:

    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="ec-1", use="sig", alg="ES256")
    original = env.idp.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [jwk]})
        return original(request)

    env.app.state.kg.extras["oidc_http"] = lambda: httpx.Client(
        transport=httpx.MockTransport(handler)
    )

    def mint(nonce: str) -> str:
        return jwt.encode(env.idp.claims(nonce), key, algorithm="ES256", headers={"kid": "ec-1"})

    assert session_headers(env.login(org, mint=mint))


# --------------------------------------------------------------------- token rejections


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"nonce": "not-the-nonce"}, "token_invalid"),
        ({"nonce": None}, "token_invalid"),
        ({"aud": "someone-else"}, "token_invalid"),
        ({"aud": [CLIENT_ID, "other"]}, "token_invalid"),
        ({"iss": "https://evil.example.test"}, "token_invalid"),
        ({"exp": int(time.time()) - 3600}, "token_invalid"),
        ({"iat": int(time.time()) + 3600}, "token_invalid"),
        ({"iat": int(time.time()) - 86400}, "token_invalid"),
        ({"sub": None}, "token_invalid"),
        ({"email_verified": False}, "email_not_verified"),
        ({"email_verified": None}, "email_not_verified"),
        ({"email_verified": "false"}, "email_not_verified"),
        ({"email": None}, "email_not_verified"),
        ({"email": "someone@evil.test"}, "domain_not_allowed"),
        ({"email": "someone@sub.example.com"}, "domain_not_allowed"),
    ],
)
def test_bad_claims_are_rejected(env: Env, org: Org, over: dict[str, Any], code: str) -> None:
    assert error_code(env.login(org, **over)) == code


def test_wrong_nonce_is_rejected_even_with_a_valid_signature(env: Env, org: Org) -> None:
    state, params = env.start(org)
    code = env.idp.issue_code(
        params["nonce"],
        params["code_challenge"],
        lambda _n: env.idp.sign(env.idp.claims("a-different-nonce")),
    )
    response = env.client.get("/api/sso/callback", params={"code": code, "state": state})
    assert error_code(response) == "token_invalid"


def test_alg_none_is_rejected(env: Env, org: Org) -> None:
    def mint(nonce: str) -> str:
        header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        body = _b64(json.dumps(env.idp.claims(nonce)).encode())
        return f"{header}.{body}."

    assert error_code(env.login(org, mint=mint)) == "token_invalid"


def test_hs256_signed_with_the_public_key_is_rejected(env: Env, org: Org) -> None:
    def mint(nonce: str) -> str:
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "key-1"}).encode())
        body = _b64(json.dumps(env.idp.claims(nonce)).encode())
        signing = f"{header}.{body}".encode()
        sig = hmac.new(env.idp.key.public_pem, signing, hashlib.sha256).digest()
        return f"{header}.{body}.{_b64(sig)}"

    assert error_code(env.login(org, mint=mint)) == "token_invalid"


def test_a_token_signed_by_another_key_is_rejected(env: Env, org: Org) -> None:
    rogue = Keypair("key-1")  # same kid, different key
    mint = lambda n: env.idp.sign(env.idp.claims(n), rogue)  # noqa: E731
    assert error_code(env.login(org, mint=mint)) == "token_invalid"


def test_unknown_kid_is_rejected_and_does_not_hammer_the_idp(env: Env, org: Org) -> None:
    rogue = Keypair("never-published")
    mint = lambda n: env.idp.sign(env.idp.claims(n), rogue)  # noqa: E731
    for _ in range(4):
        assert error_code(env.login(org, mint=mint)) == "token_invalid"
    jwks_calls = [r for r in env.idp.requests if r.url.path == "/jwks"]
    assert len(jwks_calls) == 1  # fetched once, then the cooldown stops refetching


def test_key_rotation_refetches_the_jwks_once(env: Env, org: Org) -> None:
    assert session_headers(env.login(org))
    cache = env.app.state.kg.extras["oidc_cache"]
    cache.refresh_cooldown = 0.0
    new = Keypair("key-2")
    env.idp.published = [new]
    mint = lambda n: env.idp.sign(env.idp.claims(n), new)  # noqa: E731
    assert session_headers(env.login(org, mint=mint))
    assert len([r for r in env.idp.requests if r.url.path == "/jwks"]) == 2


def test_discovery_is_cached(env: Env, org: Org) -> None:
    session_headers(env.login(org))
    session_headers(env.login(org))
    assert len([r for r in env.idp.requests if "openid-configuration" in r.url.path]) == 1


def test_idp_failures_become_stable_codes(env: Env, org: Org) -> None:
    env.idp.token_status = 500
    assert error_code(env.login(org)) == "token_exchange_failed"
    env.idp.token_status = 200
    env.idp.token_body_size = 2_000_000
    assert error_code(env.login(org)) == "token_exchange_failed"


def test_discovery_issuer_mismatch_makes_sso_unavailable(env: Env, org: Org) -> None:
    env.idp.discovery_issuer = "https://other.example.test"
    response = env.client.get("/api/sso/login", params={"org": env.slug(org)})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "sso_not_available"


# ------------------------------------------------------------------------------ state


def test_unknown_state_is_rejected(env: Env, org: Org) -> None:
    response = env.client.get("/api/sso/callback", params={"code": "c", "state": "nope"})
    assert error_code(response) == "invalid_state"
    assert error_code(env.client.get("/api/sso/callback")) == "invalid_state"
    assert error_code(env.client.get("/api/sso/callback", params={"code": "c"})) == "invalid_state"


def test_state_is_single_use(env: Env, org: Org) -> None:
    state, params = env.start(org)
    code = env.idp.issue_code(
        params["nonce"], params["code_challenge"], lambda n: env.idp.sign(env.idp.claims(n))
    )
    first = env.client.get("/api/sso/callback", params={"code": code, "state": state})
    assert session_headers(first)
    replay = env.client.get("/api/sso/callback", params={"code": code, "state": state})
    assert error_code(replay) == "invalid_state"


def test_expired_state_is_rejected_and_consumed(env: Env, org: Org) -> None:
    state, params = env.start(org)
    with env.db.tx(org.id) as conn:
        conn.execute(
            "UPDATE sso_states SET created_at = now() - interval '11 minutes' WHERE state = %s",
            (state,),
        )
    code = env.idp.issue_code(
        params["nonce"], params["code_challenge"], lambda n: env.idp.sign(env.idp.claims(n))
    )
    response = env.client.get("/api/sso/callback", params={"code": code, "state": state})
    assert error_code(response) == "invalid_state"
    with env.db.tx(org.id) as conn:
        left = conn.execute("SELECT count(*) AS n FROM sso_states WHERE state = %s", (state,))
        assert left.fetchone()["n"] == 0  # type: ignore[index]


def test_idp_error_param_is_not_reflected_and_burns_the_state(env: Env, org: Org) -> None:
    state, _ = env.start(org)
    response = env.client.get(
        "/api/sso/callback",
        params={"error": "access_denied<script>", "error_description": "boom", "state": state},
    )
    assert error_code(response) == "idp_error"
    assert "script" not in response.headers["location"]
    again = env.client.get("/api/sso/callback", params={"code": "c", "state": state})
    assert error_code(again) == "invalid_state"


def test_a_bad_login_still_burns_the_state(env: Env, org: Org) -> None:
    state, params = env.start(org)
    code = env.idp.issue_code(
        params["nonce"],
        params["code_challenge"],
        lambda n: env.idp.sign(env.idp.claims(n, aud="x")),
    )
    assert error_code(env.client.get("/api/sso/callback", params={"code": code, "state": state}))
    assert (
        error_code(env.client.get("/api/sso/callback", params={"code": code, "state": state}))
        == "invalid_state"
    )


# ------------------------------------------------------------------ provisioning rules


def test_existing_user_signs_in_and_subject_is_pinned(env: Env, org: Org) -> None:
    email = unique_email("member")
    invited = env.client.post(
        "/api/users",
        json={"email": email, "role": "approver"},  # no password: SSO-only
        headers=org.auth,
    )
    assert invited.status_code == 201
    headers = session_headers(env.login(org, email=email, sub="s-1"))
    assert env.client.get("/api/me", headers=headers).json()["user"]["role"] == "approver"
    assert session_headers(env.login(org, email=email, sub="s-1"))
    assert error_code(env.login(org, email=email, sub="s-2")) == "account_unavailable"
    with env.db.tx(org.id) as conn:
        row = conn.execute("SELECT oidc_subject FROM users WHERE email = %s", (email,)).fetchone()
    assert row is not None and row["oidc_subject"] == "s-1"


def test_an_account_created_with_a_password_is_never_claimed_by_an_idp_login(
    env: Env, org: Org
) -> None:
    """Whoever controls an IdP must not be able to become an existing password account."""
    assert error_code(env.login(org, email=org.email, sub="attacker")) == "account_unavailable"
    me = env.client.get("/api/me", headers=org.auth).json()["user"]
    assert me["email"] == org.email and me["role"] == "owner"
    with env.db.tx(org.id) as conn:
        row = conn.execute(
            "SELECT oidc_subject FROM users WHERE email = %s", (org.email,)
        ).fetchone()
    assert row is not None and row["oidc_subject"] is None


def test_an_email_in_another_org_is_refused_generically(env: Env, org: Org) -> None:
    other = env.org("Other")
    assert error_code(env.login(org, email=other.email)) == "account_unavailable"
    with env.db.tx(other.id) as conn:
        user = conn.execute("SELECT oidc_subject FROM users WHERE email = %s", (other.email,))
        assert user.fetchone()["oidc_subject"] is None  # type: ignore[index]


def test_a_disabled_user_is_refused_with_the_same_code(env: Env, org: Org) -> None:
    email = unique_email("gone")
    user = env.client.post(
        "/api/users", json={"email": email, "role": "viewer"}, headers=org.auth
    ).json()
    assert env.client.delete(f"/api/users/{user['id']}", headers=org.auth).status_code == 204
    assert error_code(env.login(org, email=email)) == "account_unavailable"


def test_disabled_config_and_missing_config_are_the_same_404(env: Env, org: Org) -> None:
    bare = env.org("Bare")
    assert env.configure(org, enabled=False).status_code == 200
    bodies = []
    for slug in (env.slug(org), env.slug(bare), "no-such-org"):
        response = env.client.get("/api/sso/login", params={"org": slug})
        assert response.status_code == 404
        bodies.append(response.json())
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["error"]["code"] == "sso_not_available"


def test_login_is_refused_when_the_plan_loses_sso(env: Env, org: Org, db: Database) -> None:
    gated = Env(make_app(db, plan={"features": ()}), env.idp, db)
    # The org was configured while it had the feature; the plan no longer allows it.
    response = gated.client.get("/api/sso/login", params={"org": env.slug(org)})
    assert response.status_code == 404


# --------------------------------------------------------------------------- config API


def test_config_roundtrip_never_returns_or_stores_the_secret_in_clear(env: Env, org: Org) -> None:
    put = env.configure(org, allowed_domains=["example.com", "corp.example.org"])
    assert put.status_code == 200
    got = env.client.get("/api/sso", headers=org.auth)
    for response in (put, got):
        assert CLIENT_SECRET not in response.text
        body = response.json()
        assert body["client_secret_set"] is True
        assert "client_secret" not in body and "client_secret_enc" not in body
        assert body["issuer"] == ISSUER
        assert body["allowed_domains"] == ["example.com", "corp.example.org"]
        assert body["redirect_uri"] == f"{PUBLIC_URL}/api/sso/callback"
    with env.db.tx(org.id) as conn:
        sealed = conn.execute("SELECT client_secret_enc FROM sso_configs").fetchone()
    assert sealed is not None and CLIENT_SECRET not in sealed["client_secret_enc"]
    assert env.app.state.kg.secrets.open(sealed["client_secret_enc"]) == CLIENT_SECRET


def test_omitting_the_secret_keeps_the_stored_one(env: Env, org: Org) -> None:
    body = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "allowed_domains": ["example.com"],
        "default_role": "approver",
    }
    assert env.client.put("/api/sso", json=body, headers=org.auth).status_code == 200
    assert session_headers(env.login(org))  # still authenticates at the IdP
    fresh = env.org("Fresh")
    missing = env.client.put("/api/sso", json=body, headers=fresh.auth)
    assert missing.status_code == 422 and missing.json()["error"]["code"] == "secret_required"


def test_delete_removes_the_config(env: Env, org: Org) -> None:
    assert env.client.delete("/api/sso", headers=org.auth).status_code == 204
    assert env.client.get("/api/sso", headers=org.auth).status_code == 404
    assert env.client.get("/api/sso/login", params={"org": env.slug(org)}).status_code == 404


def test_config_changes_are_audited_without_the_secret(env: Env, org: Org) -> None:
    with env.db.tx(org.id) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM audit_records WHERE event_type = 'sso.configured'"
        ).fetchall()
    assert rows and CLIENT_SECRET not in rows[-1]["payload_json"]


@pytest.mark.parametrize("role", ["admin", "approver", "viewer"])
def test_only_the_owner_configures_sso(env: Env, org: Org, role: str) -> None:
    headers = org.invite(role)
    assert env.client.get("/api/sso", headers=headers).status_code == 403
    body = {"issuer": ISSUER, "client_id": "x", "client_secret": "y", "allowed_domains": ["a.com"]}
    assert env.client.put("/api/sso", json=body, headers=headers).status_code == 403
    assert env.client.delete("/api/sso", headers=headers).status_code == 403
    assert env.client.get("/api/sso").status_code == 401
    assert env.client.get("/api/sso", headers=org.auth).status_code in (200, 404)


def test_the_default_role_can_never_be_admin(env: Env, org: Org) -> None:
    body = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "allowed_domains": ["example.com"],
        "default_role": "admin",
    }
    assert env.client.put("/api/sso", json=body, headers=org.auth).status_code == 422


def test_a_callback_without_the_browsers_own_state_cookie_is_refused(env: Env, org: Org) -> None:
    """Login CSRF: finishing someone else's login URL in a different browser must not sign in."""
    state, params = env.start(org)
    code = env.idp.issue_code(
        params["nonce"],
        params["code_challenge"],
        lambda n: env.idp.sign(env.idp.claims(n, email=unique_email("v"), sub="v-1")),
    )
    other_browser = TestClient(env.app, base_url="https://testserver", follow_redirects=False)
    response = other_browser.get("/api/sso/callback", params={"code": code, "state": state})
    assert error_code(response) == "invalid_state"
    # The state was consumed, so the legitimate browser cannot be tricked into replaying it either.
    assert error_code(env.client.get("/api/sso/callback", params={"code": code, "state": state}))


def test_a_plan_without_sso_cannot_configure_it(db: Database) -> None:
    gated = Env(make_app(db, plan={"features": ()}), FakeIdp(), db)
    owner = gated.org()
    for response in (
        gated.configure(owner),
        gated.client.get("/api/sso", headers=owner.auth),
        gated.client.delete("/api/sso", headers=owner.auth),
    ):
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "plan_required"


def test_org_b_cannot_read_or_change_org_a(env: Env, org: Org) -> None:
    other = env.org("Other")
    assert env.client.get("/api/sso", headers=other.auth).status_code == 404
    assert env.client.delete("/api/sso", headers=other.auth).status_code == 204
    assert env.client.get("/api/sso", headers=org.auth).json()["issuer"] == ISSUER
    assert (
        env.configure(other, issuer="https://other-idp.example.test", client_id="b").status_code
        == 200
    )
    mine = env.client.get("/api/sso", headers=org.auth).json()
    assert mine["issuer"] == ISSUER and mine["client_id"] == CLIENT_ID
    # a state minted for org A cannot be completed as org B
    state, params = env.start(org)
    code = env.idp.issue_code(
        params["nonce"], params["code_challenge"], lambda n: env.idp.sign(env.idp.claims(n))
    )
    session = session_headers(
        env.client.get("/api/sso/callback", params={"code": code, "state": state})
    )
    assert env.client.get("/api/me", headers=session).json()["org"]["id"] == org.id


@pytest.mark.parametrize(
    "issuer",
    [
        "http://idp.example.test",
        "ftp://idp.example.test",
        "https://127.0.0.1",
        "https://localhost.:443@evil.example.test",
        "https://169.254.169.254/latest",
        "https://10.1.2.3",
        "https://192.168.0.10",
        "https://172.16.5.5",
        "https://100.64.0.1",
        "https://0.0.0.0",
        "https://[::1]",
        "https://[::ffff:127.0.0.1]",
        "https://[fd00::1]",
        "https://[fe80::1]",
        "https://rebind.example.test",
        "https://user:pw@idp.example.test",  # pragma: allowlist secret
        "https://idp.example.test?x=1",
        "https://idp.example.test#frag",
        "https://",
        "idp.example.test",
        "",
    ],
)
def test_ssrf_prone_issuers_are_rejected(env: Env, issuer: str) -> None:
    owner = env.org()
    response = env.client.put(
        "/api/sso",
        json={
            "issuer": issuer,
            "client_id": "x",
            "client_secret": CLIENT_SECRET,
            "allowed_domains": ["example.com"],
        },
        headers=owner.auth,
    )
    assert response.status_code == 422, issuer
    assert CLIENT_SECRET not in response.text
    assert env.client.get("/api/sso", headers=owner.auth).status_code == 404


def test_http_and_private_issuers_are_allowed_only_with_the_dev_flag(db: Database) -> None:
    dev = Env(make_app(db, oidc_allow_http=True), FakeIdp(), db)
    owner = dev.org()
    ok = dev.client.put(
        "/api/sso",
        json={
            "issuer": "http://127.0.0.1:9000/realm",
            "client_id": "x",
            "client_secret": "y",
            "allowed_domains": ["example.com"],
        },
        headers=owner.auth,
    )
    assert ok.status_code == 200
    still_bad = dev.client.put(
        "/api/sso",
        json={
            "issuer": "http://u:p@127.0.0.1",
            "client_id": "x",
            "client_secret": "y",
            "allowed_domains": ["example.com"],
        },
        headers=owner.auth,
    )
    assert still_bad.status_code == 422


def test_endpoints_named_by_discovery_are_ssrf_checked_too(env: Env, org: Org) -> None:
    doc = env.idp.discovery()
    doc["token_endpoint"] = "https://169.254.169.254/token"
    env.idp.discovery = lambda: doc  # type: ignore[method-assign]
    assert error_code(env.login(org)) == "token_exchange_failed"
    assert not [r for r in env.idp.requests if r.url.host == "169.254.169.254"]


@pytest.mark.parametrize(
    "domains",
    [
        [],
        ["Example.com"],
        ["example"],
        ["exa mple.com"],
        ["-bad.com"],
        ["a@b.com"],
        ["x.com/"],
        ["*.example.com"],
        ["example.com."],
    ],
)
def test_allowed_domains_are_validated(env: Env, domains: list[str]) -> None:
    owner = env.org()
    response = env.configure(owner, allowed_domains=domains)
    assert response.status_code == 422, domains


def test_validation_errors_never_echo_the_secret(env: Env) -> None:
    owner = env.org()
    for body in (
        {"client_secret": CLIENT_SECRET},  # missing fields: FastAPI would echo the whole body
        {
            "issuer": ISSUER,
            "client_id": "x",
            "client_secret": CLIENT_SECRET,
            "allowed_domains": ["example.com"],
            "default_role": "owner",
        },
        {
            "issuer": ISSUER,
            "client_id": "x",
            "client_secret": CLIENT_SECRET,
            "allowed_domains": ["example.com"],
            "surprise": 1,
        },
    ):
        response = env.client.put("/api/sso", json=body, headers=owner.auth)
        assert response.status_code == 422
        assert CLIENT_SECRET not in response.text
    raw = env.client.put(
        "/api/sso",
        content=b"{not json" + CLIENT_SECRET.encode(),
        headers={**owner.auth, "content-type": "application/json"},
    )
    assert raw.status_code == 422 and CLIENT_SECRET not in raw.text


def test_the_secret_never_reaches_the_logs_or_the_browser(
    env: Env, org: Org, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env.idp.token_status = 500
    response = env.login(org)
    assert CLIENT_SECRET not in response.headers["location"]
    env.idp.token_status = 200
    session_headers(env.login(org))
    env.client.get("/api/sso", headers=org.auth)
    assert CLIENT_SECRET not in caplog.text
    assert CLIENT_SECRET not in repr(env.client.get("/api/sso", headers=org.auth).headers)
