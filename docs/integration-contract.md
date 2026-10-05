# Integration contract

This page defines the **public API** downstream projects may depend on. It is
versioned with [semantic versioning](https://semver.org/) and is the only part of
Keelgate that carries a stability promise.

!!! info "Status after Phase K2"
    Implemented: **tools, capabilities, policy, audit, approvals** (K1) and
    **loop, context, memory, llm, testing** plus the framework **adapters** (K2).
    Still *planned*: **telemetry** and **evals**. `tests/test_imports.py`
    enforces the module list; the tables below are the symbol-level
    specification.

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
| `InMemoryIdempotencyStore`, `SqliteIdempotencyStore`, `IdempotencyStore` | store | The SQLite store is durable: a key left in flight by a dead process reads as *outcome unknown* and is never retried. Implement the protocol for shared use |
| `CallContext.allowed_side_effects` | field | Optional per-call ceiling (used by read-only loops); a refusal is `side_effect_not_permitted` |
| `ToolSpec.input_schema` | field | Optional explicit JSON schema (used for governed external MCP tools) |

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
| `BudgetLedger`, `InMemoryBudgetLedger`, `SqliteBudgetLedger`, `RevocationList`, `InMemoryRevocationList`, `SqliteRevocationList` | protocols and stores | The SQLite versions survive restarts and are safe across handles and processes on one host; implement the protocols for shared use |

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

### `keelgate.loop`

| Symbol | Kind | Notes |
|---|---|---|
| `Loop(gateway=, registry=, planner=, checkpoints=, grant_token=, policy_context=, stop=, verifier=, ...)` | class | `await loop.run(goal=, tenant_id=, agent_id=, as_of=, run_id=)`, `resume(tenant_id, run_id, stop=)`, `run_or_resume(...)`, `reconcile(...)` |
| `StopConditions(max_iterations, max_tokens, max_dollars, timeout, max_verifier_rejections, goal)` | dataclass | Every limit is optional; the first reached wins. A stop is not a failure |
| `StopReason`, `LoopResult` | types | `result.ok`, `result.resumable`, `result.stop_reason`, `result.final_answer`. Reasons include `goal_reached`, `max_iterations`, `token_budget`, `dollar_budget`, `timeout`, `verifier_rejections`, `outcome_unknown`, `cost_unknown`, `approval_pending`, `error` |
| `Planner`, `LLMPlanner`, `Verifier`, `AcceptAllVerifier`, `CallableVerifier`, `LLMVerifier` | roles | The model proposes plans and verdicts; neither can execute anything |
| `CheckpointStore`, `InMemoryCheckpointStore`, `SqliteCheckpointStore`, `StaleCheckpointError`, `default_checkpointer` | stores | Atomic save; a sequence that is not newer is refused |
| `keelgate.loop.langgraph_store.LangGraphCheckpointStore` | store | Extra `keelgate[langgraph]`. Thread-safe, **not** multi-process safe (see ADR-0004) |
| `LoopRunner`, `LoopSpec`, `LoopOutcome`, `InProcessRunner` | interface | How a run is (re)started; the Temporal adapter implements it |
| `VerificationLoop`, `MonitorLoop`, `Schedule`, `LoopType` | kinds | Verification and monitor loops are READ-only by default, enforced by the gateway |

Guarantees: planned actions are checkpointed **before** they run; resume never
re-asks the model and never re-executes a completed WRITE; a WRITE whose outcome
is unknown stops the loop for a human. See
[ADR-0004](adr/0004-durable-loop-and-resume-semantics.md).

### `keelgate.context`

| Symbol | Kind | Notes |
|---|---|---|
| `ContextBuilder(as_of=, token_budget=, ...)` | class | `add` / `try_add` / `await build()`. **Rejects** any item with `published_at > as_of`, and any outside item with no publication time |
| `ContextItem`, `ItemKind`, `Trust`, `Provenance` | models | Provenance on every item. Only harness-authored kinds can be `TRUSTED`, enforced by type |
| `BuiltContext`, `Rejection`, `RejectionReason` | results | Rejections record a hash of the content, never the content |
| `StructuredSummary`, `Compaction`, `ExtractiveSummarizer`, `LLMSummarizer`, `RecordStore`, `InMemoryRecordStore` | compaction | Over-budget context is compacted into structured summaries that carry pointers to the full records |
| `UNTRUSTED_NOTICE`, fenced `<untrusted ...>` blocks | rendering | Outside text is fenced and cannot break out of its fence |

### `keelgate.memory`

| Symbol | Kind | Notes |
|---|---|---|
| `Memory`, `MemoryTier` | protocol, enum | One interface over four tiers |
| `WorkingMemory`, `EpisodicMemory`, `SemanticMemory`, `ProceduralMemory` | tiers | Working (per run), episodic (decisions + outcomes), semantic (facts with `valid_from`/`valid_to`, vector search), procedural (versioned skills and playbooks) |
| `MemoryRecord`, `Attribution` | models | Every write is a **new version** attributed to an agent and a trace id; nothing is edited in place |
| `SqliteMemoryBackend`, `PostgresMemoryBackend` | backends | Postgres uses pgvector. Bitemporal: `recorded_at` is assigned by the store's clock |
| `Embedder`, `HashEmbedder` | embedding | `HashEmbedder` is a deterministic **lexical** stand-in for tests; it is not semantic. Supply a real embedder |

Guarantees: reads honour `as_of` on both axes (valid time and record time);
every operation is tenant-scoped; retrieved memory reaches a prompt only as an
`UNTRUSTED` context item.

### `keelgate.llm`

| Symbol | Kind | Notes |
|---|---|---|
| `LLMClient` | protocol | `name` and `async complete(LLMRequest) -> LLMResponse`. Provider logic never leaves this package |
| `LLMRequest`, `LLMResponse`, `Message`, `ToolCall`, `ToolSchema`, `Usage`, `Role`, `FinishReason` | models | A model's reply is a proposal; arguments are untrusted |
| `PricingTable`, `ModelPrice` | pricing | **No default prices.** An unpriced model has `cost_usd = None` |
| `LLMError`, `LLMAuthError`, `LLMRateLimitError` | exceptions | `error.retryable` says whether a retry can help |
| `keelgate.llm.providers.AnthropicClient`, `OpenAIClient`, `GoogleClient`, `OllamaClient`, `VLLMClient` | clients | Optional SDKs are imported lazily. Contract-tested through the real SDKs over a mocked transport; **not tested against live services** |

### `keelgate.testing`

| Symbol | Kind | Notes |
|---|---|---|
| `FakeLLM(script, indexed=, pricing=)`, `Reply.say/call/calls` | fake | Scripted and deterministic. `indexed=True` makes a reply a pure function of the call index, so a restarted process continues the script |
| `build_governed_harness`, `GovernedHarness`, `ManualClock`, `StaticPolicyEngine` | harness | A real, in-memory gateway with a scripted policy. `StaticPolicyEngine` is **DENY by default**; `allow_all()` is explicit and test-only |
| pytest fixtures `fake_llm`, `keelgate_clock`, `governed_harness`, `static_policy` | plugin | Loaded automatically through the `pytest11` entry point; opt out with `-p no:keelgate`. Importing `keelgate.testing` never imports pytest |

### `keelgate.adapters`

Adapters wrap a framework; they do not replace it. Every one routes a tool call
through the same path: **registry, grant, policy, approvals, audit**
(`GovernedToolset`), and puts tool output under the key `untrusted_tool_output`.

| Module | Extra | What it provides |
|---|---|---|
| `keelgate.adapters.governed` | none | `GovernedToolset`: the one governed path every adapter uses |
| `keelgate.adapters.langgraph` | `langgraph` | `governed_langchain_tools`, `KeelgateChatModel` (async only) |
| `keelgate.adapters.openai_agents` | `openai` | `governed_function_tools`, `KeelgateModel` (non-streaming) |
| `keelgate.adapters.claude_agent_sdk` | `anthropic` | `governed_sdk_mcp_server`, `governed_claude_options`: built-in tools off, other MCP servers off, settings off, `can_use_tool` allows only governed tools. Tested **at the boundary**: the CLI cannot run offline |
| `keelgate.adapters.mcp` | `mcp` | `GovernedMCPServer` (stdio and streamable HTTP) exposes governed tools. `GovernedMCPClient` governs *consumed* tools: allowlist, capability mapping, optional schema-digest pinning, local argument validation, untrusted output |
| `keelgate.adapters.a2a` | `a2a` | `build_agent_card`, `build_a2a_app`, `GovernedA2AExecutor`. Task intake is a governed PROPOSE tool (`a2a_task_intake`, capability `a2a:task_submit`); remote text is untrusted and never becomes the goal |
| `keelgate.adapters.temporal` | `temporal` | `KeelgateLoopWorkflow`, `TemporalRunner`, `build_worker`: the loop as a Temporal activity |

## Planned

These modules import today and are empty. Their symbols are specified here so the
implementation has a target.

| Module | Symbols |
|---|---|
| `keelgate.telemetry` | `instrument()`, span helpers (GenAI semantic conventions) |
| `keelgate.evals` | `OutcomeMetric` protocol, entry-point discovery, `run_suite()` |

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
| `keelgate[openai]` | OpenAI SDK client and Agents SDK adapter |
| `keelgate[anthropic]` | Anthropic SDK client and Claude Agent SDK adapter |
| `keelgate[google]` | Gemini client (`google-genai`) |
| `keelgate[mcp]` | MCP server and governed MCP client |
| `keelgate[a2a]` | A2A agent card and task intake |
| `keelgate[temporal]` | Temporal durable-execution adapter |
| `keelgate[cedar]` | `CedarEngine` |
| `keelgate[server]` | FastAPI approvals API, Postgres audit store, Redis |

Ollama and vLLM need no extra (plain `httpx`; vLLM reuses the OpenAI wire format
through `keelgate[openai]`). Adapter modules (`keelgate.adapters.*`) are part of
the contract only in that they exist and import.

## Guarantees that outlive any signature

Even where the API changes, these properties hold. Breaking one is a security
bug, not a version bump:

1. A `WRITE` tool cannot execute without a policy decision.
2. A capability that was never granted cannot be exercised.
3. Nothing published after `as_of` enters context or memory.
4. A tampered audit chain fails `verify_chain()`.
5. No tenant can observe or affect another tenant.
6. There is no live execution path: only `paper` and `simulation`.
