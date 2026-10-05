"""Tool declaration: the ``@tool`` decorator, ``ToolSpec`` and ``ToolRegistry``.

A decorated function is wrapped in a :class:`Tool` that **refuses to be called
directly**. The only code path that runs the underlying function is the
:class:`~keelgate.tools.gateway.ToolGateway`, after grant, policy and (when
required) approval checks. That makes "a WRITE tool ran without a decision" a
bug you have to go out of your way to write, not a mistake you can make by
importing a function.
"""

from __future__ import annotations

import inspect
import math
import re
import threading
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel

from keelgate.capabilities import Capability

_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")

IdempotencyKeyFn = Callable[[Any], str]
ResourceFn = Callable[[Any], Mapping[str, Any]]


class SideEffect(StrEnum):
    """What a tool can do to the world. This is what the policy gate keys off."""

    READ = "READ"
    PROPOSE = "PROPOSE"
    WRITE = "WRITE"


class ToolDefinitionError(ValueError):
    """The tool is declared unsafely or inconsistently. Raised at import time."""


class ToolRefusedError(Exception):
    """Raised by a tool body to say: I did not do anything, so a retry is safe.

    Any other exception from a WRITE tool is treated as possibly-partial, and the
    call is never retried automatically.
    """


class DirectInvocationError(RuntimeError):
    """A tool was called without going through the gateway."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    capability: Capability
    side_effect: SideEffect
    timeout_s: float
    cost_estimate: float
    idempotency_key: IdempotencyKeyFn | None
    resource: ResourceFn | None


class Tool:
    """A declared tool. Not callable: execute it through the gateway."""

    def __init__(self, spec: ToolSpec, fn: Callable[..., Any]) -> None:
        self.spec = spec
        self._fn = fn
        self.is_async = inspect.iscoroutinefunction(fn)

    @property
    def name(self) -> str:
        return self.spec.name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        raise DirectInvocationError(
            f"tool {self.spec.name!r} must be invoked through a ToolGateway; calling it "
            "directly would skip the capability, policy and approval checks"
        )

    def __repr__(self) -> str:
        return f"Tool({self.spec.name!r}, {self.spec.side_effect.value})"


def tool(
    *,
    capability: str,
    side_effect: SideEffect,
    name: str | None = None,
    description: str | None = None,
    timeout_s: float = 10.0,
    cost_estimate: float = 0.0,
    idempotency_key: IdempotencyKeyFn | None = None,
    resource: ResourceFn | None = None,
) -> Callable[[Callable[..., Any]], Tool]:
    """Declare a tool.

    The function takes exactly one argument, a Pydantic model, and returns a
    Pydantic model; both annotations are required and become the input and
    output schemas. ``resource`` maps the validated input to the facts the policy
    needs (``{"symbol": ..., "notional": ...}``) so policy packs stay generic.
    A WRITE tool must supply ``idempotency_key``.
    """

    def decorate(fn: Callable[..., Any]) -> Tool:
        tool_name = fn.__name__ if name is None else name
        if not _NAME_RE.fullmatch(tool_name):
            raise ToolDefinitionError(f"tool name {tool_name!r} must match {_NAME_RE.pattern}")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ToolDefinitionError("timeout_s must be a positive, finite number")
        if not math.isfinite(cost_estimate) or cost_estimate < 0:
            raise ToolDefinitionError("cost_estimate must be a non-negative, finite number")
        if side_effect is SideEffect.WRITE and idempotency_key is None:
            raise ToolDefinitionError(
                f"WRITE tool {tool_name!r} needs an idempotency_key so a retry cannot "
                "repeat the side effect"
            )
        input_model, output_model = _models(fn, tool_name)
        doc = (description or inspect.getdoc(fn) or "").strip()
        spec = ToolSpec(
            name=tool_name,
            description=doc.splitlines()[0] if doc else tool_name,
            input_model=input_model,
            output_model=output_model,
            capability=Capability(capability),
            side_effect=side_effect,
            timeout_s=timeout_s,
            cost_estimate=cost_estimate,
            idempotency_key=idempotency_key,
            resource=resource,
        )
        return Tool(spec, fn)

    return decorate


def _models(fn: Callable[..., Any], tool_name: str) -> tuple[type[BaseModel], type[BaseModel]]:
    params = list(inspect.signature(fn).parameters.values())
    if len(params) != 1:
        raise ToolDefinitionError(
            f"tool {tool_name!r} must take exactly one Pydantic model argument"
        )
    try:
        hints = typing.get_type_hints(fn)
    except (NameError, TypeError) as exc:
        raise ToolDefinitionError(
            f"tool {tool_name!r}: cannot resolve its type annotations ({exc}). Define the "
            "input and output models at module level so they can be found."
        ) from exc
    arg_type = hints.get(params[0].name)
    return_type = hints.get("return")
    if not (inspect.isclass(arg_type) and issubclass(arg_type, BaseModel)):
        raise ToolDefinitionError(
            f"tool {tool_name!r}: the argument must be annotated with a Pydantic model"
        )
    if not (inspect.isclass(return_type) and issubclass(return_type, BaseModel)):
        raise ToolDefinitionError(
            f"tool {tool_name!r}: the return must be annotated with a Pydantic model"
        )
    return arg_type, return_type


class RegistryFrozenError(RuntimeError):
    """A tool was registered after the registry was frozen."""


class ToolRegistry:
    """The set of tools an agent may be offered.

    A gateway freezes its registry on construction, so a tool cannot appear
    mid-run.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._frozen = False
        self._lock = threading.Lock()

    def register(self, tool_: Tool) -> Tool:
        if not isinstance(tool_, Tool):
            raise TypeError("register() takes a Tool produced by @tool")
        with self._lock:
            if self._frozen:
                raise RegistryFrozenError("registry is frozen; build a new one")
            if tool_.name in self._tools:
                raise ValueError(f"tool {tool_.name!r} is already registered")
            self._tools[tool_.name] = tool_
        return tool_

    def freeze(self) -> None:
        with self._lock:
            self._frozen = True

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        """Tool schemas to offer a model. Omits capabilities and policy details."""
        return [
            {
                "name": t.spec.name,
                "description": t.spec.description,
                "input_schema": t.spec.input_model.model_json_schema(),
            }
            for t in (self._tools[n] for n in self.names())
        ]
