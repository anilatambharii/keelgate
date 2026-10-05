# ADR-0002: Policy engine: OPA/Rego by default, Cedar optional

- **Status:** Accepted
- **Date:** 2026-10-04
- **Deciders:** Keelgate maintainers
- **Supersedes:** none

## Context

Every WRITE must pass a deterministic policy gate, and "deterministic" has to mean
more than "written in code". The decision logic must be:

1. **reviewable by people who are not the engineers** (risk, compliance, audit);
2. **separable from the agent code**, so changing a limit is not a code release;
3. **testable on its own**, including its failure modes;
4. **attributable**: an audit record must say exactly which policy decided;
5. **fail-closed**: an engine that is down, confused, or handed garbage must deny.

The harness also has to run in three places: a developer laptop with nothing
installed, CI, and a production deployment next to other services.

## Options considered

| Option | For | Against |
|---|---|---|
| **Plain Python rules** | Zero dependencies, easy to start | Policy and agent code ship together; reviewers must read Python; easy to leak model data into a condition; no independent test story |
| **OPA / Rego** | Mature, CNCF graduated, policy-as-data, built-in unit testing (`opa test`), decision logs, bundles, a large ecosystem | Rego is unusual to read at first; undefined-versus-false semantics are a footgun (see below) |
| **Cedar** | Analyzable (it can prove properties about policies), clear permit/forbid model, designed to be readable | No floats; no time-zone arithmetic; only permit/deny, so approval needs a convention; errors in a policy are *skipped* |
| **Casbin** | Simple RBAC/ABAC models | Weaker fit for numeric limits and time rules |
| **OpenFGA / SpiceDB** | Relationship-based authorisation at scale | Solves "who may access what", not "may this order of this size go out now" |
| **Cerbos** | Pleasant YAML policies | A younger ecosystem and no built-in equivalent of `opa test` for arbitrary decision shapes |

## Decision

1. **One interface.** `PolicyEngine` is a protocol with a single method,
   `decide(PolicyInput) -> PolicyDecision`, returning `ALLOW`, `DENY` or
   `REQUIRE_APPROVAL` with reasons, an approval tier, and the **policy version
   hash**. The gateway knows nothing else about engines.
2. **OPA over HTTP is the default** (`OpaHttpEngine`). It is what production uses.
   Each decision is stamped with a hash of the policy sources *the server reports
   as loaded*, not of files on the client's disk.
3. **An in-process Rego engine** (`RegoEngine`, via `regopy`) evaluates the *same*
   `.rego` files with no server and no binary. It exists for tests, the
   quickstart and single-process use, so a fresh clone is productive immediately.
4. **Cedar is optional** (`CedarEngine`, extra `keelgate[cedar]`) behind the same
   interface, with a documented mapping onto three Cedar actions
   (`invoke`, `explicit_signoff`, `one_click`).
5. **Packs are data in `policies/`**, shipped inside the wheel. The first is
   `finance_basic`: paper-only enforcement, restricted list, per-action and daily
   limits, trading hours and approval tiers.
6. **Every failure is a DENY.** A crashed engine, an unreachable server, a
   timeout, a malformed result, an unknown effect, NaN in the input, a pack that
   defines no decision: all become `DENY` with `policy_version = "unavailable"`
   where the version cannot be attested.

### Rules for writing policy

These come from defects found while building the first pack and are binding for
every pack:

- **State conditions positively and deny on `not <positive rule>`.**
  In Rego, `not x in set` does **not** hold when `x` is undefined, so
  `deny if not input.mode in {"paper"}` *allowed* a request with no mode at all.
  The unit test that caught it is kept as a regression.
- **Reasons never echo model-supplied strings.** They go to humans and back to
  the model.
- **Anything not explicitly permitted is denied.** Unknown capabilities, unknown
  side effects and missing limits all deny.
- **Decide against `as_of`, never the wall clock**, so a replay decides the same way.

## Consequences

### Good

- Risk and compliance can review a pack without reading Python, and a limit
  change is a policy deploy, not a code release.
- The version hash in every audit record answers "which policy decided this?".
- Two independent Rego implementations run **one conformance table**
  (`tests/test_policy_conformance.py`), so drift between them is caught rather
  than assumed away. Cedar runs its own table against the same decisions.
- A contributor needs nothing but `uv` to run the full suite; OPA and Docker add
  coverage, they are not prerequisites.

### Bad, and accepted

- **Two Rego implementations can disagree.** Found already: the in-process engine
  rejects the `Z` suffix in `time.parse_rfc3339_ns` where OPA accepts it, and
  throws on an unknown time zone where OPA yields undefined. Mitigated by always
  rendering `as_of` as `+00:00`, by failing closed on any exception, and by the
  conformance table. **Production should use OPA.**
- **Cedar is a subset.** No float arithmetic (amounts are rounded *up* to whole
  units, so a cap is only ever hit early), no time-zone maths (trading hours are
  not ported), and approval is expressed by convention rather than natively.
- **Cedar skips policies that error**, so a `forbid` over a missing attribute is
  silently absent. The adapter rejects any verdict that carries evaluation errors.
  A test pins the raw upstream behaviour so we notice if it changes.
- **Rego has a learning curve.** The mitigation is the rules above, a heavily
  tested reference pack, and `opa fmt` / `opa check --strict` in CI.
- **OPA is another service to run.** It is already in the dev stack, and
  `RegoEngine` covers single-process deployments.

### Follow-ups

- Signed policy bundles, so a deployed pack can be verified against a signature
  and not only hashed after the fact.
- A Cedar port of the trading-hours rule once time facts are precomputed upstream.
- Policy decision-log shipping into the audit chain.
