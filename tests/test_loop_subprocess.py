"""Kill-and-resume with REAL process deaths.

The in-process tests in ``test_loop_resume.py`` raise an exception at a crash window. These
go further: a worker subprocess calls ``os._exit(137)`` at the window, so nothing is flushed
or cleaned up, and a *second* fresh process resumes from whatever the first left on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KILLED = 137
FINAL = "Bought 5000 of AAPL at the quoted price."


def worker(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, our own module
        [sys.executable, "-m", "tests.loop_worker", str(root), mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def executions(root: Path) -> list[dict[str, object]]:
    path = root / "executions.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line]
        if path.exists()
        else []
    )


def result_of(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert proc.returncode == 0, proc.stderr[-2000:]
    parsed: dict[str, object] = json.loads(proc.stdout.strip().splitlines()[-1])
    return parsed


@pytest.mark.parametrize(
    "window",
    ["after_plan:2", "after_gateway_call:2", "after_action_checkpoint:2", "after_verify:2"],
)
def test_a_killed_process_resumes_in_a_new_process_without_a_duplicate_write(
    tmp_path: Path, window: str
) -> None:
    root = tmp_path / "state"
    killed = worker(root, f"crash:{window}")
    assert killed.returncode == KILLED, killed.stderr[-2000:]

    resumed = result_of(worker(root, "resume"))
    assert resumed["stop_reason"] == "goal_reached" and resumed["final_answer"] == FINAL
    assert [e["client_order_id"] for e in executions(root)] == ["o-1"], f"killed at {window}"


def test_the_write_really_ran_in_the_dead_process_and_resume_replayed_it(tmp_path: Path) -> None:
    """Killed after the tool finished but before anything recorded it."""
    root = tmp_path / "state"
    assert worker(root, "crash:after_gateway_call:2").returncode == KILLED
    assert len(executions(root)) == 1  # the order genuinely went through before the kill

    resumed = result_of(worker(root, "resume"))
    assert resumed["stop_reason"] == "goal_reached"
    assert len(executions(root)) == 1  # and the new process did not place it again


def test_a_process_killed_inside_the_tool_body_is_never_retried_by_the_next_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    killed = worker(root, "tool-crash")
    assert killed.returncode == KILLED, killed.stderr[-2000:]
    assert len(executions(root)) == 1  # the body began: it may or may not have taken effect

    resumed = result_of(worker(root, "resume"))
    assert resumed["stop_reason"] == "outcome_unknown"  # waits for a human
    assert len(executions(root)) == 1  # not retried

    again = result_of(worker(root, "resume"))
    assert again["stop_reason"] == "outcome_unknown" and len(executions(root)) == 1


def test_several_kills_in_a_row_still_place_the_order_once(tmp_path: Path) -> None:
    root = tmp_path / "state"
    # Occurrence counters are per process, so each window below fires at the first opportunity
    # of the process that follows the previous kill.
    for window in ("after_plan:1", "after_gateway_call:1", "after_observe:1"):
        assert worker(root, f"crash:{window}").returncode == KILLED
    resumed = result_of(worker(root, "resume"))
    assert resumed["stop_reason"] == "goal_reached"
    assert len(executions(root)) == 1
