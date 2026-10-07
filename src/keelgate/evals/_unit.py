"""Unit evals: does the model choose the right tool, with arguments that validate?

Each case is a task and the tool the model should pick for it. The model is offered the real tool
schemas of the eval stack; the reply is checked three ways:

* **tool choice**: it picked ``expect_tool`` (or no tool at all, when ``expect_tool`` is None);
* **argument validity**: the arguments validate against that tool's input schema;
* **argument match**: the fields the case cares about carry the expected values.

In scripted mode the reply comes from the case, so this suite guards the *contract* (rename a
field or tighten a schema and the scripted call stops validating). In live mode a real model
answers, and the same checks measure how often it gets it right.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from keelgate.evals._stack import EvalStack
from keelgate.evals._types import CaseResult, MetricResult, SuiteResult
from keelgate.llm import LLMRequest, Message, Role, ToolSchema
from keelgate.testing import Reply

if TYPE_CHECKING:
    from keelgate.evals._context import EvalContext

SYSTEM = (
    "You are a careful trading assistant. Use a tool only when the task needs one, and never "
    "place an order unless the user explicitly asks for it."
)


@dataclass(frozen=True)
class UnitCase:
    case_id: str
    prompt: str
    expect_tool: str | None
    expect_args: dict[str, Any] = field(default_factory=dict)
    scripted: Reply = field(default_factory=lambda: Reply.say("ok"))


CASES: tuple[UnitCase, ...] = (
    UnitCase(
        "U-01",
        "What is the latest price of AAPL?",
        "market_quote",
        {"symbol": "AAPL"},
        Reply.call("market_quote", symbol="AAPL"),
    ),
    UnitCase(
        "U-02",
        "Any news on interest rates?",
        "get_news",
        {},
        Reply.call("get_news", topic="interest rates"),
    ),
    UnitCase(
        "U-03",
        "Open document q3-report and tell me what it says.",
        "read_document",
        {"doc_id": "q3-report"},
        Reply.call("read_document", doc_id="q3-report"),
    ),
    UnitCase(
        "U-04",
        "Buy 5000 dollars of MSFT. Use client order id abc-1.",
        "paper_order",
        {"symbol": "MSFT", "notional": 5000, "client_order_id": "abc-1"},
        Reply.call("paper_order", symbol="MSFT", notional=5000, client_order_id="abc-1"),
    ),
    UnitCase(
        "U-05",
        "Do not trade anything. Just tell me the price of NVDA.",
        "market_quote",
        {"symbol": "NVDA"},
        Reply.call("market_quote", symbol="NVDA"),
    ),
    UnitCase("U-06", "Say hello.", None, {}, Reply.say("Hello!")),
    UnitCase(
        "U-07",
        "How is the market feeling about energy stocks today?",
        "get_news",
        {},
        Reply.call("get_news", topic="energy"),
    ),
)


def _schemas(stack: EvalStack) -> tuple[ToolSchema, ...]:
    return tuple(
        ToolSchema(name=d["name"], description=d["description"], input_schema=d["input_schema"])
        for d in stack.registry.describe()
    )


async def _check(ctx: EvalContext, stack: EvalStack, case: UnitCase) -> CaseResult:
    started = time.perf_counter()
    llm = ctx.llm([case.scripted])
    response = await llm.complete(
        LLMRequest(
            model=ctx.model,
            messages=(
                Message(role=Role.SYSTEM, content=SYSTEM),
                Message(role=Role.USER, content=case.prompt),
            ),
            tools=_schemas(stack),
            max_tokens=300,
            metadata={"call_index": 0},
        )
    )
    calls = response.tool_calls
    chosen = calls[0].name if calls else None
    choice_ok = chosen == case.expect_tool
    valid = True
    match = True
    detail = ""
    if calls:
        tool = stack.registry.get(chosen or "")
        if tool is None:
            valid = match = False
            detail = "the model named a tool that does not exist"
        else:
            try:
                tool.spec.input_model.model_validate(calls[0].arguments)
            except ValidationError:
                valid = False
                detail = "the arguments do not validate against the tool schema"
            match = all(calls[0].arguments.get(k) == v for k, v in case.expect_args.items())
    if not choice_ok and not detail:
        detail = f"expected {case.expect_tool or 'no tool'}, got {chosen or 'no tool'}"
    elif choice_ok and calls and not match and not detail:
        detail = "the arguments validate but do not carry the expected values"
    return CaseResult(
        suite="unit",
        case_id=case.case_id,
        title=case.prompt,
        passed=choice_ok and valid and match,
        detail=detail or "tool choice and arguments are correct",
        evidence={
            "expected_tool": case.expect_tool,
            "chosen_tool": chosen,
            "tool_choice_ok": choice_ok,
            "arguments_valid": valid,
            "arguments_match": match,
        },
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def run_unit(ctx: EvalContext, cases: tuple[UnitCase, ...] = CASES) -> SuiteResult:
    suite = SuiteResult("unit", "Tool choice and argument validity")
    stack = EvalStack()
    for case in cases:
        suite.cases.append(await _check(ctx, stack, case))
    evidence = [c.evidence for c in suite.cases]
    called = [e for e in evidence if e["chosen_tool"] is not None]
    n = len(evidence) or 1
    suite.metrics = [
        MetricResult(
            "tool_choice_accuracy", sum(e["tool_choice_ok"] for e in evidence) / n, len(evidence)
        ),
        MetricResult(
            "argument_validity_rate",
            sum(e["arguments_valid"] for e in called) / len(called) if called else 1.0,
            len(called),
        ),
        MetricResult(
            "argument_match_rate",
            sum(e["arguments_match"] for e in called) / len(called) if called else 1.0,
            len(called),
        ),
    ]
    return suite
