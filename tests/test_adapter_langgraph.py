"""LangGraph adapter: governed tools, the chat-model wrapper, and a real agent driven by FakeLLM."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.prebuilt import create_react_agent

from keelgate.adapters._governed import UNTRUSTED_KEY, GovernedToolset
from keelgate.adapters.langgraph import (
    KeelgateChatModel,
    governed_langchain_tools,
    to_keelgate_messages,
)
from keelgate.llm import Role
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import SideEffect
from tests.conftest import Harness, run

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TRADE = {"symbol": "AAPL", "notional": 5000, "client_order_id": "lg-1"}


def toolset(h: Harness, **kw: Any) -> GovernedToolset:
    return GovernedToolset(
        gateway=h.gateway,
        registry=h.registry,
        grant_token=h.grant(),
        context_factory=h.ctx,
        **kw,
    )


def tool_named(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


# --------------------------------------------------------------------------- tools


def test_each_governed_tool_becomes_a_langchain_tool_with_its_schema(h: Harness) -> None:
    tools = governed_langchain_tools(toolset(h))
    assert {t.name for t in tools} == set(h.registry.names())
    quote = tool_named(tools, "market_quote")
    assert "symbol" in quote.args and quote.description


def test_a_langchain_tool_call_runs_through_the_gateway_and_is_labelled_untrusted(
    h: Harness,
) -> None:
    quote = tool_named(governed_langchain_tools(toolset(h)), "market_quote")
    body = json.loads(run(quote.ainvoke({"symbol": "AAPL"})))
    assert body["status"] == "OK" and body[UNTRUSTED_KEY]["price"] == 101.5
    assert h.audit.verify_chain("tenant-1").ok


def test_a_refusal_comes_back_as_text_for_the_agent_to_read_not_as_a_raised_error(
    h: Harness,
) -> None:
    trade = tool_named(governed_langchain_tools(toolset(h)), "trade_paper_execute")
    body = json.loads(run(trade.ainvoke({**TRADE, "symbol": "TSLA"})))
    assert body["status"] == "DENIED" and body["error"]["code"] == "policy_denied"
    assert h.executed == []


def test_a_gated_call_is_reported_as_awaiting_approval(h: Harness) -> None:
    trade = tool_named(governed_langchain_tools(toolset(h)), "trade_paper_execute")
    body = json.loads(run(trade.ainvoke({**TRADE, "notional": 30_000})))
    assert body["status"] == "APPROVAL_REQUIRED" and body["approval_id"]
    assert h.executed == []


def test_a_read_only_toolset_does_not_offer_write_tools(h: Harness) -> None:
    tools = governed_langchain_tools(toolset(h, allowed_side_effects=frozenset({SideEffect.READ})))
    assert [t.name for t in tools] == ["market_quote"]


# ---------------------------------------------------------------------- chat model


def test_messages_convert_with_roles_tool_calls_and_tool_results() -> None:
    converted = to_keelgate_messages(
        [
            SystemMessage(content="be careful"),
            HumanMessage(content="buy"),
            AIMessage(
                content="",
                tool_calls=[{"name": "t", "args": {"a": 1}, "id": "c1", "type": "tool_call"}],
            ),
            ToolMessage(content="done", tool_call_id="c1"),
        ]
    )
    assert [m.role for m in converted] == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.TOOL]
    assert converted[2].tool_calls[0].name == "t" and converted[2].tool_calls[0].arguments == {
        "a": 1
    }
    assert converted[3].tool_call_id == "c1"


def test_bind_tools_converts_schemas_and_returns_a_new_model(h: Harness) -> None:
    base = KeelgateChatModel(llm=FakeLLM([Reply.say("x")]))
    bound = base.bind_tools(governed_langchain_tools(toolset(h)))
    assert base.tool_schemas == () and {s.name for s in bound.tool_schemas} == set(
        h.registry.names()
    )
    assert (
        "symbol"
        in next(s for s in bound.tool_schemas if s.name == "market_quote").input_schema[
            "properties"
        ]
    )


def test_the_chat_model_is_async_only_and_reports_usage() -> None:
    model = KeelgateChatModel(llm=FakeLLM([Reply.say("hello there")]))
    with pytest.raises(NotImplementedError, match="async-only"):
        model.invoke("hi")
    reply = run(model.ainvoke("hi"))
    assert reply.content == "hello there"
    assert reply.usage_metadata is not None and reply.usage_metadata["total_tokens"] > 0


def test_each_call_gets_a_fresh_index_so_an_indexed_script_advances() -> None:
    model = KeelgateChatModel(llm=FakeLLM([Reply.say("first"), Reply.say("second")], indexed=True))
    assert run(model.ainvoke("a")).content == "first"
    assert run(model.ainvoke("b")).content == "second"


# ------------------------------------------------------------------ a real agent


def agent_for(h: Harness, script: list[Reply]) -> Any:
    llm = FakeLLM(script, indexed=True)
    model = KeelgateChatModel(llm=llm)
    return create_react_agent(model, governed_langchain_tools(toolset(h))), llm


def tool_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]


def test_a_real_langgraph_agent_reads_is_denied_and_finishes(h: Harness) -> None:
    agent, llm = agent_for(
        h,
        [
            Reply.call("market_quote", symbol="AAPL"),
            Reply.call(
                "trade_paper_execute", **{**TRADE, "symbol": "TSLA", "client_order_id": "lg-deny"}
            ),
            Reply.say("I could not trade TSLA, so I stopped."),
        ],
    )
    result = run(agent.ainvoke({"messages": [("user", "Research and trade.")]}))

    assert result["messages"][-1].content == "I could not trade TSLA, so I stopped."
    first, second = tool_results(result)
    assert first["status"] == "OK" and second["status"] == "DENIED"
    assert h.executed == [] and llm.calls_made == 3
    kinds = [r.event_type for r in h.audit.records("tenant-1")]
    assert kinds.count("tool.call") == 2 and "policy.decision" in kinds
    assert h.audit.verify_chain("tenant-1").ok


def test_a_real_langgraph_agent_places_an_allowed_order_exactly_once(h: Harness) -> None:
    agent, _ = agent_for(
        h,
        [
            Reply.call("trade_paper_execute", **TRADE),
            Reply.call("trade_paper_execute", **TRADE),  # the agent retries; idempotency absorbs it
            Reply.say("Order placed."),
        ],
    )
    result = run(agent.ainvoke({"messages": [("user", "Buy a little AAPL.")]}))
    first, second = tool_results(result)
    assert first["status"] == "OK" and second["status"] == "OK"
    assert len(h.executed) == 1


def test_a_real_langgraph_agent_cannot_widen_its_authority_by_naming_a_tool_it_lacks(
    h: Harness,
) -> None:
    ts = toolset(h, allowed_side_effects=frozenset({SideEffect.READ}))
    llm = FakeLLM(
        [Reply.call("trade_paper_execute", **TRADE), Reply.say("done")],
        indexed=True,
        strict_tools=False,
    )
    agent = create_react_agent(KeelgateChatModel(llm=llm), governed_langchain_tools(ts))
    # The agent's tool list has no trade tool, so LangGraph reports the unknown tool itself...
    result = run(agent.ainvoke({"messages": [("user", "go")]}))
    assert result["messages"][-1].content == "done"
    # ...and in any case nothing was executed.
    assert h.executed == []
