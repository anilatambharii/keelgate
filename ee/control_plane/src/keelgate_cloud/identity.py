"""Orgs, users, sessions and API keys."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import psycopg.errors

from keelgate_cloud import security
from keelgate_cloud.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    SubscriptionInactiveError,
    ValidationFailedError,
)

if TYPE_CHECKING:
    from keelgate_cloud.db import Database, Row
    from keelgate_cloud.plans import PlanCatalogue

Role = Literal["owner", "admin", "approver", "viewer"]
ROLES: Final = ("owner", "admin", "approver", "viewer")
_LAST_USED_REFRESH: Final = timedelta(minutes=1)
_ACTIVE_STATUSES: Final = frozenset({"trialing", "active", "past_due"})
SIGNUP_PLAN: Final = "team"
MAX_ACTIVE_KEYS: Final = 25


@dataclass(frozen=True)
class Principal:
    """Who is calling, resolved from a session token or an API key."""

    org_id: str
    kind: Literal["user", "api_key"]
    user_id: str | None = None
    email: str | None = None
    role: Role | None = None
    approval_tier: str = "ONE_CLICK"
    api_key_id: str | None = None

    @property
    def actor(self) -> str:
        """How this caller appears in audit records."""
        return f"user:{self.user_id}" if self.kind == "user" else f"key:{self.api_key_id}"


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "org"
    return f"{base}-{secrets.token_hex(3)}"


def _normalise_email(email: str) -> str:
    value = email.strip().lower()
    if "@" not in value[1:] or len(value) > 254 or " " in value:
        raise ValidationFailedError("that does not look like an e-mail address")
    return value


class Identity:
    """Account operations. Every method states which org it acts for; the database enforces it."""

    def __init__(self, db: Database, catalogue: PlanCatalogue, *, session_ttl_hours: int = 12):
        self._db = db
        self._catalogue = catalogue
        self._session_ttl = timedelta(hours=session_ttl_hours)

    # ------------------------------------------------------------------------------ sign-up

    def signup(self, *, org_name: str, email: str, password: str) -> tuple[Row, str]:
        """Create an org on a trial of the sign-up plan with its first (owner) user.

        Returns the org row and a session token for the new owner.
        """
        email = _normalise_email(email)
        org_name = org_name.strip()
        if not 1 <= len(org_name) <= 200:
            raise ValidationFailedError("organisation name must be 1 to 200 characters")
        try:
            password_hash = security.hash_password(password)
        except security.WeakPasswordError as exc:
            raise ValidationFailedError(str(exc), code="weak_password") from exc
        plan = self._catalogue.get(SIGNUP_PLAN)
        org_id = str(uuid.uuid4())
        trial_end = datetime.now(UTC) + timedelta(days=plan.trial_days)
        try:
            with self._db.tx(org_id) as conn:
                org = conn.execute(
                    "INSERT INTO orgs (id, name, slug, plan, billing_status, trial_ends_at) "
                    "VALUES (%s, %s, %s, %s, 'trialing', %s) RETURNING *",
                    (org_id, org_name, _slugify(org_name), plan.id, trial_end),
                ).fetchone()
                user_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO users (id, org_id, email, password_hash, role) "
                    "VALUES (%s, %s, %s, %s, 'owner')",
                    (user_id, org_id, email, password_hash),
                )
                token = self._new_session(conn, org_id, user_id)
        except psycopg.errors.UniqueViolation as exc:
            raise ConflictError(
                "an account with that e-mail already exists", code="account_exists"
            ) from exc
        assert org is not None  # noqa: S101 - RETURNING always yields the inserted row
        return org, token

    # --------------------------------------------------------------------------- sessions

    def login(self, *, email: str, password: str) -> str:
        """Check a password and open a session. Unknown user and wrong password look identical."""
        email = _normalise_email(email)
        with self._db.tx(email=email) as conn:
            user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        ok = security.verify_password(password, user["password_hash"] if user else None)
        if not (ok and user) or user["disabled_at"] is not None:
            raise AuthenticationError("incorrect e-mail or password", code="invalid_credentials")
        with self._db.tx(str(user["org_id"])) as conn:
            return self._new_session(conn, str(user["org_id"]), str(user["id"]))

    def _new_session(self, conn: psycopg.Connection[Any], org_id: str, user_id: str) -> str:
        token, token_hash = security.new_session_token()
        conn.execute(
            "INSERT INTO sessions (token_hash, org_id, user_id, expires_at) VALUES (%s,%s,%s,%s)",
            (token_hash, org_id, user_id, datetime.now(UTC) + self._session_ttl),
        )
        return token

    def open_session_for(self, org_id: str, user_id: str) -> str:
        """Open a session for a user that was authenticated elsewhere (single sign-on)."""
        with self._db.tx(org_id) as conn:
            return self._new_session(conn, org_id, user_id)

    def logout(self, token: str) -> None:
        """End a session. Unknown tokens are ignored."""
        token_hash = security.sha256_hex(token)
        with self._db.tx(session=token_hash) as conn:
            row = conn.execute("SELECT org_id FROM sessions").fetchone()
        if row:
            with self._db.tx(str(row["org_id"])) as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))

    def authenticate_session(self, token: str) -> Principal:
        """Resolve a session token to a user, or raise :class:`AuthenticationError`."""
        if not token.startswith(security.SESSION_PREFIX):
            raise AuthenticationError("invalid session")
        token_hash = security.sha256_hex(token)
        with self._db.tx(session=token_hash) as conn:
            session = conn.execute("SELECT * FROM sessions").fetchone()
        if session is None or session["expires_at"] <= datetime.now(UTC):
            raise AuthenticationError("session expired or invalid", code="invalid_session")
        org_id = str(session["org_id"])
        with self._db.tx(org_id) as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id = %s", (session["user_id"],)
            ).fetchone()
        if user is None or user["disabled_at"] is not None:
            raise AuthenticationError("session expired or invalid", code="invalid_session")
        return Principal(
            org_id=org_id,
            kind="user",
            user_id=str(user["id"]),
            email=user["email"],
            role=user["role"],
            approval_tier=user["approval_tier"],
        )

    # ------------------------------------------------------------------------------ keys

    def create_api_key(
        self, org_id: str, *, name: str, created_by: str | None, expires_in_days: int | None = None
    ) -> tuple[Row, str]:
        """Mint a key. The full key is returned once and never stored."""
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValidationFailedError("key name must be 1 to 100 characters")
        with self._db.tx(org_id) as conn:
            active = conn.execute(
                "SELECT count(*) AS n FROM api_keys WHERE revoked_at IS NULL"
            ).fetchone()
        if active is not None and active["n"] >= MAX_ACTIVE_KEYS:
            raise ConflictError(
                f"an org can have at most {MAX_ACTIVE_KEYS} active API keys; revoke one first",
                code="key_cap_reached",
            )
        key, prefix, key_hash = security.new_api_key()
        expires = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
        with self._db.tx(org_id) as conn:
            row = conn.execute(
                "INSERT INTO api_keys (org_id, name, key_prefix, key_hash, created_by, expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "RETURNING id, name, key_prefix, created_at, expires_at, last_used_at, revoked_at",
                (org_id, name, prefix, key_hash, created_by, expires),
            ).fetchone()
        assert row is not None  # noqa: S101
        return row, key

    def list_api_keys(self, org_id: str) -> list[Row]:
        """The org's keys (never the secret), newest first."""
        with self._db.tx(org_id) as conn:
            return conn.execute(
                "SELECT id, name, key_prefix, created_at, expires_at, last_used_at, revoked_at "
                "FROM api_keys ORDER BY created_at DESC"
            ).fetchall()

    def revoke_api_key(self, org_id: str, key_id: str) -> None:
        """Revoke a key. Idempotent."""
        with self._db.tx(org_id) as conn:
            found = conn.execute(
                "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()) WHERE id = %s "
                "RETURNING id",
                (key_id,),
            ).fetchone()
        if found is None:
            raise NotFoundError("no such API key")

    def authenticate_api_key(self, key: str) -> Principal:
        """Resolve an API key to its org, or raise :class:`AuthenticationError`."""
        if not security.looks_like_api_key(key):
            raise AuthenticationError("invalid API key", code="invalid_api_key")
        key_hash = security.sha256_hex(key)
        with self._db.tx(key=key_hash) as conn:
            row = conn.execute("SELECT * FROM api_keys").fetchone()
        now = datetime.now(UTC)
        if (
            row is None
            or row["revoked_at"] is not None
            or (row["expires_at"] is not None and row["expires_at"] <= now)
        ):
            raise AuthenticationError("invalid API key", code="invalid_api_key")
        org_id = str(row["org_id"])
        last = row["last_used_at"]
        if last is None or now - last > _LAST_USED_REFRESH:
            with self._db.tx(org_id) as conn:
                conn.execute(
                    "UPDATE api_keys SET last_used_at = %s WHERE id = %s", (now, row["id"])
                )
        return Principal(org_id=org_id, kind="api_key", api_key_id=str(row["id"]))

    # ------------------------------------------------------------------------------ users

    def list_users(self, org_id: str) -> list[Row]:
        """The org's users (never password hashes)."""
        with self._db.tx(org_id) as conn:
            return conn.execute(
                "SELECT id, email, display_name, role, approval_tier, created_at, disabled_at "
                "FROM users ORDER BY created_at"
            ).fetchall()

    def invite_user(
        self,
        org_id: str,
        *,
        email: str,
        role: str,
        password: str | None = None,
        approval_tier: str = "ONE_CLICK",
        granted_by_owner: bool = False,
    ) -> Row:
        """Add a user to the org. Without a password the user can only sign in with SSO.

        Clearance above ONE_CLICK can be granted only by an owner (``granted_by_owner``): an admin
        who could create a cleared approver could approve their own agent's requests.
        """
        email = _normalise_email(email)
        if role not in ROLES or role == "owner":
            raise ValidationFailedError("role must be admin, approver or viewer")
        if approval_tier not in ("ONE_CLICK", "EXPLICIT_SIGNOFF"):
            raise ValidationFailedError("unknown approval tier")
        if approval_tier != "ONE_CLICK" and not granted_by_owner:
            raise PermissionDeniedError("only an owner can grant EXPLICIT_SIGNOFF clearance")
        try:
            hashed = security.hash_password(password) if password else None
        except security.WeakPasswordError as exc:
            raise ValidationFailedError(str(exc), code="weak_password") from exc
        try:
            with self._db.tx(org_id) as conn:
                row = conn.execute(
                    "INSERT INTO users (org_id, email, password_hash, role, approval_tier) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "RETURNING id, email, display_name, role, approval_tier, created_at",
                    (org_id, email, hashed, role, approval_tier),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise ConflictError("that e-mail is already in use", code="account_exists") from exc
        assert row is not None  # noqa: S101
        return row

    def set_approval_tier(self, org_id: str, user_id: str, approval_tier: str) -> None:
        """Set the strictest approval tier a user is cleared to decide."""
        if approval_tier not in ("ONE_CLICK", "EXPLICIT_SIGNOFF"):
            raise ValidationFailedError("unknown approval tier")
        with self._db.tx(org_id) as conn:
            done = conn.execute(
                "UPDATE users SET approval_tier = %s WHERE id = %s", (approval_tier, user_id)
            )
        if not done.rowcount:
            raise NotFoundError("no such user")

    def disable_user(self, org_id: str, user_id: str) -> None:
        """Disable a user and end their sessions. The last owner cannot be disabled."""
        with self._db.tx(org_id) as conn:
            target = conn.execute("SELECT role FROM users WHERE id = %s", (user_id,)).fetchone()
            if target is None:
                raise NotFoundError("no such user")
            if target["role"] == "owner":
                owners = conn.execute(
                    "SELECT count(*) AS n FROM users WHERE role = 'owner' AND disabled_at IS NULL"
                ).fetchone()
                if owners is not None and owners["n"] <= 1:
                    raise ConflictError("an org needs at least one owner", code="last_owner")
            conn.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))

    # ------------------------------------------------------------------------------- orgs

    def get_org(self, org_id: str) -> Row:
        """The org row."""
        with self._db.tx(org_id) as conn:
            org = conn.execute("SELECT * FROM orgs").fetchone()
        if org is None:
            raise NotFoundError("no such organisation")
        return org

    def require_active(self, org_id: str) -> Row:
        """The org, or raise if its trial ended or its subscription lapsed.

        A trial that ran out is closed here, lazily, the first time it is noticed.
        """
        org = self.get_org(org_id)
        status = org["billing_status"]
        if (
            status == "trialing"
            and org["stripe_subscription_id"] is None
            and org["trial_ends_at"] is not None
            and org["trial_ends_at"] <= datetime.now(UTC)
        ):
            with self._db.tx(org_id) as conn:
                conn.execute("UPDATE orgs SET billing_status = 'trial_expired'")
            status = "trial_expired"
        if status not in _ACTIVE_STATUSES:
            raise SubscriptionInactiveError(
                "the trial has ended or the subscription is not active; "
                "choose a plan in the dashboard to continue"
            )
        return org
