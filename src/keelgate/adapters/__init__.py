"""Adapters that wrap existing agent frameworks. Keelgate never requires them.

``GovernedToolset`` is the one governed path every adapter uses: the tools an agent may call,
each call routed through the gateway (grant, policy, approvals, audit), with tool output labelled
untrusted. It needs no optional dependency; the framework-specific adapters live in the
subpackages (``mcp``, ``langgraph``, ``openai_agents``, ``claude_agent_sdk``, ``a2a`` and
``temporal``).
"""

from keelgate.adapters._governed import UNTRUSTED_KEY, GovernedToolset

__all__ = ["UNTRUSTED_KEY", "GovernedToolset"]
