# Integration contract

This page is the contract between Keelgate and the libraries built on it (Tycheon is the first). It
says what you may depend on, how, and what the promise is. It has three parts that work together:

| Document | What it is | Who writes it |
|---|---|---|
| **This page** | Semantics, guarantees, the rules for depending on Keelgate, and a worked example | People |
| [**API contract**](api-contract.md) | **Every public symbol** with its kind, stability level, full signature and summary | Generated from the code; a test fails if it is stale |
| [**Versioning**](versioning.md) | What each version bump means, and how anything is deprecated | People |

## Depending on Keelgate

```toml
# pyproject.toml of your library
dependencies = ["keelgate>=0.2,<0.3"]          # pin a minor while Keelgate is 0.x
```

1. **Import from the package, never from a module inside it.** The public API is what each package
   exports (`from keelgate.tools import ToolGateway`). Every module whose name starts with an
   underscore is private, as is anything not in a package's `__all__`, and may change in any
   release. `keelgate migrate-imports` fixes old deep imports for you.
2. **Prefer stable symbols.** Each symbol in the [API contract](api-contract.md) is marked
   *stable* or *provisional*. Provisional symbols may change in a minor release; pin the minor.
3. **Treat enums as open.** A new member is a minor change. Do not write an exhaustive `match` that
   crashes on an unknown member.
4. **Extend through the protocols,** not by subclassing internals: `PolicyEngine`, `LLMClient`,
   `CheckpointStore`, `IdempotencyStore`, `AuditStore`, `Planner`, `Verifier`, `OutcomeMetric`.
5. **Register metrics by entry point** (`keelgate.outcome_metrics`), below.
6. **Test with the testing kit,** not with mocks of internals: `FakeLLM` and the governed harness.

The public API is enforced, not just documented: a test fails if a module that is not on the
registry is importable by a plain name, if a public symbol has no docstring of its own, or if the
generated API contract differs from the code. A second set of tests behaves like a downstream
library and imports **only** the public API; another test fails if those files import anything
else.

## Who depends on this

