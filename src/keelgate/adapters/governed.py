"""``GovernedToolset``: the one path from any agent framework to a tool body.

Every adapter (LangGraph, the OpenAI Agents SDK, the Claude Agent SDK, MCP, A2A) is a
translation layer over this class. None of them runs a tool, checks a grant, evaluates policy
or writes an audit record itself. They ask the toolset, which asks the gateway, so the
guarantees do not depend on which framework the agent happens to use:

    framework call  ->  GovernedToolset.invoke  ->  ToolGateway
                         (grant -> policy -> approval -> audit -> run)

Two trust rules hold in every adapter:

* the per-call context (tenant, policy facts, ``as_of``) comes from ``context_factory``, which
  is harness code. The framework and the model never supply it;
* a tool's output is untrusted data. :meth:`GovernedToolset.render` labels it as such in the
  text it hands back, because that text goes straight into a model's context.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from keelgate.llm.types import ToolSchema
from keelgate.tools.outcomes import OutcomeStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from keelgate.tools.gateway import CallContext, ToolGateway
    from keelgate.tools.outcomes import ToolOutcome
    from keelgate.tools.spec import SideEffect, Tool, ToolRegistry

UNTRUSTED_KEY: Final = "untrusted_tool_output"


@dataclass
class GovernedToolset:
    """A registry of tools bound to a gateway, a grant and a trusted context."""

    gateway: ToolGateway
    registry: ToolRegistry
    grant_token: str | Callable[[], str]
    context_factory: Callable[[], CallContext]
    # Optional extra restriction, enforced by the gateway on every call, not merely by
    # hiding tools.
    allowed_side_effects: frozenset[SideEffect] | None = None

    def tools(self) -> list[Tool]:
        """The tools offered to the agent, in a stable order."""
        found: list[Tool] = []
        for name in self.registry.names():
            tool = self.registry.get(name)
            if tool is None:
                continue
            if (
                self.allowed_side_effects is not None
                and tool.spec.side_effect not in self.allowed_side_effects
            ):
                continue
            found.append(tool)
        return found

    def schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name=t.spec.name,
                description=t.spec.description,
                input_schema=t.spec.input_schema or t.spec.input_model.model_json_schema(),
            )
            for t in self.tools()
        ]

    def _token(self) -> str:
        return self.grant_token() if callable(self.grant_token) else self.grant_token

    async def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Run one proposed call through the gateway. Never raises for a refusal."""
        context = self.context_factory()
        if self.allowed_side_effects is not None:
            context = dataclasses.replace(context, allowed_side_effects=self.allowed_side_effects)
        return await self.gateway.call(
            tool_name=name,
            arguments=arguments,
            grant_token=self._token(),
            context=context,
        )

    def render(self, outcome: ToolOutcome) -> str:
        """The result as JSON text for a framework that only accepts strings.

        The control-plane view (status, error code, hint, approval id) is plain. The tool's own
        output sits under :data:`UNTRUSTED_KEY`, so a model reading it sees a label saying it is
        data from outside. That label is a mitigation, not a control: the gateway already
        decided what could run.
        """
        body: dict[str, Any] = dict(outcome.for_model())
        if outcome.status is OutcomeStatus.OK and outcome.output is not None:
            # The single place adapters unwrap tool output, to place it behind the label.
            payload = outcome.output.unwrap_untrusted()
            body[UNTRUSTED_KEY] = json.loads(payload.model_dump_json())
        return json.dumps(body, sort_keys=True)

    @staticmethod
    def is_error(outcome: ToolOutcome) -> bool:
        """Whether a framework should flag the result as an error (anything but OK)."""
        return outcome.status is not OutcomeStatus.OK
