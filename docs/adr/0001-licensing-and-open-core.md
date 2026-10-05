# ADR-0001: Licensing and open core

- **Status:** Accepted
- **Date:** 2026-10-04
- **Deciders:** Keelgate maintainers
- **Supersedes:** —

## Context

Keelgate ships as two products from one repository: an open-source agent harness
and a paid cloud service. That arrangement has to be settled before any code is
written, because the licence boundary determines where files may live, what
contributors are agreeing to, and what a downstream project such as Tycheon can
safely depend on. Retrofitting a licence decision is expensive and sometimes
impossible once third-party contributions exist.

Three things pull on the decision:

1. **Adoption.** Keelgate is a safety control. Nobody adopts a safety control
   they cannot read, and the harness is most useful when it is embedded in other
   people's agents. That argues for a permissive licence and for the harness
   being genuinely complete on its own.
2. **A viable business.** The cloud product — a hosted control plane, an approval
   dashboard, multi-tenant policy management, metered billing — is what funds
   continued work on the harness.
3. **Trust.** A safety project that quietly moves controls behind a paywall
   loses the argument for its own existence. Users must be able to verify that
   the enforcement path is the open one.

Options considered:

- **Apache-2.0 throughout, no paid product.** Maximum adoption, no funding.
- **A copyleft licence (AGPL) with a commercial exception.** Protects against
  hosted competitors, but AGPL in the dependency graph is a blocker at exactly
  the regulated institutions Keelgate targets. It would prevent Tycheon and
  others from embedding the harness freely.
- **A source-available licence (BSL, SSPL, Elastic).** Protects revenue, but is
  not open source, and a "safety harness you may not use commercially without
  terms" undercuts adoption and credibility.
- **Open core: Apache-2.0 harness, proprietary cloud.** Conventional, legible to
  enterprise legal teams, and keeps the enforcement path open.

## Decision

**Apache-2.0 for the harness; proprietary for `ee/`.**

1. Everything in this repository is licensed **Apache-2.0** (`LICENSE` at the
   root) **except** the `ee/` directory.
2. `ee/` — Keelgate Cloud's control plane and dashboard — is **proprietary**,
   carrying its own `ee/LICENSE`. It lives in this repository for development
   convenience and so that customers can read it for security review.
3. Apache-2.0 specifically, rather than MIT or BSD, for its **express patent
   grant** and its `NOTICE` conventions. Regulated adopters ask about patents.
4. The boundary is a **directory boundary**, enforced by review.

### Boundary rules

These are the rules a reviewer applies, and they are the operative part of this
ADR:

- **Every safety-relevant control is Apache-2.0.** Capability scoping, the
  policy gate, `as_of` enforcement, the audit chain and its verifier, approval
  semantics, and tenant isolation primitives live outside `ee/`. Without
  exception.
- **`ee/` may depend on the OSS harness. The harness may never depend on
  `ee/`.** A one-way dependency, verifiable by import.
- **The OSS harness is complete and useful alone.** No deliberately missing
  piece, no stub whose only implementation is in `ee/`.
- **`ee/` sells operation, not permission.** Hosting, multi-tenant management,
  dashboards, SSO, long-term retention, billing, support. It never sells the
  right to a safety property.
- **`verify_chain()` is Apache-2.0, always.** A customer must be able to verify
  a Cloud-written audit chain with open-source code they control. Putting
  verification behind the paywall would make the audit trail worthless.

### Contributions

Contributions are accepted under Apache-2.0 §5 — inbound equals outbound — with
no CLA for now. Contributions to `ee/` are not accepted from outside the
maintainers, since they cannot be relicensed into a proprietary product. If a
CLA or DCO becomes necessary (for example to relicense or to accept substantial
corporate contributions), that will be a separate ADR, and it will not be applied
retroactively.

## Consequences

### Good

- Adopters and auditors can read and verify every control that matters.
- Tycheon and other downstream projects embed the harness with no licence
  friction and no copyleft in their dependency graph.
- The Apache patent grant answers a question enterprise legal teams always ask.
- The business model is legible: pay for operating it, not for being safe.
- The boundary is mechanical, so review decisions are cheap.

### Bad, and accepted

- **A competitor may host Keelgate OSS.** Apache-2.0 permits it. We accept the
  risk: the moat is the hosted product, the policy packs and the eval corpus,
  not the licence.
- **`ee/` contributions are maintainer-only**, which narrows who can help on the
  cloud product.
- **Mixed-licence repositories confuse people.** Mitigated by the directory
  boundary, `ee/LICENSE`, and a licensing section in the README.
- **The temptation to move a control into `ee/` will recur**, most likely when a
  large customer asks. The boundary rules above exist to make saying no the
  default, and changing them requires superseding this ADR in public.

### Follow-ups

- `ee/LICENSE` is a placeholder pending final commercial terms and counsel
  review. Its *scope* is authoritative; its *wording* is provisional.
- Trademark policy for the Keelgate name is not addressed here and needs its own
  decision.
- A `NOTICE` file will be added when the project first redistributes third-party
  Apache-2.0 code that requires attribution.
