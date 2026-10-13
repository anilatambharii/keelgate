"""Shared test helpers: an app on a throwaway database, and a live server for the OSS client."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn

from keelgate_cloud.app import create_app
from keelgate_cloud.config import Settings
from keelgate_cloud.plans import PlanCatalogue, default_catalogue

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from keelgate_cloud.db import Database

SECRET = "test-secret-key-0123456789-0123456789"  # pragma: allowlist secret
PASSWORD = "correct horse battery staple"  # pragma: allowlist secret


def make_settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"database_url": "unused", "secret_key": SECRET}
    base.update(over)
    return Settings(**base)


def make_app(db: Database, *, plan: dict[str, Any] | None = None, **over: Any) -> FastAPI:
    """An app on ``db``. ``plan`` overrides fields of the sign-up (team) plan for this app only."""
    catalogue = default_catalogue()
    if plan:
        plans = {pid: catalogue.get(pid) for pid in catalogue.ids()}
        plans["team"] = plans["team"].model_copy(update=plan)
        catalogue = PlanCatalogue(plans)
    return create_app(make_settings(**over), db=db, catalogue=catalogue)


def unique_email(prefix: str = "u") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


class Org:
    """A signed-up org: its session token and an API key."""

    def __init__(self, client: TestClient, name: str = "Acme") -> None:
        self.client = client
        self.email = unique_email("owner")
        response = client.post(
            "/api/signup", json={"org_name": name, "email": self.email, "password": PASSWORD}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        self.token: str = body["token"]
        self.id: str = body["org"]["id"]
        self.api_key: str = self.create_key()

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def key_auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def create_key(self, name: str = "agent") -> str:
        response = self.client.post("/api/keys", json={"name": name}, headers=self.auth)
        assert response.status_code == 201, response.text
        return str(response.json()["key"])

    def invite(self, role: str, *, tier: str = "ONE_CLICK") -> dict[str, str]:
        email = unique_email(role)
        response = self.client.post(
            "/api/users",
            json={"email": email, "role": role, "password": PASSWORD, "approval_tier": tier},
            headers=self.auth,
        )
        assert response.status_code == 201, response.text
        login = self.client.post("/api/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        return {"Authorization": f"Bearer {login.json()['token']}"}


@contextmanager
def live_server(app: FastAPI) -> Iterator[str]:
    """Run the app on a free local port so the real HTTP client can talk to it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)
