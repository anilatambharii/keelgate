"""``instrument()``: one call to get Keelgate traces into Jaeger, Phoenix or any OTLP backend."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Final

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from keelgate import __version__
from keelgate.telemetry._core import Telemetry, activate
from keelgate.telemetry._redaction import RedactingSpanProcessor, Redactor

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace.export import SpanExporter

DEFAULT_ENDPOINT: Final = "http://localhost:4318"
CAPTURE_ENV: Final = "OTEL_GENAI_CAPTURE_MESSAGE_CONTENT"


def _otlp_exporter(endpoint: str | None, headers: Mapping[str, str] | None) -> SpanExporter:
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    if protocol not in ("http/protobuf", "http/json"):
        raise ValueError(
            f"OTEL_EXPORTER_OTLP_PROTOCOL={protocol!r} is not supported: Keelgate ships the "
            "OTLP/HTTP exporter (port 4318). Set it to http/protobuf, or pass your own exporter."
        )
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ImportError("OTLP export needs the exporter: pip install 'keelgate[otlp]'") from exc
    # Without an explicit endpoint the exporter reads OTEL_EXPORTER_OTLP_* itself; only fall back
    # to the local default when nothing at all is configured.
    configured = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if configured:
        return OTLPSpanExporter(endpoint=configured, headers=dict(headers or {}) or None)
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
    return OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=dict(headers or {}) or None)


def instrument(
    *,
    service_name: str | None = None,
    endpoint: str | None = None,
    headers: Mapping[str, str] | None = None,
    exporter: SpanExporter | None = None,
    redact: bool | Redactor = False,
    capture_content: bool | None = None,
    batch: bool = True,
    resource_attributes: Mapping[str, Any] | None = None,
    meter_provider: MeterProvider | None = None,
    set_global: bool = False,
) -> Telemetry:
    """Configure tracing and make it the active telemetry for Keelgate.

    * ``exporter``: any OpenTelemetry span exporter. Default: OTLP/HTTP to
      ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or ``http://localhost:4318``, which is Jaeger from
      ``make up``). Needs ``keelgate[otlp]``.
    * ``redact``: ``True`` for the default PII patterns, or a :class:`Redactor`. Spans are redacted
      before they reach the exporter.
    * ``capture_content``: record prompts and responses on LLM spans. Off by default (they are
      untrusted and may be sensitive); ``OTEL_GENAI_CAPTURE_MESSAGE_CONTENT=true`` turns it on.
    * ``set_global``: also install the provider as OpenTelemetry's global tracer provider. Leave it
      off to keep Keelgate's spans separate from the application's own.

    Returns the :class:`Telemetry`; call ``shutdown()`` on it at exit to flush pending spans.
    """
    resource = Resource.create(
        {
            "service.name": service_name or os.environ.get("OTEL_SERVICE_NAME", "keelgate"),
            "service.version": __version__,
            **dict(resource_attributes or {}),
        }
    )
    provider = TracerProvider(resource=resource)
    chosen = exporter or _otlp_exporter(endpoint, headers)
    processor: SpanProcessor = BatchSpanProcessor(chosen) if batch else SimpleSpanProcessor(chosen)
    if redact:
        processor = RedactingSpanProcessor(
            processor, redact if isinstance(redact, Redactor) else Redactor()
        )
    provider.add_span_processor(processor)
    if set_global:
        trace.set_tracer_provider(provider)
    if capture_content is None:
        capture_content = os.environ.get(CAPTURE_ENV, "false").lower() == "true"
    telemetry = Telemetry(
        tracer_provider=provider, meter_provider=meter_provider, capture_content=capture_content
    )
    activate(telemetry)
    return telemetry
