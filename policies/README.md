# Policy packs

Deterministic decision logic lives here, not in prompts. Rego is the default;
Cedar is optional behind the same `PolicyEngine` interface.

`docker-compose.dev.yml` mounts this directory read-only into OPA at `/policies`,
so anything you add is loaded on restart.

| Pack | Status | Purpose |
|---|---|---|
| `keelgate/health.rego` | shipped | Liveness fixture so the dev OPA container starts with a non-empty bundle. Carries no authorisation logic. |
| `finance_basic/` | planned | The first real pack: side-effect gating, per-tenant limits, approval thresholds. |

A policy decision is one of `ALLOW`, `DENY` or `REQUIRE_APPROVAL`. Deny is the
default for anything a pack does not explicitly allow.
