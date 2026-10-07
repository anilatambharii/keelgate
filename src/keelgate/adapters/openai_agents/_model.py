"""Use any Keelgate ``LLMClient`` as an OpenAI Agents SDK model.

This lets a deterministic ``FakeLLM`` drive a real ``Runner.run`` in tests, and lets an agent
built with the Agents SDK use Keelgate's provider-agnostic clients (Anthropic, Google, local).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage as AgentsUsage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from keelgate.llm._types import LLMClient, LLMRequest, Message, Role, ToolCall, ToolSchema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence


def _field(item: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an input item that may be a dict or a pydantic object."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        text = _field(part, "text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def to_keelgate_messages(
    system_instructions: str | None, items: str | Sequence[Any]
) -> tuple[Message, ...]:
    messages: list[Message] = []
    if system_instructions:
        messages.append(Message(role=Role.SYSTEM, content=system_instructions))
    if isinstance(items, str):
        messages.append(Message(role=Role.USER, content=items))
        return tuple(messages)

    pending_calls: list[ToolCall] = []

    def flush() -> None:
        nonlocal pending_calls
        if pending_calls:
            messages.append(Message(role=Role.ASSISTANT, tool_calls=tuple(pending_calls)))
            pending_calls = []

    for item in items:
        kind = _field(item, "type")
        if kind == "function_call":
            try:
                args = json.loads(_field(item, "arguments") or "{}")
            except ValueError:
                args = {}
            pending_calls.append(
                ToolCall(
                    id=str(_field(item, "call_id", "")),
                    name=str(_field(item, "name")),
                    arguments=args,
                )
            )
            continue
        flush()
        if kind == "function_call_output":
            messages.append(
                Message(
                    role=Role.TOOL,
                    content=str(_field(item, "output", "")),
                    tool_call_id=str(_field(item, "call_id", "")),
                )
            )
            continue
        role = _field(item, "role")
        text = _text_of(_field(item, "content"))
        if role == "assistant" or (kind == "message" and role == "assistant"):
            messages.append(Message(role=Role.ASSISTANT, content=text))
        elif role == "system":
            messages.append(Message(role=Role.SYSTEM, content=text))
        else:
            messages.append(Message(role=Role.USER, content=text))
    flush()
    return tuple(messages)


class KeelgateModel(Model):
    """An Agents SDK ``Model`` whose completions come from a Keelgate ``LLMClient``."""

    def __init__(
        self, llm: LLMClient, *, model_name: str = "keelgate", max_tokens: int = 1024
    ) -> None:
        self._llm = llm
        self._model_name = model_name
        self._max_tokens = max_tokens
        self._calls = 0

    async def get_response(  # noqa: PLR0917 - signature fixed by the SDK
        self,
        system_instructions: str | None,
        input: Any,  # noqa: A002 - the SDK's own parameter name
        model_settings: Any,  # noqa: ARG002
        tools: list[Any],
        output_schema: Any,  # noqa: ARG002
        handoffs: list[Any],  # noqa: ARG002
        tracing: Any,  # noqa: ARG002
        *,
        previous_response_id: str | None = None,  # noqa: ARG002
        conversation_id: str | None = None,  # noqa: ARG002
        prompt: Any = None,  # noqa: ARG002
    ) -> ModelResponse:
        index = self._calls
        self._calls += 1
        schemas = tuple(
            ToolSchema(
                name=t.name,
                description=getattr(t, "description", "") or "",
                input_schema=getattr(t, "params_json_schema", {"type": "object"}),
            )
            for t in tools
            if hasattr(t, "params_json_schema")
        )
        response = await self._llm.complete(
            LLMRequest(
                model=self._model_name,
                messages=to_keelgate_messages(system_instructions, input),
                tools=schemas,
                max_tokens=self._max_tokens,
                metadata={"call_index": index},
            )
        )
        output: list[Any] = []
        if response.text:
            output.append(
                ResponseOutputMessage(
                    id=f"msg_{index}",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(type="output_text", text=response.text, annotations=[])
                    ],
                )
            )
        output.extend(
            ResponseFunctionToolCall(
                type="function_call",
                call_id=c.id,
                name=c.name,
                arguments=json.dumps(c.arguments, sort_keys=True),
                id=f"fc_{index}_{n}",
            )
            for n, c in enumerate(response.tool_calls)
        )
        usage = AgentsUsage(
            requests=1,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.total_tokens,
        )
        return ModelResponse(output=output, usage=usage, response_id=None)

    async def stream_response(
        self,
        *args: Any,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        for chunk in self._streaming_unsupported():
            yield chunk

    @staticmethod
    def _streaming_unsupported() -> Iterator[Any]:
        raise NotImplementedError("KeelgateModel does not stream; use Runner.run")
