"""OpenAI Chat Completions client, also used for vLLM and any OpenAI-compatible server.

``pip install 'keelgate[openai]'``. vLLM, LM Studio, llama.cpp's server and others speak this wire
format, so :class:`VLLMClient` is the same client pointed at a local ``base_url``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from keelgate.llm._types import (
    FinishReason,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolCall,
)
from keelgate.llm.providers._common import error_for_status, parse_arguments, usage_for

if TYPE_CHECKING:
    from keelgate.llm._pricing import PricingTable

_FINISH = {
    "stop": FinishReason.STOP,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "length": FinishReason.LENGTH,
}


def to_openai_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.TOOL:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
        elif message.role is Role.ASSISTANT:
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in message.tool_calls
                ]
            out.append(entry)
        else:
            out.append({"role": message.role.value, "content": message.content})
    return out


class OpenAIClient:
    """An :class:`~keelgate.llm._types.LLMClient` for OpenAI and OpenAI-compatible servers."""

    name = "openai"

    def __init__(
        self,
        *,
        client: Any = None,
        api_key: str | None = None,
        base_url: str | None = None,
        pricing: PricingTable | None = None,
        token_param: str = "max_completion_tokens",  # noqa: S107 - a request field name
    ) -> None:
        if client is None:
            try:
                import openai  # noqa: PLC0415 - optional dependency, imported lazily
            except ImportError as exc:  # pragma: no cover - depends on the extra
                raise ImportError(
                    "OpenAIClient needs the OpenAI SDK: pip install 'keelgate[openai]'"
                ) from exc
            client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._client = client
        self._pricing = pricing
        self._token_param = token_param

    async def complete(self, request: LLMRequest) -> LLMResponse:
        params: dict[str, Any] = {
            "model": request.model,
            "messages": to_openai_messages(request.messages),
            self._token_param: request.max_tokens,
        }
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        try:
            response = await self._client.chat.completions.create(**params)
        except Exception as exc:
            raise _translate(self.name, exc) from exc
        return self._parse(request, response)

    def _parse(self, request: LLMRequest, response: Any) -> LLMResponse:
        if not response.choices:
            raise LLMError("the provider returned no choices", provider=self.name, retryable=True)
        choice = response.choices[0]
        message = choice.message
        calls = tuple(
            ToolCall(
                id=c.id or f"call_{n}",
                name=c.function.name,
                arguments=parse_arguments(c.function.arguments),
            )
            for n, c in enumerate(message.tool_calls or [])
            if getattr(c, "function", None) is not None
        )
        usage = response.usage
        return LLMResponse(
            message=Message(role=Role.ASSISTANT, content=message.content or "", tool_calls=calls),
            usage=usage_for(
                self._pricing,
                request.model,
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
            ),
            finish_reason=_FINISH.get(choice.finish_reason or "", FinishReason.OTHER),
            model=getattr(response, "model", None) or request.model,
            response_id=getattr(response, "id", None),
        )


class VLLMClient(OpenAIClient):
    """A vLLM (or other OpenAI-compatible) server. No API key is required by default."""

    name = "vllm"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "EMPTY",
        client: Any = None,
        pricing: PricingTable | None = None,
    ) -> None:
        super().__init__(
            client=client,
            api_key=api_key,
            base_url=base_url,
            pricing=pricing,
            token_param="max_tokens",  # noqa: S106 - a request field name
        )


def _translate(provider: str, exc: Exception) -> LLMError:
    status = getattr(exc, "status_code", None)
    if type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
        return LLMError(str(exc), provider=provider, retryable=True)
    if isinstance(status, int):
        return error_for_status(provider, status, str(exc))
    return LLMError(str(exc), provider=provider, retryable=False)
