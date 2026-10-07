"""Helpers the rest of Keelgate uses to emit spans: a method decorator and the run root span."""

from __future__ import annotations

import contextlib
import functools
import inspect
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from keelgate.telemetry import attributes as attr
from keelgate.telemetry.core import bind_run, set_attributes, span

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from opentelemetry.trace import Span

P = ParamSpec("P")
R = TypeVar("R")


def traced(
    name: str,
    *,
    pre: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    post: Callable[[Any], Mapping[str, Any]] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Run a synchronous method inside a span.

    ``pre`` receives the bound call arguments (``self`` included) and returns the attributes to
    start the span with; ``post`` receives the result and returns attributes to add. Neither is
    handed anything that should be recorded verbatim: choose attributes deliberately.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attributes = pre(signature.bind(*args, **kwargs).arguments) if pre else {}
            with span(name, attributes=attributes) as current:
                result = fn(*args, **kwargs)
                if post is not None:
                    set_attributes(current, post(result))
                return result

        return wrapper

    return decorate


# ------------------------------------------------------------------ the run root span


@dataclass(frozen=True)
class RunSpan:
    """The root span of a loop run, with the ids that identify the run's trace."""

    span: Span
    trace_id: str | None
    span_id: str | None


def _hex_int(value: str | None, width: int) -> int | None:
    if not value or len(value) != width:
        return None
    try:
        parsed = int(value, 16)
    except ValueError:
        return None
    return parsed or None


def ids_of(current: Span) -> tuple[str | None, str | None]:
    ctx = current.get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


@contextlib.contextmanager
def run_span(
    *,
    tenant_id: str,
    agent_id: str,
    run_id: str,
    loop_type: str,
    trace_id: str | None = None,
    root_span_id: str | None = None,
    resumed: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> Iterator[RunSpan]:
    """The root span for one loop run, bound so everything inside is attributed to the run.

    A new run starts a new trace. A resumed run (possibly in another process, days later)
    is parented on the original root span, so the whole life of the run is one trace. Passing
    a ``trace_id`` to a new run joins that trace instead.
    """
    parent = None
    wanted = _hex_int(trace_id, 32)
    if wanted is not None:
        original = _hex_int(root_span_id, 16)
        parent = trace.set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=wanted,
                    span_id=original or secrets.randbits(63) + 1,
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
            )
        )
    attributes = {
        attr.GEN_AI_OPERATION_NAME: attr.OP_INVOKE_AGENT,
        attr.GEN_AI_AGENT_ID: agent_id,
        attr.GEN_AI_CONVERSATION_ID: run_id,
        attr.TENANT_ID: tenant_id,
        attr.AGENT_ID: agent_id,
        attr.RUN_ID: run_id,
        attr.LOOP_TYPE: loop_type,
        attr.RESUMED: resumed,
        **dict(extra or {}),
    }
    with (
        bind_run(tenant_id, agent_id, run_id),
        span(
            f"{attr.OP_INVOKE_AGENT} {agent_id}", attributes=attributes, context=parent
        ) as current,
    ):
        new_trace, new_span = ids_of(current)
        yield RunSpan(current, new_trace, new_span)
