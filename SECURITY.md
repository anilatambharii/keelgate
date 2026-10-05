# Security Policy

Keelgate exists to constrain AI agents that touch money. A vulnerability here is
a vulnerability in someone's controls, and we would much rather hear about it
from you.

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/anilatambharii/keelgate/security/advisories/new)
(repository, then *Security*, then *Advisories*, then *Report a vulnerability*).
If that is unavailable to you, contact the maintainer
[@anilatambharii](https://github.com/anilatambharii) and ask for a private
channel before sharing details.

Helpful reports include the affected version or commit, the configuration in
play (policy engine, adapters, extras), a minimal reproduction, and the impact
you believe it has.

## What to expect

| Stage | Target |
|---|---|
| Acknowledgement | within 3 business days |
| Initial assessment | within 10 business days |
| Fix or mitigation plan | communicated with the assessment |
| Public disclosure | coordinated; by default 90 days after the report, or on patch release, whichever is sooner |

We will credit you in the advisory unless you ask us not to.

## Supported versions

| Version | Supported |
|---|---|
| `0.1.x` (pre-release) | Yes — current development line |
| `< 0.1` | No |

Keelgate is pre-1.0. Until 1.0, fixes land on the development line rather than
as backports.

## In scope

Anything that lets an agent reach a side effect it should not have, or that
corrupts the record of what happened:

- **Policy bypass** — reaching a WRITE without a policy decision, or forcing `ALLOW`.
- **Prompt injection that changes a decision.** Injection that changes model
  *output* is expected and is where the threat model starts. Injection that
  changes an *authorisation outcome* is a vulnerability. Report it.
- **Capability escalation** — invoking a tool outside the granted scope.
- **Tenant isolation failures** — any cross-tenant read or write.
- **`as_of` violations** — data published after the cutoff entering context or memory.
- **Audit tampering** — mutating or reordering chained records while `verify_chain()` still passes.
- **Approval forgery** — satisfying a `REQUIRE_APPROVAL` gate without a genuine human decision.
- **Secret exposure** — credentials in logs, spans, audit records or error paths.

## Out of scope

- The contents of `ee/` as deployed by third parties outside our control.
- Findings that require already holding the signing key, the database, or host privileges.
- The deliberately weak local credentials in `docker-compose.dev.yml` and
  `.env.example`. That stack is localhost-only and is not a product.
- A model producing a wrong or unsafe *proposal*. That is the assumed baseline;
  Keelgate's job is to refuse to act on it. A proposal that gets *executed* is
  in scope.
- Missing hardening in modules documented as not yet implemented. See the
  project status in the README.

## Safe harbour

We will not pursue or support action against researchers acting in good faith:
test only against your own installations, do not access or modify other people's
data, avoid privacy violations and service degradation, and give us reasonable
time to fix before disclosing.

## A note on the threat model

Keelgate assumes the model is untrusted, its tool output is untrusted, and the
network is hostile. Safety properties are enforced by deterministic code, never
by prompt text. If you find a place where a prompt *is* the control, that is a
bug worth reporting. See [docs/security-model.md](docs/security-model.md).
