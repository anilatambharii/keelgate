"""Signed, expiring, scoped capability grants.

A grant is a PASETO ``v4.public`` token (Ed25519). Signing and verification use
different keys, so the component that *checks* grants never holds the ability to
*mint* them, and a leaked verifier key forges nothing. PASETO fixes the
algorithm per version, which removes the ``alg`` confusion class that JWT
verifiers must defend against by hand.

Every grant is bound to one agent, one tenant, a closed set of capabilities and
a spending budget. It carries a hard expiry, and verification refuses grants
whose lifetime exceeds ``max_ttl`` even if correctly signed. Time comes from an
injected clock so expiry is testable and replayable.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import pyseto
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from keelgate.capabilities._capability import Capability, InvalidCapabilityError
from keelgate.capabilities._errors import (
    GrantExpiredError,
    GrantInvalidError,
    GrantNotYetValidError,
    GrantRevokedError,
    UnknownKeyError,
)

GRANT_TYPE: Final = "keelgate.grant.v1"
# Bound into the token's signature. A PASETO minted for any other purpose fails
# verification here even when signed by the same key.
_IMPLICIT_ASSERTION: Final = b"keelgate:capability-grant:v1"
DEFAULT_MAX_TTL: Final = timedelta(hours=24)
_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PASETO_PARTS: Final = 4  # version.purpose.payload.footer

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Budget(BaseModel):
    """A spending ceiling, in whatever cost units the tools declare."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost: float = Field(ge=0, allow_inf_nan=False)


