"""Human-in-the-loop approval requests and the queue that serves them.

The REST surface lives in ``keelgate.approvals.rest`` and needs the ``server``
extra; it is not imported here so the core stays dependency-light.
"""

from keelgate.approvals._models import (
    ApprovalRequest,
    ApprovalStatus,
    Approver,
    EvidenceBundle,
    EvidenceSource,
    action_hash,
    sanitize_for_display,
)
from keelgate.approvals._queue import (
    ApprovalBackend,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotAuthorisedError,
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    ApprovalNotUsableError,
    ApprovalQueue,
    ApprovalSignoffError,
    ApprovalStore,
    SqliteApprovalStore,
)
from keelgate.approvals._tiers import ApprovalTier

__all__ = [
    "ApprovalBackend",
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
    "ApprovalStore",
    "ApprovalTier",
    "Approver",
    "EvidenceBundle",
    "EvidenceSource",
    "SqliteApprovalStore",
    "action_hash",
    "sanitize_for_display",
]
