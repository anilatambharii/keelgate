"""Cedar backend (``pip install 'keelgate[cedar]'``), behind the same ``PolicyEngine``.

Cedar answers permit or deny; Keelgate needs ALLOW, DENY or REQUIRE_APPROVAL.
The adapter maps three Cedar actions onto that:

* ``Action::"invoke"``: must be permitted for the call to be allowed at all.
  Cedar is deny-by-default, and a matching ``forbid`` always wins.
* ``Action::"explicit_signoff"`` / ``Action::"one_click"``: consulted only once
  ``invoke`` is permitted. A permit here turns ALLOW into REQUIRE_APPROVAL at
  that tier, strictest first.

Two Cedar behaviours the adapter exists to neutralise:

* **An erroring policy is skipped, not fatal.** If a ``forbid`` references an
  attribute that is not there, Cedar ignores that policy and may still return
  Allow. The adapter treats *any* evaluation error as DENY.
* **Cedar has integers, not floats.** Money amounts are rounded **up** to whole
  units, so a cap can only ever be hit early, never missed.

Reasons come from each policy's ``@id("...")`` annotation, which is author
controlled; they never echo model-supplied text.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any

import cedarpy

from keelgate.approvals._tiers import ApprovalTier
from keelgate.policy._engine import deny, hash_sources
from keelgate.policy._types import Decision, PolicyDecision

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from keelgate.policy._types import PolicyInput

ACTION_INVOKE = "invoke"
ACTION_EXPLICIT = "explicit_signoff"
ACTION_ONE_CLICK = "one_click"


def _whole_units_up(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("expected a finite number")
    return math.ceil(value)


def load_cedar_sources(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        raise FileNotFoundError(f"Cedar pack directory not found: {directory}")
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.cedar"))}
    if not sources:
        raise FileNotFoundError(f"no .cedar sources in {directory}")
    return sources


class CedarEngine:
    name = "cedar"

    def __init__(self, pack_dir: Path) -> None:
        sources = load_cedar_sources(pack_dir)
        self.policy_version = hash_sources(sources)
        self._policies = "\n".join(sources[name] for name in sorted(sources))
        # Parsing here means a pack that does not compile fails at start-up.
        parsed = json.loads(cedarpy.policies_to_json_str(self._policies))
        self._labels: dict[str, str] = {
            policy_id: str(body.get("annotations", {}).get("id", policy_id))
            for policy_id, body in parsed.get("staticPolicies", {}).items()
        }

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        try:
            document = policy_input.to_document()
        except Exception as exc:
            return deny(
                f"policy input rejected: {type(exc).__name__}",
                engine=self.name,
                policy_version=self.policy_version,
            )
        return await self.decide_document(document)

    async def decide_document(self, document: Mapping[str, Any]) -> PolicyDecision:
        try:
            request_parts = self._request_parts(document)
            invoke = self._evaluate(ACTION_INVOKE, *request_parts)
            if not invoke.allowed:
                return self._denied(self._labels_of(invoke.diagnostics.reasons))
            for action, tier, reason in (
                (ACTION_EXPLICIT, ApprovalTier.EXPLICIT_SIGNOFF, "needs explicit sign-off"),
                (ACTION_ONE_CLICK, ApprovalTier.ONE_CLICK, "needs one-click approval"),
            ):
                if self._evaluate(action, *request_parts).allowed:
                    return PolicyDecision(
                        effect=Decision.REQUIRE_APPROVAL,
                        reasons=(reason,),
                        approval_tier=tier,
                        policy_version=self.policy_version,
                        engine=self.name,
                    )
            return PolicyDecision(
                effect=Decision.ALLOW,
                reasons=tuple(self._labels_of(invoke.diagnostics.reasons)) or ("permitted",),
                policy_version=self.policy_version,
                engine=self.name,
            )
        except Exception as exc:  # includes any Cedar evaluation error: fail closed
            return deny(
                f"policy evaluation failed: {type(exc).__name__}",
                engine=self.name,
                policy_version=self.policy_version,
            )

    # ------------------------------------------------------------------ internals

    def _evaluate(
        self, action: str, entities: list[dict[str, Any]], context: dict[str, Any]
    ) -> cedarpy.AuthzResult:
        request = {
            "principal": {"type": "Agent", "id": "caller"},
            "action": {"type": "Action", "id": action},
            "resource": {"type": "Resource", "id": "call"},
            "context": context,
        }
        result = cedarpy.is_authorized(request, self._policies, entities)
        if list(result.diagnostics.errors):
            # Cedar skips a policy that errors, so a broken forbid is simply absent
            # and the verdict can still be Allow. Never accept a verdict with errors.
            raise RuntimeError("Cedar reported policy evaluation errors")
        return result

    def _labels_of(self, policy_ids: Any) -> list[str]:
        return sorted({self._labels.get(pid, pid) for pid in policy_ids})

    def _denied(self, labels: list[str]) -> PolicyDecision:
        reasons = tuple(f"denied by policy {label}" for label in labels) or (
            "no Cedar policy permits this action",
        )
        return PolicyDecision(
            effect=Decision.DENY,
            reasons=reasons,
            policy_version=self.policy_version,
            engine=self.name,
        )

    @staticmethod
    def _request_parts(document: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        action, actor = document["action"], document["actor"]
        resource, ctx = document.get("resource", {}), document["context"]
        limits, exposure = ctx["limits"], ctx["exposure"]

        attrs: dict[str, Any] = {
            "tool": action["tool"],
            "side_effect": action["side_effect"],
            "capability": action["capability"],
        }
        if isinstance(resource.get("symbol"), str):
            attrs["symbol"] = resource["symbol"].upper()
        if "notional" in resource:
            attrs["notional"] = _whole_units_up(resource["notional"])

        context: dict[str, Any] = {
            "execution_mode": ctx["execution_mode"],
            "as_of_epoch": int(datetime.fromisoformat(ctx["as_of"]).timestamp()),
            "daily_notional": _whole_units_up(exposure["daily_notional"]),
            "max_notional_per_action": _whole_units_up(limits["max_notional_per_action"]),
            "max_daily_exposure": _whole_units_up(limits["max_daily_exposure"]),
            "approval_one_click_notional": _whole_units_up(limits["approval_one_click_notional"]),
            "approval_explicit_notional": _whole_units_up(limits["approval_explicit_notional"]),
            "restricted_symbols": [str(s).upper() for s in limits["restricted_symbols"]],
        }
        entities = [
            {
                "uid": {"type": "Agent", "id": "caller"},
                "attrs": {"tenant_id": actor["tenant_id"], "grant_id": actor["grant_id"]},
                "parents": [],
            },
            {"uid": {"type": "Resource", "id": "call"}, "attrs": attrs, "parents": []},
        ]
        return entities, context
