"""The consumer tests and the documented example may import ONLY the public API.

This is what makes them contract tests: if they pass, a downstream library that restricts itself
to the documented imports works. It checks every `import` and `from ... import` of keelgate in
those files against the registry in `keelgate._internal.api`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from keelgate._internal import apidoc
from keelgate._internal.api import PUBLIC_MODULES, PUBLIC_PACKAGES

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = set(PUBLIC_PACKAGES) | set(PUBLIC_MODULES)
CONSUMERS = [
    ROOT / "tests" / "contract" / "test_consumer.py",
    ROOT / "examples" / "use_keelgate_from_another_library.py",
    ROOT / "examples" / "governed_langgraph_agent.py",
    ROOT / "examples" / "mcp_server.py",
    ROOT / "examples" / "custom_policy_pack" / "run.py",
    ROOT / "examples" / "tutorial_governed_agent.py",
    ROOT / "examples" / "research_loop.py",
    ROOT
    / "examples"
    / "outcome_metric_plugin"
    / "src"
    / "keelgate_example_metrics"
    / "__init__.py",
]


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "keelgate"
        ):
            if node.module not in PUBLIC:
                problems.append(f"{path.name}:{node.lineno} imports private module {node.module}")
                continue
            allowed = set(apidoc.symbols_of(node.module))
            for alias in node.names:
                submodule = f"{node.module}.{alias.name}"
                if alias.name not in allowed and submodule not in PUBLIC:
                    problems.append(
                        f"{path.name}:{node.lineno} imports {alias.name} from {node.module}, "
                        "which is not a public symbol"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "keelgate" and alias.name not in PUBLIC:
                    problems.append(
                        f"{path.name}:{node.lineno} imports private module {alias.name}"
                    )
    return problems


@pytest.mark.parametrize("path", [p for p in CONSUMERS if p.exists()], ids=lambda p: p.name)
def test_consumer_code_imports_only_public_names(path: Path) -> None:
    assert violations(path) == []


def test_the_checker_actually_catches_a_private_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from keelgate.tools._gateway import ToolGateway\n"
        "from keelgate.tools import NotARealName\n"
        "import keelgate.loop._engine\n",
        encoding="utf-8",
    )
    found = violations(bad)
    assert len(found) == 3 and "private module" in found[0] and "not a public symbol" in found[1]


def test_every_documented_example_exists_and_is_checked() -> None:
    missing = [p.name for p in CONSUMERS if not p.exists()]
    assert missing == [], missing
