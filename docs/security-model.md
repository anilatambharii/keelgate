# Security model

This page states what Keelgate defends, against whom, how, and what it does **not**
yet defend. Where a control is implemented it links to the test that proves it.
Where it is missing it says so under [Known gaps](#known-gaps).

## Assumptions

1. **The model is untrusted.** Not malicious by default, but steerable. Its output
   is a *proposal*, never an instruction to the harness.
2. **Tool output is untrusted.** Anything returned by a tool, a web page, a document
   or a memory lookup can carry attacker-written text.
3. **Prompts are not a security boundary.** No instruction hardening counts as a
   control. If a property matters, deterministic code enforces it.
4. **The harness code and its configuration are trusted.** Keelgate enforces the
   policy pack, grants and limits it is given. It does not defend against a
   malicious operator or a malicious policy author.
5. **The host, the database and the signing key are trusted** to the extent that
   an attacker who holds them can defeat the audit chain (see the gaps).

## The decision path

```mermaid
flowchart TB
    subgraph untrusted["Untrusted"]
        M["Model proposal"]
        T["Tool output, web, documents"]
    end

    M --> G1["1. Verify signed grant (PASETO v4.public)"]
    G1 --> G2["2. Tenant matches the harness-asserted tenant"]
    G2 --> G3["3. Grant names the tool's capability exactly"]
    G3 --> G4["4. Execution mode is paper or simulation"]
    G4 --> G5["5. Arguments validate against the input schema"]
    G5 --> G6["6. Budget reserved"]
    G6 --> G7["7. Idempotency: exact repeat replays, conflict refuses"]
    G7 --> P{"8. Policy engine<br/>(READ skips this step)"}
    P -->|ALLOW| X["9. Execute under timeout"]
    P -->|DENY| R["Refused"]
    P -->|REQUIRE_APPROVAL| H["Park for a human"]
    H -->|"approved, bound to these exact arguments, single use"| X
    X --> A["Audit: result, hash-chained"]
    R --> A
    X -->|"output wrapped as Untrusted"| T
```

Every gate fails closed, and **the audit record is written before the side
effect**. If the audit log cannot be written, nothing runs.

The policy engine's input is built by harness code. The model contributes only
the *arguments*, which are schema-validated and from which a tool-supplied
function derives the facts policy needs (`symbol`, `notional`). The model never
supplies the policy context (limits, exposure, mode, `as_of`).

## STRIDE

| # | Threat | Control | Evidence | Residual risk |
|---|---|---|---|---|
| **S**poofing | | | | |
| S1 | Forged or altered grant | PASETO `v4.public`; asymmetric, so verifiers cannot mint; purpose bound by an implicit assertion; `kid` cross-checked; lifetime capped even when correctly signed | `test_capabilities.py` (tamper, wrong key, cross-purpose, forged claims) | Bearer token: theft means use until expiry. No sender binding. |
| S2 | Stolen or replayed grant | Hard expiry (default max 24 h), `nbf`, revocation list | expiry boundary and revocation tests | Revocation list is **process-local** (gap G1) |
| S3 | Caller claims another tenant | Tenant must equal the grant's tenant, else `not_authorised`, nothing written to the other tenant | `test_a_grant_for_another_tenant_is_refused` | Trusts the harness to derive `tenant_id` from an authenticated session |
| S4 | Impersonating an approver | REST: bearer tokens, constant-time compare against all tokens; tenant comes from the token, never the request | `test_rest_*` | Static tokens are dev-grade; no SSO/MFA (gap G6) |
| S5 | Agent approves its own request | Separation of duties in the queue | `test_a_requester_cannot_approve_its_own_request` | A colluding human approver is out of scope |
| **T**ampering | | | | |
| T1 | Edit or reorder audit records | SHA-256 chain per tenant over exact payload text; append-only triggers; pure `verify_chain()` | `test_audit.py` tamper suite (edit, delete, reorder, splice, duplicate) | Someone with DDL rights can drop triggers and rewrite the **whole** chain (gap G2) |
| T2 | Truncate the audit tail | `head()` can be anchored externally; `verify_chain(expected_head=...)` detects removal and rewrite | anchor tests | Needs an anchor the writer cannot reach; not automated yet (gap G2) |
| T3 | Swap arguments after approval | Approval bound to `SHA-256(tool, canonical args)`; single use | `test_an_approval_cannot_authorise_different_arguments` | none known |
| T4 | Alter the policy | Every decision records the hash of the policy sources the engine actually has loaded | conformance test: disk hash equals the hash OPA reports | Does not stop a *bad* policy being deployed; needs review and signing |
| T5 | Poison the policy context via the model | Context is harness-supplied only; arguments cannot carry it | `test_prompt_injection.py` | Harness bugs that copy model data into context |
| T6 | Postgres JSONB reorders audit payloads | Payload stored as `TEXT`, hashed as the exact string | golden-vector and Postgres tests | none known |
| **R**epudiation | | | | |
| R1 | "The agent never did that" | `tool.call`, `policy.decision`, `tool.result` share a `call_id`; actor is the verified agent; written before the effect | `test_the_audit_record_exists_before_the_side_effect` | Args are logged unredacted (gap G7) |
| R2 | "I never approved that" | `approval.decided` records approver id, tier, args hash, evidence hash | `test_every_state_change_lands_in_the_audit_chain` | The CLI trusts the operator-supplied identity (gap G6) |
| **I**nformation disclosure | | | | |
| I1 | Probing the gate for internals | Fixed-wording errors; auth failures are indistinguishable (`Authorisation failed.`); capability names not revealed | `test_authorisation_failures_give_the_model_no_reason` | Policy reasons are shown to the model by design (see below) |
| I2 | Model text echoed into reasons | Pack rule: reasons never echo model-supplied strings | `test_reasons_never_echo_model_supplied_strings` (both engines) | A future pack author could break the rule; review item |
| I3 | Cross-tenant reads | Every query is tenant-scoped; another tenant's request is *not found*, not *forbidden* | `test_requests_are_invisible_across_tenants` | none known |
| I4 | Terminal escapes in evidence | Control characters stripped before display | `test_cli_strips_terminal_escapes_from_model_text` | A web UI must escape too; the REST API returns text as JSON |
| I5 | Secrets in logs | Tokens and keys hidden from `repr` | `test_token_and_private_key_do_not_leak_through_repr` | Exception text from tools is not returned to the model but is in process logs |
| **D**enial of service | | | | |
| D1 | Runaway cost | Per-grant budget; atomic reserve, released on any non-execution | `test_spend_never_exceeds_the_grant_budget` (property) | In-memory ledger (gap G1) |
| D2 | Oversized input | 64 KB argument cap; 256 KB audit payload cap; large outputs hashed, not stored | gateway hardening tests | No request-rate limiting (gap G8) |
| D3 | Policy engine down | Fails **closed**: every WRITE and PROPOSE is denied | `test_opa_unreachable_denies` | This is an availability cost by design |
| D4 | Hung tool | Per-tool timeout; a timed-out WRITE is parked `UNKNOWN`, never auto-retried | `test_a_timed_out_write_is_never_retried_automatically` | A synchronous tool's thread cannot be killed (gap G9) |
| D5 | Approval queue flooding | Requests expire; pending list is tenant-scoped | expiry tests | No per-agent rate limit (gap G8) |
| **E**levation of privilege | | | | |
| E1 | Capability escalation | Exact string match; **no wildcards**; empty grants refused | `test_wildcards_and_malformed_capabilities_are_rejected` | none known |
| E2 | Running a WRITE without a decision | One choke point; `Tool` refuses direct calls; registry frozen at gateway build; final "cleared" check | `test_properties.py`: hypothesis over arbitrary call sequences, plus mutation checks that disable each gate | In-process Python cannot truly seal a function (gap G3) |
| E3 | Live money movement | Refused at the gateway before policy; denied again by the pack; constructor rejects any non-paper mode | `test_live_mode_is_refused_before_policy_is_even_asked` | v1 has no live path at all |
| E4 | Engine divergence causing a fail-open | One conformance table runs through both Rego engines; Cedar adapter rejects any verdict with evaluation errors | conformance and Cedar tests | New edge cases need new rows |
| E5 | Replaying an approval | Single use; atomic compare-and-swap on status | `test_an_approval_can_be_spent_exactly_once` | none known |
| E6 | Reusing an idempotency key for a different action | Key bound to the argument hash; mismatch refused | `test_reusing_a_key_with_different_arguments_is_refused` | In-memory store (gap G1) |
| E7 | Prompt injection changing an authorisation outcome | Outcomes are a function of (grant, trusted context, arguments) only | `test_injected_text_cannot_change_the_outcome_of_any_candidate_action` | See "What injection can still do" |

## K2: long-running agents, context, memory and adapters

K2 adds a durable loop, an `as_of` context firewall, layered memory, model
clients and framework adapters. Each one widens the attack surface, so each has
its own rows. The earlier STRIDE table still applies unchanged.

| # | Threat | Control | Evidence | Residual risk |
|---|---|---|---|---|
| L1 | A crash makes a WRITE run twice | Planned actions are checkpointed *before* they run; resume re-submits the saved arguments; durable idempotency keys | `test_a_crash_at_any_window_resumes_without_a_duplicate_write`, `test_a_killed_process_resumes_in_a_new_process_without_a_duplicate_write` | Exactly-once is a Keelgate-side record; the downstream system must honour the key (ADR-0004) |
| L2 | The model changes its mind after a restart | Resume never re-plans a saved plan | `test_a_model_that_would_say_something_different_cannot_change_a_saved_action` | none known |
| L3 | A WRITE dies mid-flight and is blindly retried | The key stays in flight across restarts; reads as *outcome unknown*; the loop stops for a human | `test_a_process_killed_inside_the_tool_body_is_never_retried_by_the_next_one`, `test_a_write_interrupted_mid_flight_stays_unrepeatable_after_a_restart` | Needs a human; an unattended agent stalls by design |
| L4 | A "read-only" loop quietly writes | The loop passes `allowed_side_effects` to the gateway, which refuses PROPOSE and WRITE tools even when the grant allows them | `test_a_verification_loop_cannot_write_even_if_the_model_asks_anyway`, `test_a_monitor_is_read_only_by_default_even_against_a_hostile_script` | none known |
| L5 | Runaway spend | Token, dollar, iteration and active-time stops; unknown price under a dollar budget stops | `test_an_unpriced_model_under_a_dollar_budget_stops_instead_of_spending_blind`, `test_a_token_budget_stops_before_acting_and_resume_does_not_replan` | A single model call can overshoot a budget before it is counted |
| C1 | Future information leaks into a decision | The context builder rejects any item published after `as_of`, and any outside item with no publication time | `test_an_item_published_after_as_of_is_refused`, `test_nothing_published_after_as_of_ever_reaches_the_prompt`, `test_a_tool_result_dated_after_as_of_never_reaches_the_model` | The harness must supply a truthful `published_at`; Keelgate cannot know a source lied |
| C2 | Outside text escapes into an instruction position | Trust is enforced by type; outside text is fenced and cannot close its own fence | `test_outside_text_can_never_be_marked_trusted`, `test_content_cannot_break_out_of_its_fence` | A model can still *choose* to obey fenced text; the gate, not the fence, is the control |
| C3 | Compaction launders untrusted text | A summary of untrusted text stays untrusted; summaries carry pointers to the full record and cannot be dated after `as_of` | `test_an_llm_summarizer_cannot_forge_or_omit_pointers`, `test_the_summary_is_never_dated_after_as_of` | none known |
| M1 | One tenant reads or alters another tenant memory | Every memory operation is tenant-scoped; identical keys in two tenants do not collide | `test_one_tenant_cannot_see_or_change_another_tenants_memory`, `test_search_never_crosses_tenants_however_similar` | Postgres row-level security is not enabled; scoping is in the queries |
| M2 | A back-dated memory rewrites what was known | Bitemporal and append-only: `recorded_at` comes from the store clock and is clamped | `test_a_retroactive_correction_does_not_rewrite_what_was_known_earlier`, `test_recorded_at_comes_from_the_store_clock_not_the_caller` | none known |
| M3 | Memory poisoning: stored text steers a later run | Retrieved memory is always an UNTRUSTED context item; every write is attributed (agent, trace id) and audited without content | `test_retrieved_memory_is_always_untrusted_context`, `test_every_write_is_audited_without_its_content` | A poisoned fact still reaches the model, labelled; attribution makes it traceable, not impossible |
| A1 | An adapter becomes a side door | All adapters call one `GovernedToolset`; the Claude Agent SDK adapter switches off built-in tools and other MCP servers and denies anything ungoverned in `can_use_tool` | `test_the_options_confine_the_agent_to_the_governed_tools`, `test_the_security_relevant_options_cannot_be_overridden` | Verified at the boundary only: the Claude CLI cannot run offline here, so no live agent run was observed |
| A2 | A malicious external MCP server | Allowlist and capability mapping; optional schema-digest pinning (rug-pull defence); local argument validation; output untrusted | `test_only_allowlisted_tools_are_registered_and_the_rest_are_reported_blocked`, `test_a_pinned_schema_that_still_matches_is_accepted_and_one_that_changed_is_not` | Pinning is opt-in; an unpinned server can change its tools between sessions |
| A3 | An MCP caller exploits the shared server grant (confused deputy) | Per-request toolsets give each caller its own authority | `test_a_per_request_toolset_gives_each_caller_its_own_authority` | The default shares one grant; deployments must supply per-caller resolution on HTTP |
| A4 | A remote agent smuggles instructions or work through A2A | Intake is a governed PROPOSE tool (`a2a:task_submit`); remote text is an untrusted fenced item, never the goal; the executor never raises | `test_an_injection_in_the_request_is_passed_as_data_never_as_the_goal`, `test_a_remote_request_reaches_the_loop_only_as_an_untrusted_fenced_item` | A2A transport authentication is the deployment job; the card is unsigned |
| P1 | Provider clients mis-handle failures or hostile output | Auth failures are never retryable; broken tool-call JSON becomes an invalid-arguments refusal, not a crash; keys are read by the vendor SDK from the environment | `test_http_failures_map_to_keelgate_errors`, `test_broken_tool_arguments_become_empty_not_a_crash` | Not exercised against live services |

## What prompt injection can still do

Keelgate does not stop a model being talked into *proposing* something. It makes
sure a bad proposal cannot be *executed* beyond what the grant and the policy
allow. Within those bounds an injected instruction can still:

- steer the agent toward an **allowed but poor** action (a legal trade the human
  would not have wanted). Limits, approval tiers and the audit trail bound and
  document that; they do not eliminate it.
- put misleading text in the **evidence** shown to an approver. The evidence is
  sanitised and clearly model-derived, but a human can still be persuaded. Treat
  the rationale as untrusted input to a person.
- waste budget up to the grant's ceiling.

## Fail-closed behaviour

Policy engines return a decision or, on **any** error, a DENY. This includes a
crashed engine, an unreachable OPA, malformed or unknown results, NaN or
non-JSON input, and a pack that defines no decision.

Two upstream behaviours were found while building this and are neutralised:

- **Cedar skips a policy that errors and can still return Allow.** If a `forbid`
  references a missing attribute, Cedar ignores it. The adapter rejects any
  verdict that carries evaluation errors.
- **`not x in set` does not deny when `x` is undefined** in Rego. The pack states
  every condition positively (`mode_ok`, `market_open`, `limits_ok`) and denies on
  `not <positive rule>`. A unit test pins the case that failed open in the first
  draft.

## Known gaps

These are known and accepted, and listed so nobody assumes otherwise. G12 onward were added in K2.

| # | Gap | Impact | Planned |
|---|---|---|---|
| G1 | The default revocation list, budget ledger and idempotency store are **in-memory**. K2 adds SQLite versions that survive restarts and are safe on one host | Multi-host deployments still need shared (Postgres or Redis) implementations | Shared durable stores |
| G2 | The audit chain is not **externally anchored** automatically | A party who can rewrite the whole table undetectably forges history | Signed, periodically published heads |
| G3 | Python cannot hide a function body | Code in the same process that deliberately reaches into `Tool._fn` bypasses the gate | Out-of-process tool execution |
| G4 | Approvals are SQLite only | No shared approval queue across processes | Postgres backend |
| G5 | No key rotation tooling or HSM support | Operational burden | Cloud control plane |
| G6 | REST approver auth is a static token table; the CLI trusts the operator | Not production identity | OIDC/SSO in Cloud |
| G7 | Tool **arguments are written to the audit log unredacted** | Sensitive values could persist | Field-level redaction rules |
| G8 | No rate limiting on calls or approval requests | Abuse within budget | Gateway-level limits |
| G9 | A timed-out *synchronous* tool keeps running in its thread | Resource use; the effect may still land | Process isolation |
| G10 | The in-process Rego engine is a different implementation from OPA | Possible divergence | Conformance suite; use OPA in production |
| G11 | Cedar pack is a subset of `finance_basic` (no trading hours) | Feature gap | Port with precomputed time facts |
| G12 | The LangGraph checkpoint store is safe between threads but **not between processes** | Two processes driving one run could both save | Use `SqliteCheckpointStore` (primary-key enforced) where several processes may drive a run |
| G13 | Provider clients are tested through the real SDKs over a mocked transport, never live | Wire-format drift, or a vendor behaviour we did not anticipate, would not be caught | A live smoke test behind explicit credentials |
| G14 | The Claude Agent SDK adapter is verified at its boundary only | No live agent run was observed | A live run in an environment with the CLI |
| G15 | `HashEmbedder` is lexical, not semantic | Semantic recall is poor with it | Supply a real `Embedder` |
| G16 | A model call that crosses a token or dollar budget is already paid for | Overshoot by at most one call | Pre-call estimates |
| G17 | `OUTCOME_UNKNOWN` needs a human | Unattended agents stall | Optional downstream-confirm hooks |
| G18 | A2A cards are unsigned; transport auth is not provided | Impersonating an agent is possible without deployment-level auth | Signed cards, auth middleware |

## Secrets

Secrets come from the environment and are never committed. `.env` is gitignored
and `.env.example` documents every variable with an empty value; a test fails the
build if a secret-shaped key has a value. CI runs gitleaks over full history and
detect-secrets against a baseline. The only credentials in the repository are the
deliberately weak localhost placeholders in `docker-compose.dev.yml`.

## Reporting

See [SECURITY.md](https://github.com/anilatambharii/keelgate/blob/main/SECURITY.md).
Policy bypass, capability escalation, tenant leakage, `as_of` violations, audit
tampering and approval forgery are all in scope.
