"""The registry of the public API: which modules are public and how stable each symbol is.

The public API is what these modules export in ``__all__``. Every other module is private
(underscored), and anything not in an ``__all__`` is private whatever its module is called.
``keelgate._internal.apidoc`` renders this registry into ``docs/api-contract.md``, and the contract
tests fail if code and document drift apart.
"""

from __future__ import annotations

from typing import Final

STABLE: Final = "stable"
PROVISIONAL: Final = "provisional"

# Packages whose ``__all__`` is the contract, in the order they appear in the documentation.
PUBLIC_PACKAGES: Final = (
    "keelgate.tools",
    "keelgate.capabilities",
    "keelgate.policy",
    "keelgate.audit",
    "keelgate.approvals",
    "keelgate.loop",
    "keelgate.context",
    "keelgate.memory",
    "keelgate.llm",
    "keelgate.telemetry",
    "keelgate.evals",
    "keelgate.testing",
    "keelgate.adapters",
    "keelgate.llm.providers",
    "keelgate.adapters.mcp",
    "keelgate.adapters.langgraph",
    "keelgate.adapters.openai_agents",
    "keelgate.adapters.claude_agent_sdk",
    "keelgate.adapters.a2a",
    "keelgate.adapters.temporal",
)

# Plain modules that are public on purpose. Each one is either an entry-point or plugin target
# that is loaded by name, or an optional-dependency module whose symbols are intentionally not
# importable from the package root (so importing the root never needs the dependency).
PUBLIC_MODULES: Final = (
    "keelgate",
    "keelgate.cli",
    "keelgate.approvals.cli",
    "keelgate.approvals.rest",
    "keelgate.policy.cedar",
    "keelgate.loop.langgraph_store",
    "keelgate.testing.plugin",
    "keelgate.telemetry.attributes",
)

# Whole modules that are provisional: their symbols may change in a minor release.
PROVISIONAL_MODULES: Final = frozenset(
    {
        "keelgate.llm.providers",
        "keelgate.adapters.mcp",
        "keelgate.adapters.langgraph",
        "keelgate.adapters.openai_agents",
        "keelgate.adapters.claude_agent_sdk",
        "keelgate.adapters.a2a",
        "keelgate.adapters.temporal",
        "keelgate.approvals.rest",
        "keelgate.policy.cedar",
        "keelgate.loop.langgraph_store",
        "keelgate.testing.plugin",
        "keelgate.telemetry.attributes",
        "keelgate.cli",
        "keelgate.approvals.cli",
    }
)

# Provisional symbols inside otherwise-stable modules.
PROVISIONAL_SYMBOLS: Final[dict[str, frozenset[str]]] = {
    "keelgate.capabilities": frozenset({"SqliteBudgetLedger", "SqliteRevocationList"}),
    "keelgate.tools": frozenset({"SqliteIdempotencyStore"}),
    "keelgate.audit": frozenset({"PostgresAuditStore"}),
    "keelgate.loop": frozenset(
        {
            "READ_ONLY",
            "Divergence",
            "GroundedAnswerVerifier",
            "HistoryCheckpointStore",
            "InProcessRunner",
            "LoopOutcome",
            "LoopRunner",
            "LoopSpec",
            "MonitorLoop",
            "MonitorSummary",
            "NotReplayableError",
            "OutcomeConfirmer",
            "RecordedAction",
            "RecordedStep",
            "Recording",
            "ReplayReport",
            "Schedule",
            "VerificationLoop",
            "diff",
            "find_run",
            "replay",
        }
    ),
    "keelgate.memory": frozenset(
        {
            "EmbeddingError",
            "HashEmbedder",
            "MemoryBackend",
            "OllamaEmbedder",
            "OpenAICompatibleEmbedder",
            "PostgresMemoryBackend",
            "SqliteMemoryBackend",
            "cosine",
        }
    ),
    "keelgate.testing": frozenset(
        {"GovernedHarness", "ManualClock", "StaticPolicyEngine", "build_governed_harness"}
    ),
    "keelgate.telemetry": frozenset(
        {
            "UNATTRIBUTED",
            "CostTotals",
            "CostTracker",
            "RedactingSpanProcessor",
            "Redactor",
            "RunSpan",
            "attributes",
            "redact_span",
            "run_span",
        }
    ),
    "keelgate.evals": frozenset(
        {
            "SUITES",
            "AccuracyMetric",
            "CaseResult",
            "EvalContext",
            "EvalReport",
            "EvalStack",
            "LoadedMetric",
            "SuiteResult",
            "baseline_of",
            "compare",
            "load_records",
            "run_metrics",
            "run_outcome",
            "run_suites",
            "sample_records",
            "to_dict",
            "to_html",
            "to_markdown",
            "write_html",
            "write_json",
            "write_markdown",
        }
    ),
}


def tier(module: str, name: str) -> str:
    """``stable`` or ``provisional`` for a public symbol."""
    if module in PROVISIONAL_MODULES or name in PROVISIONAL_SYMBOLS.get(module, frozenset()):
        return PROVISIONAL
    return STABLE
