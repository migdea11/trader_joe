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
# Floor is 0.9.17: below that, `exclude-newer = "7 days"` in pyproject.toml is not rejected,
# it is silently ignored, and the install cooldown stops protecting anything.
UV_VERSION := 0.12.19

# THE LOCK IS FROZEN BY DEFAULT (tj-3zh7ss). Exported, so every uv below -- and every uv those
# recipes start -- installs from uv.lock exactly as committed and never re-resolves it. Without
# this, any `uv run` or `uv sync` re-locked whenever pyproject.toml had moved: a plain
# `make test` once silently moved ten packages, sqlalchemy to a pre-release among them.
# The ONE deliberate way to change the lock is `make lock`. An explicit `--locked` still wins
# over this variable (uv warns and asserts instead), so the stale-lock checks in `security`
# and CI keep asserting. CI sets the same variable in its workflow env.
export UV_FROZEN := 1

# Scopes lint/format/test to one component, e.g. `make test PATHS=common`.
PATHS ?= .

# Bootstraps uv only when the host has none. An existing uv is used as-is: `uv self update`
# fails outright for a system- or package-managed install, and would downgrade one that is
# already newer than the pin. One definition, used by every recipe that runs uv in a way that
# can write uv.lock, so the guard cannot differ between them.
define UV_PIN_CHECK
@if ! command -v uv > /dev/null; then \
	echo "Installing uv $(UV_VERSION)"; \
	curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --version $(UV_VERSION); \
else \
	found=$$(uv --version | awk '{print $$2}'); \
	oldest=$$(printf '%s\n%s\n' "$$found" "$(UV_VERSION)" | sort -V | head -n1); \
	if [ "$$oldest" = "$(UV_VERSION)" ]; then \
		echo "Using uv $$found already on PATH"; \
	else \
		echo "Error: uv $$found is older than the pinned $(UV_VERSION)."; \
		echo "  An older uv does not merely fail to apply settings it does not know about --"; \
		echo "  it RE-RESOLVES AND OVERWRITES uv.lock, reverting pinned versions and the"; \
		echo "  install cooldown, while every command still exits 0. This was a warning until"; \
		echo "  an agent's test run silently reverted a correct lock (tj-jon3d1)."; \
		echo "  Install the pin:  curl -LsSf https://astral.sh/uv/$(UV_VERSION)/install.sh | sh"; \
		echo "  Or, if uv is already installed:  uv self update $(UV_VERSION)"; \
		echo "  (Agents cannot run the piped installer -- the isolation guard refuses '| sh'.)"; \
		exit 1; \
	fi; \
fi
endef

VENV_MARKER := .venv_init
# The marker depends on the dependency declarations so the sync re-runs when they change,
# instead of going stale behind a marker file that already exists.
$(VENV_MARKER): pyproject.toml uv.lock  ## Internal option to install uv and sync the virtual environment
	$(UV_PIN_CHECK)
	@if [ ! -d .venv ]; then \
		echo "Creating uv venv"; \
		uv venv; \
	fi
# Sync here, not only in `init`, so every target below gets a usable environment on a bare
# checkout: the test suite imports asyncpg and sqlalchemy, which live in the data-store group
# and so are not covered by `default-groups`. The group set matches CI, less `security` —
# that tooling is heavy and only `make security` needs it. UV_FROZEN makes this install the
# committed lock as-is: a pyproject.toml edit mid-work does not block the sync, and it does not
# re-lock either -- a new dependency reaches the venv only after `make lock`.
	uv sync --all-groups --no-group security
	touch $(VENV_MARKER)

# The one deliberate re-lock. Everything else runs frozen, so this is the only target that can
# change uv.lock: after a dependency edit in pyproject.toml, run it and commit the lock with the
# edit. Same uv pin guard as the venv recipe, because a re-lock is exactly the operation an old
# uv gets silently wrong. UV_FROZEN is removed for this one command only -- `uv lock` reads it
# as --check-exists and would otherwise check instead of lock.
.PHONY: lock
lock:  ## Re-resolve uv.lock from pyproject.toml (the only target that changes the lock)
	$(UV_PIN_CHECK)
	env -u UV_FROZEN uv lock

.PHONY: init
init: $(VENV_MARKER)  ## Initialize the project, including the security tooling
	uv sync --all-groups

