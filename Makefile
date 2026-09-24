# Define default shell
SHELL := /bin/bash

# Every target except $(VENV_MARKER) is a command, not a file, and is declared .PHONY next to
# its own recipe so a new target is hard to add without one. Undeclared, a file or directory of
# the same name -- a test/ or build/ at the repo root -- makes the target "up to date": make
# runs nothing and exits 0, so `make test` would report success having run no test (tj-06uflo).
.PHONY: help
help:  ## Show this help message
	@echo "Available make commands:"
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make <target>\n\nTargets:\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# Matches the uv pinned by CI (.github/workflows) and the Dockerfile.
UV_VERSION := 0.9.3

# Scopes lint/format/test to one component, e.g. `make test PATHS=common`.
PATHS ?= .

VENV_MARKER := .venv_init
# Bootstraps uv only when the host has none. An existing uv is used as-is: `uv self update`
# fails outright for a system- or package-managed install, and would downgrade one that is
# already newer than the pin.
#
# The marker depends on the dependency declarations so the sync re-runs when they change,
# instead of going stale behind a marker file that already exists.
$(VENV_MARKER): pyproject.toml uv.lock  ## Internal option to install uv and sync the virtual environment
	@if ! command -v uv > /dev/null; then \
		echo "Installing uv $(UV_VERSION)"; \
		curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --version $(UV_VERSION); \
	else \
		found=$$(uv --version | awk '{print $$2}'); \
		oldest=$$(printf '%s\n%s\n' "$$found" "$(UV_VERSION)" | sort -V | head -n1); \
		if [ "$$oldest" = "$(UV_VERSION)" ]; then \
			echo "Using uv $$found already on PATH"; \
		else \
			echo "Warning: uv $$found is older than the pinned $(UV_VERSION); upgrade it if a command fails"; \
		fi; \
	fi
	@if [ ! -d .venv ]; then \
		echo "Creating uv venv"; \
		uv venv; \
	fi
# Sync here, not only in `init`, so every target below gets a usable environment on a bare
# checkout: the test suite imports asyncpg and sqlalchemy, which live in the data-store group
# and so are not covered by `default-groups`. The group set matches CI, less `security` —
# that tooling is heavy and only `make security` needs it. Unlike CI this omits `--locked`:
# CI must fail when the lock is stale, but locally that would block anyone mid-edit of
# pyproject.toml.
	uv sync --all-groups --no-group security
	touch $(VENV_MARKER)

.PHONY: init
init: $(VENV_MARKER)  ## Initialize the project, including the security tooling
	uv sync --all-groups

# Every compose target goes through one of these, and none omits -f. A bare
# `docker compose` auto-loads docker-compose.override.yaml, which is what made `launch`
# start the dev images while its help text claimed production (tj-6ap2vw).
#
# The two stacks differ only in the override: dev_image (RUN_MODE=dev, --reload, the dev
# dependency group, and the debugger of tj-g1qqf1), source bind mounts so reload sees host
# edits, and LOG_LEVEL=debug. Nothing structural. PROD_COMPOSE must never grow the override.
#
# TOOLS_COMPOSE is the dev pair plus docker-compose.tools.yaml, which holds pgAdmin and nothing
# else. Only dev-tools and dev-down use it (tj-ae3n49). pgAdmin's PGADMIN_EMAIL/PGADMIN_PASS use
# ":?" guards, and compose evaluates every file it is handed, so a file any other target loaded
# would make those credentials a requirement of the whole dev stack. A profile does not avoid
# that; a separate file does. PROD_COMPOSE never loads it.
PROD_COMPOSE := docker compose -f docker-compose.yaml
DEV_COMPOSE := docker compose -f docker-compose.yaml -f docker-compose.override.yaml
TOOLS_COMPOSE := $(DEV_COMPOSE) -f docker-compose.tools.yaml

# --wait, matching the CI deploy step: it blocks until every started service reports
# healthy and exits non-zero if one does not, so a broken deploy fails the command instead
# of printing a cheerful "Started". 300s because kafka alone declares a 90s start_period
# and data_store a 60s one. Detached is the consequence -- `prod-logs` is how you watch it.
PROD_UP := $(PROD_COMPOSE) up -d --wait --wait-timeout 300

