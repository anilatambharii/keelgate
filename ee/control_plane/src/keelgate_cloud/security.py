"""Credentials: password hashing, API keys, session tokens and secret-at-rest encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

API_KEY_PREFIX: Final = "kg_live_"
SESSION_PREFIX: Final = "kgs_"
MIN_PASSWORD_LENGTH: Final = 12
_KEY_PREFIX_SHOWN: Final = 12

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """The password is too short to accept."""


def hash_password(password: str) -> str:
    """Argon2id hash of a password."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"passwords need at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


# A hash to verify against when the user does not exist, so login takes the same time either way.
_DUMMY_HASH: Final = _hasher.hash("not-a-real-password")


def verify_password(password: str, stored: str | None) -> bool:
    """Whether ``password`` matches ``stored``. Always does one hash comparison."""
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


_TOKEN_HASH_CONTEXT: Final = b"keelgate-cloud/token-hash/v1"


def sha256_hex(value: str) -> str:
    """Hex digest for looking up a token (an API key, a session token, a login e-mail).

    These values are 256-bit random tokens, so a fast hash is the right tool (a slow password hash
    would only cost every request time); passwords use Argon2 above. It is an HMAC under a fixed
    context string so the digests cannot be confused with a plain SHA-256 of the same text.
    """
    return hmac.new(_TOKEN_HASH_CONTEXT, value.encode(), hashlib.sha256).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """A fresh API key as (full key shown once, display prefix, stored hash)."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:_KEY_PREFIX_SHOWN], sha256_hex(key)


def looks_like_api_key(value: str) -> bool:
    """Cheap shape check done before any database lookup."""
    return value.startswith(API_KEY_PREFIX) and 40 <= len(value) <= 80


def new_session_token() -> tuple[str, str]:
    """A fresh session token as (token for the client, stored hash)."""
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    return token, sha256_hex(token)


def constant_time_equal(a: str, b: str) -> bool:
    """Timing-safe string comparison."""
    return hmac.compare_digest(a.encode(), b.encode())


class SecretBox:
    """Encrypts small secrets (an IdP client secret) with a key derived from the server secret."""

    def __init__(self, secret_key: str) -> None:
        digest = hashlib.sha256(b"keelgate-cloud/secretbox/v1\0" + secret_key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def seal(self, plaintext: str) -> str:
        """Encrypt."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def open(self, sealed: str) -> str:
        """Decrypt; raises ``ValueError`` if the value was tampered with or the key changed."""
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("secret could not be decrypted") from exc
