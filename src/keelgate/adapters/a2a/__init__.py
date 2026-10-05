"""Agent-to-Agent (A2A) adapter: an agent card and task intake under policy.

Needs ``keelgate[a2a]``. ``build_agent_card`` describes the agent; ``build_a2a_app`` serves it with
the JSON-RPC binding. Every inbound task passes the governed ``a2a_task_intake`` tool first (see
:mod:`keelgate.adapters.a2a.intake`), and remote text is always treated as untrusted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
    from starlette.applications import Starlette
except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
        "keelgate.adapters.a2a needs the A2A SDK: pip install 'keelgate[a2a]'"
    ) from exc

from keelgate.adapters.a2a.intake import (
    INTAKE_TOOL,
    GovernedA2AExecutor,
    IntakeIn,
    IntakeOut,
    TaskRequest,
    TaskResult,
    loop_task_runner,
    make_intake_tool,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from keelgate.adapters.a2a.intake import GrantResolver, TaskRunner
    from keelgate.adapters.governed import GovernedToolset

RPC_URL = "/a2a"


def build_agent_card(
    *,
    name: str,
    description: str,
    url: str,
    version: str = "1.0.0",
    skills: Sequence[tuple[str, str, str]] = (),
) -> AgentCard:
    """An agent card. ``skills`` are ``(id, name, description)`` triples.

    The card advertises a JSON-RPC interface at ``url`` and no streaming or push notifications,
    which this adapter does not implement.
    """
    return AgentCard(
        name=name,
        description=description,
        version=version,
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(id=sid, name=sname, description=sdesc, tags=["keelgate"])
            for sid, sname, sdesc in skills
        ],
    )


def build_a2a_app(
    toolset: GovernedToolset,
    runner: TaskRunner,
    card: AgentCard,
    *,
    grant_resolver: GrantResolver | None = None,
    rpc_url: str = RPC_URL,
) -> Starlette:
    """A Starlette app serving the agent card and the JSON-RPC task endpoint."""
    executor = GovernedA2AExecutor(toolset, runner, grant_resolver=grant_resolver)
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url=rpc_url),
    ]
    return Starlette(routes=routes)


__all__ = [
    "INTAKE_TOOL",
    "GovernedA2AExecutor",
    "IntakeIn",
    "IntakeOut",
    "TaskRequest",
    "TaskResult",
    "build_a2a_app",
    "build_agent_card",
    "loop_task_runner",
    "make_intake_tool",
]
