"""Use a hosted control plane from a Keelgate agent.

Four small components, all plain HTTP against the documented control-plane protocol
(``docs/cloud-protocol.md``): audit events are chained and stored by the server
(:class:`CloudAuditStore`), actions that need a person go to a hosted inbox
(:class:`CloudApprovalQueue`), a tenant's deployed policy pack is fetched and evaluated in your own
process (:class:`CloudPolicyEngine`), and spans are shipped to the trace store
(:class:`CloudSpanExporter`). They need nothing but an endpoint and an API key.

Nothing here weakens a safety control: the policy gate still runs in your process, a failure to
record an audit event still refuses the action, and the audit chain a tenant exports verifies with
the open-source :func:`keelgate.audit.verify_chain`.
"""

from keelgate.cloud._approvals import CloudApprovalError, CloudApprovalQueue
from keelgate.cloud._audit import CloudAuditError, CloudAuditStore
from keelgate.cloud._client import CloudAuthError, CloudClient, CloudError, CloudQuotaError
from keelgate.cloud._policy import CloudPolicyEngine, PolicyPackError
from keelgate.cloud._traces import CloudSpanExporter

__all__ = [
    "CloudApprovalError",
    "CloudApprovalQueue",
    "CloudAuditError",
    "CloudAuditStore",
    "CloudAuthError",
    "CloudClient",
    "CloudError",
    "CloudPolicyEngine",
    "CloudQuotaError",
    "CloudSpanExporter",
    "PolicyPackError",
]
