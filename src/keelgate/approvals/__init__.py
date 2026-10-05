"""Human-in-the-loop approval requests and the queue that serves them.

The REST surface lives in ``keelgate.approvals.rest`` and needs the ``server``
extra; it is not imported here so the core stays dependency-light.
"""

from keelgate.approvals.models import (
    ApprovalRequest,
    ApprovalStatus,
    Approver,
    EvidenceBundle,
    EvidenceSource,
    action_hash,
    sanitize_for_display,
)
from keelgate.approvals.queue import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotAuthorisedError,
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    ApprovalNotUsableError,
    ApprovalQueue,
    ApprovalSignoffError,
)
from keelgate.approvals.tiers import ApprovalTier

__all__ = [
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalNotAuthorisedError",
    "ApprovalNotFoundError",
    "ApprovalNotPendingError",
    "ApprovalNotUsableError",
    "ApprovalQueue",
    "ApprovalRequest",
    "ApprovalSignoffError",
    "ApprovalStatus",
    "ApprovalTier",
    "Approver",
    "EvidenceBundle",
    "EvidenceSource",
    "action_hash",
    "sanitize_for_display",
]
