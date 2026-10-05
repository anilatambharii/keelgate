"""Packaging metadata is part of the contract: extras, version and layout."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

import keelgate

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Extras promised by AGENTS.md / the K0 brief.
# K2 added google, mcp and a2a (Gemini, the MCP SDK, the A2A SDK).
EXPECTED_EXTRAS = {
    "langgraph", "openai", "anthropic", "temporal", "cedar", "server", "google", "mcp", "a2a",
}  # fmt: skip


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    return data


def test_extras_declared(pyproject: dict[str, Any]) -> None:
    extras = set(pyproject["project"]["optional-dependencies"])
    assert extras == EXPECTED_EXTRAS


def test_every_extra_lists_at_least_one_dependency(pyproject: dict[str, Any]) -> None:
    for name, deps in pyproject["project"]["optional-dependencies"].items():
        assert deps, f"extra {name!r} declares no dependencies"


def test_version_matches_metadata(pyproject: dict[str, Any]) -> None:
    assert keelgate.__version__ == pyproject["project"]["version"]


def test_license_is_apache(pyproject: dict[str, Any]) -> None:
    assert pyproject["project"]["license"] == "Apache-2.0"
    assert (PROJECT_ROOT / "LICENSE").read_text().startswith("Apache License")


def test_requires_python_covers_the_ci_matrix(pyproject: dict[str, Any]) -> None:
    assert pyproject["project"]["requires-python"] == ">=3.11"


def test_core_dependencies_are_upper_bounded(pyproject: dict[str, Any]) -> None:
    """A library pinning only lower bounds breaks downstream resolvers later."""
    for dep in pyproject["project"]["dependencies"]:
        assert "<" in dep, f"dependency {dep!r} has no upper bound"
