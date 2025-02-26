# Define default shell
SHELL := /bin/bash

VENV_MARKER = .venv_init


help:  ## Show this help message
	@echo "Available make commands:"
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make <target>\n\nTargets:\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)


$(VENV_MARKER):  ## Internal option to create virtual environment
	uv venv  # Create virtual environment
	source .venv/bin/activate
	uv sync --all-groups
	touch $(VENV_MARKER)

build: $(VENV_MARKER)  ## Build the Docker images
	docker-compose build

launch-deps: $(VENV_MARKER)  ## Launch dependency containers
	docker-compose up -d postgres kafka pgadmin redpanda

launch: launch-deps  ## Launch production services
	docker-compose up data_store data_ingest

launch-down:  ## Stop all services
	docker-compose down

dev-launch: launch-deps  ## Launch development services
	docker-compose -f docker-compose.yaml -f docker-compose.override.yaml up data_store data_ingest

dev-prune: ## Prune development services
	docker container prune -f && docker volume prune -f && docker image prune -f

clean: launch-down  ## Clean up the project
	rm -rf .venv $(VENV_MARKER)
	[[ -d .pytest_cache ]] && rm -rf .pytest_cache || true
	[[ -d .coverage ]] && rm -rf .coverage || true
	[[ -d coverage.xml ]] && rm -rf coverage.xml || true

lint: $(VENV_MARKER)  ## Lint the project
	uv run ruff check .

lint-fix:  ## Lint the project and fix
	uv run ruff check --fix .

test: lint  ## Run tests
	uv run pytest  # Run Pytest for tests

test-cov: lint  ## Run tests with coverage
	uv run coverage run -m pytest
	uv run coverage xml
