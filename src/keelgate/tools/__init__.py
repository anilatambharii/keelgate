"""Tool registry and declaration, with each tool tagged by side effect."""

from keelgate.tools._gateway import PAPER_MODES, CallContext, ToolGateway
from keelgate.tools._idempotency import (
    Claim,
    ClaimState,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    SqliteIdempotencyStore,
)
from keelgate.tools._outcomes import (
    ErrorCode,
    OutcomeStatus,
    ToolError,
    ToolOutcome,
    Untrusted,
)
from keelgate.tools._spec import (
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
    "SqliteIdempotencyStore",
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
