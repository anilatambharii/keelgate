"""The ``PolicyEngine`` protocol and the fail-closed plumbing every engine shares.

An engine returns a decision or, on any error at all, a DENY. Nothing here can
turn a malformed or missing result into an ALLOW.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import ValidationError

from keelgate.approvals._tiers import ApprovalTier
from keelgate.policy._types import (
    MAX_REASON_LENGTH,
    MAX_REASONS,
    Decision,
    PolicyDecision,
    PolicyInput,
)

UNAVAILABLE_VERSION: Final = "unavailable"


@runtime_checkable
class PolicyEngine(Protocol):
    """Decides ALLOW / DENY / REQUIRE_APPROVAL for one proposed action.

    Implementations must be deterministic functions of ``policy_input`` and the
    loaded policy. They must not consult the wall clock, the network (other than
    their own decision point) or any model.
    """

    name: str

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision: ...


def deny(reason: str, *, engine: str, policy_version: str = UNAVAILABLE_VERSION) -> PolicyDecision:
    """A fail-closed denial."""
    return PolicyDecision(
        effect=Decision.DENY,
        reasons=(reason,),
        policy_version=policy_version,
        engine=engine,
    )


def decision_from_result(raw: object, *, policy_version: str, engine: str) -> PolicyDecision:
    """Turn an engine's raw result into a decision, failing closed.

    Anything that is not a well-formed ``{effect, reasons, approval_tier}``
    object with a known effect becomes a DENY. An approval requirement without a
    usable tier is treated as the strictest tier rather than the loosest.
    """
    if not isinstance(raw, Mapping):
        return deny("policy returned no decision", engine=engine, policy_version=policy_version)
    effect_raw = raw.get("effect")
    try:
        if not isinstance(effect_raw, str):
            raise ValueError("effect must be a string")  # noqa: TRY004
        effect = Decision(effect_raw)
    except ValueError:
        return deny(
            "policy returned an unrecognised effect", engine=engine, policy_version=policy_version
        )

    reasons = _clean_reasons(raw.get("reasons"))
    tier: ApprovalTier | None = None
    if effect is Decision.REQUIRE_APPROVAL:
        tier = _parse_tier(raw.get("approval_tier"))
    try:
        return PolicyDecision(
            effect=effect,
            reasons=reasons,
            approval_tier=tier,
            policy_version=policy_version,
            engine=engine,
        )
    except ValidationError:  # pragma: no cover - guarded by the construction above
        return deny(
            "policy decision failed validation", engine=engine, policy_version=policy_version
        )


def _parse_tier(value: object) -> ApprovalTier:
    if not isinstance(value, str):
        return ApprovalTier.EXPLICIT_SIGNOFF
    try:
        tier = ApprovalTier(value)
    except ValueError:
        return ApprovalTier.EXPLICIT_SIGNOFF
    return ApprovalTier.EXPLICIT_SIGNOFF if tier is ApprovalTier.AUTO else tier


def _clean_reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    cleaned = [r[:MAX_REASON_LENGTH] for r in value if isinstance(r, str)]
    return tuple(cleaned[:MAX_REASONS])


def hash_sources(sources: Mapping[str, str]) -> str:
    """A stable version hash over named policy sources.

    Names are bare file names so the same pack hashes identically whether it is
    read from disk or fetched back out of a running OPA.
    """
    digest = hashlib.sha256()
    for name in sorted(sources):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(sources[name].encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def load_pack_sources(directory: Path) -> dict[str, str]:
    """Read a pack's ``.rego`` sources, excluding ``*_test.rego``."""
    if not directory.is_dir():
        raise FileNotFoundError(f"policy pack directory not found: {directory}")
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.rego"))
        if not path.name.endswith("_test.rego")
    }
    if not sources:
        raise FileNotFoundError(f"no .rego sources in {directory}")
    return sources


def pack_path(name: str) -> Path:
    """Locate a shipped policy pack.

    Installed wheels carry packs under ``keelgate/policy/packs``; a source
    checkout keeps them in ``policies/`` at the repository root.
    """
    packaged = Path(__file__).resolve().parent / "packs" / name
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "policies" / name


def first_expression(results: Any) -> object:
    """The first expression value of a query result, or None if undefined."""
    if not results:
        return None
    expressions = getattr(results[0], "expressions", None)
    if not expressions:
        return None
    return expressions[0]
