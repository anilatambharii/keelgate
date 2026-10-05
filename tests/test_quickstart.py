"""The phase's acceptance artefact must keep working: run the quickstart for real."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.conftest import OPA_URL_DEFAULT, skip_or_fail

ROOT = Path(__file__).resolve().parents[1]


def run_quickstart(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, our own script
        [sys.executable, str(ROOT / "examples" / "quickstart.py"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env or {})},
        check=False,
    )


def assert_shows_every_outcome(out: str) -> None:
    assert "-> OK" in out  # ALLOW
    assert "-> DENIED" in out  # DENY
    assert "policy_denied - symbol is on the restricted list" in out
    assert "-> APPROVAL_REQUIRED" in out  # REQUIRE_APPROVAL
    assert "needs EXPLICIT_SIGNOFF" in out
    assert "replayed stored result" in out
    assert "verify_chain: OK - 19 records" in out


def test_quickstart_shows_allow_deny_and_require_approval() -> None:
    result = run_quickstart()
    assert result.returncode == 0, result.stderr
    assert_shows_every_outcome(result.stdout)
    assert "rego-inprocess" in result.stdout


def test_quickstart_catches_tampering() -> None:
    result = run_quickstart("--tamper")
    assert result.returncode == 0, result.stderr
    assert "FAILED: record hash does not match its contents" in result.stdout
    assert "records were removed" in result.stdout
    assert "OK (!)" not in result.stdout


def test_quickstart_runs_against_a_real_opa_server() -> None:
    try:
        httpx.get(f"{OPA_URL_DEFAULT}/health", timeout=1.0).raise_for_status()
    except Exception as exc:
        skip_or_fail(f"no OPA server at {OPA_URL_DEFAULT} ({type(exc).__name__})")
    result = run_quickstart(env={"KEELGATE_OPA_URL": OPA_URL_DEFAULT})
    assert result.returncode == 0, result.stderr
    assert "opa-http" in result.stdout
    assert_shows_every_outcome(result.stdout)


@pytest.mark.parametrize("flag", [[], ["--tamper"]])
def test_quickstart_is_deterministic(flag: list[str]) -> None:
    """Hashes embed as_of, so two runs must print the same chain apart from key-derived values."""
    first, second = run_quickstart(*flag), run_quickstart(*flag)
    strip = lambda s: [ln.split("  ")[0:3] for ln in s.splitlines() if ln.lstrip().startswith("#")]  # noqa: E731
    assert strip(first.stdout) == strip(second.stdout)
