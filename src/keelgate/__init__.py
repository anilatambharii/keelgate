"""Keelgate — a safety-first agent harness for AI agents that touch money.

The LLM proposes; deterministic code decides. Every WRITE action passes a
deterministic policy gate, every external string is untrusted data, and every
decision lands in a tamper-evident audit log.

Keelgate wraps existing agent frameworks (LangGraph, the OpenAI Agents SDK, the
Claude Agent SDK, MCP, A2A) rather than competing with them.

The public API that downstream projects may depend on is documented in
``docs/integration-contract.md`` and versioned with semver.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("keelgate")
except PackageNotFoundError:  # pragma: no cover - only when run from a bare tree
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
