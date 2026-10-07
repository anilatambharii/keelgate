"""OpenAI Agents SDK adapter: governed function tools, and an ``LLMClient``-backed model.

Needs ``keelgate[openai]``. Note that the SDK itself requires a recent Python 3.11 patch release
or newer: 3.11.0 has a ``typing`` bug that stops ``import agents`` from working at all.
"""

try:
    from keelgate.adapters.openai_agents._model import KeelgateModel, to_keelgate_messages
    from keelgate.adapters.openai_agents._tools import governed_function_tools
except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
        "keelgate.adapters.openai_agents needs the OpenAI Agents SDK: "
        "pip install 'keelgate[openai]'"
    ) from exc

__all__ = ["KeelgateModel", "governed_function_tools", "to_keelgate_messages"]
