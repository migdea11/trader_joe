# Define default shell
SHELL := /bin/bash

# Every target except $(VENV_MARKER) and $(VENV_PYTHON) is a command, not a file, and is declared
# .PHONY next to its own recipe so a new target is hard to add without one. Undeclared, a file or
# directory of the same name -- a test/ or build/ at the repo root -- makes the target "up to
# date": make runs nothing and exits 0, so `make test` would report success having run no test
# (tj-06uflo).
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

# THE ENVIRONMENT AND ITS MARKER (tj-3t2axg). VENV_DIR is the environment uv itself uses:
# UV_PROJECT_ENVIRONMENT when set, else uv's own default, .venv. The devcontainer sets it to a
# container-only directory, because /workspace is the host's checkout bind-mounted in: sharing one
# .venv, each side's uv rewrote bin/python to an interpreter only it has, and the other side's
# next `uv run` silently recreated the venv with default-groups only (no sqlalchemy, no asyncpg)
# while the marker still said "synced". Relative, uv resolves it against the project root, so
# every worktree still gets its own.
VENV_DIR := $(or $(UV_PROJECT_ENVIRONMENT),.venv)
VENV_PYTHON := $(VENV_DIR)/bin/python
# The marker lives INSIDE the environment it vouches for, so whenever uv deletes and recreates that
# environment -- which is what it does to one whose interpreter is gone -- the marker goes with it
# and the next target re-syncs in full, instead of trusting a marker left beside a different venv.
VENV_MARKER := $(VENV_DIR)/.trader_joe_synced

# A missing or dangling interpreter forces the full sync below. make stats through the symlink, so
# a bin/python pointing at an interpreter that does not exist here counts as missing, runs this
# rule, and leaves the marker out of date. Checked before any uv command runs, which is the point:
# the first `uv run` would otherwise rebuild the venv itself, with default-groups only.
$(VENV_PYTHON):
	@echo "No working interpreter at $@: syncing $(VENV_DIR) in full."
	@rm -f $(VENV_MARKER)

# The marker also depends on the dependency declarations so the sync re-runs when they change,
# instead of going stale behind a marker file that already exists.
$(VENV_MARKER): pyproject.toml uv.lock $(VENV_PYTHON)  ## Internal option to install uv and sync the virtual environment
	$(UV_PIN_CHECK)
	@if [ ! -d $(VENV_DIR) ]; then \
		echo "Creating uv venv"; \
		uv venv $(VENV_DIR); \
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
# Dev gains devnet through the override, which attaches every stack service to it (tj-q9ae5u
# addendum 1). PROD_COMPOSE never loads the override, so a prod launch never attaches devnet --
# which is also what keeps a dev session off a prod stack on the same machine.
DEV_COMPOSE := docker compose -f docker-compose.yaml -f docker-compose.override.yaml
TOOLS_COMPOSE := $(DEV_COMPOSE) -f docker-compose.tools.yaml

# The dev network: an ordinary bridge (NOT internal) owned by neither compose project, which the
# dev stack, pgAdmin and the agent devcontainer all join, so either side can start first. Its
# fixed name appears in exactly four places, which must agree: this variable,
# docker-compose.override.yaml, .devcontainer/compose.yml and the initializeCommand in
# .devcontainer/devcontainer.json.
DEV_NETWORK := trader_joe_devnet

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
# even when the dev stack is what is up -- postgres is the same container either way, and the
# one-off data_store container joins data_store's prod networks, store_db included, which is how
# it reaches the database. It needs no egress and no devnet.
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

# Idempotent, and safe against a concurrent create by the devcontainer's initializeCommand: look,
# else create, else look again -- a create that lost the race fails, and the second look is what
# decides. The create's error is left visible: in a lost race it is one harmless line, and in a
# real failure (daemon down, no permission) it is the only line that says why. Ordinary bridge on
# purpose: devnet is the network the dev loopback publishes go out through, and an internal one
# has no gateway to publish through. Nothing here ever removes it: no down, prune or clean recipe
# names it, and compose never removes an external network.
.PHONY: dev-network
dev-network:  ## Create the shared dev network if it is missing (never removed)
	@docker network inspect $(DEV_NETWORK) > /dev/null 2>&1 \
		|| docker network create --driver bridge $(DEV_NETWORK) > /dev/null \
		|| docker network inspect $(DEV_NETWORK) > /dev/null

