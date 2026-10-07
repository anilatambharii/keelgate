"""Telemetry core: GenAI spans, cost tracking, PII redaction and instrument()."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from keelgate import telemetry as tel
from keelgate.llm import (
    LLMError,
    LLMRequest,
    Message,
    ModelPrice,
    PricingTable,
    Role,
    Usage,
)
from keelgate.telemetry import attributes as attr
from keelgate.testing import FakeLLM, Reply
from tests.conftest import run


class Rig:
    def __init__(self, *, capture: bool = False) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.reader = InMemoryMetricReader()
        self.telemetry = tel.Telemetry(
            tracer_provider=provider,
            meter_provider=MeterProvider(metric_readers=[self.reader]),
            capture_content=capture,
        )

    @property
    def spans(self) -> list[Any]:
        return list(self.exporter.get_finished_spans())


@pytest.fixture
def rig() -> Iterator[Rig]:
    r = Rig()
    with tel.use(r.telemetry):
        yield r


def request(model: str = "m-test", **kw: Any) -> LLMRequest:
    return LLMRequest(model=model, messages=(Message(role=Role.USER, content="secret plan"),), **kw)


PRICES = PricingTable({"m-": ModelPrice(input_per_mtok=2.0, output_per_mtok=10.0)})


def llm(*replies: Reply, pricing: PricingTable | None = PRICES) -> tel.InstrumentedLLM:
    return tel.InstrumentedLLM(FakeLLM(list(replies), pricing=pricing, model="m-test"))


# --------------------------------------------------------------------------- LLM spans


def test_a_model_call_becomes_a_genai_chat_span_with_usage_and_cost(rig: Rig) -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000, cost_usd=7.0)
    response = run(llm(Reply.say("hi", usage=usage)).complete(request(max_tokens=64)))
    assert response.text == "hi"
    [span] = rig.spans
    assert span.name == "chat m-test" and span.kind is SpanKind.CLIENT
    a = span.attributes
    assert a[attr.GEN_AI_OPERATION_NAME] == "chat" and a[attr.GEN_AI_PROVIDER_NAME] == "fake"
    assert a[attr.GEN_AI_REQUEST_MODEL] == "m-test" and a[attr.GEN_AI_REQUEST_MAX_TOKENS] == 64
    assert a[attr.GEN_AI_USAGE_INPUT_TOKENS] == 1_000_000
    assert a[attr.GEN_AI_USAGE_OUTPUT_TOKENS] == 500_000
    assert a[attr.COST_USD] == pytest.approx(7.0) and a[attr.COST_KNOWN] is True
    assert tuple(a[attr.GEN_AI_RESPONSE_FINISH_REASONS]) == ("stop",)


def test_the_attribute_names_are_the_ones_in_the_genai_semantic_conventions() -> None:
    assert attr.GEN_AI_OPERATION_NAME == "gen_ai.operation.name"
    assert attr.GEN_AI_USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
    assert attr.GEN_AI_USAGE_OUTPUT_TOKENS == "gen_ai.usage.output_tokens"
    assert attr.GEN_AI_TOOL_NAME == "gen_ai.tool.name"
    assert (attr.OP_CHAT, attr.OP_EXECUTE_TOOL, attr.OP_INVOKE_AGENT) == (
        "chat",
        "execute_tool",
        "invoke_agent",
    )


def test_prompts_and_responses_are_not_recorded_by_default(rig: Rig) -> None:
    run(llm(Reply.say("the answer is 42")).complete(request()))
    [span] = rig.spans
    blob = str(dict(span.attributes)) + str([dict(e.attributes) for e in span.events])
    assert "secret plan" not in blob and "the answer is 42" not in blob
    assert span.events == ()


def test_content_capture_is_opt_in_and_goes_through_redaction() -> None:
    r = Rig(capture=True)
    with tel.use(r.telemetry):
        run(llm(Reply.say("reply to bob@example.com")).complete(request()))
    [span] = r.spans
    assert [e.name for e in span.events] == ["gen_ai.client.inference.operation.details"] * 2
    assert "secret plan" in str(dict(span.events[0].attributes))
    redacted = tel.redact_span(span, tel.Redactor())
    assert "bob@example.com" not in str([dict(e.attributes) for e in redacted.events])


def test_an_unpriced_model_is_marked_cost_unknown_not_zero(rig: Rig) -> None:
    run(llm(Reply.say("x"), pricing=None).complete(request(model="other")))
    [span] = rig.spans
    assert span.attributes[attr.COST_KNOWN] is False and attr.COST_USD not in span.attributes


def test_a_failing_call_marks_the_span_with_the_error_type_only(rig: Rig) -> None:
    class Failing:
        name = "bad"

        async def complete(self, request: LLMRequest) -> Any:
            raise LLMError("api key sk-supersecretvalue1234 rejected", provider="bad")

    with pytest.raises(LLMError):
        run(tel.InstrumentedLLM(Failing()).complete(request()))
    [span] = rig.spans
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "LLMError"
    assert "supersecret" not in str(span.status.description) + str(dict(span.attributes))
    assert span.events == ()  # the exception, with its message, is not attached


def test_the_wrapper_is_an_llm_client_and_exposes_the_wrapped_one() -> None:
    inner = FakeLLM([Reply.say("x")])
    wrapped = tel.InstrumentedLLM(inner)
    assert wrapped.name == "fake" and wrapped.wrapped is inner


# --------------------------------------------------------------------------- cost


def test_cost_is_tracked_per_tenant_and_agent(rig: Rig) -> None:
    def call(tenant: str, agent: str, n: int) -> None:
        with tel.bind_run(tenant, agent, "r"):
            for _ in range(n):
                run(
                    llm(Reply.say("x", usage=Usage(input_tokens=1000, output_tokens=0))).complete(
                        request()
                    )
                )

    call("acme", "a1", 2)
    call("acme", "a2", 1)
    call("globex", "a1", 3)
    costs = rig.telemetry.costs
    assert costs.total(tenant_id="acme").calls == 3
    assert costs.total(tenant_id="globex").input_tokens == 3000
    assert costs.total(tenant_id="acme", agent_id="a2").calls == 1
    assert set(costs.by_tenant()) == {"acme", "globex"}
    assert set(costs.by_agent("acme")) == {"a1", "a2"}
    assert costs.total().calls == 6


def test_spans_inside_a_run_carry_tenant_agent_and_run_ids(rig: Rig) -> None:
    with tel.bind_run("acme", "a1", "run-9"):
        run(llm(Reply.say("x")).complete(request()))
    a = rig.spans[0].attributes
    assert (a[attr.TENANT_ID], a[attr.AGENT_ID], a[attr.RUN_ID]) == ("acme", "a1", "run-9")
    assert tel.current_run() is None  # the binding ended with the block


def test_a_call_outside_any_run_is_unattributed(rig: Rig) -> None:
    run(llm(Reply.say("x")).complete(request()))
    assert rig.telemetry.costs.total(tenant_id=tel.UNATTRIBUTED).calls == 1


def test_unpriced_calls_count_tokens_but_not_dollars(rig: Rig) -> None:
    run(llm(Reply.say("x"), pricing=None).complete(request(model="other")))
    t = rig.telemetry.costs.total()
    assert t.cost_usd == 0.0 and t.unpriced_calls == 1 and t.total_tokens > 0


def test_cost_and_tokens_are_exported_as_otel_metrics(rig: Rig) -> None:
    usage = Usage(input_tokens=100, output_tokens=40, cost_usd=0.5)
    with tel.bind_run("acme", "a1", "r"):
        run(llm(Reply.say("x", usage=usage)).complete(request()))
    data = rig.reader.get_metrics_data()
    names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert {"gen_ai.client.token.usage", "keelgate.llm.cost"} <= names
    cost = next(
        m
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "keelgate.llm.cost"
    )
    point = cost.data.data_points[0]
    assert point.value == pytest.approx(0.5)
    assert point.attributes[attr.TENANT_ID] == "acme" and point.attributes[attr.AGENT_ID] == "a1"


# --------------------------------------------------------------------------- redaction

CARD = "4111 1111 1111 1111"  # the standard Visa test number (passes Luhn)


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("mail alice@example.com now", "alice@example.com"),
        ("ssn 123-45-6789 on file", "123-45-6789"),
        ("call (212) 555-0198 today", "555-0198"),
        ("call 212-555-0198 today", "212-555-0198"),
        (f"card {CARD} charged", CARD),
        ("card 4111111111111111 charged", "4111111111111111"),
        ("iban GB82WEST12345698765432 ok", "GB82WEST12345698765432"),
        ("key sk-abcdefghijklmnop1234 used", "sk-abcdefghijklmnop1234"),
        ("aws AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),  # pragma: allowlist secret
        ("Authorization: Bearer abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"),
        ("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4 end", "SflKxwRJSMeKKF2QT4"),
    ],
)
def test_pii_patterns_are_redacted(text: str, gone: str) -> None:
    out = tel.Redactor().text(text)
    assert gone not in out and "[REDACTED]" in out


@pytest.mark.parametrize(
    "text",
    [
        "order id 1234567890123456",  # 16 digits that fail Luhn
        "price 187.25 notional 5000",
        "AAPL up 3.2% on 2026-10-05",
        "the quick brown fox",
        "version 1.2.3 build 20260705",
    ],
)
def test_ordinary_numbers_and_prose_are_left_alone(text: str) -> None:
    assert tel.Redactor().text(text) == text


def test_redaction_covers_attributes_events_and_names_without_touching_the_original() -> None:
    original: list[Any] = []

    class Capture(SpanProcessor):
        def on_end(self, span: Any) -> None:
            original.append(span)

    export2 = InMemorySpanExporter()
    guarded = TracerProvider()
    guarded.add_span_processor(
        tel.RedactingSpanProcessor(SimpleSpanProcessor(export2), tel.Redactor())
    )
    guarded.add_span_processor(Capture())
    tracer = guarded.get_tracer("t")
    with tracer.start_as_current_span("lookup bob@example.com") as s:
        s.set_attribute("customer", "bob@example.com")
        s.set_attribute("tags", ["ok", "ssn 123-45-6789"])
        s.set_attribute("count", 3)
        s.add_event("seen", {"note": "card 4111111111111111"})
    [out] = export2.get_finished_spans()
    assert "bob@example.com" not in out.name
    assert out.attributes["customer"] == "[REDACTED]" and out.attributes["count"] == 3
    assert "123-45-6789" not in str(out.attributes["tags"])
    assert "4111" not in str(dict(out.events[0].attributes))
    assert out.context == original[0].context  # same identity, so traces still join up
    assert original[0].attributes["customer"] == "bob@example.com"  # the live span is intact


def test_drop_keys_removes_attributes_entirely() -> None:
    r = tel.Redactor(drop_keys=frozenset({"gen_ai.input.messages"}))
    assert r.attributes({"gen_ai.input.messages": "x", "keep": "y"}) == {"keep": "y"}


# --------------------------------------------------------------------------- instrument()


def test_instrument_wires_an_exporter_activates_itself_and_flushes() -> None:
    exporter = InMemorySpanExporter()
    previous = tel.active()
    t = tel.instrument(exporter=exporter, batch=False, service_name="svc-x")
    try:
        assert tel.active() is t and t.capture_content is False
        run(llm(Reply.say("x")).complete(request()))
        t.force_flush()
        [span] = exporter.get_finished_spans()
        assert span.resource.attributes["service.name"] == "svc-x"
    finally:
        tel.activate(previous)
        t.shutdown()


def test_instrument_can_redact_before_export() -> None:
    exporter = InMemorySpanExporter()
    previous = tel.active()
    t = tel.instrument(exporter=exporter, batch=False, redact=True, capture_content=True)
    try:
        run(llm(Reply.say("write to carol@example.com")).complete(request()))
        t.force_flush()
        [span] = exporter.get_finished_spans()
        assert "carol@example.com" not in str([dict(e.attributes) for e in span.events])
    finally:
        tel.activate(previous)
        t.shutdown()


def test_content_capture_follows_the_environment_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = tel.active()
    monkeypatch.setenv("OTEL_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    try:
        assert tel.instrument(exporter=InMemorySpanExporter()).capture_content is True
        monkeypatch.setenv("OTEL_GENAI_CAPTURE_MESSAGE_CONTENT", "false")
        assert tel.instrument(exporter=InMemorySpanExporter()).capture_content is False
    finally:
        tel.activate(previous)


def test_the_default_exporter_is_otlp_http_to_the_local_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keelgate.telemetry.setup import _otlp_exporter

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    exporter = _otlp_exporter(None, None)
    assert exporter._endpoint == "http://localhost:4318/v1/traces"  # type: ignore[attr-defined]
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/")
    assert _otlp_exporter(None, None)._endpoint == "http://collector:4318/v1/traces"  # type: ignore[attr-defined]
    assert _otlp_exporter("http://x/v1/traces", None)._endpoint == "http://x/v1/traces"  # type: ignore[attr-defined]


def test_a_grpc_protocol_setting_is_refused_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keelgate.telemetry.setup import _otlp_exporter

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    with pytest.raises(ValueError, match="OTLP/HTTP"):
        _otlp_exporter(None, None)


def test_with_nothing_configured_instrumented_code_is_a_cheap_no_op() -> None:
    # The default telemetry forwards to the global (no-op) provider: nothing is recorded or raised.
    assert tel.active().capture_content is False
    out = run(llm(Reply.say("fine")).complete(request()))
    assert out.text == "fine"
