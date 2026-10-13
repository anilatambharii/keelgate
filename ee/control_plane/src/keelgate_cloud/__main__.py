"""Command line: ``python -m keelgate_cloud <command>``.

migrate           apply database migrations (needs KEELGATE_CP_OWNER_DATABASE_URL)
dev-init          create the local dev database and roles, migrate, write .env
serve             run the API (uvicorn)
sync-stripe       create or update Stripe products and prices from plans.yaml
report-usage      send this period's metered usage to Stripe
sweep-retention   delete data past each org's plan retention window
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Final

_DELEGATED: Final = {
    "sync-stripe": "keelgate_cloud.billing.cli",
    "report-usage": "keelgate_cloud.billing.cli",
    "sweep-retention": "keelgate_cloud.retention",
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch a command; returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""
    if command == "migrate":
        from keelgate_cloud.db import migrate  # noqa: PLC0415

        owner = os.environ.get("KEELGATE_CP_OWNER_DATABASE_URL")
        if not owner:
            print("KEELGATE_CP_OWNER_DATABASE_URL is required", file=sys.stderr)
            return 2
        applied = migrate(owner, app_role=os.environ.get("KEELGATE_CP_APP_ROLE", "kg_app"))
        print("applied:", ", ".join(applied) or "nothing to do")
        return 0
    if command == "dev-init":
        from keelgate_cloud.devtools import dev_init  # noqa: PLC0415

        dev_init()
        print("wrote .env (gitignored); run: python -m keelgate_cloud serve")
        return 0
    if command == "serve":
        import uvicorn  # noqa: PLC0415

        from keelgate_cloud.devtools import load_env  # noqa: PLC0415

        load_env()

        from keelgate_cloud.app import create_app  # noqa: PLC0415

        uvicorn.run(
            create_app(),
            host=os.environ.get("KEELGATE_CP_HOST", "127.0.0.1"),
            port=int(os.environ.get("KEELGATE_CP_PORT", "8000")),
        )
        return 0
    if command in _DELEGATED:
        module = importlib.import_module(_DELEGATED[command])
        return int(module.main(args))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
