"""Request-size and pre-authentication rate limits.

Both are per-process (see ``ratelimit.py``). Behind a reverse proxy, ``request.client`` is the
proxy: terminate TLS and apply per-client limits there as well, or run uvicorn with
``--proxy-headers`` and a trusted-proxy list so ``client.host`` is the real caller.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Final

from keelgate_cloud.errors import RateLimitedError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from keelgate_cloud.state import AppState

DEFAULT_MAX_BODY: Final = 4 * 1024 * 1024
# Endpoints that need no credentials get a much smaller body allowance.
PATH_LIMITS: Final = {
    "/api/signup": 8 * 1024,
    "/api/login": 8 * 1024,
    "/api/stripe/webhook": 512 * 1024,
}
_BODY_METHODS: Final = frozenset({"POST", "PUT", "PATCH"})


def limit_for(path: str) -> int:
    """The largest request body accepted at ``path``."""
    return PATH_LIMITS.get(path, DEFAULT_MAX_BODY)


class BodyLimitMiddleware:
    """Refuse request bodies over the limit, whether or not they declare a length."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._refuse(send)
            return
        seen = 0
        refused = False

        async def counted() -> Message:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    refused = True
                    return {"type": "http.disconnect"}
            return message

        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if refused:
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        await self.app(scope, counted, guarded_send)
        if refused and not started:
            await self._refuse(send)

    @staticmethod
    async def _refuse(send: Send) -> None:
        body = json.dumps(
            {"error": {"code": "payload_too_large", "message": "request body is too large"}}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"connection", b"close")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def client_ip(request: Request) -> str:
    """The peer address (see the module docstring for proxies)."""
    return request.client.host if request.client else "unknown"


def throttle(state: AppState, request: Request, bucket: str, per_minute: int, *keys: Any) -> None:
    """Rate-limit an unauthenticated action per client address and, optionally, per subject."""
    digest = (
        hashlib.sha256("|".join(str(k) for k in keys).encode()).hexdigest()[:16] if keys else ""
    )
    state.limiter.check(f"{bucket}:ip:{client_ip(request)}", per_minute)
    if keys:
        state.limiter.check(f"{bucket}:subject:{digest}", max(per_minute // 3, 3))


__all__ = ["BodyLimitMiddleware", "RateLimitedError", "client_ip", "limit_for", "throttle"]
