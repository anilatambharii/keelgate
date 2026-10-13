"""Ship finished spans to the control plane's trace store."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from keelgate.cloud._client import CloudClient, CloudError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opentelemetry.sdk.trace import ReadableSpan

_MAX_ATTRIBUTE_CHARS = 2000


def _scalar(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    return str(value)[:_MAX_ATTRIBUTE_CHARS]


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    """A compact JSON form of a span: ids, name, times, status and attributes."""
    context = span.get_span_context()
    if context is None:  # pragma: no cover - a finished span always has a context
        raise ValueError("span has no context")
    parent = span.parent
    return {
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": None if parent is None else f"{parent.span_id:016x}",
        "name": span.name,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "status": span.status.status_code.name,
        "attributes": {str(k): _scalar(v) for k, v in (span.attributes or {}).items()},
    }


class CloudSpanExporter(SpanExporter):
    """An OpenTelemetry span exporter for the control plane.

    Pass it to :func:`keelgate.telemetry.instrument` (``exporter=``), with ``redact=True`` if the
    spans may carry sensitive text. Export failures are reported to OpenTelemetry as failures and
    never raise into the agent.
    """

    def __init__(self, client: CloudClient) -> None:
        self._client = client

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            self._client.request(
                "POST", "/v1/traces", json={"spans": [span_to_dict(s) for s in spans]}
            )
        except CloudError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._client.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True