.PHONY: dev-deps
dev-deps: $(VENV_MARKER) dev-network  ## Start the development dependencies (postgres, kafka)
	$(DEV_COMPOSE) up -d postgres kafka

# pgAdmin is a tool, not a dependency: it lives in docker-compose.tools.yaml, which dev-deps and
# dev-launch never load (tj-ae3n49). This is the on-demand spelling, and the one target that
# needs PGADMIN_EMAIL/PGADMIN_PASS set. compose brings postgres up first, because pgadmin
# depends on it being healthy. Stop it with dev-down.
.PHONY: dev-tools
dev-tools: $(VENV_MARKER) dev-network  ## Start pgAdmin against the development database (needs PGADMIN_*)
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
agent-up: dev-network  ## Start the agent devcontainer
	mkdir -p "$(AGENT_HOME_PATH)"
	$(AGENT_COMPOSE) up -d

.PHONY: agent-down
agent-down:  ## Stop the agent devcontainer (host config dir is kept)
	$(AGENT_COMPOSE) down

.PHONY: agent-attach
agent-attach:  ## Open a shell inside the agent devcontainer
	$(AGENT_COMPOSE) exec agent bash

# dev-down, not prod-down: this is a workstation target -- it deletes the venv -- and dev-down is
# the one teardown that loads docker-compose.tools.yaml, so going through it leaves no pgAdmin
# behind.
.PHONY: clean
clean: dev-down  ## Clean up the project
	rm -rf $(VENV_DIR)
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

# semgrep runs with --error, so a finding fails this target (and the CI step) instead of printing
# and exiting 0 (tj-cg2i9p).
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
	uv run semgrep --config=auto --error --exclude=tests/ --exclude=.venv --exclude=docker-compose.override.yaml --exclude=.claude/worktrees .
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

