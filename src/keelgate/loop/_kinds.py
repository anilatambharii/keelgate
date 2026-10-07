"""The three loop kinds.

* **Task**: the general loop (:class:`~keelgate.loop._engine.Loop`), free to propose any tool the
  grant, policy and approvals allow.
* **Verification**: checks a claim against evidence. READ-only **by construction**, enforced at
  the gateway as well as in the tools it is offered.
* **Monitor**: re-runs a bounded READ-only check on a schedule. It stays READ-only unless the
  caller passes ``allow_writes=True`` in so many words. The restriction is enforced at the
  gateway, so merely hiding a tool is not what keeps it safe.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from keelgate.loop._engine import Loop, LoopResult
from keelgate.loop._state import LoopType, StopReason
from keelgate.loop._stop import StopConditions
from keelgate.tools._spec import SideEffect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

READ_ONLY = frozenset({SideEffect.READ})

VERIFICATION_PROMPT = (
    "You verify claims. Use the read-only tools to gather evidence, then reply with a "
    "plain-text finding that says whether the claim is supported, and cite the evidence. "
    "You cannot change anything, and you must not try to."
)


class VerificationLoop(Loop):
    """A loop that can only read. Asking for more than READ is an error, not a warning."""

    def __init__(self, **kwargs: Any) -> None:
        allowed = kwargs.pop("allowed_side_effects", READ_ONLY)
        if allowed is None or not set(allowed) <= READ_ONLY:
            raise ValueError("a VerificationLoop is read-only: allowed_side_effects must be {READ}")
        kwargs.setdefault("system_prompt", VERIFICATION_PROMPT)
        kwargs.pop("loop_type", None)
        super().__init__(
            allowed_side_effects=frozenset(allowed), loop_type=LoopType.VERIFICATION, **kwargs
        )

    async def verify_claim(
        self,
        claim: str,
        *,
        tenant_id: str,
        agent_id: str,
        as_of: datetime,
        run_id: str | None = None,
    ) -> LoopResult:
        return await self.run(
            goal=f"Verify this claim: {claim}",
            tenant_id=tenant_id,
            agent_id=agent_id,
            as_of=as_of,
            run_id=run_id,
        )


@dataclass(frozen=True)
class Schedule:
    interval: timedelta

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")

    def next_after(self, moment: datetime) -> datetime:
        return moment + self.interval


@dataclass(frozen=True)
class MonitorSummary:
    ticks_run: int
    results: tuple[LoopResult, ...]


_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


class MonitorLoop:
    """Runs a bounded loop on a schedule, finishing a half-done tick after a restart.

    Each tick is its own run, named ``<monitor_id>.tick-<n>``, so ticks are checkpointed and
    resumable independently. The next tick number comes from what is already stored: a
    restarted monitor carries on rather than starting over.

    ``loop_factory`` is called as ``loop_factory(loop_type=..., allowed_side_effects=...)``
    and must pass both through to the :class:`Loop` it builds.
    """

    def __init__(
        self,
        *,
        monitor_id: str,
        goal: str,
        loop_factory: Callable[..., Loop],
        schedule: Schedule,
        tenant_id: str,
        agent_id: str,
        as_of_for: Callable[[datetime], datetime] | None = None,
        tick_stop: StopConditions | None = None,
        allow_writes: bool = False,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monitor_id = _UNSAFE_ID.sub("_", monitor_id)
        self._goal = goal
        self._schedule = schedule
        self._tenant = tenant_id
        self._agent = agent_id
        self._as_of_for = as_of_for or (lambda now: now)
        self._tick_stop = tick_stop or StopConditions(max_iterations=3)
        self._allow_writes = allow_writes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        allowed: frozenset[SideEffect] | None = None if allow_writes else READ_ONLY
        self._loop = loop_factory(loop_type=LoopType.MONITOR, allowed_side_effects=allowed)
        if not allow_writes and not self._loop_is_read_only():
            raise ValueError(
                "loop_factory must honour allowed_side_effects for a read-only monitor"
            )

    def _loop_is_read_only(self) -> bool:
        allowed = getattr(self._loop, "_allowed", None)
        return allowed is not None and set(allowed) <= READ_ONLY

    @property
    def read_only(self) -> bool:
        return not self._allow_writes

    def _run_id(self, tick: int) -> str:
        return f"{self._monitor_id}.tick-{tick}"

    def _next_tick(self) -> int:
        store = self._loop._checkpoints
        existing = store.runs(self._tenant, prefix=f"{self._monitor_id}.tick-")
        numbers = [int(r.rsplit("-", 1)[1]) for r in existing if r.rsplit("-", 1)[1].isdigit()]
        if not numbers:
            return 1
        latest = max(numbers)
        state = store.load(self._tenant, self._run_id(latest))
        unfinished = (
            state is not None
            and not state.finished
            and state.stop_reason not in (StopReason.MAX_ITERATIONS, StopReason.GOAL_REACHED)
        )
        # A tick that stopped part-way (a crash, say) is finished before the next begins.
        return latest if unfinished else latest + 1

    async def tick(self) -> LoopResult:
        """Run, or finish, exactly one tick."""
        number = self._next_tick()
        return await self._loop.run_or_resume(
            goal=self._goal,
            tenant_id=self._tenant,
            agent_id=self._agent,
            as_of=self._as_of_for(self._clock()),
            run_id=self._run_id(number),
            stop=self._tick_stop,
        )

    async def run_ticks(self, count: int) -> MonitorSummary:
        """Run ``count`` ticks, sleeping ``schedule.interval`` between them."""
        if count <= 0:
            raise ValueError("count must be positive")
        results: list[LoopResult] = []
        for n in range(count):
            if n:
                await self._sleep(self._schedule.interval.total_seconds())
            results.append(await self.tick())
        return MonitorSummary(ticks_run=len(results), results=tuple(results))
