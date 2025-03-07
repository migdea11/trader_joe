# Define default shell
SHELL := /bin/bash

VENV_MARKER = .venv_init


help:  ## Show this help message
	@echo "Available make commands:"
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make <target>\n\nTargets:\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)


$(VENV_MARKER):  ## Internal option to install uv and create a virtual environment
	UV_VERSION=0.6.4
	if  [[ ! $(which uv) ]]; then
		curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --version $UV_VERSION
	else
		[[ $(uv --version) == $UV_VERSION ]] || uv self update $UV_VERSION
	fi
	[[ -d .venv ]] || uv venv

init $(VENV_MARKER):  ## Initialize the project
	source .venv/bin/activate
	uv sync --all-groups
	touch $(VENV_MARKER)

build: $(VENV_MARKER)  ## Build the Docker images
	docker-compose  -f docker-compose.yaml build

launch-deps: $(VENV_MARKER)  ## Launch dependency containers
	docker-compose up -d postgres kafka pgadmin redpanda

launch: launch-deps  ## Launch production services
	docker-compose up data_store data_ingest

launch-down:  ## Stop all services
	docker-compose down

dev-build: $(VENV_MARKER)  ## Build the Docker images for development
	docker compose -f docker-compose.yaml -f docker-compose.override.yaml build

dev-launch: launch-deps  ## Launch development services
	docker compose -f docker-compose.yaml -f docker-compose.override.yaml up data_store data_ingest

dev-prune: ## Prune development services
	docker container prune -f && docker volume prune -f && docker image prune -f

clean: launch-down  ## Clean up the project
	rm -rf .venv $(VENV_MARKER)
	[[ -d .pytest_cache ]] && rm -rf .pytest_cache || true
	[[ -d .coverage ]] && rm -rf .coverage || true
	[[ -d coverage.xml ]] && rm -rf coverage.xml || true

lint: $(VENV_MARKER)  ## Lint the project
	uv run ruff check .

SOURCE_DIRS :=./common ./router ./schemas ./data
lint-fix:  ## Lint the project and fix
	uv run ruff check --fix .

security: $(VENV_MARKER)  ## Check security vulnerabilities
	uv run bandit -r $(SOURCE_DIRS) --exclude tests/
	uv run semgrep --config=auto --exclude=tests/ --exclude=.venv --exclude=docker-compose.override.yaml .
	uv export --all-groups --no-group dev --no-group testing --no-group security --locked --format requirements-txt > requirements.txt
	# uv run safety scan --file requirements.txt
	uv run pip-audit -r requirements.txt --disable-pip
	rm requirements.txt

test: lint  ## Run tests
	uv run pytest

test-cov: lint  ## Run tests with coverage
	uv run coverage run -m pytest
	uv run coverage xml
