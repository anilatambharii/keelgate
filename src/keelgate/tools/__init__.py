"""Tool registry and declaration, with each tool tagged by side effect."""

from keelgate.tools.gateway import PAPER_MODES, CallContext, ToolGateway
from keelgate.tools.idempotency import (
    Claim,
    ClaimState,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)
from keelgate.tools.outcomes import (
    ErrorCode,
    OutcomeStatus,
    ToolError,
    ToolOutcome,
    Untrusted,
)
from keelgate.tools.spec import (
    DirectInvocationError,
    RegistryFrozenError,
    SideEffect,
    Tool,
    ToolDefinitionError,
    ToolRefusedError,
    ToolRegistry,
    ToolSpec,
    tool,
)

__all__ = [
    "PAPER_MODES",
    "CallContext",
    "Claim",
    "ClaimState",
    "DirectInvocationError",
    "ErrorCode",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "OutcomeStatus",
    "RegistryFrozenError",
    "SideEffect",
    "Tool",
    "ToolDefinitionError",
    "ToolError",
    "ToolGateway",
    "ToolOutcome",
    "ToolRefusedError",
    "ToolRegistry",
    "ToolSpec",
    "Untrusted",
    "tool",
]