class CapabilityGrant(BaseModel):
    """The verified claims of a grant. Only ever built by :class:`GrantVerifier`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str
    issuer: str
    key_id: str
    agent_id: str
    tenant_id: str
    capabilities: frozenset[Capability]
    budget: Budget
    issued_at: datetime
    not_before: datetime
    expires_at: datetime

    def allows(self, capability: str) -> bool:
        """True only for an exact, explicitly granted capability."""
        return capability in self.capabilities


@dataclass(frozen=True)
class SignedGrant:
    """A freshly issued grant: the bearer ``token`` and its parsed ``grant``."""

    grant: CapabilityGrant
    # The token is a bearer credential; keep it out of reprs and logs.
    token: str = field(repr=False)


class GrantSigner:
    """Holds the Ed25519 private key and signs grants. Guard it like a root key."""

    def __init__(self, private_key_pem: bytes, *, key_id: str, issuer: str = "keelgate") -> None:
        _require_id("key_id", key_id)
        _require_id("issuer", issuer)
        self._key = pyseto.Key.new(version=4, purpose="public", key=private_key_pem)
        self._private_key_pem = private_key_pem
        self.key_id = key_id
        self.issuer = issuer

    @classmethod
    def generate(cls, *, key_id: str = "k1", issuer: str = "keelgate") -> GrantSigner:
        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return cls(pem, key_id=key_id, issuer=issuer)

    def public_key_pem(self) -> bytes:
        """The verifier-side half; safe to distribute."""
        private = serialization.load_pem_private_key(self._private_key_pem, password=None)
        return private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def sign(self, claims: Mapping[str, Any]) -> str:
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":"), allow_nan=False)
        footer = json.dumps({"kid": self.key_id}, separators=(",", ":"))
        token = pyseto.encode(
            self._key,
            payload.encode(),
            footer=footer.encode(),
            implicit_assertion=_IMPLICIT_ASSERTION,
        )
        return token.decode("ascii")

    def __repr__(self) -> str:  # never render key material
        return f"GrantSigner(key_id={self.key_id!r}, issuer={self.issuer!r})"


class RevocationList(Protocol):
    """Where revoked grant ids are recorded.

    Implement this to share revocations across processes.
    """

    def is_revoked(self, grant_id: str) -> bool: ...


class InMemoryRevocationList:
    """Process-local revocations. A shared store is needed for multi-process use."""

    def __init__(self) -> None:
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    def revoke(self, grant_id: str) -> None:
        with self._lock:
            self._revoked.add(grant_id)

    def is_revoked(self, grant_id: str) -> bool:
        with self._lock:
            return grant_id in self._revoked


def _require_id(label: str, value: object) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} {value!r} must match {_ID_RE.pattern}")


def issue_grant(
    signer: GrantSigner,
    *,
    agent_id: str,
    tenant_id: str,
    capabilities: Iterable[str],
    max_cost: float,
    ttl: timedelta,
    clock: Clock = utc_now,
    not_before: datetime | None = None,
    max_ttl: timedelta = DEFAULT_MAX_TTL,
) -> SignedGrant:
    """Mint a grant. Raises ``ValueError`` rather than issuing anything unsafe.

    ``capabilities`` must be non-empty and wildcard-free. ``ttl`` must be
    positive and no longer than ``max_ttl``.
    """
    _require_id("agent_id", agent_id)
    _require_id("tenant_id", tenant_id)
    try:
        caps = frozenset(Capability(c) for c in capabilities)
    except InvalidCapabilityError as exc:
        raise ValueError(str(exc)) from exc
    if not caps:
        raise ValueError("a grant must name at least one capability")
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    if ttl > max_ttl:
        raise ValueError(f"ttl {ttl} exceeds the maximum of {max_ttl}")

    issued_at = _utc(clock())
    nbf = _utc(not_before) if not_before is not None else issued_at
    grant = CapabilityGrant(
        grant_id=uuid.uuid4().hex,
        issuer=signer.issuer,
        key_id=signer.key_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        capabilities=caps,
        budget=Budget(max_cost=max_cost),
        issued_at=issued_at,
        not_before=nbf,
        expires_at=nbf + ttl,
    )
    return SignedGrant(grant=grant, token=signer.sign(_claims(grant)))


def _claims(grant: CapabilityGrant) -> dict[str, Any]:
    return {
        "typ": GRANT_TYPE,
        "jti": grant.grant_id,
        "iss": grant.issuer,
        "kid": grant.key_id,
        "sub": grant.agent_id,
        "tenant": grant.tenant_id,
        "caps": sorted(grant.capabilities),
        "budget": {"max_cost": grant.budget.max_cost},
        "iat": grant.issued_at.isoformat(),
        "nbf": grant.not_before.isoformat(),
        "exp": grant.expires_at.isoformat(),
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    return value.astimezone(UTC)


class GrantVerifier:
    """Verifies grant tokens against a set of trusted public keys.

    Holds only public keys: it can accept grants but never mint one.
    """

    def __init__(
        self,
        trusted_keys: Mapping[str, bytes],
        *,
        clock: Clock = utc_now,
        revocations: RevocationList | None = None,
        max_ttl: timedelta = DEFAULT_MAX_TTL,
        leeway: timedelta = timedelta(0),
    ) -> None:
        if not trusted_keys:
            raise ValueError("a verifier needs at least one trusted key")
        if leeway < timedelta(0):
            raise ValueError("leeway must not be negative")
        self._keys = {
            kid: pyseto.Key.new(version=4, purpose="public", key=pem)
            for kid, pem in trusted_keys.items()
        }
        self._clock = clock
        self._revocations = revocations
        self._max_ttl = max_ttl
        self._leeway = leeway

    def verify(self, token: str) -> CapabilityGrant:
        """Return the grant claims, or raise a :class:`GrantError`."""
        key = self._select_key(token)
        try:
            decoded = pyseto.decode(key, token, implicit_assertion=_IMPLICIT_ASSERTION)
        except Exception as exc:  # any failure is an untrustworthy token
            raise GrantInvalidError("signature verification failed") from exc
        payload, footer = decoded.payload, decoded.footer
        if not isinstance(payload, bytes) or not isinstance(footer, bytes):
            raise GrantInvalidError("token payload is not a byte string")
        grant = self._parse(payload, footer)

        now = _utc(self._clock())
        if now < grant.not_before - self._leeway:
            raise GrantNotYetValidError(f"grant {grant.grant_id} is not valid yet")
        if now >= grant.expires_at + self._leeway:
            raise GrantExpiredError(f"grant {grant.grant_id} expired")
        if self._revocations is not None and self._revocations.is_revoked(grant.grant_id):
            raise GrantRevokedError(f"grant {grant.grant_id} was revoked")
        return grant

    def _select_key(self, token: str) -> pyseto.KeyInterface:
        """Pick the verification key from the *unverified* footer.

        The footer only chooses among keys we already trust. It is covered by the
        signature, and the verified ``kid`` is cross-checked in :meth:`_parse`.
        """
        if not isinstance(token, str):
            raise GrantInvalidError("token must be a string")
        parts = token.split(".")
        if len(parts) != _PASETO_PARTS or parts[0] != "v4" or parts[1] != "public":
            raise GrantInvalidError("not a v4.public token with a footer")
        try:
            footer = json.loads(_b64url_decode(parts[3]))
            kid = footer["kid"]
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise GrantInvalidError("unreadable footer") from exc
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise UnknownKeyError("token signed by an untrusted key")
        return key

    def _parse(self, payload: bytes, footer: bytes) -> CapabilityGrant:
        try:
            claims = json.loads(payload)
            if claims["typ"] != GRANT_TYPE:
                raise GrantInvalidError("wrong token type")
            if json.loads(footer)["kid"] != claims["kid"]:
                raise GrantInvalidError("footer and payload disagree on the key id")
            grant = CapabilityGrant(
                grant_id=claims["jti"],
                issuer=claims["iss"],
                key_id=claims["kid"],
                agent_id=claims["sub"],
                tenant_id=claims["tenant"],
                capabilities=frozenset(Capability(c) for c in claims["caps"]),
                budget=Budget(max_cost=claims["budget"]["max_cost"]),
                issued_at=_parse_time(claims["iat"]),
                not_before=_parse_time(claims["nbf"]),
                expires_at=_parse_time(claims["exp"]),
            )
        except GrantInvalidError:
            raise
        except (
            ValueError,
            KeyError,
            TypeError,
            ValidationError,
            InvalidCapabilityError,
        ) as exc:
            raise GrantInvalidError("malformed grant claims") from exc
        if not grant.capabilities:
            raise GrantInvalidError("grant names no capabilities")
        for label, value in (
            ("agent_id", grant.agent_id),
            ("tenant_id", grant.tenant_id),
        ):
            if not _ID_RE.fullmatch(value):
                raise GrantInvalidError(f"{label} is malformed")
        lifetime = grant.expires_at - grant.not_before
        if lifetime <= timedelta(0) or lifetime > self._max_ttl:
            raise GrantInvalidError("grant lifetime is outside the permitted range")
        return grant


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    return _utc(datetime.fromisoformat(value))


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class BudgetLedger(Protocol):
    """Where a grant's remaining budget is tracked.

    Implement this to share budgets across processes. Reservations must be atomic so spend can
    never exceed the grant.
    """

    def try_reserve(self, grant_id: str, amount: float, limit: float) -> bool: ...

    def release(self, grant_id: str, amount: float) -> None: ...

    def spent(self, grant_id: str) -> float: ...


class InMemoryBudgetLedger:
    """Atomic per-grant spend tracking.

    Process-local; a shared store is needed wherever several processes honour the
    same grant.
    """

    def __init__(self) -> None:
        self._spent: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_reserve(self, grant_id: str, amount: float, limit: float) -> bool:
        """Spend ``amount`` against ``limit`` atomically; False if it would exceed."""
        if amount < 0 or math.isnan(amount):  # negative or NaN can never be a cost
            return False
        with self._lock:
            current = self._spent.get(grant_id, 0.0)
            if current + amount > limit:
                return False
            self._spent[grant_id] = current + amount
            return True

    def release(self, grant_id: str, amount: float) -> None:
        """Return a reservation for an action that never executed."""
        with self._lock:
            self._spent[grant_id] = max(0.0, self._spent.get(grant_id, 0.0) - amount)

    def spent(self, grant_id: str) -> float:
        with self._lock:
            return self._spent.get(grant_id, 0.0)
