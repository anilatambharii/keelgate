"""Fakes and fixtures Keelgate ships for downstream projects' CI.

Importing this package never imports pytest. The fixtures live in ``keelgate.testing.plugin``,
which pytest loads through an entry point.
"""

from keelgate.testing.fake_llm import (
    FakeLLM,
    Reply,
    ScriptExhaustedError,
    UnofferedToolError,
)
from keelgate.testing.harness import (
    GovernedHarness,
    ManualClock,
    StaticPolicyEngine,
    build_governed_harness,
)

__all__ = [
    "FakeLLM",
    "GovernedHarness",
    "ManualClock",
    "Reply",
    "ScriptExhaustedError",
    "StaticPolicyEngine",
    "UnofferedToolError",
    "build_governed_harness",
]
