"""Optional PII redaction for spans, applied before export.

``RedactingSpanProcessor`` wraps another span processor (usually a ``BatchSpanProcessor``). On
``on_end`` it hands the delegate a *redacted copy* of the finished span, so the exporter, and
anything downstream of it, never sees the original text. The live span is not modified.

This is a safety net, not a guarantee. Keelgate's own spans carry no prompts, arguments or tool
output unless content capture is switched on; redaction exists for that opt-in, and for
application spans that share the same provider. Pattern matching misses things (names, free-text
addresses), so treat it as defence in depth and keep content capture off in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from opentelemetry.sdk.trace import Event, ReadableSpan, SpanProcessor

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import Span

REDACTED: Final = "[REDACTED]"


def _luhn_ok(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if flip:
            n *= 2
            if n > 9:  # noqa: PLR2004 - the Luhn rule
                n -= 9
        total += n
        flip = not flip
    return total % 10 == 0


_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

# (name, pattern). Order matters little; every pattern is applied.
DEFAULT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("ssn", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    (
        "phone",
        re.compile(r"(?<![\w.])(?:\+?\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)"),
    ),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)


@dataclass(frozen=True)
class Redactor:
    """Rewrites text and attribute values. ``drop_keys`` removes attributes outright."""

    patterns: tuple[tuple[str, re.Pattern[str]], ...] = DEFAULT_PATTERNS
    replacement: str = REDACTED
    redact_cards: bool = True
    drop_keys: frozenset[str] = field(default_factory=frozenset)

    def text(self, value: str) -> str:
        out = value
        for _name, pattern in self.patterns:
            out = pattern.sub(self.replacement, out)
        if self.redact_cards:
            out = _CARD.sub(self._card, out)
        return out

    def _card(self, match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return self.replacement if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group(0)  # noqa: PLR2004

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.value(v) for v in value)
        return value

    def attributes(self, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            key: self.value(val)
            for key, val in (attributes or {}).items()
            if key not in self.drop_keys
        }


def redact_span(span: ReadableSpan, redactor: Redactor) -> ReadableSpan:
    """A copy of a finished span with attribute values, event attributes and the name redacted."""
    events = tuple(
        Event(
            name=redactor.text(e.name),
            attributes=redactor.attributes(e.attributes),
            timestamp=e.timestamp,
        )
        for e in span.events
    )
    return ReadableSpan(
        name=redactor.text(span.name),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redactor.attributes(span.attributes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanProcessor(SpanProcessor):
    """Wrap ``delegate`` so that it only ever receives redacted spans."""

    def __init__(self, delegate: SpanProcessor, redactor: Redactor | None = None) -> None:
        self._delegate = delegate
        self._redactor = redactor or Redactor()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._delegate.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        self._delegate.on_end(redact_span(span, self._redactor))

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)
