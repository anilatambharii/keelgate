"""Local development bootstrap: a database, two roles, the schema, and a ``.env`` to use them."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Final

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from keelgate_cloud.db import migrate

DEFAULT_ADMIN_DSN: Final = (
    "postgresql://keelgate:keelgate@localhost:5432/postgres"  # pragma: allowlist secret
)
DEV_DATABASE: Final = "keelgate_cloud"
OWNER_ROLE: Final = "kg_owner"
APP_ROLE: Final = "kg_app"


def dev_init(admin_dsn: str = DEFAULT_ADMIN_DSN, env_path: Path = Path(".env")) -> dict[str, str]:
    """Create the dev database and roles if missing, migrate, and write ``.env``.

    Passwords are generated once and kept in ``.env`` (gitignored). Run again to migrate.
    """
    existing = _read_env(env_path)
    owner_pw = existing.get("OWNER_PASSWORD") or secrets.token_urlsafe(18)
    app_pw = existing.get("APP_PASSWORD") or secrets.token_urlsafe(18)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        for role, password in ((OWNER_ROLE, owner_pw), (APP_ROLE, app_pw)):
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
            else:
                admin.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
        if not admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (DEV_DATABASE,)
        ).fetchone():
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(DEV_DATABASE), sql.Identifier(OWNER_ROLE)
                )
            )
    base = conninfo_to_dict(admin_dsn)

    def dsn(user: str, password: str) -> str:
        return make_conninfo(
            user=user,
            password=password,
            dbname=DEV_DATABASE,
            host=base.get("host"),
            port=base.get("port"),
        )

    owner_dsn, app_dsn = dsn(OWNER_ROLE, owner_pw), dsn(APP_ROLE, app_pw)
    migrate(owner_dsn, app_role=APP_ROLE)
    values = {
        "OWNER_PASSWORD": owner_pw,
        "APP_PASSWORD": app_pw,
        "KEELGATE_CP_DATABASE_URL": app_dsn,
        "KEELGATE_CP_OWNER_DATABASE_URL": owner_dsn,
        "KEELGATE_CP_SECRET_KEY": existing.get("KEELGATE_CP_SECRET_KEY")
        or secrets.token_urlsafe(48),
        "KEELGATE_CP_STRIPE_API_KEY": existing.get("KEELGATE_CP_STRIPE_API_KEY", "sk_test_local"),
        "KEELGATE_CP_STRIPE_API_BASE": existing.get(
            "KEELGATE_CP_STRIPE_API_BASE", "http://localhost:12111"
        ),
        "KEELGATE_CP_STRIPE_WEBHOOK_SECRET": existing.get(
            "KEELGATE_CP_STRIPE_WEBHOOK_SECRET", "whsec_local_test_secret"
        ),
    }
    env_path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return values


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    pairs = (
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    return {k.strip(): v.strip() for k, v in pairs if not k.startswith("#")}


def load_env(path: Path = Path(".env")) -> None:
    """Put the values of a dev ``.env`` into the environment, without overriding what is set."""
    import os  # noqa: PLC0415

    for key, value in _read_env(path).items():
        os.environ.setdefault(key, value)
