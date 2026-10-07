"""Use any Keelgate ``LLMClient`` as a LangChain chat model.

This is what lets a deterministic ``FakeLLM`` (or any provider client) drive a real LangGraph
agent. It is also how an agent built with LangGraph can use Keelgate's provider-agnostic
clients instead of a LangChain provider package.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field, PrivateAttr

from keelgate.llm._types import LLMClient, LLMRequest, Message, Role, ToolCall, ToolSchema


def to_keelgate_messages(messages: list[BaseMessage]) -> tuple[Message, ...]:
    """Convert LangChain messages into Keelgate ``Message`` objects."""
    converted: list[Message] = []
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        if isinstance(m, SystemMessage):
            converted.append(Message(role=Role.SYSTEM, content=content))
        elif isinstance(m, HumanMessage):
            converted.append(Message(role=Role.USER, content=content))
        elif isinstance(m, ToolMessage):
            converted.append(Message(role=Role.TOOL, content=content, tool_call_id=m.tool_call_id))
        elif isinstance(m, AIMessage):
            calls = tuple(
                ToolCall(
                    id=str(c.get("id") or ""), name=c["name"], arguments=dict(c.get("args", {}))
                )
                for c in m.tool_calls
            )
            converted.append(Message(role=Role.ASSISTANT, content=content, tool_calls=calls))
        else:
            converted.append(Message(role=Role.USER, content=content))
    return tuple(converted)


class KeelgateChatModel(BaseChatModel):
    """A LangChain chat model whose completions come from a Keelgate ``LLMClient``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm: Any = Field(description="a keelgate.llm.LLMClient")
    model_name: str = "keelgate"
    max_tokens: int = 1024
    tool_schemas: tuple[ToolSchema, ...] = ()
    _calls: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "keelgate"

    def bind_tools(self, tools: Any, **kwargs: Any) -> KeelgateChatModel:  # noqa: ARG002
        schemas = []
        for tool in tools:
            spec = convert_to_openai_tool(tool)["function"]
            schemas.append(
                ToolSchema(
                    name=spec["name"],
                    description=spec.get("description", ""),
                    input_schema=spec.get("parameters", {"type": "object"}),
                )
            )
        return self.model_copy(update={"tool_schemas": tuple(schemas)})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("KeelgateChatModel is async-only: use ainvoke / astream")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ChatResult:
        client: LLMClient = self.llm
        index = self._calls
        self._calls += 1
        response = await client.complete(
            LLMRequest(
                model=self.model_name,
                messages=to_keelgate_messages(messages),
                tools=self.tool_schemas,
                max_tokens=self.max_tokens,
                # A scripted test double indexes by this; providers never see it.
                metadata={"call_index": index},
            )
        )
        tool_calls = [
            {"name": c.name, "args": dict(c.arguments), "id": c.id, "type": "tool_call"}
            for c in response.tool_calls
        ]
        message = AIMessage(
            content=response.text,
            tool_calls=tool_calls,
            usage_metadata={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


__all__ = ["KeelgateChatModel", "to_keelgate_messages"]
