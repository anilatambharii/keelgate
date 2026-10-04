# Keelgate Cloud (Enterprise Edition)

> **This directory is proprietary.** It is not covered by the Apache-2.0 licence
> at the repository root. See [LICENSE](LICENSE) in this directory.

Keelgate Cloud is the hosted product: a multi-tenant control plane and dashboard
built on top of the open-source harness.

| Path | Status | Contents |
|---|---|---|
| `control_plane/` | planned | Multi-tenant API, tenant and grant management, metered billing |
| `dashboard/` | planned | Next.js + TypeScript + Tailwind approvals and audit UI |

## The boundary

Two rules, and they are not negotiable in review:

1. **`ee/` may depend on the OSS harness. The harness never depends on `ee/`.**
   The dependency is one-way and verifiable by import.
2. **Every safety-relevant control stays Apache-2.0.** Capability scoping, the
   policy gate, `as_of` enforcement, the audit chain and `verify_chain()`,
   approval semantics, and tenant isolation primitives all live outside this
   directory.

What Cloud sells is *operating* the harness — hosting, multi-tenant management,
dashboards, SSO, retention, support — never permission to be safe. In
particular, `verify_chain()` is and stays open source, so a customer can verify
a Cloud-written audit chain with code they control.

The reasoning, the options rejected, and the rules a reviewer applies are
recorded in
[ADR-0001](../docs/adr/0001-licensing-and-open-core.md).

## Contributions

External contributions to this directory cannot be accepted, since they cannot
be relicensed into a proprietary product. Contributions to the harness —
everything outside `ee/` — are welcome under Apache-2.0; see
[CONTRIBUTING.md](../CONTRIBUTING.md).

Source is readable here so that customers and auditors can review it. That is
not a grant of any other right.
