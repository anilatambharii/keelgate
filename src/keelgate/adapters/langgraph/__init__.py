"""LangGraph adapter: governed tools, a chat-model wrapper, and the durable checkpoint store.

``keelgate[langgraph]`` provides the SDK. The loop's default durable checkpointer lives in
``keelgate.loop`` and is re-exported here for convenience.
"""

try:
    from keelgate.adapters.langgraph._chat_model import KeelgateChatModel, to_keelgate_messages
    from keelgate.adapters.langgraph._tools import governed_langchain_tools
    from keelgate.loop.langgraph_store import LangGraphCheckpointStore
except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
        "keelgate.adapters.langgraph needs LangGraph: pip install 'keelgate[langgraph]'"
    ) from exc

__all__ = [
    "KeelgateChatModel",
    "LangGraphCheckpointStore",
    "governed_langchain_tools",
    "to_keelgate_messages",
]
