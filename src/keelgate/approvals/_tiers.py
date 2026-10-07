"""Approval tiers, ordered from least to most human attention."""

from __future__ import annotations

from enum import StrEnum


class ApprovalTier(StrEnum):
    """How much human attention an action needs.

    ``AUTO``: no human; the policy allowed it outright.
    ``ONE_CLICK``: one authorised approver confirms.
    ``EXPLICIT_SIGNOFF``: one authorised approver confirms *and* echoes a code
    derived from the evidence, so approval cannot be a blind click.
    """

    AUTO = "AUTO"
    ONE_CLICK = "ONE_CLICK"
    EXPLICIT_SIGNOFF = "EXPLICIT_SIGNOFF"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def covers(self, required: ApprovalTier) -> bool:
        """True if an approver cleared for ``self`` may decide a ``required`` request."""
        return self.rank >= required.rank


_RANK = {ApprovalTier.AUTO: 0, ApprovalTier.ONE_CLICK: 1, ApprovalTier.EXPLICIT_SIGNOFF: 2}
