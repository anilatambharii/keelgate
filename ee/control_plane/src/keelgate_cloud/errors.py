"""Errors the API turns into stable ``{"error": {"code", "message"}}`` responses."""

from __future__ import annotations


class ApiError(Exception):
    """An error with an HTTP status and a stable machine-readable code."""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class AuthenticationError(ApiError):
    """No valid credential."""

    status = 401
    code = "unauthenticated"


class PermissionDeniedError(ApiError):
    """Authenticated, but the role or plan does not allow it."""

    status = 403
    code = "forbidden"


class NotFoundError(ApiError):
    """No such thing for this org. Another org's thing is reported identically."""

    status = 404
    code = "not_found"


class ConflictError(ApiError):
    """The request conflicts with the current state."""

    status = 409
    code = "conflict"


class ValidationFailedError(ApiError):
    """The input is well-formed JSON but not acceptable."""

    status = 422
    code = "invalid_input"


class QuotaExceededError(ApiError):
    """The plan's quota is used up."""

    status = 429
    code = "quota_exceeded"


class RateLimitedError(ApiError):
    """Too many requests for this key."""

    status = 429
    code = "rate_limited"


class SubscriptionInactiveError(ApiError):
    """The org's trial ended or subscription lapsed."""

    status = 402
    code = "subscription_inactive"


class PlanRequiredError(ApiError):
    """The feature is not part of the org's plan."""

    status = 402
    code = "plan_required"
