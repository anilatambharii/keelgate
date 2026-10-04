"""Every declared module must import cleanly.

Phase K0 ships empty modules, so this is the whole of its functional surface:
if the layout drifts from the integration contract, this fails first.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import keelgate

# The public API Tycheon depends on. Sourced from AGENTS.md and mirrored in
# docs/integration-contract.md. Removing an entry is a breaking change that
# requires a major version bump and a migration note.
CONTRACT_MODULES = [
    "keelgate.approvals",
    "keelgate.audit",
    "keelgate.capabilities",
    "keelgate.context",
    "keelgate.evals",
    "keelgate.llm",
    "keelgate.loop",
    "keelgate.memory",
    "keelgate.policy",
    "keelgate.telemetry",
    "keelgate.testing",
    "keelgate.tools",
]

ADAPTER_MODULES = [
    "keelgate.adapters",
    "keelgate.adapters.a2a",
    "keelgate.adapters.claude_agent_sdk",
    "keelgate.adapters.langgraph",
    "keelgate.adapters.mcp",
    "keelgate.adapters.openai_agents",
]


def test_top_level_import() -> None:
    assert keelgate.__version__
    assert keelgate.__doc__ is not None


@pytest.mark.parametrize("name", CONTRACT_MODULES)
def test_contract_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


@pytest.mark.parametrize("name", ADAPTER_MODULES)
def test_adapter_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


def test_no_module_in_tree_fails_to_import() -> None:
    """Walk the installed package so a new broken module cannot slip through."""
    failures: list[str] = []
    for info in pkgutil.walk_packages(keelgate.__path__, prefix="keelgate."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:
            failures.append(f"{info.name}: {exc!r}")
    assert not failures, "modules failed to import: " + "; ".join(failures)


def test_every_package_in_tree_is_declared() -> None:
    """The set of *packages* and the contract list must not drift apart.

    Submodules (``keelgate.policy.engine`` and so on) are implementation detail
    and free to grow; the package-level surface is the contract.
    """
    found = {
        info.name
        for info in pkgutil.walk_packages(keelgate.__path__, prefix="keelgate.")
        if info.ispkg
    }
    declared = set(CONTRACT_MODULES) | set(ADAPTER_MODULES)
    assert found == declared, (
        f"undeclared packages: {sorted(found - declared)}; "
        f"missing from tree: {sorted(declared - found)}"
    )


def test_package_is_typed() -> None:
    """py.typed must ship, or downstream `mypy --strict` sees Keelgate as Any."""
    assert (Path(next(iter(keelgate.__path__))) / "py.typed").is_file()
