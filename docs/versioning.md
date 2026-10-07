# Versioning and deprecation policy

Keelgate follows [Semantic Versioning 2.0.0](https://semver.org/). This page says what that means
here: what counts as the API, what is promised about it, and how anything is retired.

## What is the API

The public API is **exactly** what [`docs/api-contract.md`](api-contract.md) lists. That file is
generated from the code, and a test fails if the code and the file disagree, so a change to a
public signature always appears as a diff to it in review.

Everything else is private, whatever it is called:

- every module whose name starts with an underscore (`keelgate.tools._gateway`, ...);
- `keelgate._internal`;
- any name not exported in a public module's `__all__`;
- the wording of log lines, error messages and CLI output;
- the layout of generated reports (the keys of `report.json`, the HTML). They are provisional.

Importing a private module is allowed and unsupported; it may change in any release.

## Stability levels

Every public symbol is **stable** or **provisional**, and the contract says which.

| Level | Promise |
|---|---|
| **stable** | Will not break without a version bump that says so (below), a changelog entry and a migration note. |
| **provisional** | Works and is tested, but may change in a **minor** release, with a changelog entry. Pin a minor version (`keelgate>=0.2,<0.3`) if you depend on it. |

A symbol becomes stable when a downstream project has used it through a release without needing a
change. Moving a symbol from provisional to stable is a minor change; the other direction is a
breaking change.

Provisional today: the model provider clients, every framework adapter, memory backends and
embedders, replay, the Temporal runner, the report formats and CLI flags, and telemetry attribute
names (the OpenTelemetry GenAI conventions they follow are still in development upstream).

## Version numbers

| Change | Version bump |
|---|---|
| Bug fix; no change to any public signature or documented behaviour | patch |
| New symbol, module, optional argument or enum member | minor |
| A provisional symbol changes | minor |
| A stable symbol is removed, renamed, or its signature or documented behaviour changes | **major** (a **minor** while Keelgate is 0.x) |
| A deprecated symbol is removed in the release its warning named | minor |
| Supported Python versions are dropped | minor, with notice one release ahead |

While Keelgate is `0.x`, the project may break stable symbols in a minor release; each such
release says so at the top of its changelog entry and ships a migration note. From `1.0.0`, a
breaking change to a stable symbol needs a major bump, without exception.

Downstream projects should not assume enums are exhaustive: a new member is a minor change.

## Guarantees that outlive any version

These hold for every release, stable or provisional. Breaking one is a security bug, not a version
bump (see the [security model](security-model.md)):

1. A `WRITE` tool cannot execute without a policy decision.
2. A capability that was never granted cannot be exercised.
3. Nothing published after `as_of` enters context or memory.
4. A tampered audit chain fails `verify_chain()`.
5. No tenant can observe or affect another tenant.
6. There is no live execution path: only `paper` and `simulation`.

## Formats that are part of the contract

These are versioned data, not just code. Changing them is a major (or, in 0.x, minor) change with a
migration path:

- **Audit hash input.** Pinned by a golden-vector test. Changing it invalidates every existing
  chain.
- **Checkpoint state.** `LoopState` carries `state_version`; older checkpoints keep loading.
- **Capability grants.** PASETO `v4.public` with the claims documented in ADR-0003.
- **The policy input document** handed to a `PolicyEngine`. New fields may be added; existing
  fields keep their meaning.
- **Policy packs** are identified by the hash of their sources, recorded on every decision.
  Changing `finance_basic` changes that hash, which is how a decision is tied to the policy that
  made it. Rule changes are announced in the changelog.

## Deprecation policy

Retiring a public symbol happens in the open and in this order:

1. **Deprecate.** The symbol keeps working and now emits a `KeelgateDeprecationWarning` (a
   `DeprecationWarning`) whose text names the replacement and the version it will be removed in.
   The changelog lists it under a "Deprecated" heading. The docstring says so.
2. **Wait at least one minor release.** A deprecated symbol survives at least one full minor
   release after the one that deprecated it, and at least three months.
3. **Remove** in the release the warning named, with a migration note.

A security fix may shorten this, and says so.

Maintainers deprecate with one helper so every deprecation looks the same and can be found by
grep:

```python
from keelgate._internal.deprecation import deprecated

@deprecated(since="0.3.0", removal="0.5.0", replacement="new_name")
def old_name(...): ...
```

To find deprecations in your own code, run your tests with
`-W error::keelgate._internal.deprecation.KeelgateDeprecationWarning`.

## Supported Python versions

Python 3.11 and 3.12 are tested in CI. A version is dropped only in a minor release, announced one
release ahead.

## Optional dependencies

The adapters wrap other projects (LangGraph, the OpenAI Agents SDK, the Claude Agent SDK, MCP,
A2A, Temporal). Their version ranges are in `pyproject.toml` and move with those projects; widening
or narrowing a range is a minor change, and a wrapped project's own breaking change can force an
adapter change in a minor release. That is why adapters are provisional.

## How releases happen

Releases are cut by merging the release pull request that
[release-please](https://github.com/googleapis/release-please) maintains from Conventional
Commits. See [releasing](releasing.md). Security fixes are released as patch versions as soon as
they are verified; see [SECURITY.md](https://github.com/anilatambharii/keelgate/blob/main/SECURITY.md).
