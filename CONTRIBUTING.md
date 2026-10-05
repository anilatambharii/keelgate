# Contributing to Keelgate

Keelgate is a safety harness, so the bar for changes is a little higher than for
a typical library: a bug here is a bug in someone's control plane. Thank you for
helping.

## Ground rules

- **`make check` must pass before you push.** It is exactly what CI runs.
- **Tests alongside code.** No PR without tests. Core modules carry >=85% coverage.
- **`mypy --strict` on `src/`.** No new `type: ignore` without a code and a reason.
- **Small PRs, one concern each.** One development phase per branch.
- **[Conventional Commits](https://www.conventionalcommits.org/).**
- **Never commit a secret.** `.env` is gitignored; document new variables in `.env.example` with an empty value.
- **No TODO without a linked issue.** Unlinked TODOs get rejected in review.

## Setting up

You need Python 3.11+, [`uv`](https://docs.astral.sh/uv/), GNU Make and Docker.

```bash
git clone https://github.com/anilatambharii/keelgate.git
cd keelgate
make setup      # venv + dev deps + git hooks
make check      # the gate
make up         # Postgres+pgvector, Redis, OPA, Jaeger
```

<details>
<summary>Windows notes</summary>

`make` is not installed by default. Either use WSL, or install GNU Make and run
the targets from Git Bash:

```powershell
choco install make
```

Line endings are normalised to LF by `.gitattributes`; if whole files show as
modified, run `git add --renormalize .`.
</details>

## The targets you will use

| Target | What it does |
|---|---|
| `make setup` | Dev venv and git hooks |
| `make setup-all` | As above plus every optional extra |
| `make check` | ruff, then ruff format --check, then mypy, then pytest |
| `make fmt` | Apply formatting |
| `make test` | Tests with coverage |
| `make up` / `make down` / `make health` | Dev services |
| `make policy-test` | `opa check --strict`, `opa fmt --fail` and the Rego unit tests |
| `make test-integration` | Tests that need `make up`; a missing service **fails**, it does not skip |
| `make quickstart` | Run the quickstart with the tamper demo |
| `make test-live` | Opt-in smoke tests against real LLM providers and the Claude Agent SDK (needs keys; costs money) |
| `make research-loop` | Run the budgeted, resumable loop example (also serves its tools over MCP) |
| `make hooks` | Run pre-commit over every file |
| `make docs` | Serve the docs locally |

`make help` lists them all.

## Branches and commits

Branch as `phase-KN-short-name` for planned phases, or `fix/...` and `feat/...`
otherwise. Commit messages follow Conventional Commits:

```text
feat(policy): add REQUIRE_APPROVAL decision to the Rego pack
fix(context): reject documents published after as_of
docs(adr): record the open-core boundary
chore(ci): pin dependency-review to v5
```

## What review looks for

Beyond correctness, a reviewer will ask:

1. **Is any safety property being weakened?** If a prompt, a model output or a
   config toggle can now influence a decision that used to be deterministic,
   say so explicitly in the PR.
2. **Is external text treated as data?** Tool results, retrieved memory, web
   content and documents are untrusted. They are never instructions.
3. **Does it honour `as_of`?** Any read path that can reach data published after
   the cutoff is a bug, not a feature request.
4. **Does it deny by default?** New capabilities are opt-in and tenant-scoped.
5. **Is it auditable?** Decisions and side effects belong in the audit chain.

Every PR body carries a **security notes** section. "No security impact" is a
valid answer; leaving it blank is not.

## Architectural decisions

Anything significant — a dependency with reach, a change to the integration
contract, a new trust boundary — gets an ADR in `docs/adr/NNNN-title.md`. Copy
the shape of [ADR-0001](docs/adr/0001-licensing-and-open-core.md).

## The integration contract

The API in [docs/integration-contract.md](docs/integration-contract.md) is
consumed by downstream projects (notably Tycheon) and is versioned with semver.
Changing it is not an implementation detail:

- additive and backwards-compatible changes are a minor bump;
- any removal or signature change is a **major** bump, plus a migration note in
  the same PR.

## Writing policy

Policy is code and gets reviewed like code. Follow the rules in
[policies/README.md](policies/README.md): conditions are positive, reasons never
echo model text, and anything not allowed is denied. A rule without a test for
its *missing-input* case is not finished.

## Integration tests

Some tests need Postgres and OPA. Locally they skip with a reason when the
service is down; run `make up` first. CI sets `KEELGATE_REQUIRE_INTEGRATION=1`, so
there a missing service is a failure and a green build cannot hide skipped tests.

## Dependencies

Ask before adding anything heavy (>50MB), GPU-only, or copyleft. Optional
integrations belong in an extra, never in the base dependencies.

## Reporting security issues

Do **not** open a public issue. Follow [SECURITY.md](SECURITY.md).

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
