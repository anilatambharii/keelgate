"""Model-agnostic LLM client boundary. Provider logic stays inside ``keelgate.llm``."""

from keelgate.llm.pricing import ModelPrice, PricingTable
from keelgate.llm.types import (
    FinishReason,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    ToolSchema,
    Usage,
)

__all__ = [
    "FinishReason",
    "LLMAuthError",
    "LLMClient",
    "LLMError",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "ModelPrice",
    "PricingTable",
    "Role",
    "ToolCall",
    "ToolSchema",
    "Usage",
]
