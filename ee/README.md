# Keelgate Cloud (Enterprise Edition)

> **This directory is proprietary.** It is not covered by the Apache-2.0 licence
> at the repository root. See [LICENSE](LICENSE) in this directory.

Keelgate Cloud is the hosted product: a multi-tenant control plane and dashboard
built on top of the open-source harness.

| Path | Status | Contents |
|---|---|---|
| `control_plane/` | built (K5) | FastAPI + Postgres (row-level security): orgs, users, API keys, OIDC SSO, usage metering and quotas, hosted audit log and approvals inbox, policy registry with test-before-deploy, audit export, retention, Stripe (test mode) |
| `dashboard/` | built (K5) | Next.js + TypeScript + Tailwind: onboarding, API keys, usage and billing, trace explorer, approvals inbox, policy editor with simulation, audit export |

Architecture and its trade-offs: [ADR-0006](../docs/adr/0006-control-plane-architecture.md). The
protocol agents speak to it is open: [`docs/cloud-protocol.md`](../docs/cloud-protocol.md).

## Run it locally

```bash
docker compose -f docker-compose.dev.yml --profile cloud up -d postgres stripe-mock
cd ee/control_plane
uv sync
uv run python -m keelgate_cloud dev-init     # database, roles, schema, .env (gitignored)
uv run python -m keelgate_cloud serve        # http://localhost:8000  (docs at /api/docs)
cd ../dashboard && npm ci && npm run dev     # http://localhost:3000
```

Then sign up in the dashboard, create an API key, and run the agent:

```bash
python examples/cloud_governed_agent.py --url http://localhost:8000 --api-key kg_live_...
```

It parks a large paper trade in the hosted inbox; approve it in the dashboard. Stripe runs in
**test mode only**: the server refuses a non-test key.

## Tests

```bash
cd ee/control_plane && uv run pytest        # needs the dev Postgres and stripe-mock above
cd ee/dashboard && npm test
```

`tests/test_acceptance.py` is the end-to-end story: sign up, key, deploy a policy, run the agent,
approve, usage, audit verification, invoice preview.

## The boundary

Two rules, and they are not negotiable in review:

1. **`ee/` may depend on the OSS harness. The harness never depends on `ee/`.**
   The dependency is one-way and verifiable by import (a test enforces it, and that the
   published package ships no `ee/` code).
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
