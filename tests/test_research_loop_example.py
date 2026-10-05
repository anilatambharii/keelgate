"""The phase's acceptance artefact must keep working: run examples/research_loop.py for real."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_research_loop_stops_on_budget_resumes_once_and_serves_over_mcp() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, our own script
        [sys.executable, str(ROOT / "examples" / "research_loop.py")],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    out = result.stdout
    assert result.returncode == 0, result.stderr + out
    assert "stopped: token_budget" in out and "orders placed so far: 0" in out  # budget stop
    assert "finished: True" in out and "orders placed: ['order-1']" in out  # resumed, one WRITE
    assert "the saved plan was NOT re-planned" in out
    assert "audit chain across both processes: OK" in out
    assert "tools served over MCP: market_quote, paper_order" in out  # served over MCP
    assert "paper_order TSLA (restricted)    -> policy_denied  (is_error=True)" in out
    assert "untrusted_tool_output" in out
