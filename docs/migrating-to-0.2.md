# Migrating from 0.1 to 0.2

0.2.0 is the first release with an **enforced public API**. 0.1.0 was a snapshot: every module
could be imported by its internal path, and some projects (including Tycheon) did. 0.2.0 makes the
implementation modules private and defines the API as what the package roots export.

## What breaks

**Deep imports.** Implementation modules are now underscored:

| 0.1 | 0.2 |
|---|---|
| `from keelgate.llm.types import LLMRequest` | `from keelgate.llm import LLMRequest` |
| `from keelgate.tools.gateway import ToolGateway` | `from keelgate.tools import ToolGateway` |
| `from keelgate.loop.engine import Loop` | `from keelgate.loop import Loop` |
| `from keelgate.adapters.governed import GovernedToolset` | `from keelgate.adapters import GovernedToolset` |
| `from keelgate.policy.engine import deny` | `from keelgate.policy import deny` |

The rule is always the same: **import from the package, not from a module inside it.** 67 modules
moved this way. A handful stayed public because they are entry points or need an optional
dependency: `keelgate.cli`, `keelgate.approvals.cli`, `keelgate.approvals.rest`,
`keelgate.policy.cedar`, `keelgate.loop.langgraph_store`, `keelgate.testing.plugin` and
`keelgate.telemetry.attributes`.

## Fix it automatically

```bash
pip install -U "keelgate>=0.2,<0.3"
keelgate migrate-imports src/ tests/            # shows a diff, changes nothing
keelgate migrate-imports src/ tests/ --write    # applies it
```

The tool derives the old-to-new mapping from the installed package, rewrites every deep import
whose names are exported from the package root, and **reports, without guessing,** anything that
is not: a name that is not exported from the root is not public API, and needs a human decision
(ask for it to be made public, or stop using it). Plain `import keelgate.x.y` statements are also
reported rather than rewritten. It exits 1 while manual work remains.

## Tycheon

A dry run of the tool over Tycheon's `src/` and `tests/` found exactly two imports to change, both
onto names that are now public, and nothing needing a manual change:

```diff
-from keelgate.adapters.governed import GovernedToolset
+from keelgate.adapters import GovernedToolset
-from keelgate.policy.engine import deny
+from keelgate.policy import deny
```

and its dependency pin must move with the minor version:

```diff
-keelgate>=0.1,<0.2
+keelgate>=0.2,<0.3
```

## What is new

- **A documented API.** [`api-contract.md`](api-contract.md) lists every public symbol with its
  stability level, generated from the code and checked by tests. The
  [integration contract](integration-contract.md) has the semantics and an end-to-end example.
- **Telemetry, replay and evals** (OpenTelemetry traces, `keelgate replay`, `keelgate eval run`,
  the `keelgate.outcome_metrics` plugin point).
- **`keelgate quickstart`**, runnable straight after `pip install`.
- **Versioning and deprecation policy** ([versioning](versioning.md)).

## What did not change

Behaviour. The safety guarantees, wire formats (audit hash input, grants, checkpoints) and policy
packs are the same; only the import paths moved. Checkpoints written by 0.1 load in 0.2.
