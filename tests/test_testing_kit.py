"""The testing kit downstream projects use: pytest plugin fixtures, harness and static policy."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from keelgate.policy import Decision
from keelgate.testing import (
    FakeLLM,
    ManualClock,
    Reply,
    StaticPolicyEngine,
    build_governed_harness,
)
from keelgate.tools import SideEffect, tool
from keelgate.tools.outcomes import OutcomeStatus
from tests.conftest import run


class EchoIn(BaseModel):
    text: str


class EchoOut(BaseModel):
    text: str


@tool(capability="echo:read", side_effect=SideEffect.READ)
def echo(args: EchoIn) -> EchoOut:
    """Echo."""
    return EchoOut(text=args.text)


@tool(capability="echo:write", side_effect=SideEffect.WRITE, idempotency_key=lambda a: a.text)
def echo_write(args: EchoIn) -> EchoOut:
    """Echo, as a write."""
    return EchoOut(text=args.text)


@tool(capability="other:write", side_effect=SideEffect.WRITE, idempotency_key=lambda a: a.text)
def other_write(args: EchoIn) -> EchoOut:
    """Another write."""
    return EchoOut(text=args.text)


def test_reads_are_not_policy_gated_but_still_need_a_grant() -> None:
    engine = StaticPolicyEngine()  # denies everything it is asked about
    harness = build_governed_harness([echo], engine=engine)
    assert run(harness.call("echo", {"text": "x"})).status is OutcomeStatus.OK
    assert engine.inputs == []
    no_cap = harness.grant(["something:else"])
    assert run(harness.call("echo", {"text": "x"}, token=no_cap)).status is not OutcomeStatus.OK


def test_the_pytest_entry_point_is_declared() -> None:
    from importlib.metadata import entry_points

    found = {e.name: e.value for e in entry_points(group="pytest11")}
    assert found.get("keelgate") == "keelgate.testing.plugin"


def test_a_downstream_project_gets_the_fixtures_with_no_setup(tmp_path: Path) -> None:
    """Run pytest in a bare directory: the entry point alone must provide the fixtures."""
    import subprocess
    import sys

    lines = [
        "from keelgate.testing import Reply",
        "def test_it(fake_llm, governed_harness, static_policy, keelgate_clock):",
        "    llm = fake_llm([Reply.say('ok')])",
        "    assert llm.name == 'fake'",
        "    assert governed_harness().clock is keelgate_clock",
    ]
    (tmp_path / "test_downstream.py").write_text(chr(10).join(lines), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_fixtures_are_available_to_any_test(
    fake_llm: Any, keelgate_clock: ManualClock, static_policy: Any, governed_harness: Any
) -> None:
    llm = fake_llm([Reply.say("hi")])
    assert isinstance(llm, FakeLLM)
    harness = governed_harness(tools=[echo], engine=static_policy({"echo": Decision.ALLOW}))
    assert harness.clock is keelgate_clock  # one clock, shared


def test_the_static_engine_denies_by_default() -> None:
    harness = build_governed_harness([echo_write])
    outcome = run(harness.call("echo_write", {"text": "x"}))
    assert outcome.status is OutcomeStatus.DENIED


def test_a_scripted_allow_runs_the_tool_and_the_inputs_are_recorded() -> None:
    engine = StaticPolicyEngine({"echo_write": Decision.ALLOW})
    harness = build_governed_harness([echo_write, other_write], engine=engine)
    ok = run(harness.call("echo_write", {"text": "x"}))
    still_denied = run(harness.call("other_write", {"text": "y"}))
    assert ok.status is OutcomeStatus.OK
    assert still_denied.status is OutcomeStatus.DENIED  # unlisted tool: default DENY
    assert [i.action.tool for i in engine.inputs] == ["echo_write", "other_write"]
    assert harness.audit.verify_chain("tenant-1").ok


def test_require_approval_can_be_scripted() -> None:
    engine = StaticPolicyEngine({"echo_write": Decision.REQUIRE_APPROVAL})
    harness = build_governed_harness([echo_write], engine=engine)
    outcome = run(harness.call("echo_write", {"text": "z"}))
    assert outcome.status is OutcomeStatus.APPROVAL_REQUIRED


def test_allow_all_is_explicit() -> None:
    harness = build_governed_harness([echo_write], engine=StaticPolicyEngine.allow_all())
    assert run(harness.call("echo_write", {"text": "q"})).status is OutcomeStatus.OK


def test_a_grant_lacking_the_capability_is_refused_even_under_allow_all() -> None:
    harness = build_governed_harness([echo_write], engine=StaticPolicyEngine.allow_all())
    token = harness.grant(["echo:read"])
    outcome = run(harness.call("echo_write", {"text": "q"}, token=token))
    assert outcome.status is not OutcomeStatus.OK


def test_the_default_grant_covers_every_registered_capability() -> None:
    harness = build_governed_harness([echo, echo_write], engine=StaticPolicyEngine.allow_all())
    assert run(harness.call("echo", {"text": "a"})).status is OutcomeStatus.OK
    assert run(harness.call("echo_write", {"text": "b"})).status is OutcomeStatus.OK


def test_the_clock_moves_only_when_told_and_expires_grants() -> None:
    harness = build_governed_harness([echo], engine=StaticPolicyEngine.allow_all())
    token = harness.grant(ttl=timedelta(minutes=5))
    assert run(harness.call("echo", {"text": "a"}, token=token)).status is OutcomeStatus.OK
    harness.clock.advance(timedelta(minutes=10))
    assert run(harness.call("echo", {"text": "a"}, token=token)).status is not OutcomeStatus.OK


def test_tenants_are_isolated_in_the_harness() -> None:
    harness = build_governed_harness([echo], engine=StaticPolicyEngine.allow_all())
    other_tenant_grant = harness.grant(tenant="tenant-2")
    outcome = run(harness.call("echo", {"text": "a"}, token=other_tenant_grant))
    assert outcome.status is not OutcomeStatus.OK  # grant for tenant-2 used in a tenant-1 call


def test_a_clock_must_be_timezone_aware() -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone"):
        ManualClock(datetime(2026, 1, 1))  # noqa: DTZ001 - the point of the test


def test_importing_the_kit_does_not_import_pytest() -> None:
    import subprocess
    import sys

    code = "import sys, keelgate.testing; sys.exit(1 if 'pytest' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0  # noqa: S603
