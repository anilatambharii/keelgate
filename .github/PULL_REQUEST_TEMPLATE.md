<!--
Keelgate's definition of done: `make check` passes, CI green, docs and examples
updated, security notes filled in, no TODO without an issue.
-->

## What and why

<!-- One or two sentences. Link the issue or the phase this belongs to. -->

## Changes

<!-- The notable ones. Skip the obvious. -->

-

## Security notes

<!--
Required. "No security impact, and here is why" is a valid answer; blank is not.
Call out anything that touches a trust boundary.
-->

- **Safety properties affected:** <!-- none, or which and how -->
- **New trust boundary or external input:** <!-- none, or where it is validated -->
- **Does any model output or prompt now influence a decision that was deterministic?** <!-- no / yes, explain -->
- **New secrets or config:** <!-- none, or documented in .env.example with an empty value -->

## Integration contract

- [ ] No change to the public API in `docs/integration-contract.md`
- [ ] Additive change only — minor version bump
- [ ] **Breaking change** — major version bump, with a migration note in this PR

## Checklist

- [ ] `make check` passes locally
- [ ] Tests added or updated alongside the code
- [ ] Docs and examples updated if behaviour changed
- [ ] An ADR added under `docs/adr/` if this is a significant decision
- [ ] No new `TODO` without a linked issue
- [ ] No secret, key or token added to the repository
- [ ] Conventional Commit messages