# Every compose target goes through one of these, and none omits -f. A bare
# `docker compose` auto-loads docker-compose.override.yaml, which is what made `launch`
# start the dev images while its help text claimed production (tj-6ap2vw).
#
# The two stacks differ only in the override: dev_image (RUN_MODE=dev, --reload, the dev
# dependency group, and the debugger of tj-g1qqf1), source bind mounts so reload sees host
# edits, LOG_LEVEL=debug, and LATENCY_TEST_ENABLED=true on data_store and data_ingest. The
# first three change how the services are built and how loudly they log; the last changes what
# they DO at startup -- both create the latency Kafka topics and their RPC client/server
# consumers, and data_store serves GET /latency (tj-8mt207). That is the whole reason
# PROD_COMPOSE must never grow the override: loading it here would put the harness back into
# prod, which is the environment this repo exists to keep it out of.
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

# READ-ONLY, and the approval for this target was conditional on staying that way: `current`
# reads the alembic_version table, `history` reads the revision files, and neither writes
# anything. Nothing may be added here that mutates -- a mutating step belongs behind its own
# named target, the way `migrate` is.
#
# It exists because the hazard run_migrations.sh's header documents had no diagnostic: the code
# that runs comes from the deployed image, the revisions that get applied come from whatever is
# checked out, and the two can disagree silently. `current` says what the database actually has;
# `history` says what this checkout would apply. Read together they answer "are these the same
# thing", which no other command here could ask.
#
# Two runs of the same script rather than a second compose invocation, for every reason `migrate`
# goes through it: the repo root, the pinned -f docker-compose.yaml (so the dev override cannot
# swap in the dev image), the postgres check, and the empty-versions guard -- which matters most
# here, since an empty versions/ is the wrong-checkout symptom this target is used to diagnose.
# alembic has no one command for both, and each container is --rm, so the cost is one extra start.
.PHONY: migrate-status
migrate-status:  ## Report the applied revision and the revision history (read-only)
	./data/store/run_migrations.sh current
	./data/store/run_migrations.sh history

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

SOURCE_DIRS := ./common ./routers ./schemas ./data
.PHONY: lint-fix
lint-fix: $(VENV_MARKER)  ## Apply lint fixes and formatting (scope with PATHS=)
	uv run ruff check --fix $(PATHS)
	uv run ruff format $(PATHS)

# semgrep scans '.', so it would also scan other agents' live worktrees under .claude/worktrees
# (tj-aov3ip) -- half-edited copies of this repo. bandit needs no exclude: SOURCE_DIRS names
# its roots explicitly and none of them contains .claude.
# bandit's exclude is '*/tests/*', never a bare 'tests/' (tj-vhboky.67). bandit rewrites an exclude
# that names an EXISTING directory, relative to the cwd, into '<dir>/*' before matching, so once the
# repository-root tests/ appeared, 'tests/' became 'tests/*' and stopped matching the nested
# common/tests, data/store/tests and the rest: a thousand findings in test files. The glob exists
# nowhere as a directory, so it is matched as written, against every nested tests directory and
# no production path. The CI security job runs the identical line; change both or neither.
.PHONY: security
security: $(VENV_MARKER)  ## Check security vulnerabilities
	uv run bandit -r $(SOURCE_DIRS) --exclude '*/tests/*'
	uv run semgrep --config=auto --exclude=tests/ --exclude=.venv --exclude=docker-compose.override.yaml --exclude=.claude/worktrees .
	uv export --all-groups --no-group dev --no-group testing --no-group security --locked --format requirements-txt > requirements.txt
	uv run pip-audit -r requirements.txt --disable-pip
	rm requirements.txt

# Deliberately independent of `lint`: a test run must report a test result, not a lint failure.
# CI runs both, as separate steps.
#
# THE PR GATE -- -m "not external" -- IS IN pytest.ini's addopts, NOT in these targets, because
# CI does not go through make: .github/workflows/trader_joe_testing.yml runs `uv run pytest`
# directly. Every target here therefore inherits the gate for free, and the ones that want a
# different set pass their own -m, which wins: addopts is prepended, so the last -m on the
# command line is the one that takes effect. Putting an -m in each target instead would mean
# two places that must agree, and the CI one is the one that drifts.
#
# POSTGRES_ASYNC / POSTGRES_SYNC are import-time feature flags in
# common/database/postgres_tools.py choosing which driver is imported. They are NOT a claim
# that a database is reachable, and they are not markers. CI and .devcontainer/compose.yml set
# them too; spelled once here so the targets below cannot drift apart.
PYTEST_ENV := POSTGRES_ASYNC=true POSTGRES_SYNC=true
PYTEST := $(PYTEST_ENV) uv run pytest

.PHONY: test
test: $(VENV_MARKER)  ## Run the PR gate: every test except `external` (scope with PATHS=)
	$(PYTEST) $(PATHS)

