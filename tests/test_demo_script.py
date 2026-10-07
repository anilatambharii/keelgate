"""The README demo script must keep working, because a recording of it is what people will see."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "demo" / "demo.sh"


def test_every_command_in_the_demo_names_something_that_exists() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for path in re.findall(r"python (examples/[\w/]+\.py)", text):
        assert (ROOT / path).exists(), path
    assert "keelgate eval run" in text and "keelgate quickstart" in text


def test_the_tape_starts_the_demo_script() -> None:
    tape = (ROOT / "docs" / "demo" / "demo.tape").read_text(encoding="utf-8")
    assert "bash docs/demo/demo.sh" in tape and "Output docs/demo/keelgate-demo.gif" in tape


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX bash on PATH; CI runs it")
def test_the_demo_runs_to_the_end_with_no_pauses() -> None:
    scripts = str(Path(sys.executable).parent)  # where the venv's `keelgate` lives
    env = {**os.environ, "PACE": "0", "PATH": scripts + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(  # noqa: S603 - fixed argv, our own script
        ["bash", str(SCRIPT)],  # noqa: S607 - bash from PATH is the point
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "36/36 blocked" in out and "verify_chain -> FAILED" in out
    assert "the saved plan was NOT re-planned" in out and "orders placed: ['order-1']" in out
