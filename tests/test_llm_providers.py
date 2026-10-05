"""Provider clients, driven through the REAL vendor SDKs over a mocked HTTP transport.

No network and no API keys: each test hands the genuine ``anthropic`` / ``openai`` /
``google-genai`` client an ``httpx.MockTransport`` that returns a canned wire response, so the
SDK's own request building and response parsing run. This proves the translation logic against the
real SDK types. It does NOT prove behaviour against the live services (see docs): wire formats
here are written from the vendors' public API references.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
import pytest

from keelgate.llm import (
    FinishReason,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    Message,
    ModelPrice,
    PricingTable,
    Role,
    ToolCall,
    ToolSchema,
)
from tests.conftest import run

PRICES = PricingTable({"m-": ModelPrice(input_per_mtok=2.0, output_per_mtok=10.0)})
MODEL = "m-test"

TOOLS = (
    ToolSchema(
        name="market_quote",
        description="Latest quote",
        input_schema={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    ),
)
CONVERSATION = (
    Message(role=Role.SYSTEM, content="Be careful."),
    Message(role=Role.USER, content="Quote AAPL"),
    Message(
        role=Role.ASSISTANT,
        content="Looking it up.",
        tool_calls=(
            ToolCall(id="c1", name="market_quote", arguments={"symbol": "AAPL"}),
            ToolCall(id="c2", name="market_quote", arguments={"symbol": "MSFT"}),
        ),
    ),
    Message(role=Role.TOOL, content='{"price": 1}', tool_call_id="c1", name="market_quote"),
    Message(role=Role.TOOL, content='{"price": 2}', tool_call_id="c2", name="market_quote"),
)


def request(**kw: Any) -> LLMRequest:
    return LLMRequest(model=MODEL, messages=CONVERSATION, tools=TOOLS, max_tokens=256, **kw)


@dataclass
class Wire:
    """A canned response, and a record of what the client sent.

    The Anthropic and OpenAI SDKs are built on ``httpx2`` and reject an ``httpx`` client;
    Ollama (ours) and google-genai use ``httpx``. ``client(lib)`` builds the right kind.
    """

    status: int = 200
    body: Any = None
    seen: list[Any] = field(default_factory=list)
    fail: bool = False

    def handler(self, lib: Any) -> Callable[[Any], Any]:
        def handle(req: Any) -> Any:
            self.seen.append(req)
            if self.fail:
                raise lib.ConnectError("refused", request=req)
            return lib.Response(self.status, json=self.body)

        return handle

    def client(self, lib: Any = httpx) -> Any:
        return lib.AsyncClient(transport=lib.MockTransport(self.handler(lib)))

    @property
    def sent(self) -> dict[str, Any]:
        return json.loads(self.seen[-1].content)  # type: ignore[no-any-return]


# ----------------------------------------------------------------- canned responses


def anthropic_ok(text: str = "", call: dict[str, Any] | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    if call:
        content.append({"type": "tool_use", "id": "tu_1", **call})
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": "tool_use" if call else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000},
    }


def openai_ok(text: str | None = None, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "tool_calls": calls},
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 500_000,
            "total_tokens": 1_500_000,
        },
    }


def google_ok(parts: list[dict[str, Any]], finish: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 1_000_000, "candidatesTokenCount": 500_000},
        "modelVersion": MODEL,
    }


def ollama_ok(text: str = "", calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return {
        "model": MODEL,
        "message": message,
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 1_000_000,
        "eval_count": 500_000,
    }


# ----------------------------------------------------------------- client factories


def make_anthropic(wire: Wire) -> LLMClient:
    from anthropic import AsyncAnthropic

    from keelgate.llm.providers import AnthropicClient

    sdk = AsyncAnthropic(api_key="test", http_client=wire.client(httpx2), max_retries=0)
    return AnthropicClient(client=sdk, pricing=PRICES)


def make_openai(wire: Wire) -> LLMClient:
    from openai import AsyncOpenAI

    from keelgate.llm.providers import OpenAIClient

    sdk = AsyncOpenAI(api_key="test", http_client=wire.client(httpx2), max_retries=0)
    return OpenAIClient(client=sdk, pricing=PRICES)


def make_vllm(wire: Wire) -> LLMClient:
    from openai import AsyncOpenAI

    from keelgate.llm.providers import VLLMClient

    sdk = AsyncOpenAI(
        api_key="EMPTY",  # pragma: allowlist secret
        base_url="http://vllm.local/v1",
        http_client=wire.client(httpx2),
        max_retries=0,
    )
    return VLLMClient(client=sdk, pricing=PRICES)


def make_google(wire: Wire) -> LLMClient:
    from google import genai
    from google.genai import types

    from keelgate.llm.providers import GoogleClient

    sdk = genai.Client(
        api_key="test",  # pragma: allowlist secret
        http_options=types.HttpOptions(
            httpx_async_client=wire.client(),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    return GoogleClient(client=sdk, pricing=PRICES)


def make_ollama(wire: Wire) -> LLMClient:
    from keelgate.llm.providers import OllamaClient

    return OllamaClient(base_url="http://ollama.local", client=wire.client(), pricing=PRICES)


@dataclass(frozen=True)
class Provider:
    name: str
    make: Callable[[Wire], LLMClient]
    text: Callable[[str], Any]
    call: Callable[[str, dict[str, Any] | str], Any]  # (tool, args) -> response body


PROVIDERS = [
    Provider(
        "anthropic",
        make_anthropic,
        lambda t: anthropic_ok(text=t),
        lambda n, a: anthropic_ok(call={"name": n, "input": a}),
    ),
    Provider(
        "openai",
        make_openai,
        lambda t: openai_ok(text=t),
        lambda n, a: openai_ok(
            calls=[
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {
                        "name": n,
                        "arguments": a if isinstance(a, str) else json.dumps(a),
                    },
                }
            ]
        ),
    ),
    Provider(
        "vllm",
        make_vllm,
        lambda t: openai_ok(text=t),
        lambda n, a: openai_ok(
            calls=[
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {
                        "name": n,
                        "arguments": a if isinstance(a, str) else json.dumps(a),
                    },
                }
            ]
        ),
    ),
    Provider(
        "google",
        make_google,
        lambda t: google_ok([{"text": t}]),
        lambda n, a: google_ok([{"functionCall": {"name": n, "args": a}}]),
    ),
    Provider(
        "ollama",
        make_ollama,
        lambda t: ollama_ok(text=t),
        lambda n, a: ollama_ok(calls=[{"function": {"name": n, "arguments": a}}]),
    ),
]
IDS = [p.name for p in PROVIDERS]


@pytest.fixture(params=PROVIDERS, ids=IDS)
def provider(request: pytest.FixtureRequest) -> Provider:
    return request.param  # type: ignore[no-any-return]


# ----------------------------------------------------------------- shared contract


def test_a_text_reply_is_parsed(provider: Provider) -> None:
    wire = Wire(body=provider.text("hello there"))
    response = run(provider.make(wire).complete(request()))
    assert response.text == "hello there" and response.tool_calls == ()
    assert response.message.role is Role.ASSISTANT
    assert response.finish_reason is FinishReason.STOP


def test_a_tool_call_is_parsed_with_its_arguments(provider: Provider) -> None:
    wire = Wire(body=provider.call("market_quote", {"symbol": "AAPL"}))
    response = run(provider.make(wire).complete(request()))
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "market_quote" and call.arguments == {"symbol": "AAPL"} and call.id
    assert response.finish_reason is FinishReason.TOOL_CALLS


def test_usage_is_counted_and_priced_from_the_table(provider: Provider) -> None:
    wire = Wire(body=provider.text("x"))
    usage = run(provider.make(wire).complete(request())).usage
    assert (usage.input_tokens, usage.output_tokens) == (1_000_000, 500_000)
    assert usage.cost_usd == pytest.approx(2.0 + 5.0)  # 1M in at $2 + 0.5M out at $10


def test_an_unpriced_model_has_unknown_cost_except_local_ollama(provider: Provider) -> None:
    wire = Wire(body=provider.text("x"))
    client = provider.make(wire)
    unpriced = request().model_copy(update={"model": "other-model"})
    usage = run(client.complete(unpriced)).usage
    assert usage.cost_usd == (0.0 if provider.name == "ollama" else None)


def test_the_request_carries_the_tool_schemas_and_the_token_limit(provider: Provider) -> None:
    wire = Wire(body=provider.text("x"))
    run(provider.make(wire).complete(request()))
    body = wire.sent
    assert "market_quote" in json.dumps(body) and '"symbol"' in json.dumps(body)
    assert 256 in _flatten_numbers(body)  # max tokens reached the wire under some name


def test_the_conversation_survives_translation(provider: Provider) -> None:
    wire = Wire(body=provider.text("x"))
    run(provider.make(wire).complete(request()))
    wire_text = json.dumps(wire.sent)
    for fragment in ("Be careful.", "Quote AAPL", "Looking it up.", '{\\"price\\": 1}', "MSFT"):
        assert fragment in wire_text, fragment


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, LLMAuthError), (403, LLMAuthError), (429, LLMRateLimitError), (500, LLMError)],
)
def test_http_failures_map_to_keelgate_errors(
    provider: Provider, status: int, error: type[LLMError]
) -> None:
    wire = Wire(status=status, body={"error": {"message": "nope", "type": "x"}})
    with pytest.raises(error) as caught:
        run(provider.make(wire).complete(request()))
    assert caught.value.provider
    assert caught.value.retryable is (status in (429, 500))


def test_a_client_error_is_not_retryable(provider: Provider) -> None:
    wire = Wire(status=400, body={"error": {"message": "bad request", "type": "x"}})
    with pytest.raises(LLMError) as caught:
        run(provider.make(wire).complete(request()))
    assert not caught.value.retryable


def test_a_connection_failure_is_retryable(provider: Provider) -> None:
    client = provider.make(Wire(fail=True))
    with pytest.raises(LLMError) as caught:
        run(client.complete(request()))
    assert caught.value.retryable


def test_the_clients_satisfy_the_protocol(provider: Provider) -> None:
    client = provider.make(Wire(body=provider.text("x")))
    assert isinstance(client, LLMClient) and client.name


def test_broken_tool_arguments_become_empty_not_a_crash() -> None:
    for name in ("openai", "vllm"):
        p = next(p for p in PROVIDERS if p.name == name)
        wire = Wire(body=p.call("market_quote", "{not json"))
        response = run(p.make(wire).complete(request()))
        assert response.tool_calls[0].arguments == {}, name


# ----------------------------------------------------------------- provider specifics


def _flatten_numbers(value: Any) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _flatten_numbers(v)]
    if isinstance(value, list):
        return [n for v in value for n in _flatten_numbers(v)]
    return []


def test_anthropic_does_not_send_a_temperature_the_api_does_not_accept() -> None:
    wire = Wire(body=anthropic_ok(text="ok"))
    run(make_anthropic(wire).complete(request(temperature=0.5)))
    assert "temperature" not in wire.sent


def test_anthropic_groups_parallel_tool_results_into_one_user_turn() -> None:
    wire = Wire(body=anthropic_ok(text="ok"))
    run(make_anthropic(wire).complete(request()))
    body = wire.sent
    assert body["system"] == "Be careful."
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    results = body["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["c1", "c2"]
    assert wire.seen[-1].headers["x-api-key"] == "test"


def test_openai_uses_the_right_token_parameter_and_tool_message_shape() -> None:
    wire = Wire(body=openai_ok(text="ok"))
    run(make_openai(wire).complete(request()))
    body = wire.sent
    assert body["max_completion_tokens"] == 256 and "max_tokens" not in body
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"symbol": "AAPL"}'
    assert [m["tool_call_id"] for m in body["messages"] if m["role"] == "tool"] == ["c1", "c2"]


def test_vllm_targets_its_base_url_and_uses_max_tokens() -> None:
    wire = Wire(body=openai_ok(text="ok"))
    run(make_vllm(wire).complete(request()))
    assert str(wire.seen[-1].url).startswith("http://vllm.local/v1/")
    assert wire.sent["max_tokens"] == 256 and "max_completion_tokens" not in wire.sent


def test_google_matches_tool_results_by_name_and_batches_them() -> None:
    wire = Wire(body=google_ok([{"text": "ok"}]))
    run(make_google(wire).complete(request()))
    body = wire.sent
    assert body["systemInstruction"]["parts"][0]["text"] == "Be careful."
    contents = body["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    responses = contents[2]["parts"]
    assert [p["functionResponse"]["name"] for p in responses] == ["market_quote", "market_quote"]
    declaration = body["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "market_quote"
    assert body["generationConfig"]["maxOutputTokens"] == 256


def test_google_ignores_thought_parts_and_reports_length() -> None:
    wire = Wire(
        body=google_ok(
            [{"text": "private reasoning", "thought": True}, {"text": "answer"}], "MAX_TOKENS"
        )
    )
    response = run(make_google(wire).complete(request()))
    assert response.text == "answer" and response.finish_reason is FinishReason.LENGTH


def test_google_with_no_candidates_is_a_retryable_error() -> None:
    wire = Wire(body={"candidates": [], "usageMetadata": {}})
    with pytest.raises(LLMError) as caught:
        run(make_google(wire).complete(request()))
    assert caught.value.retryable


def test_openai_with_no_choices_is_a_retryable_error() -> None:
    body = openai_ok(text="x")
    body["choices"] = []
    with pytest.raises(LLMError) as caught:
        run(make_openai(Wire(body=body)).complete(request()))
    assert caught.value.retryable


def test_ollama_sends_native_fields_and_issues_stable_call_ids() -> None:
    wire = Wire(
        body=ollama_ok(
            calls=[
                {"function": {"name": "market_quote", "arguments": {"symbol": "A"}}},
                {"function": {"name": "market_quote", "arguments": {"symbol": "B"}}},
            ]
        )
    )
    response = run(make_ollama(wire).complete(request(temperature=0.1)))
    body = wire.sent
    assert str(wire.seen[-1].url) == "http://ollama.local/api/chat"
    assert body["stream"] is False and body["options"] == {"num_predict": 256, "temperature": 0.1}
    assert [c.id for c in response.tool_calls] == ["call_0", "call_1"]
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["arguments"] == {"symbol": "AAPL"}
    tool_msg = next(m for m in body["messages"] if m["role"] == "tool")
    assert tool_msg["tool_name"] == "market_quote"


def test_ollama_invalid_json_and_length_stop() -> None:
    from keelgate.llm.providers import OllamaClient

    def bad(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = OllamaClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(bad)), base_url="http://o"
    )
    with pytest.raises(LLMError):
        run(client.complete(request()))

    body = ollama_ok(text="cut")
    body["done_reason"] = "length"
    response = run(make_ollama(Wire(body=body)).complete(request()))
    assert response.finish_reason is FinishReason.LENGTH


def test_the_providers_package_imports_without_touching_any_sdk() -> None:
    import importlib

    module = importlib.import_module("keelgate.llm.providers")
    assert {"AnthropicClient", "GoogleClient", "OllamaClient", "OpenAIClient", "VLLMClient"} <= set(
        module.__all__
    )
