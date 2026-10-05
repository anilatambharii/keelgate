# Integration contract

This page defines the **public API** downstream projects may depend on. It is
versioned with [semantic versioning](https://semver.org/) and is the only part of
Keelgate that carries a stability promise.

!!! info "Status after Phase K1"
    The safety core is implemented: **tools, capabilities, policy, audit and
    approvals**. The remaining modules (**loop, context, memory, telemetry,
    evals, llm, testing**) exist and import, but their symbols arrive in later
    phases and are marked *planned* below. `tests/test_imports.py` enforces the
    module list; the tables below are the symbol-level specification.

## Who depends on this

[Tycheon](https://github.com/anilatambharii/tycheon), calibrated financial
forecasting and risk, consumes Keelgate as a library (`pip install keelgate`).
Anything **not** on this page is internal: importing it is allowed but
unsupported, and it may change in a patch release.

## Stability policy

| Change | Version bump | Also required |
|---|---|---|
| New module, class, function or optional keyword argument | minor | none |
| New enum member | minor | Downstreams must not assume exhaustive matches |
| Deprecation (symbol still works, warns) | minor | `DeprecationWarning` naming the replacement, and a note here |
| Removal, rename, or any signature change | **major** | Migration note in the same PR, plus a changelog entry |
| Behavioural change to a documented guarantee | **major** | Migration note, and an ADR explaining why |

Deprecations are announced for at least one minor release before removal. A
breaking change may not merge without its migration note.

## Implemented

### `keelgate.tools`

| Symbol | Kind | Notes |
|---|---|---|
| `tool(capability=, side_effect=, name=, timeout_s=, cost_estimate=, idempotency_key=, resource=)` | decorator | The function takes one Pydantic model and returns one. Both annotations become the schemas. A `WRITE` tool **must** supply `idempotency_key`. |
| `Tool` | class | A declared tool. **Not callable**: calling it raises `DirectInvocationError`. |
| `ToolRegistry` | class | `register`, `get`, `names`, `describe`, `freeze`. Frozen when a gateway is built. |
| `SideEffect` | enum | `READ`, `PROPOSE`, `WRITE` |
| `ToolGateway` | class | `await gateway.call(tool_name=, arguments=, grant_token=, context=)`; the only place a tool body runs |
| `CallContext` | dataclass | `tenant_id` and `policy_context` are **trusted**; `rationale`, `sources`, `confidence`, `verifier_flags` are model-derived evidence for a human; `approval_id` resumes a parked call |
| `ToolOutcome`, `OutcomeStatus`, `ToolError`, `ErrorCode` | types | Structured, model-actionable results. A refusal is a result, not an exception. |
| `Untrusted` | wrapper | Marks tool output. Read it with `.unwrap_untrusted()`, by name. |
| `ToolRefusedError` | exception | A tool raises this to say "I did nothing, a retry is safe". |
| `InMemoryIdempotencyStore`, `IdempotencyStore` | store | Process-local; implement the protocol for shared use |

Guarantees: a `WRITE` never runs without a policy `ALLOW`, or `REQUIRE_APPROVAL`
with a matching, unexpired, single-use approval. A timed-out or crashed `WRITE`
is parked `UNKNOWN` and never retried automatically. The audit record is written
before the side effect.

### `keelgate.capabilities`

| Symbol | Kind | Notes |
|---|---|---|
| `Capability` | `str` subclass | `<resource>:<action>`, lower snake case. **Wildcards are a syntax error.** |
| `issue_grant(signer, agent_id=, tenant_id=, capabilities=, max_cost=, ttl=)` | function | Returns `SignedGrant(grant, token)`. Refuses unsafe input. |
| `GrantSigner` | class | Holds the Ed25519 private key. `GrantSigner.generate()` for development. |
| `GrantVerifier(trusted_keys, clock=, revocations=, max_ttl=, leeway=)` | class | Holds public keys only. `verify(token)` returns a `CapabilityGrant` or raises. |
| `CapabilityGrant`, `Budget` | models | `grant.allows(capability)` is exact membership |
| `GrantError` and subclasses | exceptions | `GrantInvalidError`, `GrantExpiredError`, `GrantNotYetValidError`, `GrantRevokedError`, `UnknownKeyError` |
| `BudgetLedger`, `InMemoryBudgetLedger`, `RevocationList`, `InMemoryRevocationList` | protocols and defaults | Process-local; implement the protocols for shared use |

See [ADR-0003](adr/0003-capability-grants-paseto.md).

### `keelgate.policy`

| Symbol | Kind | Notes |
|---|---|---|
| `PolicyEngine` | protocol | `async decide(PolicyInput) -> PolicyDecision`. **Must fail closed.** |
| `Decision` | enum | `ALLOW`, `DENY`, `REQUIRE_APPROVAL` |
| `PolicyInput`, `PolicyAction`, `PolicyActor`, `PolicyContext` | models | `PolicyContext` (`as_of`, `execution_mode`, `limits`, `exposure`, `positions`) is supplied by trusted code only |
| `PolicyDecision` | model | `effect`, `reasons`, `approval_tier`, `policy_version` (hash of the policy that decided), `engine` |
| `OpaHttpEngine` | engine | Default for production |
| `RegoEngine` | engine | In-process, same `.rego` files; for tests, the quickstart and single-process use |
| `keelgate.policy.cedar.CedarEngine` | engine | Optional, extra `keelgate[cedar]`; a documented subset |
| `pack_path(name)`, `load_pack_sources`, `hash_sources` | helpers | Locate and fingerprint packs |

Packs live in `policies/` and ship in the wheel. `finance_basic` is the first;
see [ADR-0002](adr/0002-policy-engine.md) for the rules every pack follows.

### `keelgate.audit`

| Symbol | Kind | Notes |
|---|---|---|
| `AuditLog(store=, clock=)` | class | `append`, `records`, `head`, `verify_chain` |
| `verify_chain(records, tenant_id=, expected_head=)` | function | **Pure and storage-independent.** Runnable by a party that does not trust the writer. |
| `SqliteAuditStore`, `PostgresAuditStore` | stores | Per-tenant chains; append-only triggers |
| `AuditRecord`, `ChainHead`, `ChainVerification`, `EventType` | types | `event_type` is a plain string in storage, so newer event kinds still verify |

The hash input is a **stable format** pinned by a golden-vector test. Changing it
invalidates every existing chain and is a major change.

### `keelgate.approvals`

| Symbol | Kind | Notes |
|---|---|---|
| `ApprovalQueue` | class | `submit`, `get`, `list_pending`, `approve`, `reject`, `consume` |
| `ApprovalRequest`, `EvidenceBundle`, `EvidenceSource`, `Approver` | models | The evidence bundle carries action, rationale, sources, confidence, verifier flags, policy reasons and policy version |
| `ApprovalTier` | enum | `AUTO`, `ONE_CLICK`, `EXPLICIT_SIGNOFF` |
| `ApprovalError` and subclasses | exceptions | Stable `.code` strings |
| `action_hash(tool, args)` | function | What an approval is bound to |
| `keelgate.approvals.rest.create_app(queue, tokens)` | FastAPI app | Extra `keelgate[server]` |
| `keelgate-approvals` | CLI | `list`, `show`, `approve`, `reject` |

## Planned

These modules import today and are empty. Their symbols are specified here so the
implementation has a target.

| Module | Symbols |
|---|---|
| `keelgate.loop` | `Loop`, `StopConditions`, `run()`, `resume()` |
| `keelgate.context` | `ContextBuilder` with `as_of`; nothing published after `as_of` may enter |
| `keelgate.memory` | `Memory` interface: working, episodic, semantic, procedural |
| `keelgate.telemetry` | `instrument()`, span helpers (GenAI semantic conventions) |
| `keelgate.evals` | `OutcomeMetric` protocol, entry-point discovery, `run_suite()` |
| `keelgate.llm` | `LLMClient` |
| `keelgate.testing` | `FakeLLM` and fixtures for downstream CI |

### Registering an outcome metric

The extension point Tycheon uses to contribute financial metrics. Declare the
entry point in your own `pyproject.toml`:

```toml
[project.entry-points."keelgate.outcome_metrics"]
brier_score = "tycheon.metrics:BrierScore"
sharpe = "tycheon.metrics:Sharpe"
```

The group name `keelgate.outcome_metrics` is part of this contract and will not
change without a major bump.

## Optional extras

| Extra | Enables |
|---|---|
| `keelgate[langgraph]` | LangGraph adapter and checkpointers |
| `keelgate[openai]` | OpenAI SDK and Agents SDK adapter |
| `keelgate[anthropic]` | Anthropic SDK and Claude Agent SDK adapter |
| `keelgate[temporal]` | Temporal durable-execution adapter |
| `keelgate[cedar]` | `CedarEngine` |
| `keelgate[server]` | FastAPI approvals API, Postgres audit store, Redis |

Adapter modules (`keelgate.adapters.*`) are part of the contract only in that
they exist and import.

## Guarantees that outlive any signature

Even where the API changes, these properties hold. Breaking one is a security
bug, not a version bump:

1. A `WRITE` tool cannot execute without a policy decision.
2. A capability that was never granted cannot be exercised.
3. Nothing published after `as_of` enters context or memory.
4. A tampered audit chain fails `verify_chain()`.
5. No tenant can observe or affect another tenant.
6. There is no live execution path: only `paper` and `simulation`.
