"""In-process Rego evaluation.

Evaluates the *same* ``.rego`` files an OPA server would load, with no server and
no binary. It exists for tests, the quickstart and single-process deployments.

It is a different Rego implementation from OPA, so the two can disagree on edge
cases. Production uses :class:`~keelgate.policy._opa.OpaHttpEngine`; the shared
conformance tests run every policy case against both so a divergence is caught.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import regopy

from keelgate.policy._engine import (
    decision_from_result,
    deny,
    first_expression,
    hash_sources,
    load_pack_sources,
    pack_path,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from keelgate.policy._types import PolicyDecision, PolicyInput

DEFAULT_QUERY = "data.keelgate.finance_basic.decision"


class RegoEngine:
    """Policy engine that evaluates the same Rego packs in-process.

    For tests, the quickstart and single-process use; production should use OPA. Fails closed.
    """

    name = "rego-inprocess"

    def __init__(self, pack_dir: Path | None = None, *, query: str = DEFAULT_QUERY) -> None:
        directory = pack_dir if pack_dir is not None else pack_path("finance_basic")
        sources = load_pack_sources(directory)
        self.policy_version = hash_sources(sources)
        self._query = query
        self._interpreter = regopy.Interpreter()
        for filename, text in sources.items():
            # A pack that does not compile fails here, at start-up, not at the
            # first decision.
            self._interpreter.add_module(filename, text)
        self._lock = threading.Lock()

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        try:
            document = policy_input.to_document()
        except Exception as exc:  # NaN, non-JSON values, ...: refuse rather than guess
            return deny(
                f"policy input rejected: {type(exc).__name__}",
                engine=self.name,
                policy_version=self.policy_version,
            )
        return await self.decide_document(document)

    async def decide_document(self, document: Mapping[str, Any]) -> PolicyDecision:
        """Evaluate a raw input document. Used by conformance tests and adapters."""
        try:
            with self._lock:
                self._interpreter.set_input(dict(document))
                output = self._interpreter.query(self._query)
            raw = first_expression(output.results)
        except Exception as exc:  # the gate fails closed on any error at all
            return deny(
                f"policy evaluation failed: {type(exc).__name__}",
                engine=self.name,
                policy_version=self.policy_version,
            )
        return decision_from_result(raw, policy_version=self.policy_version, engine=self.name)