# coverage has to own the invocation -- `coverage run -m pytest` -- so this takes the env
# prefix rather than $(PYTEST). pytest.ini still applies, so the selected set is identical.
.PHONY: test-cov
test-cov: $(VENV_MARKER)  ## Run the PR gate with coverage (scope with PATHS=)
	$(PYTEST_ENV) uv run coverage run -m pytest $(PATHS)
	uv run coverage xml

# One parameterised target rather than one per component, so the set of components can change
# without touching this file. `make test PATHS=data/store/tests` does the same job today, but
# only until the Phase 1 restructure (tj-55cczk) moves every path -- markers survive that,
# PATHS= does not. Component names are the `markers` list in pytest.ini.
#
# A COMPONENT that matches nothing is not silently green: pytest collects nothing and exits 5.
# The guard is for the EMPTY case only, which would otherwise hand pytest the unparseable
# expression " and not external" and report a usage error instead of the missing variable.
.PHONY: test-component
test-component: $(VENV_MARKER)  ## Run one component, e.g. COMPONENT=data_store (scope with PATHS=)
	@[ -n "$(COMPONENT)" ] || { echo "make test-component needs COMPONENT=<name>; the names are the 'markers' list in pytest.ini." >&2; exit 1; }
	$(PYTEST) -m "$(COMPONENT) and not external" $(PATHS)

# The only target that runs the `external` set, and the only one CI must never call: an
# external test needs a live third-party credential, and tj-59cce6 forbids a credential in a
# branch-triggered workflow. This is a target the USER runs on their own machine; an agent
# reports it NOT RUN rather than passed. It fails rather than skips when the account is not
# answering -- see the fail-never-skip comment in pytest.ini.
#
# Named `broker` while the marker is named `external` on purpose, not by oversight: pytest.ini
# records why.
.PHONY: test-broker
test-broker: $(VENV_MARKER)  ## Run the external broker tests: needs live credentials, never CI
	$(PYTEST) -m external $(PATHS)

# -m "" REPLACES the addopts filter rather than adding to it, leaving no selection at all, so
# this is the gate plus the external set. Same caveat as test-broker: it needs live credentials.
.PHONY: test-all
test-all: $(VENV_MARKER)  ## Run every test, external included: needs live credentials
	$(PYTEST) -m "" $(PATHS)

# THE SYSTEM SUITE (tj-vhboky.48, ADR tj-fdb9gz). tests/system/ drives a stack that is ALREADY UP
# AND MIGRATED -- `make dev-launch` (or prod-launch) and then `make migrate`. This target does
# neither: bringing a stack up or migrating it is a decision about which database is touched, and
# that decision stays with whoever runs it. pytest.ini keeps tests/system out of every other target
# by directory (norecursedirs), so this is the one target that names it. SYSTEM_PATHS scopes it to
# one file inside the suite; pointing it outside tests/system collects the gate, not the suite.
#
# THE DISPOSABLE-DATABASE GUARD. The suite WRITES to whatever database it is pointed at, and this
# machine also runs the production deployment. So the target refuses unless
# SYSTEM_TEST_DISPOSABLE_DB=1 is set -- as a make argument or in the environment. It is an
# attestation by the person running it, not a detection: nothing here can tell a disposable
# Postgres from the production one, which is exactly why it has to be said out loud each time.
#
# THE ENV CONTRACT the suite reads, defined here and nowhere else:
#   SYSTEM_TEST_DATA_STORE_URL  http://127.0.0.1:<DATA_STORE_PORT from .env>. Loopback, because
#                               docker-compose.yaml publishes data_store on 127.0.0.1 only.
#   DATABASE_NAME               127.0.0.1 -- the Postgres HOST. The name is data_store's own:
#                               data/store/app/database/database.py reads DATABASE_NAME as the
#                               host, so its modules connect runner-side unchanged, and a raw
#                               asyncpg connection is built from the same five names.
#   DATABASE_PORT, POSTGRES_USER, POSTGRES_PASS, POSTGRES_DB_NAME
#                               from .env. DATABASE_PORT is the host side of the loopback mapping.
#   DATABASE_CONN_TIMEOUT       10 (seconds). database.py reads it. Unset, a Postgres that ANSWERS
#                               but refuses (still starting, bad password) sends wait_for_db into
#                               comparing elapsed time against None: a TypeError, not a message
#                               naming the database. A refused TCP connect raises straight away.
#   INSTANCE_WRITE_SECRET       from .env -- the write path fails closed without it.
#   TZ                          $(SYSTEM_TEST_TZ). NON-UTC ON PURPOSE: CI runners are UTC, and a
#                               naive-to-timestamptz shift is invisible at offset zero, so a test
#                               of it could never go red there. Checked before pytest starts: a
#                               host without that zone's tzdata silently falls back to UTC.
#   POSTGRES_ASYNC, POSTGRES_SYNC
#                               the usual $(PYTEST_ENV) driver flags, as every test target.
#   PYTHONPATH                  the repository root, so `from data.store.app... import` resolves.
#                               The gate's test directories get the root from their __init__.py
#                               chain (data/store/tests -> data/store -> data); tests/system has
#                               no package above it, so pytest would insert tests/system instead.
#
# .env IS READ, NEVER SOURCED -- the reasoning of CI's Smoke Test step: sourcing would pull
# POSTGRES_PASS and the write secret into the shell, where any stray `set -x` or echo prints them.
# Each value is grepped out on its own, the recipe is not echoed, and the banner names the two
# secrets without their values. A required value that is missing or empty fails here, naming
# only the variable.
#
# A MISSING DATABASE FAILS, NEVER SKIPS (pytest.ini, FAIL, NEVER SKIP). And an empty selection is
# a failure too: pytest exits 5 when it collects nothing, and the recipe ends on pytest so make
# returns that status untouched.
SYSTEM_TEST_TZ := America/Toronto
SYSTEM_PATHS ?= tests/system

