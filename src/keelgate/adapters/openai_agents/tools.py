"""OpenAI Agents SDK function tools backed by the governed toolset."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agents import FunctionTool

if TYPE_CHECKING:
    from agents.tool_context import ToolContext

    from keelgate.adapters.governed import GovernedToolset
    from keelgate.tools.spec import Tool

_BAD_JSON = json.dumps(
    {
        "status": "ERROR",
        "error": {
            "code": "invalid_arguments",
            "message": "The arguments must be a JSON object.",
            "retryable": True,
            "hint": "Send a JSON object matching the tool's input schema.",
        },
    }
)


def governed_function_tools(toolset: GovernedToolset) -> list[FunctionTool]:
    """One ``FunctionTool`` per governed tool. Each invocation goes through the gateway.

    The SDK's own approval and guardrail features are left alone and are not relied on:
    authorisation is decided by the gateway, whatever the SDK is configured to do.
    """
    return [_wrap(toolset, tool) for tool in toolset.tools()]


def _wrap(toolset: GovernedToolset, tool: Tool) -> FunctionTool:
    name = tool.spec.name

    async def on_invoke(_ctx: ToolContext[Any], input_json: str) -> str:
        try:
            arguments = json.loads(input_json or "{}")
        except ValueError:
            return _BAD_JSON
        if not isinstance(arguments, dict):
            return _BAD_JSON
        return toolset.render(await toolset.invoke(name, arguments))

    return FunctionTool(
        name=name,
        description=tool.spec.description or name,
        params_json_schema=tool.spec.input_schema or tool.spec.input_model.model_json_schema(),
        on_invoke_tool=on_invoke,
        # Our schemas are arbitrary Pydantic or external JSON schemas, not the strict subset
        # the SDK can enforce, and the gateway validates arguments itself.
        strict_json_schema=False,
    )