.PHONY: prod-build
prod-build: $(VENV_MARKER)  ## Build the production images (:latest)
	$(PROD_COMPOSE) build

.PHONY: prod-build-clean
prod-build-clean: $(VENV_MARKER)  ## Build the production images from scratch, no cache
	$(PROD_COMPOSE) build --no-cache

.PHONY: prod-deps
prod-deps: $(VENV_MARKER)  ## Start the production dependencies (postgres, kafka)
	$(PROD_UP) postgres kafka

.PHONY: prod-launch
prod-launch: prod-deps  ## Start the production services, waiting for healthy
	$(PROD_UP) data_store data_ingest

.PHONY: prod-logs
prod-logs:  ## Follow the production service logs
	$(PROD_COMPOSE) logs -f data_store data_ingest

.PHONY: prod-down
prod-down:  ## Stop the production stack
	$(PROD_COMPOSE) down

# The single spelling of "apply the migrations" — run it after every deploy, once the stack is
# up. Nothing else creates the schema, so a healthy stack has an empty database until this runs.
# The deploy script of tj-jm51fw will call this target rather than repeat the compose line.
#
# No $(VENV_MARKER) prerequisite, unlike every other compose target here: the script runs alembic
# inside the data_store container, never from the host venv. A host sync would be wasted work,
# and it would make the production migration path depend on uv being usable on the server.
# The script resolves the repo root itself, so this works from any directory.
#
# Production-shaped, and deliberately not a prerequisite of any launch target: the accepted
# direction (tj-x3ig38) is that migrations are a deploy-script step, never a compose
# dependency and never the entrypoint, because a rollback re-runs `compose up` and would
# re-apply the migration from the wrong revision directory. The script passes
# -f docker-compose.yaml itself, so this runs against the production data_store definition
# even when the dev stack is what is up -- postgres is the same container either way.
.PHONY: migrate
migrate:  ## Apply database migrations to the running production stack
	./data/store/run_migrations.sh

.PHONY: dev-build
dev-build: $(VENV_MARKER)  ## Build the development images (:dev)
	$(DEV_COMPOSE) build

.PHONY: dev-deps
dev-deps: $(VENV_MARKER)  ## Start the development dependencies (postgres, kafka)
	$(DEV_COMPOSE) up -d postgres kafka

# pgAdmin is a tool, not a dependency: it lives in docker-compose.tools.yaml, which dev-deps and
# dev-launch never load (tj-ae3n49). This is the on-demand spelling, and the one target that
# needs PGADMIN_EMAIL/PGADMIN_PASS set. compose brings postgres up first, because pgadmin
# depends on it being healthy. Stop it with dev-down.
.PHONY: dev-tools
dev-tools: $(VENV_MARKER)  ## Start pgAdmin against the development database (needs PGADMIN_*)
	$(TOOLS_COMPOSE) up -d pgadmin

# Foreground on purpose, unlike prod-launch: --reload prints what it reloaded and why, and
# that output is the reason to run the dev stack at all. Ctrl-C stops it.
.PHONY: dev-launch
dev-launch: dev-deps  ## Start the development services in the foreground, with reload
	$(DEV_COMPOSE) up data_store data_ingest

# Through TOOLS_COMPOSE, so a pgAdmin started by dev-tools goes down with the rest instead of
# being left running as an orphan on the project network. Costs nothing when it is not running.
#
# The placeholder PGADMIN_* values exist only to get past the ":?" guards, so that stopping the
# stack never requires pgAdmin credentials. They are safe here and ONLY here: `down` creates no
# container, so no pgAdmin account can ever be initialised from them. Shell variables outrank
# .env in compose interpolation, which is why they must never be copied onto an `up`.
.PHONY: dev-down
dev-down:  ## Stop the development stack, pgAdmin included
	PGADMIN_EMAIL=unused PGADMIN_PASS=unused $(TOOLS_COMPOSE) down

.PHONY: dev-prune
dev-prune: ## Prune development services
	docker container prune -f && docker volume prune -f && docker image prune -f