.PHONY: test-system
test-system: $(VENV_MARKER)  ## Run tests/system against an up, migrated stack (SYSTEM_TEST_DISPOSABLE_DB=1)
	@if [ "$(SYSTEM_TEST_DISPOSABLE_DB)" != "1" ]; then \
		echo "make test-system REFUSED: the system suite WRITES to the database it is pointed at," >&2; \
		echo "  and this machine also runs the production deployment." >&2; \
		echo "  Point .env at a disposable Postgres, bring the stack up and migrate it" >&2; \
		echo "  (make dev-launch, make migrate), then run:" >&2; \
		echo "    make test-system SYSTEM_TEST_DISPOSABLE_DB=1" >&2; \
		exit 1; \
	fi
	@[ -f .env ] || { echo "make test-system: no .env in $(CURDIR); the suite reads the stack's ports and credentials from it." >&2; exit 1; }
	@if [ "$$(TZ=$(SYSTEM_TEST_TZ) date +%z)" = "+0000" ]; then \
		echo "make test-system: TZ=$(SYSTEM_TEST_TZ) resolves to UTC here (no tzdata for it?)," >&2; \
		echo "  so a naive-to-timestamptz shift could not go red. Install tzdata and retry." >&2; \
		exit 1; \
	fi
	@env_value() { grep -E "^$$1=" .env | tail -n 1 | cut -d= -f2-; }; \
	data_store_port="$$(env_value DATA_STORE_PORT)"; \
	db_port="$$(env_value DATABASE_PORT)"; \
	db_user="$$(env_value POSTGRES_USER)"; \
	db_pass="$$(env_value POSTGRES_PASS)"; \
	db_name="$$(env_value POSTGRES_DB_NAME)"; \
	write_secret="$$(env_value INSTANCE_WRITE_SECRET)"; \
	missing=""; \
	[ -n "$$data_store_port" ] || missing="$$missing DATA_STORE_PORT"; \
	[ -n "$$db_port" ] || missing="$$missing DATABASE_PORT"; \
	[ -n "$$db_user" ] || missing="$$missing POSTGRES_USER"; \
	[ -n "$$db_pass" ] || missing="$$missing POSTGRES_PASS"; \
	[ -n "$$db_name" ] || missing="$$missing POSTGRES_DB_NAME"; \
	[ -n "$$write_secret" ] || missing="$$missing INSTANCE_WRITE_SECRET"; \
	if [ -n "$$missing" ]; then \
		echo "make test-system: missing or empty in .env:$$missing" >&2; \
		exit 1; \
	fi; \
	echo "System suite against data_store http://127.0.0.1:$$data_store_port and Postgres $$db_user@127.0.0.1:$$db_port/$$db_name, TZ=$(SYSTEM_TEST_TZ)"; \
	echo "POSTGRES_PASS and INSTANCE_WRITE_SECRET are set (values not shown)."; \
	$(PYTEST_ENV) TZ=$(SYSTEM_TEST_TZ) PYTHONPATH="$(CURDIR)" \
		SYSTEM_TEST_DATA_STORE_URL="http://127.0.0.1:$$data_store_port" \
		DATABASE_NAME=127.0.0.1 DATABASE_PORT="$$db_port" DATABASE_CONN_TIMEOUT=10 \
		POSTGRES_USER="$$db_user" POSTGRES_PASS="$$db_pass" POSTGRES_DB_NAME="$$db_name" \
		INSTANCE_WRITE_SECRET="$$write_secret" \
		uv run pytest $(SYSTEM_PATHS)
