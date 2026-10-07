"""Rewrite 0.1-style deep imports to the 0.2 public API.

0.1 let you import from implementation modules (``from keelgate.llm.types import LLMRequest``).
0.2 makes those modules private; the same names are importable from the package root
(``from keelgate.llm import LLMRequest``). This tool finds the old imports and moves them.

The old-to-new map is derived from the installed package (every underscored module ``pkg._leaf``
used to be ``pkg.leaf``), so it cannot go stale. A name that is *not* exported from the root is
never guessed at: it is reported for a human, because it means the symbol is not public API.

    keelgate migrate-imports src/ tests/            # show what would change
    keelgate migrate-imports src/ tests/ --write    # apply it
"""

from __future__ import annotations

import ast
import difflib
import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import keelgate

if TYPE_CHECKING:
    from pathlib import Path


def legacy_modules() -> dict[str, str]:
    """``{old dotted module: package that now exports its names}``."""
    mapping: dict[str, str] = {}
    for info in pkgutil.walk_packages(keelgate.__path__, "keelgate."):
        parts = info.name.split(".")
        leaf = parts[-1]
        if info.ispkg or not leaf.startswith("_") or "_internal" in parts or leaf.startswith("__"):
            continue
        package = ".".join(parts[:-1])
        mapping[f"{package}.{leaf[1:]}"] = package
    return mapping


@dataclass
class Migration:
    path: str
    new_text: str
    changed: int = 0
    manual: list[str] = field(default_factory=list)

    def diff(self, old_text: str) -> str:
        return "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                self.new_text.splitlines(keepends=True),
                fromfile=self.path,
                tofile=self.path,
            )
        )


def _exported(package: str) -> set[str] | None:
    try:
        module = importlib.import_module(package)
    except ImportError:
        return None  # an optional dependency is missing: do not guess
    return set(getattr(module, "__all__", ()))


def migrate_source(text: str, path: str = "<string>") -> Migration:
    """Rewrite the deep imports in one file's source."""
    legacy = legacy_modules()
    result = Migration(path, text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        result.manual.append(f"{path}: not valid Python, skipped")
        return result
    lines = text.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in legacy and node.level == 0:
            root = legacy[node.module]
            exported = _exported(root)
            missing = (
                [a.name for a in node.names if a.name != "*"]
                if exported is None
                else [a.name for a in node.names if a.name not in exported]
            )
            if exported is None or missing:
                result.manual.append(
                    f"{path}:{node.lineno}: {', '.join(missing)} "
                    f"{'cannot be checked' if exported is None else 'is not exported from'} "
                    f"{root}; it is not public API"
                )
                continue
            line = lines[node.lineno - 1]
            new_line = re.sub(
                rf"\bfrom\s+{re.escape(node.module)}\b", f"from {root}", line, count=1
            )
            if new_line != line:
                lines[node.lineno - 1] = new_line
                result.changed += 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in legacy:
                    result.manual.append(
                        f"{path}:{node.lineno}: 'import {alias.name}' needs a manual change; "
                        f"import the names you use from {legacy[alias.name]}"
                    )
    result.new_text = "\n".join(lines)
    return result


def migrate_paths(paths: list[Path], *, write: bool) -> tuple[list[Migration], str]:
    """Migrate every ``.py`` file under ``paths``; return the results and a printable report."""
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    results: list[Migration] = []
    report: list[str] = []
    for file in files:
        if any(
            part in {".venv", "node_modules", "__pycache__", "site-packages"} for part in file.parts
        ):
            continue
        old = file.read_text(encoding="utf-8")
        migration = migrate_source(old, str(file))
        if migration.changed:
            report.append(migration.diff(old))
            if write:
                file.write_text(migration.new_text, encoding="utf-8", newline="")
        report.extend(f"MANUAL {line}" for line in migration.manual)
        results.append(migration)
    changed = sum(m.changed for m in results)
    manual = sum(len(m.manual) for m in results)
    verb = "rewrote" if write else "would rewrite"
    report.append(
        f"{verb} {changed} import(s) in {sum(bool(m.changed) for m in results)} file(s); "
        f"{manual} need a manual change"
    )
    return results, "\n".join(report)
