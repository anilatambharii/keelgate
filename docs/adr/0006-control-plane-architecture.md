# ADR-0006: The hosted control plane

- **Status:** Accepted
- **Date:** 2026-10-13
- **Phase:** K5

## Context

Keelgate Cloud is the proprietary product in `ee/` (ADR-0001). It hosts what a team would
otherwise have to run itself: the audit log, the approvals inbox, the policy registry, traces,
usage and billing. Three constraints shape it.

1. **Safety controls stay open and local.** The policy gate, the capability checks and the audit
   chain are Apache-2.0 and run in the customer's process. A hosted service must not become the
   place where those decisions are made, and a customer must be able to verify what it holds
   without trusting it.
2. **The OSS package may not import `ee/`.** The agent needs a way to talk to the control plane
   that lives on the open side.
3. **One bug must not expose another tenant.** The control plane is multi-tenant and holds audit
   records, approval evidence and policy for many organisations.

## Decision

### The protocol is open, the server is not

`keelgate.cloud` (Apache-2.0, provisional API) is a thin client for a documented HTTP protocol
(`docs/cloud-protocol.md`): `CloudAuditStore`, `CloudApprovalQueue`, `CloudPolicyEngine`,
`CloudSpanExporter`. Any service that implements the protocol can back it. To make that possible
without special cases, two open-source seams were widened rather than bypassed: the gateway now
takes an `ApprovalBackend` protocol (three calls), and `ApprovalQueue` takes a pluggable
`ApprovalStore`. The control plane reuses the open-source `ApprovalQueue` and `AuditLog` unchanged,
supplying only Postgres stores, so the approval rules (tier clearance, separation of duties,
expiry, single use, exact-action binding) are the same code in both places.

A test fails if anything under `src/keelgate` imports `ee` or `keelgate_cloud`, or if the wheel
or sdist ships `ee/`.

### Tenant isolation is a database property

Every tenant table carries `org_id` and a row-level-security policy that admits only rows whose
`org_id` equals the transaction's `app.org_id` setting (`SET LOCAL`, so it cannot outlive the
transaction on a pooled connection). Policies apply to the table owner too (`FORCE ROW LEVEL
SECURITY`). The application connects as a role that is not a superuser, owns nothing and cannot
bypass RLS. A transaction that names no org sees no rows and can write none.

A few reads must happen before the caller is known (turn an API key, a session token or a login
e-mail into an org). Those policies additionally admit the single row whose secret-derived value
equals a `app.lookup_*` setting. A lookup can find one row and write nothing. This avoids a
`BYPASSRLS` role and any loosening of the `orgs` policy.

Two jobs must visit every org (usage reporting to Stripe, retention sweeping). They read
`org_index`, a table of org id, plan and billing state only, kept current by a trigger, instead of
reading `orgs` through an extra policy. The application role can list org ids and plans and
nothing more; a test asserts `orgs` keeps a single policy.

Tests prove the property against a real Postgres, and were mutation-checked by weakening a policy
and confirming they fail. They also assert the append-only audit trigger holds for the owner.

### The server writes the audit chain

Clients send *events* (type, actor, payload); the server assigns sequence number, timestamp and
hashes inside a transaction that holds a per-org advisory lock. A compromised agent process cannot
forge, reorder or rewind its history, and the exported chain still verifies with the open-source
`verify_chain`. The agent endpoint refuses `approval.*` events, which only the server writes. If
an event cannot be recorded (quota, outage), the client raises an `AuditError` and the gateway
refuses the action: fail closed.

### Policy is evaluated where the agent runs

The registry stores versioned packs and serves the deployed bundle with a source hash. The client
verifies the hash and evaluates the pack in-process. A control-plane outage can neither allow an
action nor stall one that was already decided; before a pack loads, every decision is DENY. A
version can be deployed only when its stored test cases pass, re-run at deploy time.

### Metering commits with the record

A gated action is a `tool.call` audit event. The usage increment, the quota check and the audit
insert share one transaction under the org's lock, so a recorded call is counted exactly once, an
unrecorded call is never counted, and a quota cap cannot be overshot by concurrent writers. When
the cap is reached the call is refused and leaves no record; events for calls already in flight
are still accepted so approvals can finish.

### Billing is test-mode, and the local estimate is the figure of record

Stripe products and prices are synced from `plans.yaml`; metered usage is reported as a delta
claimed in the database first and sent with a deterministic identifier. The server refuses to
start with a non-test Stripe key unless an owner sets an explicit environment variable. The
invoice preview shown to customers is computed locally from plan config and metered usage;
Stripe's own preview is attached for comparison. Webhooks are signature-verified and deduplicated.

## Consequences

- Rate limiting is per-process. Behind several instances the effective limit multiplies; a shared
  limiter (Redis) is needed before scaling out.
- Audit records are never deleted by the application, so `audit_min_retention_days` is a
  guaranteed minimum and not a deletion schedule. Pruning would break verification from genesis
  and needs a checkpoint scheme the open-source verifier understands first.
- Data residency is a recorded setting. This deployment is single-region and routes nothing.
- The client trusts the `actor` an authenticated agent reports for its own tenant. A stolen API
  key can write misleading events *into its own tenant's chain*, but cannot read, alter or
  remove existing ones, and cannot touch another tenant.
- Plan values the commercial brief did not give (agent cap, rate limits, Team audit window, trial
  lengths, Business trace retention) are marked `assumed` in `plans.yaml` for the owner to confirm.

## Rejected

- **Server-side policy evaluation.** Simpler to operate, but makes a hosted dependency part of
  every gate decision and lets an outage or a bug change what is allowed.
- **A BYPASSRLS service role for lookups.** Works, but one leaked credential would read every
  tenant. Secret-keyed lookup policies expose one row to someone who already holds its secret.
- **Clients building the audit records.** Would need a hash-validating server and would let a
  compromised agent choose its own timestamps and ordering.
