"""A scripted, deterministic ``LLMClient`` for tests.

Downstream projects get the same double Keelgate tests itself with, so CI never needs an
API key or a network call.

Two ways to script it:

* **Sequential** (default): replies come back in order, one per call. Simple, and right
  for a single process.
* **Indexed** (``indexed=True``): the reply is ``script[request.metadata["call_index"]]``.
  The reply is then a pure function of the request, so a process that was killed and
  restarted continues the script at the right place with no shared state. This is what
  resume tests use.

It fails loudly on a mistake: running past the script, or scripting a tool call the code
under test never offered, raises instead of quietly passing.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from keelgate.llm._types import (
    FinishReason,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    Usage,
)

if TYPE_CHECKING:
    from keelgate.llm._pricing import PricingTable


class ScriptExhaustedError(AssertionError):
    """The code under test made more LLM calls than the script has replies for."""


class UnofferedToolError(AssertionError):
    """The script asked for a tool that the request did not offer."""


@dataclass(frozen=True)
class Reply:
    """One scripted model turn: text, tool calls, or both."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    finish_reason: FinishReason | None = None

    @classmethod
    def say(cls, text: str, *, usage: Usage | None = None) -> Reply:
        return cls(text=text, usage=usage)

    @classmethod
    def call(
        cls, name: str, /, *, call_id: str = "", usage: Usage | None = None, **args: Any
    ) -> Reply:
        return cls(tool_calls=(ToolCall(id=call_id, name=name, arguments=args),), usage=usage)

    @classmethod
    def calls(cls, *calls: tuple[str, dict[str, Any]], usage: Usage | None = None) -> Reply:
        return cls(
            tool_calls=tuple(ToolCall(id="", name=n, arguments=a) for n, a in calls), usage=usage
        )


ReplyFn = Callable[[LLMRequest], Reply]
ScriptItem = Reply | str | ReplyFn


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4)) if text else 0


class FakeLLM:
    """A scripted, deterministic ``LLMClient`` for tests.

    Replies come from a script, in order or indexed by call number (so a restarted process continues
    the script). Records every request it receives.
    """

    name = "fake"

    def __init__(
        self,
        script: Sequence[ScriptItem],
        *,
        indexed: bool = False,
        default: Reply | None = None,
        pricing: PricingTable | None = None,
        model: str = "fake-model",
        strict_tools: bool = True,
    ) -> None:
        self._script: list[Reply | ReplyFn] = [
            Reply(text=s) if isinstance(s, str) else s for s in script
        ]
        self._indexed = indexed
        self._default = default
        self._pricing = pricing
        self._model = model
        self._strict_tools = strict_tools
        self._cursor = 0
        # Nothing awaits inside the critical section, so a thread lock is enough and,
        # unlike asyncio.Lock, is never tied to one event loop.
        self._lock = threading.Lock()
        self.requests: list[LLMRequest] = []

    @property
    def calls_made(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> LLMRequest:
        return self.requests[-1]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self.requests.append(request)
            item = self._next(request)
        reply = item(request) if callable(item) else item
        return self._respond(request, reply)

    # ------------------------------------------------------------------ internals

    def _next(self, request: LLMRequest) -> Reply | ReplyFn:
        if self._indexed:
            index = request.metadata.get("call_index")
            if not isinstance(index, int):
                raise ScriptExhaustedError("indexed FakeLLM needs metadata['call_index']")
        else:
            index = self._cursor
            self._cursor += 1
        if 0 <= index < len(self._script):
            return self._script[index]
        if self._default is not None:
            return self._default
        raise ScriptExhaustedError(
            f"call #{index + 1} but the script has only {len(self._script)} repl"
            f"{'y' if len(self._script) == 1 else 'ies'}"
        )

    def _respond(self, request: LLMRequest, reply: Reply) -> LLMResponse:
        calls = tuple(
            c if c.id else c.model_copy(update={"id": f"fake-{len(self.requests)}-{i}"})
            for i, c in enumerate(reply.tool_calls)
        )
        if self._strict_tools and calls:
            offered = {t.name for t in request.tools}
            unoffered = [c.name for c in calls if c.name not in offered]
            if unoffered:
                raise UnofferedToolError(
                    f"script asked for {unoffered} but the request offered {sorted(offered)}"
                )
        usage = reply.usage or self._estimate_usage(request, reply, calls)
        return LLMResponse(
            message=Message(role=Role.ASSISTANT, content=reply.text, tool_calls=calls),
            usage=usage,
            finish_reason=reply.finish_reason
            or (FinishReason.TOOL_CALLS if calls else FinishReason.STOP),
            model=self._model,
            response_id=f"fake-resp-{len(self.requests)}",
        )

    def _estimate_usage(
        self, request: LLMRequest, reply: Reply, calls: tuple[ToolCall, ...]
    ) -> Usage:
        input_tokens = sum(_tokens(m.content) for m in request.messages)
        output_tokens = _tokens(reply.text) + sum(
            _tokens(json.dumps(c.arguments, sort_keys=True)) + _tokens(c.name) for c in calls
        )
        if self._pricing is not None:
            return self._pricing.usage(self._model, input_tokens, output_tokens)
        return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=None)
