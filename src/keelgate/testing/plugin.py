"""Pytest fixtures for projects built on Keelgate.

Installed automatically through the ``pytest11`` entry point (``pip install keelgate`` then run
pytest); nothing to import or register. To opt out, run ``pytest -p no:keelgate``.

Fixtures:

``fake_llm``
    A factory: ``fake_llm([Reply.call(...), Reply.say("done")], indexed=True)`` -> ``FakeLLM``.
``keelgate_clock``
    A :class:`ManualClock` to share with grants, audit and approvals.
``governed_harness``
    A factory: ``governed_harness(tools=[...], engine=...)`` -> :class:`GovernedHarness`.
``static_policy``
    A factory: ``static_policy({"my_tool": Decision.ALLOW})`` -> :class:`StaticPolicyEngine`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from keelgate.testing._fake_llm import FakeLLM, Reply
from keelgate.testing._harness import (
    GovernedHarness,
    ManualClock,
    StaticPolicyEngine,
    build_governed_harness,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


@pytest.fixture
def keelgate_clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def fake_llm() -> Callable[..., FakeLLM]:
    def make(script: Sequence[Reply], **kwargs: Any) -> FakeLLM:
        return FakeLLM(list(script), **kwargs)

    return make


@pytest.fixture
def static_policy() -> Callable[..., StaticPolicyEngine]:
    return StaticPolicyEngine


@pytest.fixture
def governed_harness(keelgate_clock: ManualClock) -> Callable[..., GovernedHarness]:
    def make(*args: Any, **kwargs: Any) -> GovernedHarness:
        kwargs.setdefault("clock", keelgate_clock)
        return build_governed_harness(*args, **kwargs)

    return make
