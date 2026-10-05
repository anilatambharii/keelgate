"""Ollama client over plain ``httpx`` (no extra dependency): talks to ``/api/chat``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from keelgate.llm.providers._common import (
    HTTP_CLIENT_ERROR,
    error_for_status,
    parse_arguments,
    usage_for,
)
from keelgate.llm.types import (
    FinishReason,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolCall,
)

if TYPE_CHECKING:
    from keelgate.llm.pricing import PricingTable


def to_ollama_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.TOOL:
            entry: dict[str, Any] = {"role": "tool", "content": message.content}
            if message.name:
                entry["tool_name"] = message.name
            out.append(entry)
        elif message.role is Role.ASSISTANT:
            assistant: dict[str, Any] = {"role": "assistant", "content": message.content}
            if message.tool_calls:
                assistant["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}}
                    for c in message.tool_calls
                ]
            out.append(assistant)
        else:
            out.append({"role": message.role.value, "content": message.content})
    return out


class OllamaClient:
    """An :class:`~keelgate.llm.types.LLMClient` for a local Ollama server.

    Local models are free to run, so cost is reported as ``0.0`` unless a pricing table says
    otherwise. Ollama does not issue tool-call ids, so stable ones are generated.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        client: httpx.AsyncClient | None = None,
        pricing: PricingTable | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._pricing = pricing

    async def complete(self, request: LLMRequest) -> LLMResponse:
        options: dict[str, Any] = {"num_predict": request.max_tokens}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        body: dict[str, Any] = {
            "model": request.model,
            "messages": to_ollama_messages(request.messages),
            "stream": False,
            "options": options,
        }
        if request.tools:
            body["tools"] = [
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
            response = await self._client.post(f"{self._base_url}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(str(exc), provider=self.name, retryable=True) from exc
        if response.status_code >= HTTP_CLIENT_ERROR:
            raise error_for_status(self.name, response.status_code, response.text[:300])
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("the server returned invalid JSON", provider=self.name) from exc
        return self._parse(request, data)

    def _parse(self, request: LLMRequest, data: dict[str, Any]) -> LLMResponse:
        message = data.get("message") or {}
        calls = tuple(
            ToolCall(
                id=f"call_{n}",
                name=str((c.get("function") or {}).get("name", "")),
                arguments=parse_arguments((c.get("function") or {}).get("arguments")),
            )
            for n, c in enumerate(message.get("tool_calls") or [])
        )
        usage = usage_for(
            self._pricing,
            request.model,
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )
        if usage.cost_usd is None:
            usage = usage.model_copy(update={"cost_usd": 0.0})
        reason = (
            FinishReason.TOOL_CALLS
            if calls
            else FinishReason.LENGTH
            if data.get("done_reason") == "length"
            else FinishReason.STOP
        )
        return LLMResponse(
            message=Message(
                role=Role.ASSISTANT, content=str(message.get("content") or ""), tool_calls=calls
            ),
            usage=usage,
            finish_reason=reason,
            model=str(data.get("model") or request.model),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
