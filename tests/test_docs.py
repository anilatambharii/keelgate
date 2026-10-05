"""Docs that cite evidence must cite evidence that exists."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TESTS = ROOT / "tests"


def all_test_names() -> set[str]:
    names: set[str] = set()
    for path in TESTS.glob("test_*.py"):
        names.update(re.findall(r"^\s*(?:async )?def (test_\w+)", path.read_text(), re.MULTILINE))
    return names


def test_every_test_cited_in_the_security_model_exists() -> None:
    text = (DOCS / "security-model.md").read_text()
    cited = set(re.findall(r"`(test_[A-Za-z0-9_*]+)(?:\.py)?`", text))
    assert cited, "the security model should cite its evidence"
    existing = all_test_names()
    missing = [
        name
        for name in sorted(cited)
        if not (
            any(fnmatch.fnmatch(e, name) for e in existing)
            if "*" in name
            else name in existing or (TESTS / f"{name}.py").exists()
        )
    ]
    assert not missing, f"security-model.md cites tests that do not exist: {missing}"


def test_every_test_file_cited_in_the_docs_exists() -> None:
    for doc in DOCS.rglob("*.md"):
        for filename in re.findall(r"`(test_[a-z0-9_]+\.py)`", doc.read_text()):
            assert (TESTS / filename).exists(), f"{doc.name} cites missing {filename}"


@pytest.mark.parametrize("doc", sorted(DOCS.rglob("*.md")), ids=lambda p: str(p.relative_to(DOCS)))
def test_relative_links_resolve(doc: Path) -> None:
    for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)#\s]+)", doc.read_text()):
        resolved = (doc.parent / target).resolve()
        assert resolved.exists(), f"{doc.relative_to(ROOT)} links to missing {target}"


def test_adr_numbers_are_unique_and_sequential() -> None:
    numbers = sorted(int(p.name[:4]) for p in (DOCS / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert numbers == list(range(1, len(numbers) + 1))


def test_every_adr_is_in_the_nav() -> None:
    nav = (ROOT / "mkdocs.yml").read_text()
    for adr in (DOCS / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"):
        assert f"adr/{adr.name}" in nav, f"{adr.name} is not in mkdocs.yml"


def test_the_integration_contract_lists_every_contract_module() -> None:
    text = (DOCS / "integration-contract.md").read_text()
    for module in (
        "tools",
        "capabilities",
        "policy",
        "loop",
        "context",
        "memory",
        "approvals",
        "audit",
        "telemetry",
        "evals",
        "llm",
        "testing",
    ):
        assert f"keelgate.{module}" in text, f"keelgate.{module} missing from the contract"