# Removed spellings. Each one used to resolve a stack its name did not state: `launch` and
# `launch-deps` ran dev while the help text said production, and `build` produced prod
# images no target ever started (tj-6ap2vw). Failing here rather than deleting the names
# outright means muscle memory gets a pointer instead of picking a stack silently -- and
# data/store/run_migrations.sh still names `make launch-deps` in its error path, so that
# hint degrades into this message rather than into nothing. No `##`: `make help` lists the
# real targets only.
.PHONY: build build-clean launch launch-deps launch-down
build build-clean launch launch-deps launch-down:
	@echo "'make $@' is gone: it did not state which stack it meant (tj-6ap2vw)." >&2
	@echo "Use the prod-* or dev-* target for the stack you want -- see 'make help'." >&2
	@exit 1

AGENT_COMPOSE := docker compose -f .devcontainer/compose.yml

# The agent config directory holds the session transcripts, bind-mounted from the host.
# devcontainer.json's initializeCommand creates it when an IDE starts the container, but that
# hook does not run for a plain `compose up` — so agent-up creates it too. Docker would otherwise
# create the missing bind source as a root-owned directory the agent user cannot write to.
# Exported so compose.yml resolves the same path this file does.
export AGENT_HOME_PATH ?= $(HOME)/.claude-agent-homes/trader_joe

.PHONY: agent-build
agent-build:  ## Build the agent devcontainer image
	$(AGENT_COMPOSE) build

.PHONY: agent-up
agent-up:  ## Start the agent devcontainer
	mkdir -p "$(AGENT_HOME_PATH)"
	$(AGENT_COMPOSE) up -d

.PHONY: agent-down
agent-down:  ## Stop the agent devcontainer (host config dir is kept)
	$(AGENT_COMPOSE) down

.PHONY: agent-attach
agent-attach:  ## Open a shell inside the agent devcontainer
	$(AGENT_COMPOSE) exec agent bash

# dev-down, not prod-down: this is a workstation target -- it deletes .venv -- and dev-down is
# the one teardown that loads docker-compose.tools.yaml, so going through it leaves no pgAdmin
# behind.
.PHONY: clean
clean: dev-down  ## Clean up the project
	rm -rf .venv $(VENV_MARKER)
	[[ -d .pytest_cache ]] && rm -rf .pytest_cache || true
	[[ -d .coverage ]] && rm -rf .coverage || true
	[[ -d coverage.xml ]] && rm -rf coverage.xml || true

.PHONY: lint
lint: $(VENV_MARKER)  ## Lint and format-check the project (scope with PATHS=)
	uv run ruff check $(PATHS)
	uv run ruff format --check $(PATHS)

SOURCE_DIRS := ./common ./router ./schemas ./data
.PHONY: lint-fix
lint-fix: $(VENV_MARKER)  ## Apply lint fixes and formatting (scope with PATHS=)
	uv run ruff check --fix $(PATHS)
	uv run ruff format $(PATHS)

.PHONY: security
security: $(VENV_MARKER)  ## Check security vulnerabilities
	uv run bandit -r $(SOURCE_DIRS) --exclude tests/
	uv run semgrep --config=auto --exclude=tests/ --exclude=.venv --exclude=docker-compose.override.yaml .
	uv export --all-groups --no-group dev --no-group testing --no-group security --locked --format requirements-txt > requirements.txt
	# uv run safety scan --file requirements.txt
	uv run pip-audit -r requirements.txt --disable-pip
	rm requirements.txt

# Deliberately independent of `lint`: a test run must report a test result, not a lint failure.
# CI runs both, as separate steps.
.PHONY: test
test: $(VENV_MARKER)  ## Run tests (scope with PATHS=)
	POSTGRES_ASYNC=true POSTGRES_SYNC=true uv run pytest $(PATHS)

.PHONY: test-cov
test-cov: $(VENV_MARKER)  ## Run tests with coverage (scope with PATHS=)
	POSTGRES_ASYNC=true POSTGRES_SYNC=true uv run coverage run -m pytest $(PATHS)
	uv run coverage xml
