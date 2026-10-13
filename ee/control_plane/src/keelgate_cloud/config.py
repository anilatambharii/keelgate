"""Settings, read from the environment. Secrets are never given defaults that would work."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

TEST_MODE_KEY_PREFIX: Final = "sk_test_"
_REQUIRED_SECRET_LENGTH: Final = 32


class ConfigError(Exception):
    """The environment is missing something required, or holds something unsafe."""


@dataclass(frozen=True)
class Settings:
    """Everything the control plane reads from its environment."""

    database_url: str
    secret_key: str = field(repr=False)
    public_url: str = "http://localhost:8000"
    dashboard_url: str = "http://localhost:3000"
    stripe_api_key: str | None = field(default=None, repr=False)
    stripe_webhook_secret: str | None = field(default=None, repr=False)
    stripe_api_base: str | None = None
    # Live Stripe keys are refused unless this is set by the owner, in the environment, on purpose.
    allow_live_stripe: bool = False
    session_ttl_hours: int = 12
    # Pre-authentication limits, per client address (login also per e-mail at a third of this).
    signup_per_minute: int = 5
    login_per_minute: int = 30
    docs_enabled: bool = True
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    # Lets an OIDC issuer be plain http and/or on a private network (a local IdP). Never in prod.
    oidc_allow_http: bool = False

    def __post_init__(self) -> None:
        if len(self.secret_key) < _REQUIRED_SECRET_LENGTH:
            raise ConfigError("KEELGATE_CP_SECRET_KEY must be at least 32 characters")
        key = self.stripe_api_key
        if key and not key.startswith(TEST_MODE_KEY_PREFIX) and not self.allow_live_stripe:
            raise ConfigError(
                "refusing to start with a non-test Stripe key: live mode needs the owner's "
                "explicit approval (KEELGATE_CP_ALLOW_LIVE_STRIPE=1)"
            )

    @classmethod
    def from_env(cls) -> Settings:
        """Read settings from ``KEELGATE_CP_*`` variables."""
        env = os.environ
        try:
            database_url = env["KEELGATE_CP_DATABASE_URL"]
            secret_key = env["KEELGATE_CP_SECRET_KEY"]
        except KeyError as exc:
            raise ConfigError(f"{exc.args[0]} is required") from exc
        return cls(
            database_url=database_url,
            secret_key=secret_key,
            public_url=env.get("KEELGATE_CP_PUBLIC_URL", "http://localhost:8000"),
            dashboard_url=env.get("KEELGATE_CP_DASHBOARD_URL", "http://localhost:3000"),
            stripe_api_key=env.get("KEELGATE_CP_STRIPE_API_KEY") or None,
            stripe_webhook_secret=env.get("KEELGATE_CP_STRIPE_WEBHOOK_SECRET") or None,
            stripe_api_base=env.get("KEELGATE_CP_STRIPE_API_BASE") or None,
            allow_live_stripe=env.get("KEELGATE_CP_ALLOW_LIVE_STRIPE") == "1",
            session_ttl_hours=int(env.get("KEELGATE_CP_SESSION_TTL_HOURS", "12")),
            signup_per_minute=int(env.get("KEELGATE_CP_SIGNUP_PER_MINUTE", "5")),
            login_per_minute=int(env.get("KEELGATE_CP_LOGIN_PER_MINUTE", "30")),
            docs_enabled=env.get("KEELGATE_CP_DOCS", "1") == "1",
            oidc_allow_http=env.get("KEELGATE_CP_OIDC_ALLOW_HTTP") == "1",
            cors_origins=tuple(
                o
                for o in env.get("KEELGATE_CP_CORS_ORIGINS", "http://localhost:3000").split(",")
                if o
            ),
        )
