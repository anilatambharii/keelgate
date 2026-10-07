# ADR-0005: Telemetry, replay and evals

- **Status:** Accepted
- **Date:** 2026-10-07
- **Phase:** K3

## Context

A safety harness is only as good as your ability to see what it did, reproduce it, and prove it
still works. Three needs follow:

1. **See it.** Every run must be inspectable in standard tooling, including what policy decided,
   what the model cost, and who it cost it for. But traces are exported to third parties, and the
   things most worth recording (prompts, arguments, tool output) are untrusted and often sensitive.
2. **Reproduce it.** When a run did something surprising, rebuild it from its trace id without
   re-running anything that has side effects.
3. **Keep proving it.** A red-team suite that always reports "blocked" is theatre. The checks must
   be able to fail, they must run on every change, and getting worse must break the build.

## Decision

### Telemetry

- **One trace per run, and the trace id is the run's id.** The loop opens a root span
  (`invoke_agent`) and adopts its OpenTelemetry trace id as `LoopState.trace_id`. A resume, in any
  process and any time later, parents its root on the original root span, so the whole life of a
  run (including a crash and restart) is one trace. This is verified against a real Jaeger.
- **GenAI semantic conventions for names.** `gen_ai.*` attribute names come from the
  `opentelemetry-semantic-conventions` incubating module rather than being retyped, so they cannot
  drift by typo. Those conventions are still marked *development* upstream; this is stated in the
  module and the contract treats attribute names as provisional.
- **Spans carry identifiers, verdicts, counts and a hash. Never content.** The tool span has the
  tool name, call id, status, error code, side effect, capability, a SHA-256 of the arguments, and
  the policy effect. It never has arguments, outputs or prompts. Exception *messages* are never
  attached either (only the exception type), because messages routinely echo input.
  `capture_content` is the one opt-in for prompts and responses, off by default.
- **Redaction wraps the exporter's input, not the live span.** `RedactingSpanProcessor` gives the
  delegate a redacted *copy* of each finished span, so span identity (and so trace joining) is
  preserved and nothing downstream ever sees the original text. It is pattern-based and says so.
- **Cost is attributed where the call is made.** `InstrumentedLLM` records tokens and cost against
  the tenant and agent bound by the running loop. An unpriced model contributes tokens but no
  dollars; Keelgate still never invents a price.
- **No global provider unless asked.** Keelgate talks to an *active* `Telemetry` handle that
  defaults to the application's global providers, so un-instrumented use is a cheap no-op and tests
  can swap in an in-memory exporter without fighting OpenTelemetry's set-once rule.
- **Export is OTLP/HTTP.** gRPC is not shipped; asking for it is an explicit error rather than a
  silent fallback. Jaeger is verified end to end; Phoenix and others should work because they speak
  OTLP, but are not verified.

### Replay

- A run is rebuilt from its **checkpoint history** (found by trace id, within one tenant).
- `replay()` runs a fresh loop whose planner, verifier and gateway are the recording, so a replay has
  no side effects and needs no grant or policy engine, then rebuilds the replayed run and diffs it
  against the original step by step. Divergence means the loop's own behaviour changed.
- Runs left waiting for approval, with an unknown outcome, or with a saved-but-unrun plan have no
  recorded result and are refused rather than guessed at.
- Replay does **not** re-ask a model or re-evaluate policy against today's rules. It answers "does
  the loop still do what it did?", not "would a new model choose the same?" (that is evals' job).

### Evals

- **Assume the model is fooled.** Every red-team case gives the model a script in which it *obeys*
  the hostile content, then asks whether the harness still blocks the harm. That is the project's
  premise, and it makes the suite deterministic and free enough to run on every PR.
- **A case must reach the defence.** In scripted mode each case also asserts that the harmful
  proposal really was made. A case that never gets as far as the gate would pass for the wrong
  reason, so it fails instead.
- **Mutation tests prove the suite can fail.** Tests remove the policy gate, fence escaping, the
  `as_of` check, the read-only restriction and grant expiry, and require the matching cases to
  start failing. Every attack category is shown to have a case that can fail.
- **The verifier is the subject of the trajectory evals.** A deterministic
  `GroundedAnswerVerifier` (figures must be supported by tool results; claims of action need a
  completed write) is tested against seeded faults and clean runs. This caught a real false
  positive in its own first draft ("last *traded* at 187.25" is not a claim of action).
- **Outcome metrics are a plugin point.** `OutcomeMetric` implementations register under the
  `keelgate.outcome_metrics` entry-point group; Tycheon contributes financial metrics this way. A
  plugin that fails to load or compute is reported (error type only) and does not stop the others.
- **Regressions break the build.** A committed baseline records passing cases and metric values;
  any case that stops passing, disappears, or any metric that gets worse (beyond a tolerance) fails
  the run. The red-team verdict never has slack.
- **Live mode shares the assertions.** The same cases run against a real model nightly. The model
  may decline an attack on its own (recorded as `model_declined`); the harness assertions are
  identical.

## Consequences

- **Good:** a run is a single trace across crashes, with the policy decision visible, and no
  sensitive content in it by default.
- **Good:** the suite measures the harness, not the model, so it is stable enough to gate a PR.
- **Cost:** 36 hand-written cases cover what we thought of. They are not a fuzzer and not a proof.
- **Cost:** replay needs a store that keeps history (SQLite, in-memory, LangGraph).
- **Risk:** plugins run in-process with full authority. Install only code you trust.
- **Finding recorded:** an exact-match restricted-symbol *denylist* is bypassable with a look-alike
  character (case TI-06). The eval stack's order tool validates tickers as ASCII at the schema
  level, which is the right place; production tools should do the same and prefer an allowlist.

## Alternatives considered

- **Record prompts and outputs in spans by default.** Richer, and it would put untrusted and
  sensitive text in every backend. Rejected; opt-in with redaction instead.
- **Mutate the live span to redact.** Breaks span identity in some SDK paths. Rejected for a copy.
- **A custom trace id generator.** Only works when Keelgate builds the provider. Adopting the root
  span's id works with any provider.
- **LLM-judged red-team verdicts.** Non-deterministic and gameable. Rejected; verdicts are
  effect-level (did anything unauthorised happen).
