# Integration contract

This page defines the **public API** downstream projects may depend on. It is
versioned with [semantic versioning](https://semver.org/) and is the only part
of Keelgate that carries a stability promise.

!!! warning "Phase K0 — surface declared, not yet implemented"
    Every module listed below exists and imports cleanly. The symbols are
    **not** yet present; they land in later phases. Treat this page as the
    specification the implementation must satisfy, and as the checklist review
    uses when judging whether a change is breaking.

    `tests/test_imports.py` enforces the module list today. Symbol-level
    enforcement arrives with the symbols.

## Who depends on this

[Tycheon](https://github.com/anilatambharii/tycheon) — calibrated financial
forecasting and risk — consumes Keelgate as a library (`pip install keelgate`).
Tycheon's CI pins a Keelgate version and imports only what is on this page.

Anything **not** on this page is internal. Importing it is allowed but
unsupported, and it may change in a patch release.

## Stability policy

| Change | Version bump | Also required |
|---|---|---|
| New module, class, function or optional keyword argument | minor | — |
| New enum member | minor | Downstreams must not assume exhaustive matches |
| Deprecation (symbol still works, warns) | minor | `DeprecationWarning` naming the replacement, and a note here |
| Removal, rename, or any signature change | **major** | Migration note in the same PR, plus an entry in the changelog |
| Behavioural change to a documented guarantee | **major** | Migration note, and an ADR explaining why |

Deprecations are announced for at least one minor release before removal.

A breaking change may not be merged without the migration note. Reviewers treat
the absence of one as a blocking defect.

## The contract

### `keelgate.tools`

Tool declaration and registry. Every tool is tagged with the side effect it can
produce, which is what the policy gate keys off.

| Symbol | Kind | Notes |
|---|---|---|
| `ToolRegistry` | class | Registration and lookup of available tools |
| `tool` | decorator | Declares a function as a tool |
| `SideEffect` | enum | `READ`, `PROPOSE`, `WRITE` |

`SideEffect.WRITE` is the trigger for the deterministic policy gate. A tool that
can cause an external effect and is not tagged `WRITE` is a bug.

### `keelgate.capabilities`

Explicit, scoped grants. Deny by default: absence of a grant is a denial.

| Symbol | Kind | Notes |
|---|---|---|
| `Capability` | class | A capability a tool may require |
| `CapabilityGrant` | class | A scoped, tenant-bound grant |
| `issue_grant()` | function | Mints a grant |

### `keelgate.policy`

The decision point. OPA/Rego is the default engine; Cedar is optional behind the
same protocol.

| Symbol | Kind | Notes |
|---|---|---|
| `PolicyEngine` | protocol | Implement to supply your own engine |
| `Decision` | enum | `ALLOW`, `DENY`, `REQUIRE_APPROVAL` |
| policy packs | data | Shipped Rego/Cedar bundles, e.g. `finance_basic` |

Decisions are deterministic functions of the request, the grants and the pack.
No model output may influence the outcome.

### `keelgate.loop`

The durable, budgeted agent loop.

| Symbol | Kind | Notes |
|---|---|---|
| `Loop` | class | The harness loop |
| `StopConditions` | class | Step, cost and wall-clock budgets |
| `run()` | function | Start a run |
| `resume()` | function | Continue a checkpointed run |

### `keelgate.context`

As-of-time context assembly.

| Symbol | Kind | Notes |
|---|---|---|
| `ContextBuilder` | class | Takes an `as_of` timestamp |

`as_of` is a hard cutoff, not a hint. Nothing published after it may enter the
context, which is what makes a backtest or a replay trustworthy.

### `keelgate.memory`

| Symbol | Kind | Notes |
|---|---|---|
| `Memory` | interface | Working, episodic, semantic and procedural layers |

Retrieved memory is untrusted data: it may contain text an attacker placed there
earlier. It is never treated as instructions.

### `keelgate.approvals`

| Symbol | Kind | Notes |
|---|---|---|
| `ApprovalRequest` | class | A pending human decision |
| `ApprovalQueue` | class | Delivery and resolution of requests |

### `keelgate.audit`

| Symbol | Kind | Notes |
|---|---|---|
| `AuditLog` | class | Append-only, hash-chained record |
| `verify_chain()` | function | Independent verification of integrity |

`verify_chain()` must be callable by a party that does not trust the writer.

### `keelgate.telemetry`

| Symbol | Kind | Notes |
|---|---|---|
| `instrument()` | function | Wire OpenTelemetry into a harness instance |
| span helpers | functions | GenAI semantic conventions |

Prompt and response content is **not** recorded on spans by default; it would
carry untrusted text into your telemetry backend.

### `keelgate.evals`

| Symbol | Kind | Notes |
|---|---|---|
| `OutcomeMetric` | protocol | Implement to score a run |
| entry-point discovery | mechanism | Group `keelgate.outcome_metrics` |
| `run_suite()` | function | Execute an eval or red-team suite |

#### Registering an outcome metric

This is the extension point Tycheon uses to contribute financial metrics such
as calibration and realised P&L. Declare the entry point in your own
`pyproject.toml`:

```toml
[project.entry-points."keelgate.outcome_metrics"]
brier_score = "tycheon.metrics:BrierScore"
sharpe = "tycheon.metrics:Sharpe"
```

Keelgate discovers every registered metric at eval time. The entry-point group
name `keelgate.outcome_metrics` is part of this contract and will not change
without a major bump.

### `keelgate.llm`

| Symbol | Kind | Notes |
|---|---|---|
| `LLMClient` | protocol | Model-agnostic boundary |

Provider SDKs are optional extras. Keelgate never requires a specific vendor.

### `keelgate.testing`

| Symbol | Kind | Notes |
|---|---|---|
| `FakeLLM` | class | Deterministic model stand-in |
| fixtures | pytest fixtures | For downstream projects' CI |

This module exists so that downstream CI never needs an API key or a network
call. It is supported API, not a test helper that happens to be importable.

## Optional extras

Adapters are optional. Installing Keelgate pulls in none of these.

| Extra | Enables |
|---|---|
| `keelgate[langgraph]` | LangGraph adapter and checkpointers |
| `keelgate[openai]` | OpenAI SDK and Agents SDK adapter |
| `keelgate[anthropic]` | Anthropic SDK and Claude Agent SDK adapter |
| `keelgate[temporal]` | Temporal durable-execution adapter |
| `keelgate[cedar]` | Cedar policy engine |
| `keelgate[server]` | FastAPI control surface, Postgres, Redis |

Adapter modules (`keelgate.adapters.*`) are part of the contract only in that
they exist and import. Their internals follow the upstream framework and may
change with it.

## Guarantees that outlive any signature

Even where the API changes, these properties hold. Breaking one is a security
bug, not a version bump:

1. A `WRITE` tool cannot execute without a policy decision.
2. A capability that was never granted cannot be exercised.
3. Nothing published after `as_of` enters context or memory.
4. A tampered audit chain fails `verify_chain()`.
5. No tenant can observe or affect another tenant.
