"""Google Gemini client via the ``google-genai`` SDK (``pip install 'keelgate[google]'``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from keelgate.llm.providers._common import (
    error_for_status,
    parse_arguments,
    split_system,
    tool_names_by_call_id,
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


class GoogleClient:
    """An :class:`~keelgate.llm.types.LLMClient` for Gemini models.

    Gemini matches a tool result to its call by *name*, so the name is recovered from the
    assistant turn that made the call. Automatic function calling is switched off: Keelgate runs
    tools itself, through the gateway.
    """

    name = "google"

    def __init__(
        self,
        *,
        client: Any = None,
        api_key: str | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        try:
            from google import genai  # noqa: PLC0415 - optional dependency, imported lazily
            from google.genai import types  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError(
                "GoogleClient needs the google-genai SDK: pip install 'keelgate[google]'"
            ) from exc
        self._types = types
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self._pricing = pricing

    def _contents(self, messages: list[Message], names: dict[str, str]) -> list[Any]:
        t = self._types
        contents: list[Any] = []
        for message in messages:
            if message.role is Role.TOOL:
                part = t.Part.from_function_response(
                    name=message.name or names.get(message.tool_call_id or "", "tool"),
                    response={"output": message.content},
                )
                last = contents[-1] if contents else None
                if last is not None and last.role == "user" and _only_responses(last):
                    last.parts.append(part)
                else:
                    contents.append(t.Content(role="user", parts=[part]))
            elif message.role is Role.ASSISTANT:
                parts = [t.Part.from_text(text=message.content)] if message.content else []
                parts.extend(
                    t.Part.from_function_call(name=c.name, args=c.arguments)
                    for c in message.tool_calls
                )
                contents.append(t.Content(role="model", parts=parts or [t.Part.from_text(text="")]))
            else:
                contents.append(
                    t.Content(role="user", parts=[t.Part.from_text(text=message.content)])
                )
        return contents

    async def complete(self, request: LLMRequest) -> LLMResponse:
        t = self._types
        system, conversation = split_system(request.messages)
        config: dict[str, Any] = {
            "max_output_tokens": request.max_tokens,
            "automatic_function_calling": t.AutomaticFunctionCallingConfig(disable=True),
        }
        if system:
            config["system_instruction"] = system
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.tools:
            config["tools"] = [
                t.Tool(
                    function_declarations=[
                        t.FunctionDeclaration(
                            name=tool.name,
                            description=tool.description,
                            parameters_json_schema=tool.input_schema,
                        )
                        for tool in request.tools
                    ]
                )
            ]
        try:
            response = await self._client.aio.models.generate_content(
                model=request.model,
                contents=self._contents(conversation, tool_names_by_call_id(request.messages)),
                config=t.GenerateContentConfig(**config),
            )
        except Exception as exc:
            raise _translate(exc) from exc
        return self._parse(request, response)

    def _parse(self, request: LLMRequest, response: Any) -> LLMResponse:
        candidates = response.candidates or []
        if not candidates:
            raise LLMError(
                "the provider returned no candidates", provider=self.name, retryable=True
            )
        candidate = candidates[0]
        text: list[str] = []
        calls: list[ToolCall] = []
        for part in (candidate.content.parts if candidate.content else None) or []:
            if getattr(part, "function_call", None) is not None:
                fc = part.function_call
                calls.append(
                    ToolCall(
                        id=fc.id or f"call_{len(calls)}",
                        name=fc.name,
                        arguments=parse_arguments(fc.args),
                    )
                )
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                text.append(part.text)
        meta = response.usage_metadata
        finish = str(getattr(candidate.finish_reason, "name", candidate.finish_reason) or "")
        if calls:
            reason = FinishReason.TOOL_CALLS
        elif finish == "STOP":
            reason = FinishReason.STOP
        elif finish == "MAX_TOKENS":
            reason = FinishReason.LENGTH
        else:
            reason = FinishReason.OTHER
        return LLMResponse(
            message=Message(role=Role.ASSISTANT, content="".join(text), tool_calls=tuple(calls)),
            usage=usage_for(
                self._pricing,
                request.model,
                getattr(meta, "prompt_token_count", 0),
                getattr(meta, "candidates_token_count", 0),
            ),
            finish_reason=reason,
            model=getattr(response, "model_version", None) or request.model,
            response_id=getattr(response, "response_id", None),
        )


def _only_responses(content: Any) -> bool:
    return bool(content.parts) and all(
        getattr(p, "function_response", None) is not None for p in content.parts
    )


def _translate(exc: Exception) -> LLMError:
    if isinstance(exc, httpx.TransportError):  # the SDK lets transport errors through as-is
        return LLMError(str(exc), provider="google", retryable=True)
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return error_for_status("google", code, str(exc))
    return LLMError(str(exc), provider="google", retryable=False)
