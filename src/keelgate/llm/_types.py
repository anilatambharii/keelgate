"""Provider-neutral LLM request and response types.

Everything provider specific (SDK imports, wire formats, retry quirks) lives under
``keelgate.llm.providers``. The rest of Keelgate, and its users, see only these types,
so swapping Anthropic for a local model changes one constructor call.

The model's output is a *proposal*. Nothing in :class:`LLMResponse` is trusted: tool
calls are checked by the gateway, and text is untrusted data.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    """Who said a message: system, user, assistant or tool."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """A tool invocation the model proposes. ``arguments`` is untrusted JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One message in a conversation: a role, text, any tool calls the assistant made, and for a
    tool result which call it answers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # For role=TOOL: which call this answers.
    tool_call_id: str | None = None
    name: str | None = None


class ToolSchema(BaseModel):
    """A tool offered to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class LLMRequest(BaseModel):
    """A provider-neutral request: model, messages, the tools offered, a token limit.

    ``metadata`` is for correlation only and is never sent to a provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...] = ()
    max_tokens: int = Field(default=1024, gt=0)
    temperature: float | None = Field(default=None, ge=0)
    # Free-form correlation data (run id, iteration, call index). Never sent to a provider.
    metadata: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    """Tokens and money for one call. ``cost_usd`` is None when the price is unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class FinishReason(StrEnum):
    """Why a model stopped: a natural stop, tool calls, the length limit, or something else."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    OTHER = "other"


class LLMResponse(BaseModel):
    """A provider-neutral reply: one assistant message, usage with cost if known, and why it
    stopped.

    Nothing in it is trusted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: Message
    usage: Usage = Field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.STOP
    model: str = ""
    response_id: str | None = None

    @property
    def text(self) -> str:
        return self.message.content

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return self.message.tool_calls


@runtime_checkable
class LLMClient(Protocol):
    """A model-agnostic chat client. Implementations must be safe to share between tasks."""

    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class LLMError(Exception):
    """A provider call failed. ``retryable`` says whether trying again can help."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class LLMAuthError(LLMError):
    """Credentials are missing or rejected. Never retryable."""

    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message, provider=provider, retryable=False)


class LLMRateLimitError(LLMError):
    """The provider asked the caller to slow down. Retryable."""

    def __init__(self, message: str, *, provider: str = "") -> None:
        super().__init__(message, provider=provider, retryable=True)
