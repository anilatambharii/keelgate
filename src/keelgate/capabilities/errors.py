"""Grant failures.

Every failure is a distinct type so the audit log records *why* a grant was
refused, while the model-facing message stays deliberately uninformative.
"""

from __future__ import annotations


class GrantError(Exception):
    """Base class: the presented grant cannot be used."""

    code = "grant_invalid"


class GrantInvalidError(GrantError):
    """Malformed, wrongly signed, wrong purpose, or otherwise untrustworthy."""

    code = "grant_invalid"


class UnknownKeyError(GrantError):
    """Signed by a key the verifier does not trust."""

    code = "grant_unknown_key"


class GrantExpiredError(GrantError):
    code = "grant_expired"


class GrantNotYetValidError(GrantError):
    code = "grant_not_yet_valid"


class GrantRevokedError(GrantError):
    code = "grant_revoked"
