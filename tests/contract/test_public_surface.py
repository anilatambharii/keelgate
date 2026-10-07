"""The shape of the public API: what is public, that it is documented, and that it has not drifted.

These tests may look at Keelgate's internals, because they are the ones that police the boundary.
The consumer tests next door may not.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

import pytest

import keelgate
from keelgate._internal import apidoc
from keelgate._internal.api import (
    PROVISIONAL,
    PROVISIONAL_MODULES,
    PROVISIONAL_SYMBOLS,
    PUBLIC_MODULES,
    PUBLIC_PACKAGES,
    STABLE,
    tier,
)
from tests.conftest import ROOT_DIR

ALL_PUBLIC = [*PUBLIC_PACKAGES, *(m for m in PUBLIC_MODULES if m not in PUBLIC_PACKAGES)]


def walk() -> list[tuple[str, bool]]:
    """Every module in the distribution as (dotted name, is_package)."""
    out = []
    for info in pkgutil.walk_packages(keelgate.__path__, "keelgate."):
        out.append((info.name, info.ispkg))
    return out


def test_every_module_is_either_underscored_or_deliberately_public() -> None:
    """The audit: a new public-looking module must be added to the registry on purpose."""
    stray = [
        name
        for name, is_package in walk()
        if not any(part.startswith("_") for part in name.split("."))
        and not is_package
        and name not in PUBLIC_MODULES
    ]
    assert stray == [], (
        f"these modules look public but are not in keelgate._internal.api.PUBLIC_MODULES: {stray}. "
        "Underscore them (they are implementation) or register them (they are API)."
    )


def test_every_public_package_is_registered() -> None:
    packages = {
        name
        for name, is_package in walk()
        if is_package and not any(p.startswith("_") for p in name.split("."))
    }
    assert packages == set(PUBLIC_PACKAGES) - {"keelgate"}


@pytest.mark.parametrize("module", ALL_PUBLIC)
def test_a_public_module_imports_and_exports_only_real_public_names(module: str) -> None:
    mod = importlib.import_module(module)
    names = apidoc.symbols_of(module)
    assert names, module
    for name in names:
        assert not name.startswith("_") or name == "__version__", (module, name)
        assert hasattr(mod, name), f"{module}.{name} is in the contract but does not exist"
    if module not in apidoc.MODULE_SYMBOLS and module != "keelgate.telemetry.attributes":
        assert len(set(names)) == len(names)


@pytest.mark.parametrize("module", ALL_PUBLIC)
def test_no_public_symbol_lacks_a_real_docstring(module: str) -> None:
    if module == "keelgate.telemetry.attributes":
        pytest.skip("attribute names are constants; the module docstring covers them")
    mod = importlib.import_module(module)
    undocumented = [
        n for n in apidoc.symbols_of(module) if not apidoc.has_real_doc(getattr(mod, n))
    ]
    assert undocumented == [], f"{module}: {undocumented}"


def test_the_stability_registry_names_only_things_that_exist() -> None:
    for module, names in PROVISIONAL_SYMBOLS.items():
        assert module in PUBLIC_PACKAGES
        missing = names - set(apidoc.symbols_of(module))
        assert not missing, f"{module}: {sorted(missing)} are not public symbols"
    assert set(ALL_PUBLIC) >= PROVISIONAL_MODULES


def test_every_symbol_has_exactly_one_stability_level() -> None:
    for module in ALL_PUBLIC:
        for name in apidoc.symbols_of(module):
            assert tier(module, name) in {STABLE, PROVISIONAL}


def test_the_core_contract_is_stable_and_the_framework_edges_are_not() -> None:
    for module, name in [
        ("keelgate.tools", "ToolGateway"),
        ("keelgate.tools", "tool"),
        ("keelgate.capabilities", "issue_grant"),
        ("keelgate.policy", "PolicyEngine"),
        ("keelgate.audit", "verify_chain"),
        ("keelgate.approvals", "ApprovalQueue"),
        ("keelgate.loop", "Loop"),
        ("keelgate.loop", "StopConditions"),
        ("keelgate.context", "ContextBuilder"),
        ("keelgate.memory", "SemanticMemory"),
        ("keelgate.llm", "LLMClient"),
        ("keelgate.testing", "FakeLLM"),
        ("keelgate.evals", "OutcomeMetric"),
        ("keelgate.telemetry", "instrument"),
        ("keelgate.adapters", "GovernedToolset"),
    ]:
        assert tier(module, name) == STABLE, (module, name)
    for module, name in [
        ("keelgate.llm.providers", "AnthropicClient"),
        ("keelgate.adapters.mcp", "GovernedMCPServer"),
        ("keelgate.loop", "replay"),
        ("keelgate.memory", "PostgresMemoryBackend"),
    ]:
        assert tier(module, name) == PROVISIONAL, (module, name)


def test_the_api_contract_document_matches_the_code() -> None:
    """The API snapshot: changing a public signature changes docs/api-contract.md.

    If this fails you have changed the public API. If that was deliberate, regenerate with
    `python -m keelgate._internal.apidoc`, and expect the diff to be reviewed as an API change
    (and to need a changelog entry and, if it breaks a stable symbol, a migration note).
    """
    committed = (ROOT_DIR / "docs" / "api-contract.md").read_text(encoding="utf-8")
    assert committed.replace("\r\n", "\n") == apidoc.render()


def test_the_version_is_a_plain_semantic_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", keelgate.__version__), keelgate.__version__


def test_the_integration_contract_names_every_public_symbol() -> None:
    """Every public symbol is mentioned by name in the prose contract (the API contract lists them all)."""
    text = (ROOT_DIR / "docs" / "integration-contract.md").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"`([A-Za-z_][\w.]*)", text))
    missing = [
        f"{module}.{name}"
        for module in PUBLIC_PACKAGES
        for name in apidoc.symbols_of(module)
        if name not in mentioned
        and not any(t.startswith(name) or t.endswith("." + name) for t in mentioned)
    ]
    assert not missing, f"not named in docs/integration-contract.md: {missing}"
