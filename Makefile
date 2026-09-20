# Define default shell
SHELL := /bin/bash

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

init: $(VENV_MARKER)  ## Initialize the project, including the security tooling
	uv sync --all-groups

build: $(VENV_MARKER)  ## Build the Docker images
	docker compose  -f docker-compose.yaml build

build-clean: $(VENV_MARKER)  ## Build the Docker images
	docker compose  -f docker-compose.yaml build --no-cache

launch-deps: $(VENV_MARKER)  ## Launch dependency containers
	docker compose up -d postgres kafka pgadmin redpanda

launch: launch-deps  ## Launch production services
	docker compose up data_store data_ingest

launch-down:  ## Stop all services
	docker compose down

dev-build: $(VENV_MARKER)  ## Build the Docker images for development
	docker compose -f docker-compose.yaml -f docker-compose.override.yaml build

dev-launch: launch-deps  ## Launch development services
	docker compose -f docker-compose.yaml -f docker-compose.override.yaml up data_store data_ingest

dev-prune: ## Prune development services
	docker container prune -f && docker volume prune -f && docker image prune -f

AGENT_COMPOSE := docker compose -f .devcontainer/compose.yml

# The agent config directory holds the session transcripts, bind-mounted from the host.
# devcontainer.json's initializeCommand creates it when an IDE starts the container, but that
# hook does not run for a plain `compose up` — so agent-up creates it too. Docker would otherwise
# create the missing bind source as a root-owned directory the agent user cannot write to.
# Exported so compose.yml resolves the same path this file does.
export AGENT_HOME_PATH ?= $(HOME)/.claude-agent-homes/trader_joe

agent-build:  ## Build the agent devcontainer image
	$(AGENT_COMPOSE) build

agent-up:  ## Start the agent devcontainer
	mkdir -p "$(AGENT_HOME_PATH)"
	$(AGENT_COMPOSE) up -d

agent-down:  ## Stop the agent devcontainer (host config dir is kept)
	$(AGENT_COMPOSE) down

agent-attach:  ## Open a shell inside the agent devcontainer
	$(AGENT_COMPOSE) exec agent bash

clean: launch-down  ## Clean up the project
	rm -rf .venv $(VENV_MARKER)
	[[ -d .pytest_cache ]] && rm -rf .pytest_cache || true
	[[ -d .coverage ]] && rm -rf .coverage || true
	[[ -d coverage.xml ]] && rm -rf coverage.xml || true

lint: $(VENV_MARKER)  ## Lint and format-check the project (scope with PATHS=)
	uv run ruff check $(PATHS)
	uv run ruff format --check $(PATHS)

SOURCE_DIRS := ./common ./router ./schemas ./data
lint-fix: $(VENV_MARKER)  ## Apply lint fixes and formatting (scope with PATHS=)
	uv run ruff check --fix $(PATHS)
	uv run ruff format $(PATHS)

security: $(VENV_MARKER)  ## Check security vulnerabilities
	uv run bandit -r $(SOURCE_DIRS) --exclude tests/
	uv run semgrep --config=auto --exclude=tests/ --exclude=.venv --exclude=docker-compose.override.yaml .
	uv export --all-groups --no-group dev --no-group testing --no-group security --locked --format requirements-txt > requirements.txt
	# uv run safety scan --file requirements.txt
	uv run pip-audit -r requirements.txt --disable-pip
	rm requirements.txt

# Deliberately independent of `lint`: a test run must report a test result, not a lint failure.
# CI runs both, as separate steps.
test: $(VENV_MARKER)  ## Run tests (scope with PATHS=)
	POSTGRES_ASYNC=true POSTGRES_SYNC=true uv run pytest $(PATHS)

test-cov: $(VENV_MARKER)  ## Run tests with coverage (scope with PATHS=)
	POSTGRES_ASYNC=true POSTGRES_SYNC=true uv run coverage run -m pytest $(PATHS)
	uv run coverage xml
