# Keelgate developer entrypoints. `make check` is the gate: it must pass before
# any claim that something works, and it is exactly what CI runs.
.DEFAULT_GOAL := help

UV      ?= uv
COMPOSE ?= docker compose -f docker-compose.dev.yml
# Rego tooling: a local `opa` binary when present, otherwise the pinned image via the
# compose `opa-tools` service. Container paths live in the compose file rather than on
# the command line, which keeps them away from Windows shells that rewrite "/paths".
ifeq ($(shell command -v opa >/dev/null 2>&1 && echo yes),yes)
OPA     := opa
OPA_DIR := policies
else
OPA     := $(COMPOSE) run --rm opa-tools
OPA_DIR := .
endif

.PHONY: help setup setup-all lock fmt lint format-check types test check \
        up down restart logs health hooks secrets-baseline docs docs-build \
        build clean quickstart research-loop

help: ## Show this help
	@awk 'BEGIN{FS=":.*?## "} /^[a-zA-Z_-]+:.*?## /{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

# ------------------------------------------------------------------- setup
setup: ## Create the dev venv and install git hooks
	$(UV) sync
	$(UV) run pre-commit install
	@echo "ready — run 'make check'"

setup-all: ## Install every optional integration extra as well
	$(UV) sync --all-extras
	$(UV) run pre-commit install

lock: ## Refresh uv.lock (resolves base deps, all extras and all groups)
	$(UV) lock

# -------------------------------------------------------------------- gate
fmt: ## Format the codebase
	$(UV) run ruff format .

lint: ## Lint (ruff check)
	$(UV) run ruff check .

format-check: ## Verify formatting without writing
	$(UV) run ruff format --check .

types: ## Type-check src/ with mypy --strict
	$(UV) run mypy

test: ## Run the test suite with coverage
	$(UV) run pytest

check: lint format-check types test ## Lint, format-check, type-check and test
	@echo "make check: PASS"

# ------------------------------------------------------------- policy packs
policy-lint: ## opa check --strict and opa fmt over the Rego packs
	$(OPA) check --strict $(OPA_DIR)
	$(OPA) fmt --fail $(OPA_DIR)

policy-test: policy-lint ## Run the Rego unit tests with real OPA
	$(OPA) test $(OPA_DIR) -v

test-integration: ## Run the tests that need `make up` (fails, not skips, if a service is down)
	KEELGATE_REQUIRE_INTEGRATION=1 $(UV) run pytest tests/test_policy_conformance.py tests/test_audit.py tests/test_quickstart.py tests/test_memory.py tests/test_adapter_temporal.py -p no:cacheprovider --no-cov -rs

quickstart: ## Run the quickstart, including the tamper demo
	$(UV) run python examples/quickstart.py --tamper

research-loop: ## Run the budgeted, resumable loop example (also serves its tools over MCP)
	$(UV) run python examples/research_loop.py

# ---------------------------------------------------------- dev services
up: ## Start Postgres+pgvector, Redis, OPA and Jaeger, waiting for health
	$(COMPOSE) up -d --wait
	$(MAKE) health

down: ## Stop dev services and delete their volumes
	$(COMPOSE) down -v

restart: down up ## Recreate dev services from scratch

logs: ## Tail dev service logs
	$(COMPOSE) logs -f

health: ## Probe each dev service from the host
	$(COMPOSE) ps
	@echo "--- endpoint probes ---"
	@$(COMPOSE) exec -T postgres pg_isready -U keelgate -d keelgate >/dev/null 2>&1 && echo "postgres :5432 ok" || echo "postgres :5432 UNREACHABLE"
	@$(COMPOSE) exec -T postgres psql -U keelgate -d keelgate -tAc "select 1 from pg_extension where extname = 'vector'" 2>/dev/null | grep -q 1 && echo "pgvector      ok" || echo "pgvector      MISSING"
	@$(COMPOSE) exec -T redis redis-cli ping >/dev/null 2>&1 && echo "redis    :6379 ok" || echo "redis    :6379 UNREACHABLE"
	@curl -fsS -o /dev/null http://localhost:8181/health && echo "opa      :8181 ok" || echo "opa      :8181 UNREACHABLE"
	@curl -fsS -o /dev/null http://localhost:16686/ && echo "jaeger   :16686 ok" || echo "jaeger   :16686 UNREACHABLE"
	@curl -fsS -o /dev/null http://localhost:14269/ && echo "jaeger otlp admin :14269 ok" || echo "jaeger admin :14269 UNREACHABLE"

# -------------------------------------------------------------- hygiene
hooks: ## Run every pre-commit hook over all files
	$(UV) run pre-commit run --all-files

secrets-baseline: ## Regenerate the detect-secrets baseline
	$(UV) run detect-secrets scan --baseline .secrets.baseline

# ------------------------------------------------------------------ docs
docs: ## Serve the docs locally
	$(UV) run --group docs mkdocs serve

docs-build: ## Build the docs strictly (warnings are errors)
	$(UV) run --group docs mkdocs build --strict

build: ## Build the wheel and sdist
	$(UV) build

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov site dist build
	rm -f .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
