"""Render the public API (see ``keelgate._internal.api``) as ``docs/api-contract.md``.

Everything in the output is read from the code: the symbol list from each module's ``__all__``,
the signatures from ``inspect``, the summary from the first line of the docstring. Nothing is typed
twice, so the document cannot say something the code does not do. A contract test regenerates it
and compares; an API change therefore shows up as a diff in this file, in code review.

    python -m keelgate._internal.apidoc            # rewrite docs/api-contract.md
    python -m keelgate._internal.apidoc --check    # exit 1 if the file is out of date
"""

from __future__ import annotations

import enum
import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import Any, Final

from keelgate._internal.api import (
    PROVISIONAL,
    PUBLIC_MODULES,
    PUBLIC_PACKAGES,
    STABLE,
    tier,
)

TARGET: Final = Path("docs") / "api-contract.md"
MAX_CONSTANT_CHARS: Final = 60

# Plain public modules have no ``__all__``; these are the symbols that make up their contract.
MODULE_SYMBOLS: Final[dict[str, tuple[str, ...]]] = {
    "keelgate": ("__version__",),
    "keelgate.cli": ("main", "build_parser"),
    "keelgate.approvals.cli": ("main",),
    "keelgate.approvals.rest": ("create_app",),
    "keelgate.policy.cedar": ("CedarEngine",),
    "keelgate.loop.langgraph_store": ("LangGraphCheckpointStore",),
    "keelgate.testing.plugin": (
        "fake_llm",
        "governed_harness",
        "keelgate_clock",
        "static_policy",
    ),
}

_IGNORED_BASES: Final = {"object", "BaseModel", "Enum", "StrEnum", "Protocol", "Generic"}


def symbols_of(module_name: str) -> list[str]:
    """The public symbols of a module, in a stable order."""
    module = importlib.import_module(module_name)
    if module_name == "keelgate.telemetry.attributes":
        return sorted(n for n in vars(module) if n.isupper() and not n.startswith("_"))
    if module_name in MODULE_SYMBOLS:
        return list(MODULE_SYMBOLS[module_name])
    exported = getattr(module, "__all__", None)
    if exported is None:
        raise AttributeError(f"{module_name} is public but defines no __all__")
    return sorted(exported)


def stable_repr(value: Any) -> str:
    """``repr`` with sets sorted: set order changes with hash randomisation between runs."""
    if isinstance(value, (set, frozenset)):
        inner = ", ".join(sorted(stable_repr(v) for v in value))
        return f"frozenset({{{inner}}})" if isinstance(value, frozenset) else f"{{{inner}}}"
    if isinstance(value, tuple):
        return (
            "(" + ", ".join(stable_repr(v) for v in value) + ("," if len(value) == 1 else "") + ")"
        )
    return repr(value)


def _default(value: Any) -> str:
    if value is None or isinstance(value, (bool, int, float, str)):
        return repr(value)
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, (tuple, frozenset)):
        return (
            stable_repr(value) if value else ("()" if isinstance(value, tuple) else "frozenset()")
        )
    return "..."  # anything else has an unstable repr (addresses, lambdas)


def _annotation(value: Any) -> str:
    text = value if isinstance(value, str) else inspect.formatannotation(value)
    text = re.sub(r"<class '([\w.]+)'>", r"\1", text).replace("typing.", "")
    return re.sub(r"\bkeelgate(?:\.\w+)+\.(\w+)", r"\1", text)


def signature_of(obj: Any) -> str:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return "(...)"
    parts: list[str] = []
    star_done = False
    for p in sig.parameters.values():
        if p.name in {"self", "cls"}:
            continue
        if p.kind is p.VAR_POSITIONAL:
            parts.append("*" + p.name)
            star_done = True
            continue
        if p.kind is p.VAR_KEYWORD:
            parts.append("**" + p.name)
            continue
        if p.kind is p.KEYWORD_ONLY and not star_done:
            parts.append("*")
            star_done = True
        text = p.name
        if p.annotation is not p.empty:
            text += f": {_annotation(p.annotation)}"
        if p.default is not p.empty:
            text += f" = {_default(p.default)}"
        parts.append(text)
    returns = ""
    if sig.return_annotation is not sig.empty:
        returns = f" -> {_annotation(sig.return_annotation)}"
    return f"({', '.join(parts)}){returns}"


def has_real_doc(obj: Any) -> bool:
    """False for no docstring, and for the signature text dataclasses generate in its place."""
    if not (inspect.isclass(obj) or inspect.isfunction(obj) or inspect.ismodule(obj)):
        return True
    doc = (inspect.getdoc(obj) or "").strip()
    return bool(doc) and not doc.startswith(f"{getattr(obj, '__name__', '')}(")


def _summary(obj: Any) -> str:
    if not has_real_doc(obj):
        return ""
    doc = inspect.getdoc(obj) or ""
    line = doc.strip().split("\n\n")[0].replace("\n", " ").strip()
    first = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    return first.replace("|", "\\|")