# THE SYSTEM SUITE (tj-vhboky.48, ADR tj-fdb9gz; tj-q9ae5u addendum 1 items 4' and 6').
# tests/system/ drives a stack that is ALREADY UP AND MIGRATED -- `make dev-launch` (or prod-launch)
# and then `make migrate`. This target does neither: bringing a stack up or migrating it is a
# decision about which database is touched, and that decision stays with whoever runs it.
# pytest.ini keeps tests/system out of every other target by directory (norecursedirs), so this is
# the one target that names it.
#
# THE SUITE RUNS FROM A CLIENT CONTAINER, test_client in docker-compose.test-client.yaml, on the
# stack's own networks -- store_api for data_store's API, store_db for Postgres -- never from the
# host, which in prod reaches neither: prod publishes nothing. store_db (like every other unfixed
# network) resolves as <compose project>_store_db, and the project name defaults to the checkout's
# directory name, so this target joins the running stack only when run from a checkout whose
# directory name matches the one that launched it; store_api is fixed-name and unaffected.
# SYSTEM_PATHS scopes it, relative to /code in the container, where only tests/system is mounted;
# a path outside it fails the target with a non-zero pytest status -- 4 when the path does not
# exist in the container, 5 when it exists but collects nothing.
#
# THE DISPOSABLE-DATABASE GUARD. The suite WRITES to whatever database it is pointed at. This target
# is run on the HOST by its owner -- never by an agent, whose container has no Docker. prod-launch
# lives in this same Makefile and this same compose project, so that host may be the one running
# the production deployment, and the client would join that stack's networks and write to its
# database. So the target refuses unless SYSTEM_TEST_DISPOSABLE_DB=1 is set -- as a make argument or
# in the environment. It is an attestation by the person running it, not a detection: nothing here
# can tell a stack you can wipe from the production one, which is why it has to be said out loud
# each time.
#
# THE ENV CONTRACT the suite reads -- the twelve names tests/system/conftest.py checks through its
# _contract() -- is SET in docker-compose.test-client.yaml, in test_client's environment block, and
# documented here:
#   DATABASE_NAME               postgres: the compose SERVICE name, the Postgres HOST on store_db.
#                               The name is data_store's own: data/store/app/database/database.py
#                               reads DATABASE_NAME as the host, so its modules connect unchanged,
#                               and a raw asyncpg connection is built from the same five names.
#   DATABASE_PORT               5432, the container port: the client is on the network.
#   SYSTEM_TEST_DATA_STORE_URL  http://data_store:<APP_INTERNAL_PORT>, the SERVICE name on store_api.
#   POSTGRES_USER, POSTGRES_PASS, POSTGRES_DB_NAME, INSTANCE_WRITE_SECRET
#                               interpolated by compose from .env, with no :? guard -- CI blanks the
#                               write secret mid-job and still needs the client. conftest's
#                               _contract() fails naming a missing one (fail, never skip).
#   DATABASE_CONN_TIMEOUT       10 (seconds). database.py reads it. Unset, a Postgres that ANSWERS
#                               but refuses (still starting, bad password) sends wait_for_db into
#                               comparing elapsed time against None: a TypeError, not a message
#                               naming the database. A refused TCP connect raises straight away.
#   TZ                          America/Toronto. NON-UTC ON PURPOSE: CI runners are UTC, and a
#                               naive-to-timestamptz shift is invisible at offset zero, so a test
#                               of it could never go red there. The image carries tzdata, and the
#                               container's entrypoint refuses to start pytest at offset +0000.
#   POSTGRES_ASYNC, POSTGRES_SYNC
#                               the driver flags, as $(PYTEST_ENV) sets them for every host target.
#   PYTHONPATH                  /code, the mount root, so `from data.store.app... import` resolves:
#                               tests/system has no package chain above it.
#
# THE RECIPE READS NO .env VALUE; .env only has to exist. Compose interpolates the credentials into
# the container itself, so no secret passes through make's shell, a command line or this recipe's
# output. No $(VENV_MARKER) prerequisite, for the reason `migrate` has none: nothing here runs in
# the host venv, and the host may be a server with no usable uv.
#
# A MISSING DATABASE FAILS, NEVER SKIPS (pytest.ini, FAIL, NEVER SKIP). And an empty selection is a
# failure too: pytest exits 5 when it collects nothing, and the recipe ends on the compose run,
# whose exit status is pytest's, so make returns it untouched.
#
# The client invocation: the base file for the networks, the client file, and nothing else -- not
# the dev override, so the client needs no devnet and behaves the same against a dev stack and in CI.
TEST_CLIENT_COMPOSE := docker compose -f docker-compose.yaml -f docker-compose.test-client.yaml
SYSTEM_PATHS ?= tests/system

.PHONY: test-system
test-system:  ## Run tests/system from the test_client container against an up, migrated stack (SYSTEM_TEST_DISPOSABLE_DB=1)
	@if [ "$(SYSTEM_TEST_DISPOSABLE_DB)" != "1" ]; then \
		echo "make test-system REFUSED: the system suite WRITES to the database it is pointed at," >&2; \
		echo "  and this host may also run the production deployment, whose networks the client would join." >&2; \
		echo "  Run it only against a stack you can wipe: bring one up and migrate it" >&2; \
		echo "  (make dev-launch, make migrate), then run:" >&2; \
		echo "    make test-system SYSTEM_TEST_DISPOSABLE_DB=1" >&2; \
		exit 1; \
	fi
	@[ -f .env ] || { echo "make test-system: no .env in $(CURDIR); compose interpolates the stack's credentials from it." >&2; exit 1; }
	@echo "System suite from test_client against data_store (service data_store, on store_api) and Postgres (service postgres, on store_db); TZ set in docker-compose.test-client.yaml and checked by the client's entrypoint."
	@echo "The database password and the instance write secret reach the container from .env through compose (values not shown)."
	$(TEST_CLIENT_COMPOSE) run --rm --no-deps --build test_client $(SYSTEM_PATHS)
