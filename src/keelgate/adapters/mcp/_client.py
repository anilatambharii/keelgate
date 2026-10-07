"""Govern the external MCP tools an agent consumes (``pip install 'keelgate[mcp]'``).

An external MCP server is somebody else's code. Connecting to it must not hand its tools to the
agent. Instead:

* **Allowlist.** Only tools named in the mapping are registered. Everything else the server
  advertises is ignored and reported as *blocked*.
* **Capability mapping.** Each allowed tool is mapped to a Keelgate capability and side effect
  by *you*, not by the server. A tool the server calls "read-only" is whatever the mapping says.
* **Schema pinning (optional, recommended).** Pin the SHA-256 of a tool's name, description and
  input schema. If the server later changes any of them, the tool is not registered. This is the
  defence against a vetted tool being quietly altered (a "rug pull").
* **Local validation.** Arguments are checked against the remote's JSON schema before anything
  is sent.
* **Untrusted output.** A registered tool is an ordinary Keelgate tool, so its result comes back
  from the gateway wrapped as untrusted data.
* **Same gate as everything else.** Registered tools run through grant, policy, approval, audit,
  idempotency and timeouts. A WRITE mapping must supply an idempotency key.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from mcp import types
from pydantic import BaseModel, ConfigDict, model_validator

from keelgate.capabilities import Capability
from keelgate.tools._spec import SideEffect, Tool, ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from keelgate.tools._spec import ToolRegistry

MAX_OUTPUT_CHARS: Final = 64_000
_UNSAFE_NAME = re.compile(r"[^a-z0-9_.]")


class ExternalToolError(Exception):
    """The external server reported an error, or returned something unusable."""


class ExternalArgs(BaseModel):
    """Passthrough arguments for an external tool; the remote JSON schema is enforced separately."""

    model_config = ConfigDict(extra="allow")


class ExternalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    structured: dict[str, Any] | None = None


def schema_digest(tool: types.Tool) -> str:
    """The pin for a remote tool: a hash over its name, description and input schema."""
    canonical = json.dumps(
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ExternalToolMapping:
    """How one external tool is admitted: the capability and side effect *you* assign."""

    remote_name: str
    capability: str
    side_effect: SideEffect
    local_name: str | None = None
    schema_sha256: str | None = None
    idempotency_key: Callable[[Any], str] | None = None
    resource: Callable[[Any], Mapping[str, Any]] | None = None
    cost_estimate: float = 0.0
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        Capability(self.capability)  # raises on a malformed or wildcard capability
        if self.side_effect is SideEffect.WRITE and self.idempotency_key is None:
            raise ValueError(
                f"{self.remote_name}: a WRITE mapping needs an idempotency_key so a retry "
                "cannot repeat the side effect"
            )


@dataclass(frozen=True)
class Discovery:
    """What was found on the server, and what happened to each tool."""

    registered: tuple[str, ...] = ()  # local names now in the registry
    blocked: tuple[str, ...] = ()  # advertised by the server, not on the allowlist
    missing: tuple[str, ...] = ()  # on the allowlist, not advertised by the server
    mismatched: tuple[str, ...] = ()  # on the allowlist, but the pinned schema hash differs
    tools: tuple[Tool, ...] = field(default=(), repr=False)


class GovernedMCPClient:
    """Wraps an MCP client (anything with ``list_tools`` and ``call_tool``)."""

    def __init__(
        self, client: Any, *, server_name: str, allowlist: Sequence[ExternalToolMapping]
    ) -> None:
        names = [m.remote_name for m in allowlist]
        if len(names) != len(set(names)):
            raise ValueError("the allowlist names the same remote tool twice")
        self._client = client
        self._server = server_name
        self._allow = {m.remote_name: m for m in allowlist}

    def _local_name(self, mapping: ExternalToolMapping) -> str:
        raw = mapping.local_name or f"{self._server}.{mapping.remote_name}"
        name = _UNSAFE_NAME.sub("_", raw.lower())
        return name if name[:1].isalpha() else f"x{name}"

    async def discover(self) -> Discovery:
        listed = await self._client.list_tools()
        remote = {t.name: t for t in listed.tools}
        blocked = tuple(sorted(n for n in remote if n not in self._allow))
        missing = tuple(sorted(n for n in self._allow if n not in remote))

        tools: list[Tool] = []
        mismatched: list[str] = []
        for name, mapping in self._allow.items():
            tool = remote.get(name)
            if tool is None:
                continue
            if mapping.schema_sha256 is not None and schema_digest(tool) != mapping.schema_sha256:
                mismatched.append(name)
                continue
            tools.append(self._build(mapping, tool))
        return Discovery(
            registered=tuple(t.name for t in tools),
            blocked=blocked,
            missing=missing,
            mismatched=tuple(sorted(mismatched)),
            tools=tuple(tools),
        )

    async def register(self, registry: ToolRegistry) -> Discovery:
        """Discover, then register the vetted tools into ``registry``."""
        discovery = await self.discover()
        for tool in discovery.tools:
            registry.register(tool)
        return discovery

    # ---------------------------------------------------------------------- internals

    def _build(self, mapping: ExternalToolMapping, remote: types.Tool) -> Tool:
        schema = dict(remote.input_schema)
        local = self._local_name(mapping)

        class ValidatedArgs(ExternalArgs):
            """Passthrough arguments, checked against this tool's remote JSON schema."""

            @model_validator(mode="after")
            def _conform_to_remote_schema(self) -> ValidatedArgs:
                import jsonschema  # noqa: PLC0415 - a dependency of the mcp extra

                try:
                    jsonschema.validate(self.model_dump(mode="json"), schema)
                except jsonschema.ValidationError as exc:
                    raise ValueError(
                        f"arguments do not match the tool's schema: {exc.message[:200]}"
                    ) from exc
                return self

        ValidatedArgs.__name__ = f"ExternalArgs_{local.replace('.', '_')}"
        args_model = ValidatedArgs

        remote_name = mapping.remote_name
        client = self._client

        async def call(args: ExternalArgs) -> ExternalOutput:
            result = await client.call_tool(remote_name, args.model_dump(mode="json"))
            if result.is_error:
                raise ExternalToolError(f"{remote_name} reported an error")
            text = "\n".join(c.text for c in result.content if isinstance(c, types.TextContent))
            if any(not isinstance(c, types.TextContent) for c in result.content):
                text += "\n[non-text content omitted]"
            return ExternalOutput(
                text=text[:MAX_OUTPUT_CHARS], structured=result.structured_content
            )

        spec = ToolSpec(
            name=local,
            description=(remote.description or remote.name)[:300],
            input_model=args_model,
            output_model=ExternalOutput,
            capability=Capability(mapping.capability),
            side_effect=mapping.side_effect,
            timeout_s=mapping.timeout_s,
            cost_estimate=mapping.cost_estimate,
            idempotency_key=mapping.idempotency_key,
            resource=mapping.resource,
            input_schema=schema,
        )
        return Tool(spec, call)
