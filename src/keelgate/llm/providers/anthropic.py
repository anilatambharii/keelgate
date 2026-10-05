"""Anthropic Messages API client (``pip install 'keelgate[anthropic]'``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from keelgate.llm.providers._common import (
    error_for_status,
    parse_arguments,
    split_system,
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

_STOP_REASONS = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "tool_use": FinishReason.TOOL_CALLS,
    "max_tokens": FinishReason.LENGTH,
}


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Conversation turns in Anthropic's shape.

    Tool results travel as ``tool_result`` blocks inside a *user* turn, and consecutive results
    (the answers to one assistant turn's parallel calls) must share a single turn.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.TOOL:
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.content,
            }
            last = out[-1] if out else None
            if last and last["role"] == "user" and _is_results(last["content"]):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif message.role is Role.ASSISTANT:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in message.tool_calls
            )
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            out.append({"role": "user", "content": message.content})
    return out


def _is_results(content: Any) -> bool:
    return isinstance(content, list) and bool(content) and content[0].get("type") == "tool_result"


class AnthropicClient:
    """An :class:`~keelgate.llm.types.LLMClient` for Claude models.

    ``client`` may be any ``anthropic.AsyncAnthropic``; otherwise one is built from the
    environment (``ANTHROPIC_API_KEY``). ``LLMRequest.temperature`` is ignored: the current
    Messages API does not accept a sampling temperature.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        client: Any = None,
        api_key: str | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        if client is None:
            try:
                import anthropic  # noqa: PLC0415 - optional dependency, imported lazily
            except ImportError as exc:  # pragma: no cover - depends on the extra
                raise ImportError(
                    "AnthropicClient needs the Anthropic SDK: pip install 'keelgate[anthropic]'"
                ) from exc
            client = anthropic.AsyncAnthropic(api_key=api_key)
        self._client = client
        self._pricing = pricing

    async def complete(self, request: LLMRequest) -> LLMResponse:
        system, conversation = split_system(request.messages)
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": to_anthropic_messages(conversation),
        }
        if system:
            params["system"] = system
        if request.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        try:
            response = await self._client.messages.create(**params)
        except Exception as exc:
            raise _translate(exc) from exc
        return self._parse(request, response)

    def _parse(self, request: LLMRequest, response: Any) -> LLMResponse:
        text: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content or []:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=parse_arguments(block.input))
                )
        model = getattr(response, "model", None) or request.model
        usage = response.usage
        return LLMResponse(
            message=Message(role=Role.ASSISTANT, content="".join(text), tool_calls=tuple(calls)),
            usage=usage_for(self._pricing, request.model, usage.input_tokens, usage.output_tokens),
            finish_reason=_STOP_REASONS.get(response.stop_reason or "", FinishReason.OTHER),
            model=model,
            response_id=getattr(response, "id", None),
        )


def _translate(exc: Exception) -> LLMError:
    """Map an Anthropic SDK exception to a Keelgate one (the SDK is imported lazily)."""
    status = getattr(exc, "status_code", None)
    kind = type(exc).__name__
    if kind in {"APIConnectionError", "APITimeoutError"}:
        return LLMError(str(exc), provider="anthropic", retryable=True)
    if isinstance(status, int):
        return error_for_status("anthropic", status, str(exc))
    return LLMError(str(exc), provider="anthropic", retryable=False)
