"""LangChain/LangGraph tools backed by the governed toolset."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from keelgate.adapters._governed import GovernedToolset
    from keelgate.tools._spec import Tool


def governed_langchain_tools(toolset: GovernedToolset) -> list[StructuredTool]:
    """One LangChain tool per governed tool. Each call goes through the gateway.

    The coroutine never raises for a refusal: a denial, a parked approval or a validation
    error comes back as the rendered result text, so the agent can read it and adapt, and the
    framework's own error handling never sees (or retries) a policy decision.
    """
    return [_wrap(toolset, tool) for tool in toolset.tools()]


def _wrap(toolset: GovernedToolset, tool: Tool) -> StructuredTool:
    name = tool.spec.name

    async def run(**arguments: Any) -> str:
        outcome = await toolset.invoke(name, arguments)
        return toolset.render(outcome)

    schema: Any = (
        tool.spec.input_schema if tool.spec.input_schema is not None else tool.spec.input_model
    )
    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description=tool.spec.description or name,
        args_schema=schema,
    )
