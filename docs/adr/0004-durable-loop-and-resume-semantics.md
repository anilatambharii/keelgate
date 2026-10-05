# ADR-0004: Durable loop and resume semantics

- **Status:** Accepted
- **Date:** 2026-10-05
- **Phase:** K2

## Context

A long-running agent will be interrupted: a process is killed, a deploy rolls, a
budget runs out, a human takes an hour to approve something. The safety promise
("a WRITE never runs twice, never runs unapproved") must survive all of that,
not just the happy path. Two failure shapes matter most:

1. The process dies **after a WRITE ran but before the harness recorded it**.
   Re-running the step would place the order twice.
2. The process dies **in the middle of a WRITE**, so nobody knows whether it
   happened. Re-running might duplicate it; skipping might lose it.

Asking the model again after a restart is not an answer to either: a model may
propose something different the second time.

## Decision

### The loop

`plan -> act -> observe -> verify -> (revise | stop)`. Every phase boundary writes
a checkpoint, and every stop reason is explicit: `max_iterations`, token budget,
dollar budget, timeout, a goal predicate, a verifier-rejection limit. A stop for
budget is **not a failure**: the state is kept and a resume with a larger budget
continues from exactly where it stopped.

### Write-ahead of planned actions

The planner's chosen actions (tool, arguments, idempotency key) are
**checkpointed before any of them runs**. A resume re-submits the *same*
arguments from the checkpoint and never asks the model to plan again. A test
(`test_a_model_that_would_say_something_different_cannot_change_a_saved_action`)
proves that a model that would answer differently cannot alter a saved action.

### Idempotency is the second line, and it is durable

Each WRITE carries an idempotency key derived from its arguments. The key store
is durable (SQLite here, any shared store behind the same protocol). On resume:

- a key recorded as **completed** replays the stored result and runs nothing;
- a key left **in flight** by a process that died is read as
  `OUTCOME_UNKNOWN`. The loop **stops** and never auto-retries it. A human settles
  it with `Loop.reconcile(tenant, run, action, executed=..., by=...)`:
  `executed=True` records it as done, `executed=False` abandons it. Either way
  the decision is attributed and audited, and the loop then continues.

Failing to know is treated as a reason to stop, not to retry. This is the same
rule the gateway already applies to a timed-out WRITE (K1).

### Crash windows are named and tested

The loop exposes failpoints at `after_plan`, `before_action`,
`after_gateway_call`, `after_action_checkpoint`, `after_observe` and
`after_verify`. Tests crash at each window, at the 1st, 2nd and 3rd occurrence,
under in-process "restarts" (every in-memory object rebuilt over the same files),
under Hypothesis-generated crash sequences, and under **real process deaths**
(a worker subprocess calls `os._exit(137)` at the window or mid tool body, so
nothing is flushed). The assertion
is always the same: the WRITE's side effect count is exactly one.

### Budget semantics

- A model call that crosses a token or dollar budget is **accepted** (it has
  already been paid for) but **nothing further is acted on**. The plan is saved
  as pending, so a resume with a larger budget runs it without re-planning.
- The timeout counts **active** time only; time spent stopped (for example
  waiting for a human) does not consume it.
- A dollar budget with a model whose price is unknown stops with `COST_UNKNOWN`
  rather than spending unmetered. Keelgate ships **no default prices**.

### Checkpoint stores

`CheckpointStore` is a small protocol: `save` (atomic, refuses a sequence that is
not newer), `load`, `runs`. Implementations: in-memory (tests), SQLite (stdlib,
default), and a LangGraph-saver-backed store (the "LangGraph checkpointer
default" from AGENTS.md, when `keelgate[langgraph]` is installed). All three hold
the same JSON, so a run can move between them; one contract test runs against
all of them. The LangGraph store's stale-writer check is a read-then-write that
the saver API cannot make atomic: it is safe between threads (a lock) but **not
between processes**, where the SQLite store's primary key must be used. This is
documented in the class and listed as a known gap.

### The driver is pluggable

`LoopRunner` is a one-method interface over a serialisable `LoopSpec`. The
in-process runner just awaits the loop. The Temporal adapter runs the loop as an
**activity** inside a deterministic workflow: if a worker dies, Temporal retries
the activity, which calls `Loop.run_or_resume`, loads the checkpoint, and
continues. A budget stop is a normal result (`resumable=True`), so Temporal does
not retry it. Tested against a real local Temporal server, including an
attempt that "dies" right after the WRITE ran.

### Loop kinds

`task` (default), `verification` (READ-only, enforced), and scheduled
`monitor` loops (READ-only by default; each tick is its own run
`<id>.tick-<n>`). "READ-only" is not a prompt instruction: the loop passes
`allowed_side_effects` to the gateway, which refuses PROPOSE and WRITE tools
with `side_effect_not_permitted` even if the grant would allow them.

## Consequences

- **Good:** the "no duplicate WRITE" property is established by construction
  (write-ahead + durable idempotency) and by adversarial tests, not by hoping.
- **Good:** nothing about resume depends on the model being deterministic.
- **Cost:** an `OUTCOME_UNKNOWN` needs a human. That is intended, but it is an
  operational burden for unattended agents.
- **Cost:** durability needs shared stores. The defaults are single-host SQLite;
  multi-host deployments must supply shared implementations of the same protocols
  (Postgres is on the roadmap).
- **Limit:** exactly-once is a property of the *Keelgate-side* record. If an
  external system accepted a request and the tool crashed before returning, the
  tool's own idempotency (the key is passed through) is what prevents a
  duplicate downstream. Keelgate refuses to guess; it does not remove the need
  for the downstream system to honour the key.

## Amendments

Added after the first K2 review, without changing the decisions above:

- **Pre-call budget check.** Before each planning call the loop estimates the prompt's size
  and refuses a call whose *input alone* would cross the token budget; given a pricing table
  (`pricing=`, `price_model=`) it does the same for the dollar budget. The stopped step does
  not count as an iteration, and a resume with more budget retries it. Output remains
  unknowable, so a call can still overshoot by its output; cap it with the request's
  `max_tokens`.
- **Outcome confirmer.** `Loop(confirmer=...)` accepts trusted harness code that asks the
  downstream system of record whether an unknown-outcome WRITE took effect. A definite `True`
  settles it as done, a definite `False` abandons it (never retried under the same key);
  anything else leaves it for a human, exactly as before. The model's or the tool's own claim
  is never accepted as confirmation. Confirmer decisions and human reconciles are both written
  to the audit chain as `loop.reconciled` events (actor, source, verdict; never arguments).
- **LangGraph store across processes.** `.sqlite(path)` now takes a cross-process lock (a
  separate lock file held under `BEGIN IMMEDIATE`) around the read-then-write stale check,
  proven by a multi-process race test that fails without the lock.

## Alternatives considered

- **Re-plan on resume.** Simpler, but a different plan could duplicate or skip a
  step. Rejected.
- **Make the whole loop a LangGraph graph.** Ties the core to one framework and
  conflicts with "wrap, don't compete". The checkpointer is used as a store only.
- **Temporal as the only durability layer.** Heavy for local use and tests;
  rejected as a default, kept as an optional driver.
- **Auto-retry unknown outcomes with the same key.** Safe only if the downstream
  system deduplicates, which Keelgate cannot verify. Rejected in favour of
  stop-and-ask.
