"""ADR-0001: the open-source harness never depends on the proprietary ``ee/`` tree."""

from __future__ import annotations

import ast
import tomllib

from tests.conftest import ROOT_DIR

FORBIDDEN = ("keelgate_cloud", "ee")


def test_the_harness_never_imports_ee() -> None:
    offenders: list[str] = []
    for path in (ROOT_DIR / "src" / "keelgate").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            offenders += [
                f"{path.relative_to(ROOT_DIR)}: {name}"
                for name in names
                if name.split(".")[0] in FORBIDDEN
            ]
    assert not offenders, offenders


def test_the_published_package_contains_no_ee_code() -> None:
    config = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    build = config["tool"]["hatch"]["build"]["targets"]
    shipped = [
        *build["wheel"]["packages"],
        *build["wheel"].get("force-include", {}),
        *build["sdist"]["include"],
    ]
    assert not [p for p in shipped if p.strip("/").split("/")[0] == "ee"], shipped