def _kind(obj: Any) -> str:  # noqa: PLR0911 - one return per kind reads best
    if inspect.ismodule(obj):
        return "module"
    if inspect.isclass(obj):
        if issubclass(obj, enum.Enum):
            return "enum"
        if issubclass(obj, BaseException):
            return "exception"
        if getattr(obj, "_is_protocol", False):
            return "protocol"
        if hasattr(obj, "model_fields"):
            return "model"
        return "class"
    if inspect.iscoroutinefunction(obj):
        return "async function"
    if callable(obj) and not inspect.isclass(obj):
        return "function"
    return "constant"


def _members(cls: type) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for klass in cls.__mro__:
        if klass.__name__ in _IGNORED_BASES or not klass.__module__.startswith("keelgate"):
            continue
        for name, member in vars(klass).items():
            if name.startswith("_") or name in seen or name.startswith("model_"):
                continue
            seen.add(name)
            raw = member.__func__ if isinstance(member, (classmethod, staticmethod)) else member
            if isinstance(member, property):
                lines.append(f"    property {name}")
            elif inspect.isfunction(raw):
                prefix = "async " if inspect.iscoroutinefunction(raw) else ""
                prefix += "classmethod " if isinstance(member, classmethod) else ""
                prefix += "staticmethod " if isinstance(member, staticmethod) else ""
                lines.append(f"    {prefix}{name}{signature_of(raw)}")
    return sorted(lines, key=lambda s: s.strip().split("(")[0])


def _signature_block(name: str, obj: Any, module: str) -> list[str]:  # noqa: PLR0911
    kind = _kind(obj)
    if module == "keelgate.telemetry.attributes":
        return [f"{name} = {stable_repr(obj)}"]
    if kind == "module":
        return [f"module {name}"]
    if kind == "constant":
        text = stable_repr(obj)
        return [
            f"{name} = {text if len(text) < MAX_CONSTANT_CHARS else type(obj).__name__ + '(...)'}"
        ]
    if kind == "enum":
        members = ", ".join(f"{m.name}={m.value!r}" for m in obj)
        return [f"enum {name}: {members}"]
    if kind == "protocol":
        header = f"protocol {name}"
        return [header, *_members(obj)]
    if kind in {"function", "async function"}:
        prefix = "async def" if kind == "async function" else "def"
        return [f"{prefix} {name}{signature_of(obj)}"]
    bases = [b.__name__ for b in obj.__bases__ if b.__name__ not in _IGNORED_BASES]
    base_text = f"({', '.join(bases)})" if bases else ""
    return [f"{kind} {name}{base_text}{signature_of(obj)}", *_members(obj)]


def render() -> str:
    out = [
        "# API contract",
        "",
        "<!-- GENERATED by `python -m keelgate._internal.apidoc`. Do not edit: change the code or "
        "keelgate/_internal/api.py and regenerate. -->",
        "",
        "Every public symbol of Keelgate, generated from the code. The public API is exactly "
        "what the modules below export; every other module is private (underscored) and every "
        "name not listed here is private whatever it is called. The narrative, the semantics and "
        "an end-to-end example are in the [integration contract](integration-contract.md); the "
        "policy for changing anything on this page is in [versioning](versioning.md).",
        "",
        f"**{STABLE}**: covered by semantic versioning; a breaking change needs a major bump (a "
        f"minor bump while Keelgate is 0.x) and a migration note. **{PROVISIONAL}**: works and is "
        "tested, but may change in a minor release with a changelog entry.",
        "",
        "A change to this file in a pull request *is* a change to the public API.",
        "",
    ]
    totals: list[tuple[str, int, int]] = []
    sections: list[str] = []
    for module_name in [*PUBLIC_PACKAGES, *(m for m in PUBLIC_MODULES if m not in PUBLIC_PACKAGES)]:
        module = importlib.import_module(module_name)
        names = symbols_of(module_name)
        rows = ["| Symbol | Kind | Stability | Summary |", "|---|---|---|---|"]
        blocks: list[str] = []
        stable = 0
        for name in names:
            obj = getattr(module, name)
            level = tier(module_name, name)
            stable += level == STABLE
            summary = "" if module_name == "keelgate.telemetry.attributes" else _summary(obj)
            rows.append(f"| `{name}` | {_kind(obj)} | {level} | {summary} |")
            blocks.extend(_signature_block(name, obj, module_name))
        totals.append((module_name, stable, len(names) - stable))
        heading = (_summary(module) or "").rstrip(".")
        sections += [
            f"## `{module_name}`",
            "",
            heading + "." if heading else "",
            "",
            *rows,
            "",
            "<details><summary>Signatures</summary>",
            "",
            "```text",
            *blocks,
            "```",
            "",
            "</details>",
            "",
        ]
    out += ["| Module | Stable | Provisional |", "|---|---|---|"]
    out += [f"| `{m}` | {s} | {p} |" for m, s, p in totals]
    out += [
        f"| **total** | **{sum(s for _, s, _ in totals)}** | **{sum(p for _, _, p in totals)}** |",
        "",
        *sections,
    ]
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = render()
    if "--check" in args:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current.replace("\r\n", "\n") != text:
            sys.stderr.write(f"{TARGET} is out of date: run python -m keelgate._internal.apidoc\n")
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {TARGET} ({len(text.splitlines())} lines)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
