# Policy packs

Deterministic decision logic lives here, not in prompts. Rego is the default;
Cedar is optional behind the same `PolicyEngine` interface.
See [ADR-0002](../docs/adr/0002-policy-engine.md).

`docker-compose.dev.yml` mounts this directory read-only into OPA at `/policies`,
and the default packs ship inside the wheel.

| Pack | Engine | Status | Purpose |
|---|---|---|---|
| `finance_basic/` | Rego | **shipped** | Paper-only enforcement, restricted list, per-action cap, daily exposure, trading hours, approval tiers |
| `cedar_minimal/` | Cedar | shipped, **subset** | The same shape without trading hours or `report:write`; shows the Cedar backend end to end |
| `keelgate/health.rego` | Rego | fixture | Liveness fixture so the dev OPA starts with a non-empty bundle. No authorisation logic. |

A decision is `ALLOW`, `DENY` or `REQUIRE_APPROVAL`. Anything a pack does not
explicitly allow is denied.

## Rules for writing a pack

These were learned the hard way; one of them was a real fail-open in the first draft.

1. **State conditions positively, deny on `not <positive rule>`.** In Rego,
   `not x in set` is *not* true when `x` is undefined. Write `mode_ok if x in set`
   and `deny if not mode_ok`.
2. **Reasons never echo model-supplied strings.** Fixed wording only.
3. **Deny by default.** Unknown capabilities, unknown side effects, missing limits.
4. **Decide against `as_of`**, never the wall clock.
5. **Every rule needs a test**, including the case where its input is missing.

## Working on Rego

```bash
make policy-test     # opa check --strict, opa fmt --fail, then all unit tests
```

This uses a local `opa` if installed, otherwise the pinned image through the
`opa-tools` compose service. Python-side tests run the same cases through both
the in-process engine and a real OPA server (`make test-integration`).

## The input contract

Built by Keelgate, never by the model:

```text
input.action   {tool, side_effect, capability, args}
input.actor    {agent_id, tenant_id, grant_id}
input.resource {symbol, notional}              derived from validated tool args
input.context  {as_of, execution_mode, exposure, limits, positions}
```

`as_of` is RFC 3339 UTC with an explicit `+00:00` offset. `context` comes from
trusted harness code only.
