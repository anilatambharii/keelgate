"""The HTTP client every ``keelgate.cloud`` component shares.

The wire protocol is documented in ``docs/cloud-protocol.md``. Any server that implements it can
back these classes: Keelgate Cloud is one, and a self-hosted service can be another.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_TIMEOUT_S: Final = 10.0
USER_AGENT: Final = "keelgate-cloud-client"
_RETRY_STATUS: Final = frozenset({502, 503, 504})
_BACKOFF_S: Final = (0.2, 0.5, 1.0)


class CloudError(Exception):
    """The control plane refused a request or could not be reached.

    ``code`` is the server's stable machine-readable code (or ``unreachable`` / ``bad_response``);
    ``status`` is the HTTP status, or ``None`` if there was no response. The message never
    contains the API key.
    """

    def __init__(self, message: str, *, code: str = "cloud_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class CloudAuthError(CloudError):
    """The API key was rejected (missing, wrong, revoked or expired)."""


class CloudQuotaError(CloudError):
    """The plan quota or a rate limit stopped the request. Retry later or upgrade the plan."""


class CloudClient:
    """A small synchronous client for the control plane's agent-facing API.

    It authenticates with an API key. The key names the tenant, so a client can only ever act for
    its own tenant: a request that names a different tenant is refused by the server.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send one request and return the decoded JSON body.

        Reads are retried on a transient failure. A write is retried only when it carries an
        ``idempotency_key`` the server uses to deduplicate it.
        """
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        retryable = method == "GET" or idempotency_key is not None
        attempts = len(_BACKOFF_S) + 1 if retryable else 1
        last: CloudError | None = None
        for attempt in range(attempts):
            try:
                response = self._http.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                last = CloudError(
                    f"control plane unreachable: {type(exc).__name__}", code="unreachable"
                )
            else:
                if response.status_code not in _RETRY_STATUS:
                    return self._decode(response)
                last = CloudError(
                    "control plane unavailable", code="unavailable", status=response.status_code
                )
            if attempt < attempts - 1:
                time.sleep(_BACKOFF_S[attempt])
        raise last if last is not None else CloudError("no attempt was made")

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise CloudError(
                    "control plane sent a malformed body", code="bad_response"
                ) from exc
        code, message = "cloud_error", f"control plane returned HTTP {response.status_code}"
        try:
            error = response.json()["error"]
            code, message = str(error["code"]), str(error["message"])
        except (ValueError, KeyError, TypeError):
            pass
        status = response.status_code
        if status in (401, 403):
            raise CloudAuthError(message, code=code, status=status)
        if status in (402, 429):
            raise CloudQuotaError(message, code=code, status=status)
        raise CloudError(message, code=code, status=status)

    def whoami(self) -> dict[str, Any]:
        """The tenant id, key id and plan this API key belongs to."""
        body: dict[str, Any] = self.request("GET", "/v1/whoami")
        return body

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()
