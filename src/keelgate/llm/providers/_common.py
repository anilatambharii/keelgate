"""Helpers shared by the provider clients. Internal."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from keelgate.llm._pricing import PricingTable
from keelgate.llm._types import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    Message,
    Role,
    Usage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


HTTP_REQUEST_TIMEOUT = 408
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR = 500
HTTP_CLIENT_ERROR = 400


def usage_for(
    pricing: PricingTable | None, model: str, input_tokens: int | None, output_tokens: int | None
) -> Usage:
    """Usage with a cost when the model is priced; ``None`` cost otherwise."""
    table = pricing or PricingTable()
    return table.usage(model, int(input_tokens or 0), int(output_tokens or 0))


def error_for_status(provider: str, status: int | None, message: str) -> LLMError:
    """Map an HTTP status to the right Keelgate error. 429 and 5xx are worth retrying."""
    if status in (401, 403):
        return LLMAuthError(message, provider=provider)
    if status == HTTP_RATE_LIMITED:
        return LLMRateLimitError(message, provider=provider)
    retryable = status is None or status >= HTTP_SERVER_ERROR or status == HTTP_REQUEST_TIMEOUT
    return LLMError(message, provider=provider, retryable=retryable)


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments from a provider: a JSON string or a dict. Anything else becomes ``{}``.

    Never raises: a model that emits broken JSON must produce an invalid-arguments refusal at
    the gateway, not a crash in the client.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def tool_names_by_call_id(messages: Iterable[Message]) -> dict[str, str]:
    """Which tool each call id belongs to, for providers that match results by name."""
    names: dict[str, str] = {}
    for message in messages:
        if message.role is Role.ASSISTANT:
            for call in message.tool_calls:
                names[call.id] = call.name
    return names


def split_system(messages: Iterable[Message]) -> tuple[str, list[Message]]:
    """Separate system text (joined) from the conversation, for providers that want it apart."""
    system: list[str] = []
    rest: list[Message] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            system.append(message.content)
        else:
            rest.append(message)
    return "\n\n".join(s for s in system if s), rest
