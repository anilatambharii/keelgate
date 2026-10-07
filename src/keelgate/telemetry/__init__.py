"""OpenTelemetry tracing, cost tracking and PII redaction for Keelgate.

One trace per loop run; GenAI-convention spans for model calls and tools; Keelgate spans for
policy decisions, approvals and memory. Nothing is exported until ``instrument()`` is called.
"""

from keelgate.telemetry import attributes
from keelgate.telemetry.core import (
    RunContext,
    Telemetry,
    activate,
    active,
    bind_run,
    current_run,
    set_attributes,
    span,
    use,
)
from keelgate.telemetry.cost import UNATTRIBUTED, CostTotals, CostTracker
from keelgate.telemetry.hooks import RunSpan, run_span, traced
from keelgate.telemetry.llm import InstrumentedLLM
from keelgate.telemetry.redaction import RedactingSpanProcessor, Redactor, redact_span
from keelgate.telemetry.setup import instrument

__all__ = [
    "UNATTRIBUTED",
    "CostTotals",
    "CostTracker",
    "InstrumentedLLM",
    "RedactingSpanProcessor",
    "Redactor",
    "RunContext",
    "RunSpan",
    "Telemetry",
    "activate",
    "active",
    "attributes",
    "bind_run",
    "current_run",
    "instrument",
    "redact_span",
    "run_span",
    "set_attributes",
    "span",
    "traced",
    "use",
]
