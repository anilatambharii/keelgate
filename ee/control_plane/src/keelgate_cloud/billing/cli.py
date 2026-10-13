"""``python -m keelgate_cloud sync-stripe`` and ``report-usage``. Nothing here runs by itself."""

from __future__ import annotations

import re
import sys
from typing import Any

import stripe

from keelgate_cloud.billing.client import make_client
from keelgate_cloud.billing.sync import sync_stripe
from keelgate_cloud.billing.usage import report_usage
from keelgate_cloud.config import ConfigError, Settings
from keelgate_cloud.db import Database
from keelgate_cloud.devtools import load_env
from keelgate_cloud.plans import default_catalogue

USAGE = "usage: python -m keelgate_cloud sync-stripe | report-usage [--period YYYY-MM]"


def main(args: list[str]) -> int:
    """Run a billing command; ``args[0]`` is its name. Returns the exit code."""
    command = args[0] if args else ""
    if command not in ("sync-stripe", "report-usage"):
        print(USAGE, file=sys.stderr)
        return 2
    load_env()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    api = make_client(settings)
    if api is None:
        print(
            "error: KEELGATE_CP_STRIPE_API_KEY is not set (test-mode key sk_test_...)",
            file=sys.stderr,
        )
        return 2
    try:
        if command == "sync-stripe":
            for action in sync_stripe(api, default_catalogue()):
                print(action)
            return 0
        return _report(settings, api, args)
    except stripe.StripeError as exc:
        print(f"error: Stripe refused the request ({type(exc).__name__})", file=sys.stderr)
        return 1


def _report(settings: Settings, api: Any, args: list[str]) -> int:
    period = None
    if "--period" in args:
        idx = args.index("--period")
        period = args[idx + 1] if idx + 1 < len(args) else ""
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
            print("error: --period must look like 2026-10", file=sys.stderr)
            return 2
    db = Database(settings.database_url)
    try:
        reports = report_usage(db, api, default_catalogue(), period)
    finally:
        db.close()
    failed = 0
    for report in reports:
        who = f"org {report.org_id} {report.period}"
        if report.error:
            failed += 1
            print(f"{who}: FAILED ({report.error}); will retry next run")
        else:
            print(f"{who}: sent {report.sent}, total {report.reported_total}")
    print(f"{len(reports)} org(s), {failed} failed")
    return 1 if failed else 0
