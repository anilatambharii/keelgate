"""How a suite obtains a model: scripted in CI, real in the nightly run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from keelgate.testing import FakeLLM, Reply

if TYPE_CHECKING:
    from collections.abc import Sequence

    from keelgate.llm import LLMClient, LLMRequest, LLMResponse


class RecordingLLM:
    """Wraps a real client and keeps what it was sent, so a case can inspect the prompt."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self.name = client.name
        self.requests: list[LLMRequest] = []

    @property
    def calls_made(self) -> int:
        return len(self.requests)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return await self._client.complete(request)


@dataclass
class EvalContext:
    """``fake`` mode replays each case's scripted model (deterministic, free, runs on every PR).

    ``live`` mode ignores the script and lets a real model respond to the same inputs. The
    assertions are about what the *harness* allowed, so they are identical in both modes; a real
    model that simply declines an attack counts as blocked by the model, and the evidence says so.
    """

    mode: str = "fake"
    live_client: LLMClient | None = None
    model: str = "eval-model"
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def llm(self, script: Sequence[Reply] = ()) -> Any:
        if self.is_live:
            if self.live_client is None:
                raise ValueError("live mode needs a client")
            return RecordingLLM(self.live_client)
        # strict_tools=False: a fooled model may call a tool it was never offered.
        return FakeLLM(list(script), indexed=True, strict_tools=False)
