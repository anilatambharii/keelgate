"""The K2 example's trace, in a real Jaeger.

Needs ``make up`` (Jaeger on :16686 for queries, :4318 for OTLP/HTTP). A missing Jaeger SKIPS
locally and FAILS in CI (KEELGATE_REQUIRE_INTEGRATION), so this can never silently pass.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import skip_or_fail

ROOT = Path(__file__).resolve().parents[1]
JAEGER = "http://localhost:16686"
pytestmark = pytest.mark.integration


def jaeger_up(wait: float = 30.0) -> bool:
    """Jaeger reports "running" before its query API answers, so give it a moment."""
    deadline = time.time() + wait
    while True:
        try:
            if httpx.get(f"{JAEGER}/api/services", timeout=3).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(1)


def fetch_trace(trace_id: str, wait: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + wait
    while time.time() < deadline:
        response = httpx.get(f"{JAEGER}/api/traces/{trace_id}", timeout=5)
        if response.status_code == 200 and response.json().get("data"):
            trace: dict[str, Any] = response.json()["data"][0]
            return trace
        time.sleep(0.5)
    raise AssertionError(f"Jaeger never returned trace {trace_id}")


def tags(span: dict[str, Any]) -> dict[str, Any]:
    return {t["key"]: t["value"] for t in span["tags"]}


def test_the_research_loop_trace_is_one_trace_in_jaeger_with_policy_spans() -> None:
    if not jaeger_up():
        skip_or_fail("Jaeger is not reachable on localhost:16686 (run `make up`)")
    result = subprocess.run(  # noqa: S603 - fixed argv, our own example
        [sys.executable, str(ROOT / "examples" / "research_loop.py"), "--trace"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    match = re.search(r"trace id: ([0-9a-f]{32})", result.stdout)
    assert match, result.stdout
    trace = fetch_trace(match.group(1))
    spans = trace["spans"]
    names = [s["operationName"] for s in spans]

    # one service, one trace, one root: the restart did not start a second trace
    assert {p["serviceName"] for p in trace["processes"].values()} == {"keelgate-research-loop"}
    roots = [s for s in spans if not s.get("references")]
    assert [r["operationName"] for r in roots] == ["invoke_agent research-agent"]
    runs = [s for s in spans if s["operationName"].startswith("invoke_agent")]
    assert sorted(tags(s)["keelgate.loop.resumed"] for s in runs) == [False, True]

    # GenAI spans with usage and cost
    chats = [s for s in spans if s["operationName"] == "chat scripted-model"]
    assert len(chats) == 3
    first = tags(chats[0])
    assert first["gen_ai.operation.name"] == "chat"
    assert first["gen_ai.usage.input_tokens"] == 100 and first["gen_ai.usage.output_tokens"] == 400
    assert first["keelgate.cost.known"] is True and first["keelgate.cost.usd"] == pytest.approx(
        0.0063
    )

    # tool spans, and a policy-decision span for the WRITE (reads skip policy)
    assert names.count("execute_tool market_quote") == 1
    assert names.count("execute_tool paper_order") == 1
    [decision] = [s for s in spans if s["operationName"] == "keelgate.policy.decide"]
    d = tags(decision)
    assert d["keelgate.policy.effect"] == "ALLOW" and d["gen_ai.tool.name"] == "paper_order"
    assert str(d["keelgate.policy.version"]).startswith("sha256:")
    parent = next(r for r in decision["references"] if r["refType"] == "CHILD_OF")["spanID"]
    assert (
        next(s for s in spans if s["spanID"] == parent)["operationName"]
        == "execute_tool paper_order"
    )

    # nothing sensitive leaked into any tag
    blob = " ".join(str(tags(s)) for s in spans)
    for leaked in ("AAPL", "order-1", "187.25", "5,000"):
        assert leaked not in blob, leaked
