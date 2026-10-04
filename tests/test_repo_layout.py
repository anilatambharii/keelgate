"""A fresh clone must contain everything `make setup && make check && make up` needs."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    "README.md",
    "SECURITY.md",
    ".env.example",
    ".gitignore",
    ".pre-commit-config.yaml",
    "docker-compose.dev.yml",
    "mkdocs.yml",
    "pyproject.toml",
    "ee/LICENSE",
    "docs/index.md",
    "docs/integration-contract.md",
    "docs/security-model.md",
    "docs/adr/0001-licensing-and-open-core.md",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/dependency-review.yml",
    ".github/workflows/secret-scan.yml",
]


@pytest.mark.parametrize("relpath", REQUIRED_FILES)
def test_required_file_exists(relpath: str) -> None:
    assert (PROJECT_ROOT / relpath).is_file(), f"missing {relpath}"


def test_env_example_contains_no_filled_secret() -> None:
    """`.env.example` is committed, so every secret-ish key must be empty."""
    secretish = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "SIGNING_KEY")
    offenders: list[str] = []
    for raw in (PROJECT_ROOT / ".env.example").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if any(marker in key.upper() for marker in secretish) and value.strip():
            offenders.append(key)
    assert not offenders, f"committed example has filled secrets: {offenders}"


def test_env_example_documents_the_live_execution_switch() -> None:
    text = (PROJECT_ROOT / ".env.example").read_text()
    assert "KEELGATE_ALLOW_LIVE_EXECUTION=false" in text


def test_ee_is_not_apache_licensed() -> None:
    """The open-core boundary: ee/ must carry its own proprietary license."""
    ee_license = (PROJECT_ROOT / "ee" / "LICENSE").read_text()
    assert "Apache License, Version 2.0 that governs" in ee_license
    assert "PROPRIETARY" in ee_license
