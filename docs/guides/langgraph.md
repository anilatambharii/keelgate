# A governed LangGraph agent

Keelgate wraps LangGraph; it does not replace it. LangGraph still owns the agent loop, the graph
and the state. What changes is the **tools**: instead of plain functions, the agent is handed
Keelgate's, so every call it makes passes the gate.

```bash
pip install "keelgate[langgraph]"
python examples/governed_langgraph_agent.py
```

## The change

```python
from keelgate.adapters import GovernedToolset
from keelgate.adapters.langgraph import KeelgateChatModel, governed_langchain_tools

toolset = GovernedToolset(
    gateway=gateway,            # the ToolGateway from the tutorial
    registry=registry,
    grant_token=grant.token,
    context_factory=context,    # builds the trusted CallContext on every call
)

tools = governed_langchain_tools(toolset)     # ordinary LangChain tools
agent = create_agent(your_chat_model, tools)  # or langgraph.prebuilt.create_react_agent
```

That is the whole integration. `governed_langchain_tools` returns plain LangChain `StructuredTool`s,
so they work in any LangGraph graph, with any chat model, in any node.

## What the agent sees

A tool call comes back as JSON text the agent can read:

| Situation | What the model reads |
|---|---|
| Allowed | `{"status": "OK", "untrusted_tool_output": {...}}` |
| Refused by policy | `{"status": "DENIED", "error": {"code": "policy_denied", "message": ..., "hint": ...}}` |
| Needs a human | `{"status": "APPROVAL_REQUIRED", ...}` |
| Bad arguments | `{"status": "ERROR", "error": {"code": "invalid_arguments", ...}}` |

A refusal is a result, not a raised exception, so the agent can choose another approach instead of
crashing, and the hints are fixed wording rather than anything an attacker wrote. The tool's actual
output is under the key `untrusted_tool_output` so that it is clear, to the model and to anyone
reading the trace, that it is data and not instructions.

## What you get for free

- **Least privilege.** `allowed_side_effects=frozenset({SideEffect.READ})` on the toolset hides and
  refuses every write tool: a read-only research agent cannot trade even if its grant would allow it.
- **The same audit chain** as any other Keelgate call, so a LangGraph run is verifiable.
- **Idempotency.** If LangGraph retries a node, the order is not placed twice.

## Running it with no API key

The example drives a real LangGraph agent with a scripted model, using `KeelgateChatModel` to wrap a
Keelgate `LLMClient` as a LangChain chat model. The script has the agent try a restricted symbol
(denied by policy) and then a legitimate order (placed once):

```text
  tool call -> OK
  tool call -> DENIED   policy_denied
  tool call -> OK

  paper orders actually placed: ['lg-2']
  audit chain intact: True
```

!!! note "Compatibility"
    LangGraph 1.x moved the prebuilt agent to `langchain.agents.create_agent`;
    `langgraph.prebuilt.create_react_agent` still works and is deprecated for removal in LangGraph 2.
    The example uses whichever is installed. The adapter itself only produces LangChain tools and a
    chat model, so it is unaffected.
