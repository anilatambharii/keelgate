"""OpenAI Agents SDK adapter, with a real ``Runner.run`` driven by FakeLLM."""

from __future__ import annotations

import json
from typing import Any

import pytest

# The SDK cannot even be imported on a Python 3.11.0 interpreter (a typing bug fixed in 3.11.1),
# and that is an environment problem, not a Keelgate one. Skip loudly, never silently pass.
try:
    import agents
    from agents import Agent, Runner
except Exception as exc:
    pytest.skip(
        f"the OpenAI Agents SDK is not importable here: {type(exc).__name__}",
        allow_module_level=True,
    )

from keelgate.adapters.governed import UNTRUSTED_KEY, GovernedToolset
from keelgate.adapters.openai_agents import (
    KeelgateModel,
    governed_function_tools,
    to_keelgate_messages,
)
from keelgate.llm import Role
from keelgate.testing import FakeLLM, Reply
from keelgate.tools import SideEffect
from tests.conftest import Harness, run

TRADE = {"symbol": "AAPL", "notional": 5000, "client_order_id": "oa-1"}

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _no_tracing() -> Any:
    agents.set_tracing_disabled(True)
    yield


def toolset(h: Harness, **kw: Any) -> GovernedToolset:
    return GovernedToolset(
        gateway=h.gateway,
        registry=h.registry,
        grant_token=h.grant(),
        context_factory=h.ctx,
        **kw,
    )


async def invoke(tool: Any, payload: Any) -> dict[str, Any]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    result = await tool.on_invoke_tool(None, raw)
    return json.loads(result)  # type: ignore[no-any-return]


def named(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


# ----------------------------------------------------------------------- tools


def test_each_governed_tool_becomes_a_function_tool_with_its_schema(h: Harness) -> None:
    tools = governed_function_tools(toolset(h))
    assert {t.name for t in tools} == set(h.registry.names())
    quote = named(tools, "market_quote")
    assert quote.params_json_schema["properties"].keys() == {"symbol"}
    assert quote.strict_json_schema is False


def test_a_function_tool_call_runs_through_the_gateway(h: Harness) -> None:
    tools = governed_function_tools(toolset(h))
    body = run(invoke(named(tools, "market_quote"), {"symbol": "AAPL"}))
    assert body["status"] == "OK" and body[UNTRUSTED_KEY]["price"] == 101.5
    ok = run(invoke(named(tools, "trade_paper_execute"), TRADE))
    assert ok["status"] == "OK" and len(h.executed) == 1


def test_denials_and_approvals_are_results_not_exceptions(h: Harness) -> None:
    trade = named(governed_function_tools(toolset(h)), "trade_paper_execute")
    denied = run(invoke(trade, {**TRADE, "symbol": "TSLA"}))
    gated = run(invoke(trade, {**TRADE, "notional": 30_000, "client_order_id": "oa-big"}))
    assert denied["error"]["code"] == "policy_denied" and gated["status"] == "APPROVAL_REQUIRED"
    assert h.executed == []


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '"a string"', "null"])
def test_malformed_tool_input_is_reported_not_crashed_on(h: Harness, bad: str) -> None:
    body = run(invoke(named(governed_function_tools(toolset(h)), "market_quote"), bad))
    assert body["status"] == "ERROR" and body["error"]["code"] == "invalid_arguments"
    assert h.executed == []


def test_a_read_only_toolset_offers_no_write_tools(h: Harness) -> None:
    tools = governed_function_tools(toolset(h, allowed_side_effects=frozenset({SideEffect.READ})))
    assert [t.name for t in tools] == ["market_quote"]


# ----------------------------------------------------------------------- model


def test_input_items_convert_with_tool_calls_and_outputs() -> None:
    converted = to_keelgate_messages(
        "be careful",
        [
            {"role": "user", "content": "buy"},
            {"type": "function_call", "name": "t", "arguments": '{"a": 1}', "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
            {"role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
        ],
    )
    assert [m.role for m in converted] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assert converted[2].tool_calls[0].arguments == {"a": 1} and converted[3].tool_call_id == "c1"
    assert to_keelgate_messages(None, "just text")[0].content == "just text"
    assert (
        to_keelgate_messages(
            None, [{"type": "function_call", "name": "t", "arguments": "{oops", "call_id": "c"}]
        )[0]
        .tool_calls[0]
        .arguments
        == {}
    )


def test_the_model_refuses_streaming_clearly() -> None:
    model = KeelgateModel(FakeLLM([Reply.say("x")]))

    async def go() -> None:
        async for _ in model.stream_response(None, "hi", None, [], None, [], None):
            pass

    with pytest.raises(NotImplementedError, match="does not stream"):
        run(go())


# ------------------------------------------------------------------ a real agent


def run_agent(h: Harness, script: list[Reply], **ts: Any) -> Any:
    llm = FakeLLM(script, indexed=True)
    agent = Agent(
        name="trader",
        instructions="You trade carefully.",
        tools=governed_function_tools(toolset(h, **ts)),
        model=KeelgateModel(llm),
    )
    return run(Runner.run(agent, "Research AAPL and trade.")), llm


def test_a_real_agents_sdk_run_reads_is_denied_and_finishes(h: Harness) -> None:
    result, llm = run_agent(
        h,
        [
            Reply.call("market_quote", symbol="AAPL"),
            Reply.call(
                "trade_paper_execute", **{**TRADE, "symbol": "TSLA", "client_order_id": "oa-deny"}
            ),
            Reply.say("I could not trade TSLA."),
        ],
    )
    assert result.final_output == "I could not trade TSLA."
    outputs = [json.loads(i.output) for i in result.new_items if i.type == "tool_call_output_item"]
    assert [o["status"] for o in outputs] == ["OK", "DENIED"]
    assert h.executed == [] and llm.calls_made == 3
    assert h.audit.verify_chain("tenant-1").ok


def test_a_real_agents_sdk_run_places_an_allowed_order_exactly_once(h: Harness) -> None:
    result, _ = run_agent(
        h,
        [
            Reply.call("trade_paper_execute", **TRADE),
            Reply.call("trade_paper_execute", **TRADE),  # a retry: idempotency absorbs it
            Reply.say("Placed."),
        ],
    )
    assert result.final_output == "Placed."
    assert len(h.executed) == 1


def test_a_real_agents_sdk_run_is_held_to_the_toolsets_side_effect_limit(h: Harness) -> None:
    llm = FakeLLM([Reply.call("market_quote", symbol="AAPL"), Reply.say("done")], indexed=True)
    agent = Agent(
        name="reader",
        tools=governed_function_tools(
            toolset(h, allowed_side_effects=frozenset({SideEffect.READ}))
        ),
        model=KeelgateModel(llm),
    )
    assert run(Runner.run(agent, "go")).final_output == "done"
    assert {t.name for t in llm.requests[0].tools} == {
        "market_quote"
    }  # the model was never offered a write
