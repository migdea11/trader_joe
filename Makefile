# Define default shell
SHELL := /bin/bash

VENV_MARKER = .venv_init

# Ensure init only runs if the marker file is missing
$(VENV_MARKER):
	uv venv  # Create virtual environment
	source .venv/bin/activate
	uv sync --all-groups
	touch $(VENV_MARKER)

# Target: Build the Docker images
build: $(VENV_MARKER)
	docker-compose build

# Target: Launch only dependencies (Postgres, Kafka, PgAdmin, Redpanda)
launch-deps: $(VENV_MARKER)
	docker-compose up -d postgres kafka pgadmin redpanda

# Target: Launch development services (depends on `launch-deps`)
launch-dev: launch-deps
	docker-compose -f docker-compose.yaml -f docker-compose.override.yaml up data_store data_ingest

# Target: Launch production services without override file (depends on `launch-deps`)
launch: launch-deps
	docker-compose up data_store data_ingest

clean:
	rm -rf .venv $(VENV_MARKER)
	docker-compose down

# Target: Run linting and tests
test:  $(VENV_MARKER)
	uv run ruff check .  # Run Ruff for linting
	uv run pytest  # Run Pytest for tests
