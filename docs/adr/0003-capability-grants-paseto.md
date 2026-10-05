# ADR-0003: Capability grants as PASETO v4.public tokens

- **Status:** Accepted
- **Date:** 2026-10-04
- **Deciders:** Keelgate maintainers
- **Supersedes:** none

## Context

An agent must hold an explicit, scoped, expiring authority for each thing it may
do. The authority has to be:

- **bound to one agent, one tenant, a closed set of capabilities and a budget**;
- **verifiable by a component that cannot mint it**, so compromising the gateway
  does not hand an attacker the ability to forge grants;
- **short-lived**, with a hard ceiling that holds even for a correctly signed token;
- **checkable offline**, with no database round trip on the hot path;
- **testable against time**, because expiry is a security property.

## Options considered

| Option | For | Against |
|---|---|---|
| **JWT (JWS)** | Ubiquitous, library support everywhere | Algorithm is attacker-influenced via the `alg` header; a long history of `none`, HS/RS confusion and key-confusion bugs. Safe use means every verifier pinning the algorithm by hand |
| **PASETO v4.public** | One algorithm per version (Ed25519), so no `alg` negotiation; asymmetric; an **implicit assertion** binds a token to its purpose; footer for a key id; small, readable spec | Smaller ecosystem than JWT; fewer off-the-shelf gateways understand it |
| **Macaroons** | Elegant attenuation: a holder can narrow a capability without the issuer | Caveat languages are easy to get subtly wrong; verification needs the root key (no asymmetric verifier) |
| **Opaque tokens + a database** | Trivially revocable | A database lookup on every call; availability coupling |
| **mTLS client identity** | Strong transport-level identity | Says who is calling, not what that caller may do |

## Decision

Grants are **PASETO `v4.public`** tokens (Ed25519), implemented in
`keelgate.capabilities`.

- **Asymmetric.** `GrantSigner` holds the private key; `GrantVerifier` holds only
  public keys. The gateway can accept grants but never mint one.
- **Purpose-bound.** The implicit assertion `keelgate:capability-grant:v1` is part
  of what is signed. A PASETO minted for any other purpose with the same key fails
  to verify as a grant. A test builds such a token and checks it is refused.
- **Key selection from the footer.** The footer carries `{"kid": ...}`, read
  *unverified* only to choose among keys we already trust; it is covered by the
  signature and cross-checked against the payload's `kid` after verification.
- **Claims:** `jti`, `iss`, `kid`, `sub` (agent), `tenant`, `caps`, `budget`,
  `iat`, `nbf`, `exp`. Time claims are validated against an **injected clock**, so
  expiry is tested at the microsecond boundary rather than with sleeps.
- **A hard lifetime ceiling.** `issue_grant` refuses a TTL above `max_ttl`
  (default 24 h), and `verify` refuses a correctly signed token whose lifetime
  exceeds it. A leaked signing key cannot mint immortal grants for a verifier
  configured with a tighter ceiling.
- **No wildcards.** A capability is `<resource>:<action>` in lower snake case.
  `trade:*` is a syntax error. AGENTS.md requires explicit grants, and a wildcard
  is how a capability added next quarter becomes silent privilege.
- **Deny by default.** `grant.allows(c)` is exact string membership.
- **Budget is a claim, spend is state.** The ceiling travels in the token; the
  running total lives in a `BudgetLedger`, reserved before a call and released if
  the call never executes.
- **Revocation** is a `RevocationList` protocol keyed by `jti`.

## Consequences

### Good

- Verification needs no network and no database.
- The classic JWT failure modes are absent by construction: there is no `alg` to
  confuse, and the key cannot be swapped by a header.
- A compromised gateway cannot forge authority; a compromised issuer is a
  separate, smaller blast radius.
- Expiry, not-before, leeway and lifetime ceilings are all deterministic tests.

### Bad, and accepted

- **Grants are bearer tokens.** Theft means use until expiry. There is no sender
  binding (DPoP-style proof of possession). Short TTLs and revocation are the
  mitigation; transport security is the deployment's job.
- **Revocation and budget state are process-local** in this release
  (`InMemoryRevocationList`, `InMemoryBudgetLedger`). A multi-process deployment
  needs shared implementations of the same protocols.
- **Smaller ecosystem than JWT.** Tooling that understands PASETO is rarer.
  `pyseto` is the dependency; it brings `cryptography`, `pycryptodomex` and
  `argon2-cffi` (the last unused by `v4.public`, present transitively). All are
  permissively licensed and small.
- **No attenuation.** A holder cannot narrow a grant and hand it on; a new grant
  must be issued. That is a deliberate simplification for K1.
- **Key rotation is by `kid`**, supported by the verifier holding several keys,
  but there is no rotation *tooling* yet.

### Follow-ups

- Shared revocation list and budget ledger (Redis/Postgres).
- Optional sender-constrained grants.
- Rotation tooling and HSM/KMS-backed signers in Keelgate Cloud.
