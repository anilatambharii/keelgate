"""The FastAPI application."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from keelgate.audit import AuditError

from keelgate_cloud.api import (
    accounts,
    agent,
    billing,
    exports,
    inbox,
    policies,
    settings,
    sso,
    usage,
)
from keelgate_cloud.config import Settings
from keelgate_cloud.db import Database
from keelgate_cloud.errors import ApiError
from keelgate_cloud.limits import BodyLimitMiddleware
from keelgate_cloud.state import AppState, build_state

if TYPE_CHECKING:
    from keelgate_cloud.plans import PlanCatalogue

log = logging.getLogger("keelgate_cloud")

ROUTERS = (
    agent.router,
    accounts.router,
    inbox.router,
    usage.router,
    policies.router,
    billing.router,
    sso.router,
    exports.router,
    settings.router,
)


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    """The one error shape every endpoint uses."""
    return {"error": {"code": code, "message": message}}


def create_app(
    config: Settings | None = None,
    *,
    db: Database | None = None,
    catalogue: PlanCatalogue | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own database and settings."""
    config = config or Settings.from_env()
    database = db or Database(config.database_url)
    app = FastAPI(
        title="Keelgate Cloud control plane",
        version="0.1.0",
        docs_url="/api/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
    )
    state: AppState = build_state(config, database, catalogue)
    app.state.kg = state
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type", "idempotency-key"],
    )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(AuditError)
    async def _audit_error(_: Request, exc: AuditError) -> JSONResponse:
        return JSONResponse(error_body("audit_error", str(exc)), status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(error_body("internal_error", "internal error"), status_code=500)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    for router in ROUTERS:
        app.include_router(router)
    return app
