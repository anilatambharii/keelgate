"""A loop worker for real-process kill tests.

    python -m tests.loop_worker <root> crash:<failpoint>:<n>   die hard at that window
    python -m tests.loop_worker <root> tool-crash              die hard inside the tool body
    python -m tests.loop_worker <root> resume                  run, or resume, to the end

``os._exit`` skips ``finally`` blocks, ``atexit`` and buffered writes, which is what a killed
process does. Whatever survives is exactly what was already durable on disk.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from keelgate.capabilities import GrantSigner, issue_grant
from keelgate.policy import RegoEngine
from tests.conftest import MARKET_OPEN, Clock
from tests.loop_support import (
    AGENT,
    ALL_CAPS,
    TENANT,
    build_rig,
    fake,
    three_step_script,
)

GOAL = "Research AAPL and buy a small position."
RUN_ID = "r1"


def load_identity(root: Path, clock: Clock) -> tuple[GrantSigner, str]:
    """The signing key and grant must outlive every process, or a restart could not resume."""
    pem_path, token_path = root / "signer.pem", root / "token.txt"
    if pem_path.exists():
        return GrantSigner(pem_path.read_bytes(), key_id="k1"), token_path.read_text()
    signer = GrantSigner.generate(key_id="k1")
    pem_path.write_bytes(signer._private_key_pem)
    from datetime import timedelta

    token = issue_grant(
        signer,
        agent_id=AGENT,
        tenant_id=TENANT,
        capabilities=ALL_CAPS,
        max_cost=1000.0,
        ttl=timedelta(hours=12),
        clock=clock,
    ).token
    token_path.write_text(token)
    return signer, token


def die_at(point: str, occurrence: int):  # type: ignore[no-untyped-def]
    counts: dict[str, int] = {}

    def hook(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1
        if name == point and counts[name] == occurrence:
            os._exit(137)

    return hook


def main(argv: list[str]) -> int:
    root, mode = Path(argv[1]), argv[2]
    root.mkdir(parents=True, exist_ok=True)
    clock = Clock(MARKET_OPEN)
    signer, token = load_identity(root, clock)
    rig = build_rig(root, signer=signer, grant_token=token, engine=RegoEngine(), clock=clock)

    failpoint = None
    if mode.startswith("crash:"):
        _, point, occurrence = mode.split(":")
        failpoint = die_at(point, int(occurrence))
    elif mode == "tool-crash":
        rig.crash_in_tool = True
        rig.tool_crash_exit = True

    import asyncio

    loop = rig.loop(fake(three_step_script()), failpoint=failpoint)
    result = asyncio.run(
        loop.run_or_resume(
            goal=GOAL, tenant_id=TENANT, agent_id=AGENT, as_of=MARKET_OPEN, run_id=RUN_ID
        )
    )
    print(  # noqa: T201 - this is a command-line entry point
        json.dumps(
            {
                "stop_reason": result.stop_reason.value if result.stop_reason else None,
                "final_answer": result.final_answer,
                "iteration": result.state.iteration,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
