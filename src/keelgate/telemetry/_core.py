"""The active telemetry handle, the run context, and the span helper everything uses.

Keelgate's own code never touches a global OpenTelemetry provider directly. It asks for the
*active* :class:`Telemetry`, which by default forwards to whatever global providers the
application has configured (a no-op until someone configures one, so un-instrumented use costs
almost nothing). ``instrument()`` or :func:`use` swap in a specific one, which is also how tests
get an in-memory exporter without fighting OpenTelemetry's set-the-provider-once rule.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from keelgate import __version__
from keelgate.telemetry import attributes as attr
from keelgate.telemetry._cost import CostTracker

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from opentelemetry.context import Context
    from opentelemetry.metrics import Meter, MeterProvider
    from opentelemetry.trace import Span, Tracer, TracerProvider

INSTRUMENTATION_NAME: Final = "keelgate"


class Telemetry:
    """A tracer, a meter, a cost tracker, and the content-capture switch."""

    def __init__(
        self,
        *,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        capture_content: bool = False,
        costs: CostTracker | None = None,
    ) -> None:
        self.tracer_provider = tracer_provider
        self.meter_provider = meter_provider
        self.tracer: Tracer = (
            tracer_provider.get_tracer(INSTRUMENTATION_NAME, __version__)
            if tracer_provider is not None
            else trace.get_tracer(INSTRUMENTATION_NAME, __version__)
        )
        self.meter: Meter = (
            meter_provider.get_meter(INSTRUMENTATION_NAME, __version__)
            if meter_provider is not None
            else metrics.get_meter(INSTRUMENTATION_NAME, __version__)
        )
        self.costs = costs or CostTracker(self.meter)
        # Prompts and responses are untrusted and may be sensitive: off unless asked for.
        self.capture_content = capture_content

    def force_flush(self, timeout_millis: int = 30000) -> None:
        for provider in (self.tracer_provider, self.meter_provider):
            flush = getattr(provider, "force_flush", None)
            if callable(flush):
                flush(timeout_millis)

    def shutdown(self) -> None:
        for provider in (self.tracer_provider, self.meter_provider):
            stop = getattr(provider, "shutdown", None)
            if callable(stop):
                stop()


_DEFAULT: Final = Telemetry()
_active: Telemetry = _DEFAULT


def active() -> Telemetry:
    """The currently active ``Telemetry`` handle."""
    return _active


def activate(telemetry: Telemetry | None) -> None:
    """Make ``telemetry`` the active handle for the process (``None`` restores the default)."""
    global _active  # noqa: PLW0603 - one process-wide handle, by design
    _active = telemetry or _DEFAULT


@contextlib.contextmanager
def use(telemetry: Telemetry) -> Iterator[Telemetry]:
    """Temporarily activate ``telemetry`` (tests, and short scripts)."""
    previous = _active
    activate(telemetry)
    try:
        yield telemetry
    finally:
        activate(previous)


# ------------------------------------------------------------------ run context


@dataclass(frozen=True)
class RunContext:
    """The tenant, agent and run that spans and costs created inside a block are attributed to."""

    tenant_id: str
    agent_id: str
    run_id: str


_run: ContextVar[RunContext | None] = ContextVar("keelgate_run", default=None)


def current_run() -> RunContext | None:
    """The ``RunContext`` bound by the surrounding loop, or None outside a run."""
    return _run.get()


@contextlib.contextmanager
def bind_run(tenant_id: str, agent_id: str, run_id: str) -> Iterator[RunContext]:
    """Attribute spans, metrics and costs created inside the block to this run."""
    ctx = RunContext(tenant_id, agent_id, run_id)
    token = _run.set(ctx)
    try:
        yield ctx
    finally:
        _run.reset(token)


# ------------------------------------------------------------------ spans


def clean(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop ``None`` and truncate long strings. Spans are for navigation, not storage."""
    out: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        too_long = isinstance(value, str) and len(value) > attr.MAX_ATTRIBUTE_CHARS
        out[key] = value[: attr.MAX_ATTRIBUTE_CHARS] + "..." if too_long else value
    return out


def run_attributes() -> dict[str, Any]:
    run = current_run()
    if run is None:
        return {}
    return {attr.TENANT_ID: run.tenant_id, attr.AGENT_ID: run.agent_id, attr.RUN_ID: run.run_id}


@contextlib.contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, Any] | None = None,
    context: Context | None = None,
) -> Iterator[Span]:
    """A span that is current inside the block, attributed to the bound run.

    An exception marks the span as an error with only its *type*: exception messages can carry
    argument or output text, so they are never recorded.
    """
    merged = {**run_attributes(), **clean(attributes)}
    with active().tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=merged,
        context=context,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as exc:
            current.set_attribute("error.type", type(exc).__name__)
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def set_attributes(span_: Span, attributes: Mapping[str, Any]) -> None:
    """Set several span attributes at once, dropping ``None`` values and truncating long strings."""
    for key, value in clean(attributes).items():
        span_.set_attribute(key, value)
