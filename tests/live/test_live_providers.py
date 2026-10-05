"""Live smoke tests against real services. OFF by default.

    KEELGATE_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/live -m live --no-cov -rs

Every test needs its own credentials or server and skips (visibly, with a reason) without them, so
a plain ``pytest`` run never spends money or touches the network. They exist because the unit
tests drive the real vendor SDKs only over a mocked transport; these check the wire for real.

Model names are not guessed for providers whose catalogue changes: set the ``*_MODEL`` variable.

    ANTHROPIC_API_KEY   [KEELGATE_LIVE_ANTHROPIC_MODEL, default claude-haiku-4-5-20251001]
    OPENAI_API_KEY      KEELGATE_LIVE_OPENAI_MODEL
    GOOGLE_API_KEY      KEELGATE_LIVE_GOOGLE_MODEL
    Ollama              KEELGATE_LIVE_OLLAMA_MODEL  (server at KEELGATE_OLLAMA_URL or localhost)
    vLLM                KEELGATE_LIVE_VLLM_MODEL    (server at KEELGATE_VLLM_URL)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from keelgate.llm import (
    FinishReason,
    LLMClient,
    LLMRequest,
    Message,
    Role,
    ToolSchema,
)

pytestmark = pytest.mark.live

QUOTE_TOOL = ToolSchema(
    name="market_quote",
    description="Get the latest quote for a ticker symbol.",
    input_schema={
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
)


def _enabled() -> None:
    if os.environ.get("KEELGATE_LIVE") != "1":
        pytest.skip("live tests are off: set KEELGATE_LIVE=1 to enable")


def _need(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        pytest.skip(f"{var} is not set")
    return value


def _client_anthropic() -> tuple[LLMClient, str]:
    _need("ANTHROPIC_API_KEY")
    from keelgate.llm.providers import AnthropicClient

    return AnthropicClient(), os.environ.get(
        "KEELGATE_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
    )


def _client_openai() -> tuple[LLMClient, str]:
    _need("OPENAI_API_KEY")
    from keelgate.llm.providers import OpenAIClient

    return OpenAIClient(), _need("KEELGATE_LIVE_OPENAI_MODEL")


def _client_google() -> tuple[LLMClient, str]:
    _need("GOOGLE_API_KEY")
    from keelgate.llm.providers import GoogleClient

    return GoogleClient(), _need("KEELGATE_LIVE_GOOGLE_MODEL")


def _client_ollama() -> tuple[LLMClient, str]:
    from keelgate.llm.providers import OllamaClient

    model = _need("KEELGATE_LIVE_OLLAMA_MODEL")
    return OllamaClient(
        base_url=os.environ.get("KEELGATE_OLLAMA_URL", "http://localhost:11434")
    ), model


def _client_vllm() -> tuple[LLMClient, str]:
    from keelgate.llm.providers import VLLMClient

    model = _need("KEELGATE_LIVE_VLLM_MODEL")
    return VLLMClient(base_url=_need("KEELGATE_VLLM_URL")), model


FACTORIES: dict[str, Any] = {
    "anthropic": _client_anthropic,
    "openai": _client_openai,
    "google": _client_google,
    "ollama": _client_ollama,
    "vllm": _client_vllm,
}


@pytest.fixture(params=list(FACTORIES))
def live(request: pytest.FixtureRequest) -> tuple[LLMClient, str]:
    _enabled()
    return FACTORIES[request.param]()  # type: ignore[no-any-return]


def test_a_plain_completion_returns_text_and_token_counts(live: tuple[LLMClient, str]) -> None:
    client, model = live
    request = LLMRequest(
        model=model,
        messages=(Message(role=Role.USER, content="Reply with the single word: pong"),),
        max_tokens=64,
    )
    response = asyncio.run(client.complete(request))
    assert response.text.strip()
    assert response.usage.input_tokens > 0 and response.usage.output_tokens > 0
    assert response.finish_reason in (FinishReason.STOP, FinishReason.LENGTH)


def test_the_model_can_be_offered_a_tool_and_the_reply_parses(live: tuple[LLMClient, str]) -> None:
    """A model may answer in text instead of calling the tool; both must parse cleanly. If it does
    call, the call must be well formed (a name from the offered set, dict arguments)."""
    client, model = live
    request = LLMRequest(
        model=model,
        messages=(
            Message(
                role=Role.USER,
                content="Use the market_quote tool to get the quote for AAPL. Call it now.",
            ),
        ),
        tools=(QUOTE_TOOL,),
        max_tokens=256,
    )
    response = asyncio.run(client.complete(request))
    for call in response.tool_calls:
        assert call.name == "market_quote" and isinstance(call.arguments, dict) and call.id


def test_a_tool_result_can_be_sent_back_and_the_model_answers(live: tuple[LLMClient, str]) -> None:
    client, model = live
    first = asyncio.run(
        client.complete(
            LLMRequest(
                model=model,
                messages=(
                    Message(
                        role=Role.USER, content="Get the AAPL quote with the tool, then say it."
                    ),
                ),
                tools=(QUOTE_TOOL,),
                max_tokens=256,
            )
        )
    )
    if not first.tool_calls:
        pytest.skip("this model answered without calling the tool; the round trip is not exercised")
    call = first.tool_calls[0]
    followup = LLMRequest(
        model=model,
        messages=(
            Message(role=Role.USER, content="Get the AAPL quote with the tool, then say it."),
            first.message,
            Message(
                role=Role.TOOL,
                content='{"symbol": "AAPL", "price": 187.25}',
                tool_call_id=call.id,
                name=call.name,
            ),
        ),
        tools=(QUOTE_TOOL,),
        max_tokens=256,
    )
    answer = asyncio.run(client.complete(followup))
    assert answer.text.strip() or answer.tool_calls


def test_a_bad_credential_is_an_auth_error_not_a_crash() -> None:
    _enabled()
    _need("KEELGATE_LIVE_BAD_KEY_CHECK")  # opt in separately: it makes a deliberately failing call
    from keelgate.llm import LLMAuthError
    from keelgate.llm.providers import AnthropicClient

    client = AnthropicClient(api_key="invalid-key-for-test")  # pragma: allowlist secret
    with pytest.raises(LLMAuthError):
        asyncio.run(
            client.complete(
                LLMRequest(
                    model="claude-haiku-4-5-20251001",
                    messages=(Message(role=Role.USER, content="hi"),),
                    max_tokens=8,
                )
            )
        )