[Tycheon](https://github.com/anilatambharii/tycheon), calibrated financial forecasting and risk,
consumes Keelgate as a library (`pip install keelgate`).

## Stability

The full policy (version numbers, formats that are part of the contract, deprecation) is in
[versioning](versioning.md). In short:

| Level | Promise |
|---|---|
| **Stable** | Will not break without a version bump that says so, a changelog entry and a migration note (a minor bump while Keelgate is 0.x; a major one from 1.0). |
| **Provisional** | Works and is tested, but may change in a **minor** release with a changelog entry. |

Stable: the safety core (`tools`, `capabilities`, `policy`, `audit`, `approvals`); `Loop`,
`StopConditions`, `LoopResult`, `StopReason`, `CheckpointStore`; `ContextBuilder`, `ContextItem`;
the `Memory` interface and its four tiers; the `LLMClient` protocol and its request, response and
error types; `FakeLLM`; `GovernedToolset`; `instrument()`, `span`, `traced`; `OutcomeMetric`,
`OutcomeRecord`, `MetricResult`, the `keelgate.outcome_metrics` group, `discover_metrics`,
`run_suite`.

Provisional: the model provider clients (vendor SDKs move fast); every framework adapter (so do the
frameworks); memory backends and embedders; replay, the Temporal runner, `OutcomeConfirmer`,
`LoopRunner` and `LoopSpec`; the testing harness and fixture names; telemetry attribute names (the
GenAI conventions are still in development upstream); report formats, CLI flags and red-team case
ids.

The [guarantees at the end of this page](#guarantees-that-outlive-any-signature) are not tiered:
they hold for every symbol in both levels.

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

Supporting types: `ToolDefinitionError` (a tool declared unsafely, for example a `WRITE` without an
idempotency key), `RegistryFrozenError` (registering after the gateway froze the registry),
`PAPER_MODES` (the only execution modes v1 permits: `paper` and `simulation`), and `Claim` /
`ClaimState` (the outcome of claiming an idempotency key: `NEW` may run, `DONE` replays the stored
result, `IN_FLIGHT` is running now, `UNKNOWN` died mid-flight and is never retried, `CONFLICT`
means the key was reused for different arguments).

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

Also exported: `InvalidCapabilityError` (a malformed capability, including any wildcard),
`DEFAULT_MAX_TTL` (the longest grant lifetime a verifier accepts unless configured otherwise: 24
hours), and the capability strings the shipped `finance_basic` pack understands:
`MARKET_DATA_READ`, `TRADE_PROPOSE`, `TRADE_PAPER_EXECUTE` and `REPORT_WRITE`.

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

Helpers for writing your own engine: `deny(reason, engine=)` builds a fail-closed `DENY` decision,
and `decision_from_result(raw, policy_version=, engine=)` turns an engine's raw answer into a
decision, failing closed on anything malformed. `Decision`, `PolicyAction` and `PolicyActor` are the
vocabulary of `PolicyInput`.

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

Also exported: `AuditStore` (the protocol a storage backend implements), `AuditError`,
`canonical_json(value)` (the deterministic serialisation that gets hashed) and `genesis_hash(tenant)`
(the `prev_hash` of a tenant's first record).

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

Errors, each with a stable `.code`: `ApprovalNotFoundError` (unknown request, or another tenant's:
reported identically), `ApprovalNotPendingError` (already decided), `ApprovalExpiredError`,
`ApprovalNotAuthorisedError` (wrong tenant, tier too low, or the requester itself),
`ApprovalSignoffError` (explicit sign-off code missing or wrong) and `ApprovalNotUsableError` (the
request cannot back this execution). `ApprovalStatus` is `PENDING`, `APPROVED`, `REJECTED`,
`EXPIRED` or `CONSUMED`. `sanitize_for_display(text)` strips control characters from model-derived
text before a human sees it.

### `keelgate.loop`

| Symbol | Kind | Notes |
|---|---|---|
| `Loop(gateway=, registry=, planner=, checkpoints=, grant_token=, policy_context=, stop=, verifier=, pricing=, price_model=, confirmer=, ...)` | class | `await loop.run(goal=, tenant_id=, agent_id=, as_of=, run_id=)`, `resume(tenant_id, run_id, stop=)`, `run_or_resume(...)`, `reconcile(...)` |
| `StopConditions(max_iterations, max_tokens, max_dollars, timeout, max_verifier_rejections, goal)` | dataclass | Every limit is optional; the first reached wins. A stop is not a failure |
| `StopReason`, `LoopResult` | types | `result.ok`, `result.resumable`, `result.stop_reason`, `result.final_answer`. Reasons include `goal_reached`, `max_iterations`, `token_budget`, `dollar_budget`, `timeout`, `verifier_rejections`, `outcome_unknown`, `cost_unknown`, `approval_pending`, `error` |
| `Planner`, `LLMPlanner`, `Verifier`, `AcceptAllVerifier`, `CallableVerifier`, `LLMVerifier` | roles | The model proposes plans and verdicts; neither can execute anything |
| `OutcomeConfirmer` | protocol | Optional, trusted harness code that asks the downstream system of record whether an unknown-outcome WRITE took effect. Only a definite `True` or `False` settles it; `None`, an error or a non-bool leaves it for a human |
| `CheckpointStore`, `InMemoryCheckpointStore`, `SqliteCheckpointStore`, `StaleCheckpointError`, `default_checkpointer` | stores | Atomic save; a sequence that is not newer is refused |
| `keelgate.loop.langgraph_store.LangGraphCheckpointStore` | store | Extra `keelgate[langgraph]`. Safe between threads and, with `.sqlite(path)` on a file, between processes (a lock file guards the stale-writer check). With a saver you build yourself, pass `process_lock=` or it is thread-safe only |
| `LoopRunner`, `LoopSpec`, `LoopOutcome`, `InProcessRunner` | interface | How a run is (re)started; the Temporal adapter implements it |
| `VerificationLoop`, `MonitorLoop`, `Schedule`, `LoopType` | kinds | Verification and monitor loops are READ-only by default, enforced by the gateway |

Guarantees: planned actions are checkpointed **before** they run; resume never
re-asks the model and never re-executes a completed WRITE; a WRITE whose outcome
is unknown stops the loop for a human (or for a confirmer's definite answer). A
model call whose *input alone* would cross the token budget (or, with `pricing=`
and `price_model=`, the dollar budget) is refused before it is paid for; the
output of a call cannot be known in advance, so a call can still overshoot by
its output. See
[ADR-0004](adr/0004-durable-loop-and-resume-semantics.md).

The loop's data model: `LoopState` is everything a run needs to resume (what is checkpointed);
`PlannedAction` and `ActionOutcome` record each tool call and its result; `ActionStatus` is
`PENDING`, `DONE`, `DENIED`, `ERROR`, `AWAITING_APPROVAL`, `UNKNOWN` or `ABANDONED`; `Phase` is
`PLAN`, `ACT`, `OBSERVE`, `VERIFY` or `DONE`; `LoopType` is `TASK`, `VERIFICATION` or `MONITOR`.
The planner is handed a `PlanRequest` and returns a `Plan` of `ProposedAction`s or a final answer;
the verifier is handed a `VerifyRequest` and returns a `Verdict` whose `VerdictDecision` is
`ACCEPT`, `REVISE` or `REJECT`. `ContextSource` supplies extra, untrusted context for a plan step,
`GoalPredicate` is the type of `StopConditions.goal`, and `DEFAULT_SYSTEM_PROMPT` is the loop's
default prompt. Starting a run id that exists raises `RunExistsError`; resuming one that does not
raises `RunNotFoundError`. Provisional: `MonitorSummary` and `READ_ONLY` (the side-effect set a
read-only loop uses), and for replay `Recording`, `RecordedStep`, `RecordedAction`, `ReplayReport`,
`Divergence`, `diff`, `find_run` and `NotReplayableError`.

### `keelgate.context`

| Symbol | Kind | Notes |
|---|---|---|
| `ContextBuilder(as_of=, token_budget=, ...)` | class | `add` / `try_add` / `await build()`. **Rejects** any item with `published_at > as_of`, and any outside item with no publication time |
| `ContextItem`, `ItemKind`, `Trust`, `Provenance` | models | Provenance on every item. Only harness-authored kinds can be `TRUSTED`, enforced by type |
| `BuiltContext`, `Rejection`, `RejectionReason` | results | Rejections record a hash of the content, never the content |
| `StructuredSummary`, `Compaction`, `ExtractiveSummarizer`, `LLMSummarizer`, `RecordStore`, `InMemoryRecordStore` | compaction | Over-budget context is compacted into structured summaries that carry pointers to the full records |
| `UNTRUSTED_NOTICE`, fenced `<untrusted ...>` blocks | rendering | Outside text is fenced and cannot break out of its fence |

Errors, all subclasses of `ContextError`: `AsOfViolationError` (an item published after `as_of`),
`UndatedItemError` (an outside item with no publication time), `DuplicateItemError` and
`ContextBudgetError` (trusted content alone does not fit the budget). `Summarizer` and
`TokenCounter` are the protocols you implement to change how context is compacted and counted;
`ApproxTokenCounter` is the default (about four characters a token). `RejectionReason` is
`AS_OF_VIOLATION`, `UNDATED` or `DUPLICATE`; `FENCE_TOKENS` is the token overhead charged per fenced
item and `HARNESS_KINDS` the item kinds only the harness may mark trusted.

### `keelgate.memory`

| Symbol | Kind | Notes |
|---|---|---|
| `Memory`, `MemoryTier` | protocol, enum | One interface over four tiers |
| `WorkingMemory`, `EpisodicMemory`, `SemanticMemory`, `ProceduralMemory` | tiers | Working (per run), episodic (decisions + outcomes), semantic (facts with `valid_from`/`valid_to`, vector search), procedural (versioned skills and playbooks) |
| `MemoryRecord`, `Attribution` | models | Every write is a **new version** attributed to an agent and a trace id; nothing is edited in place |
| `SqliteMemoryBackend`, `PostgresMemoryBackend` | backends | Postgres uses pgvector. Bitemporal: `recorded_at` is assigned by the store's clock |
| `Embedder`, `HashEmbedder` | embedding | `HashEmbedder` is a deterministic **lexical** stand-in for tests; it is not semantic |
| `OllamaEmbedder`, `OpenAICompatibleEmbedder`, `EmbeddingError` | embedding | Real embedding models over `httpx` (Ollama, OpenAI, vLLM and compatible servers). `dim` is required and every response is checked against it; a mismatch is an error, never a truncation |

Guarantees: reads honour `as_of` on both axes (valid time and record time);
every operation is tenant-scoped; retrieved memory reaches a prompt only as an
`UNTRUSTED` context item.

Errors, all subclasses of `MemoryStoreError`: `RecordNotFoundError`, `ConcurrentWriteError` (another
writer created the next version first) and `InvalidMemoryWriteError` (malformed for its tier).
`MemoryBackend` is the protocol a storage backend implements; `cosine(a, b)` is the similarity
function the shipped backends use.

### `keelgate.llm`

| Symbol | Kind | Notes |
|---|---|---|
| `LLMClient` | protocol | `name` and `async complete(LLMRequest) -> LLMResponse`. Provider logic never leaves this package |
| `LLMRequest`, `LLMResponse`, `Message`, `ToolCall`, `ToolSchema`, `Usage`, `Role`, `FinishReason` | models | A model's reply is a proposal; arguments are untrusted |
| `PricingTable`, `ModelPrice` | pricing | **No default prices.** An unpriced model has `cost_usd = None` |
| `LLMError`, `LLMAuthError`, `LLMRateLimitError` | exceptions | `error.retryable` says whether a retry can help |
| `keelgate.llm.providers.AnthropicClient`, `OpenAIClient`, `GoogleClient`, `OllamaClient`, `VLLMClient` | clients | Optional SDKs are imported lazily. Contract-tested through the real SDKs over a mocked transport. Opt-in live smoke tests exist (`make test-live`) but have **not been run** by the project yet. `LLMRequest.temperature` is deliberately not sent to Anthropic, whose current API has no such parameter |

### `keelgate.testing`

`ScriptExhaustedError` is raised when the code under test makes more model calls than the script has
replies for, and `UnofferedToolError` when the script asks for a tool the request did not offer.

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
| `keelgate.adapters` | none | `GovernedToolset`: the one governed path every adapter uses; `UNTRUSTED_KEY`, the key tool output is returned under |
| `keelgate.adapters.langgraph` | `langgraph` | `governed_langchain_tools`, `KeelgateChatModel` (async only), `to_keelgate_messages` |
| `keelgate.adapters.openai_agents` | `openai` | `governed_function_tools`, `KeelgateModel` (non-streaming), `to_keelgate_messages` |
| `keelgate.adapters.claude_agent_sdk` | `anthropic` | `governed_sdk_mcp_server`, `governed_claude_options`, `governed_tool_names`, `BUILTIN_TOOLS`: built-in tools off, other MCP servers off, settings off, `can_use_tool` allows only governed tools. Tested **at the boundary**: the CLI cannot run offline |
| `keelgate.adapters.mcp` | `mcp` | `GovernedMCPServer` (stdio and streamable HTTP) exposes governed tools. `GovernedMCPClient` governs *consumed* tools: allowlist (`ExternalToolMapping`), capability mapping, optional schema-digest pinning (`schema_digest`), local argument validation (`ExternalArgs`), untrusted output (`ExternalOutput`); `Discovery` reports what was registered, blocked or missing, and `ExternalToolError` a failed call |
| `keelgate.adapters.a2a` | `a2a` | `build_agent_card`, `build_a2a_app`, `GovernedA2AExecutor`, `loop_task_runner`, `TaskRequest` / `TaskResult`, `make_intake_tool` / `INTAKE_TOOL` / `IntakeIn` / `IntakeOut`. Task intake is a governed PROPOSE tool (`a2a_task_intake`, capability `a2a:task_submit`); remote text is untrusted and never becomes the goal |
| `keelgate.adapters.temporal` | `temporal` | `KeelgateLoopWorkflow`, `TemporalRunner`, `build_worker`, `LoopActivities`, `WorkflowOptions`, `ACTIVITY_NAME`, `DEFAULT_TASK_QUEUE`: the loop as a Temporal activity |

### `keelgate.telemetry`

| Symbol | Kind | Notes |
|---|---|---|
| `instrument(service_name=, endpoint=, exporter=, redact=, capture_content=, ...)` | function | Configures tracing and activates it. OTLP/HTTP by default (`keelgate[otlp]`). Returns a `Telemetry`; call `.shutdown()` to flush |
| `Telemetry`, `activate`, `active`, `use` | handle | The active tracer, meter, cost tracker and content-capture switch. Defaults to the application's global providers, so un-instrumented use is a no-op |
| `span`, `traced`, `bind_run`, `run_span` | helpers | Emit a span (errors record the exception **type** only); bind tenant, agent and run to everything inside a block |
| `InstrumentedLLM(client)` | wrapper | Each `complete` call becomes a GenAI `chat` span with tokens and cost |
| `CostTracker`, `CostTotals` | cost | Per-tenant, per-agent tokens and dollars, plus OTel metrics. An unpriced model adds tokens, not dollars |
| `Redactor`, `RedactingSpanProcessor` | redaction | Exporters receive a redacted copy of each span. Pattern-based; see the security model |
| `RunContext`, `current_run()` | context | The tenant, agent and run bound by the surrounding loop; `None` outside a run |
| `RunSpan`, `set_attributes`, `redact_span`, `UNATTRIBUTED`, `attributes` | helpers | The loop's root span and its ids; set span attributes safely; redact one finished span; the cost bucket for calls outside a run; the attribute-name constants (provisional) |

One trace per loop run: the run's `trace_id` is its OpenTelemetry trace id, and a resumed run
continues the same trace. Spans never carry tool arguments, tool output, prompts or exception
messages. See [ADR-0005](adr/0005-telemetry-replay-and-evals.md).

### `keelgate.loop` replay (added in K3)

| Symbol | Kind | Notes |
|---|---|---|
| `Recording.from_store(store, tenant, trace_id=)` | classmethod | Rebuilds a run from checkpoint history; tenant-scoped |
| `replay(recording)` | async function | Replays with recorded tool outputs, runs nothing, returns a `ReplayReport` (`identical`, `divergences`) |
| `HistoryCheckpointStore` | protocol | A store that keeps every checkpoint: in-memory, SQLite and the LangGraph store do |
| `GroundedAnswerVerifier` | verifier | Deterministic: figures must be supported by tool results; claims of action need a completed write |

### `keelgate.evals`

| Symbol | Kind | Notes |
|---|---|---|
| `OutcomeMetric` | protocol | `name`, `higher_is_better`, `compute(records) -> MetricResult`. **This is how downstream projects plug in metrics** |
| `OutcomeRecord`, `MetricResult` | models | `predicted` and `realized` are free-form mappings whose keys the metric defines |
| `discover_metrics()`, `ENTRY_POINT_GROUP` | discovery | Entry-point group `keelgate.outcome_metrics`; a class, factory or instance. A broken plugin is reported and skipped |
| `run_suite(name)`, `run_suites(names)` | async functions | Suites: `unit`, `trajectory`, `redteam`, `outcome`. Scripted model by default |
| `EvalReport`, `SuiteResult`, `CaseResult`, `EvalContext` | types | `report.passed` is false on any failing case or regression |
| `to_dict`, `to_html`, `to_markdown`, `baseline_of`, `compare` | reports | JSON, a self-contained HTML page, a CI summary, and baseline comparison |
| `keelgate eval run / list`, `keelgate replay` | CLI | Exit 0 passed, 1 failed or regressed, 2 usage error |
| `load_records`, `sample_records`, `run_metrics`, `run_outcome`, `LoadedMetric`, `AccuracyMetric`, `SUITES`, `EvalStack`, `write_json`, `write_html`, `write_markdown` | helpers | Read outcome records from JSON lines; run the metrics over them; the built-in accuracy metric; the suite names; the isolated stack the red-team cases run against; write a report to disk. All provisional |

The red-team case ids (`TI-01`, `CE-06`, ...) are used by baselines. Adding cases is a minor
change; renaming or removing one makes a baseline report it as missing.

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
| `keelgate[otlp]` | OTLP/HTTP trace export (Jaeger, Phoenix) |
| `keelgate[cedar]` | `CedarEngine` |
| `keelgate[server]` | FastAPI approvals API, Postgres audit store, Redis |

Ollama and vLLM need no extra (plain `httpx`; vLLM reuses the OpenAI wire format
through `keelgate[openai]`). Every adapter symbol is in the [API contract](api-contract.md) and is
marked provisional, because the framework each one wraps is still moving.

## End to end: using Keelgate from another library

A downstream library brings its own **tools**, its own **policy**, its own **metric**, and a small
**harness** that wires them into a governed, budgeted, resumable, replayable agent. This is the
whole of it, importing only the public API. It is
[`examples/use_keelgate_from_another_library.py`][ex], run by the contract tests on every change.

[ex]: https://github.com/anilatambharii/keelgate/blob/main/examples/use_keelgate_from_another_library.py

```python
--8<-- "examples/use_keelgate_from_another_library.py"
```

## Guarantees that outlive any signature

Even where the API changes, these properties hold. Breaking one is a security
bug, not a version bump:

1. A `WRITE` tool cannot execute without a policy decision.
2. A capability that was never granted cannot be exercised.
3. Nothing published after `as_of` enters context or memory.
4. A tampered audit chain fails `verify_chain()`.
5. No tenant can observe or affect another tenant.
6. There is no live execution path: only `paper` and `simulation`.
