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

# THE BUF PIN (tj-3mk3u5.54). This file is its one authority. buf lints, format-checks and
# breaking-checks proto/ inside `make lint` (the BUF section, above `lint`); it generates nothing.
# The checksums are the release's own sha256.txt lines for the bare buf-Linux-<arch> binaries.
# THE ONE MIRROR is .devcontainer/Dockerfile's buf RUN: its build context is .devcontainer/, so it
# cannot read this file, and common/tests/test_agent_image.py pins the two equal. CI reaches the pin
# through `make buf-install` and holds no copy. A bump is these three values and the Dockerfile's
# three, in one commit, then the agent-image rebuild.
BUF_VERSION := 1.73.0
BUF_SHA256_X86_64 := 8f2986298ad08f0cc1bf999b9797b7c383adf32d7edf0f73d6f1e1a701baeac1
BUF_SHA256_AARCH64 := 902b75267db7f4391e99b7fa0756050e5354234cc0437ef50eee9c788950c7a3

# THE SHELLCHECK PIN (tj-xc6nfv). Same shape as the buf pin above, and this file is its one
# authority. shellcheck lints this repository's *.sh inside `make lint` (the SHELLCHECK section,
# above `lint`); it generates and formats nothing.
# WHY IT EXISTS: before this pin, essentially nothing in `make lint` or `make security` read the
# shell in this tree. ruff is Python, buf is proto, bandit is Python, pip-audit is the lockfile. The
# one exception is worth stating precisely rather than rounding away: `semgrep --config=auto` does
# run a BASH rule set, and on this tree that is FOUR rules over six files, against the 337 Python
# rules it runs beside them. Four generic rules are not a shell linter. So the real static check the
# ~770 lines of bash here had ever had was `bash -n`, which establishes that a file parses and
# nothing else. That gap sat under tools/source_digest.sh and server/data/store/run_migrations.sh, which
# are a digest tool and a verification guard: the least-checked code in the tree was the code a
# human is asked to trust. (And `make security` is still the wrong home for this -- shellcheck is a
# linter, and that target must keep running the identical invocation CI runs.)
# THE CHECKSUMS ARE OURS, NOT UPSTREAM'S, and that is the one difference from buf. buf publishes a
# sha256.txt with its release and these values are copied from it; shellcheck publishes no checksum
# asset at all, so the two below are the sha256 of the artifacts verified at adoption (v0.11.0,
# released 2025-08-04, fetched and run on 2026-10-04). The security property is the same from here
# on -- a later re-upload under the same tag fails the check -- but the first fetch was trust-on-
# first-use, which buf's was not. Say so rather than letting a reader assume an upstream manifest.
# .tar.gz, NOT the smaller .tar.xz, because the agent image has no xz binary (measured: `tar -tJf`
# fails with "xz: Cannot exec"), while tar and gzip are Essential in Debian and present everywhere
# this runs, CI's debian:bookworm-slim included.
# THE ONE MIRROR is .devcontainer/Dockerfile's shellcheck RUN, for the same reason buf has one:
# that build's context is .devcontainer/, so it cannot read this file. A bump is these three values
# and the Dockerfile's three, in one commit, then the agent-image rebuild.
SHELLCHECK_VERSION := 0.11.0
SHELLCHECK_SHA256_X86_64 := b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6
SHELLCHECK_SHA256_AARCH64 := 68a8133197a50beb8803f8d42f9908d1af1c5540d4bb05fdfca8c1fa47decefc

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
# re-lock either -- a new dependency reaches the venv only after `make lock`. agent-mcp is left out
# as well: it belongs to the agent-stack MCP's own image (tools/agent_mcp/Dockerfile) and nothing
# that runs in this venv imports it.
	uv sync --all-groups --no-group security --no-group agent-mcp
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

# Every group but agent-mcp, which only tools/agent_mcp/Dockerfile installs (see the venv sync).
.PHONY: init
init: $(VENV_MARKER)  ## Initialize the project, including the security tooling
	uv sync --all-groups --no-group agent-mcp

# THE gRPC CODEGEN (ADR tj-8konfu D1 and D3, re-homed by addendum A1; decision tj-3mk3u5.42 F1).
# proto/ at the repository root is the source of truth for what crosses the wire, and
# gen/proto/python/ is the COMMITTED Python tree protoc writes from it: one tree per language
# (gen/proto/<language>/), used by every Python consumer. This target is the one way that tree is
# written: run it after any .proto change and commit both together. CI's lint-and-test job runs it and
# fails on any difference, because committed generated code without that check is worse than
# generating at build time -- a committed copy can go stale, and a build-time one cannot.
#
# THE IMPORT TRAP, AND WHY THE PLAIN ROOT AVOIDS IT. protoc names every generated module, and writes
# every import in it, after the .proto's path under the include root -- never after where the output
# lands, and never after the proto package. With the plain -I$(PROTO_SRC), the output tree therefore
# mirrors the proto packages: trader_joe/proto/ping/v1/ping.proto becomes
# $(PROTO_GEN)/trader_joe/proto/ping/v1/ping_pb2.py, and ping_pb2_grpc.py imports it as
# `from trader_joe.proto.ping.v1 import ping_pb2`. So imports resolve as protoc writes them, one .proto
# can import another by its canonical path, and every descriptor records its canonical file name -- the
# name every other consumer generating from proto/ records too. gen/proto/python reaches Python by
# CONFIGURATION, never by code, in four places: pytest.ini's pythonpath, the image's PYTHONPATH plus
# its COPY of ./gen/proto/python (Dockerfile), a bind mount beside every ./common mount (the compose
# files), and docker-compose.yaml's environment: PYTHONPATH on every service built from the service
# stages -- literal, and what makes the image's value hold under compose, because the root env file's
# legacy PYTHONPATH=./ arrives through env_file:, which outranks an image's ENV (decision
# tj-3mk3u5.42 addendum F1-A). NOTHING EDITS THE OUTPUT: no rewrite, no post-processing, no check of
# import lines.
# What is committed is protoc's, byte for byte.
#
# TWO GUARDS, before anything is deleted. $(PROTO_PKG)/__init__.py, hand-committed and the one file
# there protoc does not write, must exist, so the target never clears a directory that is not the
# generated package. And every .proto must lie under $(PROTO_SRC)/trader_joe/proto/, the reserved root
# (every package is trader_joe.proto.<domain>.v1): a file anywhere else would generate outside the
# directory cleared below, so what this target clears would no longer equal what it writes. Then
# everything in $(PROTO_PKG) but its __init__.py is deleted, so a removed .proto leaves no stale module
# behind. gen/proto/python/trader_joe/ gets no __init__.py, ever: trader_joe is a PEP 420 namespace the
# hand-written trader_joe.common and trader_joe.client will share, and one __init__.py there makes the
# other portions unimportable. Inputs are sorted for a stable command line. grpcio-tools is pinned
# exactly in pyproject.toml, because its version is written into every file it emits.
#
# A SCRATCH TREE runs the real recipe without touching this one: override both roots on the command
# line, e.g. `make proto PROTO_SRC=/tmp/x/proto PROTO_GEN=/tmp/x/gen/proto/python`. PROTO_PKG follows
# PROTO_GEN, and the scratch PROTO_PKG needs its own __init__.py first.
PROTO_SRC := proto
PROTO_GEN := gen/proto/python
PROTO_PKG := $(PROTO_GEN)/trader_joe/proto

.PHONY: proto
proto: $(VENV_MARKER)  ## Regenerate gen/proto/python/ from proto/ (commit both; CI fails on a stale tree)
	@[ -f "$(PROTO_PKG)/__init__.py" ] || { echo "make proto: $(PROTO_PKG)/__init__.py is missing; refusing to clear a directory that is not the generated package." >&2; exit 1; }
	@outside="$$(find "$(PROTO_SRC)" -name '*.proto' ! -path "$(patsubst %/,%,$(PROTO_SRC))/trader_joe/proto/*" | LC_ALL=C sort)"; \
	[ -z "$$outside" ] || { \
		echo "make proto: every .proto must lie under $(PROTO_SRC)/trader_joe/proto/ (package trader_joe.proto.<domain>.v1); refusing, generating nothing. Outside it:" >&2; \
		printf '%s\n' "$$outside" | sed 's/^/  /' >&2; \
		exit 1; }
	find "$(PROTO_PKG)" -mindepth 1 -maxdepth 1 ! -name __init__.py -exec rm -rf {} +
	uv run python -m grpc_tools.protoc -I$(PROTO_SRC) --python_out=$(PROTO_GEN) --grpc_python_out=$(PROTO_GEN) --pyi_out=$(PROTO_GEN) \
		$$(find $(PROTO_SRC) -name '*.proto' | LC_ALL=C sort)

# THE ERROR CATALOGUE (ADR tj-fa1rpu, the Q-URI addendum of 2026-10-02; tj-3mk3u5.37.10). docs/errors.md
# is generated from the ONE Reason table in server/common/errors, and is committed like gen/proto/ above and for
# the same reason: this is the proto pattern (ADR tj-8konfu D3), one target that writes it and a CI step
# that regenerates and fails on any difference. Nobody edits the generated file -- change the table in
# server/common/errors and regenerate in the same commit. The generator is standard-library only and imports
# common.errors and nothing else of ours, so this needs no service, no transport and no database.
#
# `--check` writes nothing and exits non-zero when the committed file differs, which is what the
# validator's in-suite twin of the CI step drives; `--path` points either mode at a scratch copy.
#
# PYTHONPATH IS SET HERE AND THIS IS THE ONLY HOST TARGET THAT NEEDS IT (tj-iontkq.4). The generator
# runs on the host, outside pytest, so it gets neither pytest.ini's pythonpath nor the image's ENV --
# the two places the import root is otherwise configured. Before the trees moved, `python -m` putting
# the working directory on sys.path was enough, because common/ sat at the repository root; it now
# sits under server/ and that stopped being true. The roots and their order are the pair
# tools/tests/test_errors_doc.py already derives as the two a first-party import can resolve against:
# the server root first, as the service image has it. THE TEST KNEW AND THE INVOCATION DID NOT, which
# is why CI broke here and the suite stayed green -- nothing in the suite runs this target.
.PHONY: errors-doc
errors-doc: $(VENV_MARKER)  ## Regenerate docs/errors.md from server/common/errors (commit it; CI fails on a stale file)
	PYTHONPATH="$(CURDIR)/server:$(CURDIR)" uv run python -m tools.errors_doc

# Every compose target goes through one of these, and none omits -f. A bare
# `docker compose` auto-loads docker-compose.override.yaml, which is what made `launch`
# start the dev images while its help text claimed production (tj-6ap2vw).
#
# The two stacks differ only in the override: dev_image (RUN_MODE=dev, --reload, the dev
# dependency group, and the debugger of tj-g1qqf1), source bind mounts so reload sees host
# edits, LOG_LEVEL=debug, and LATENCY_TEST_ENABLED=true on data_store and data_ingest. The
# first three change how the services are built and how loudly they log; the last changes what
# they DO at startup -- both stand up the latency client and server, and data_store serves
# GET /latency (tj-8mt207). That is the whole reason
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
# THE AGENT STACK (ADR tj-4rr0la section 1 and addendum 1). The isolated stack the agent-stack MCP
# drives from an agent's worktree, under its own compose project so it never shares a container,
# network or volume with the stack prod-launch or the dev targets start (both use the default
# project, the checkout's directory name). The base file, the test client, then
# docker-compose.agent-stack.yaml so its names and image tags win over both, then the fake-mode
# overlay docker-compose.fake.yaml AFTER it (tj-vhboky.61; ADR tj-4rr0la addendum 3 (3)), so the
# agent stack's data_ingest always runs on FakeRead -- it has no egress and no broker key anyway,
# and the fake overlay touches nothing the agent-stack overlay sets. The server passes its
# generated env file as well, never the user's .env, and that env also points the services' env
# files outside the repository (ROOT_ENV_FILE, STORE_ENV_FILE, INGEST_ENV_FILE; addendum 2). No
# target here uses these: the MCP server is their only reader. And the mirror of the rule above:
# PROD_COMPOSE, DEV_COMPOSE and TOOLS_COMPOSE never load the agent-stack overlay, and this set
# never loads the dev override or the tools file -- no devnet, no publish.
AGENT_STACK_PROJECT := trader_joe_agent_stack
AGENT_STACK_COMPOSE := docker compose -p $(AGENT_STACK_PROJECT) -f docker-compose.yaml -f docker-compose.test-client.yaml -f docker-compose.agent-stack.yaml -f docker-compose.fake.yaml

# The dev network: an ordinary bridge (NOT internal) owned by neither compose project, which the
# dev stack, pgAdmin and the agent devcontainer all join, so either side can start first. Its
# fixed name appears in exactly four places, which must agree: this variable,
# docker-compose.override.yaml, .devcontainer/compose.yml and the initializeCommand in
# .devcontainer/devcontainer.json.
DEV_NETWORK := trader_joe_devnet

# --wait, matching the CI deploy step: it blocks until every started service reports
# healthy and exits non-zero if one does not, so a broken deploy fails the command instead
# of printing a cheerful "Started". 300s leaves room above data_store's 60s start_period
# and data_ingest's own. Detached is the consequence -- `prod-logs` is how you watch it.
PROD_UP := $(PROD_COMPOSE) up -d --wait --wait-timeout 300

# THE SOURCE STAMP (decision tj-yb1bxj clauses 1 and 5). tools/source_digest.sh is the ONE
# definition of "a digest of exactly the source the Dockerfile COPYs"; both sides -- the build
# here and the check in migrate-check (tj-ymsobh) -- call that same script, because two spellings
# of "hash the source" would drift and a drifting comparison fails closed forever, which just
# trains people to set the escape hatch.
#
# docker-compose.yaml interpolates SOURCE_DIGEST_DATA_STORE and SOURCE_DIGEST_DATA_INGEST from the
# environment of whoever runs the build, so the build recipes export them. Two variables, not one:
# the services COPY different app directories and so have different digests.
#
# NOT a `make` variable with $(shell ...): make expands an exported variable for EVERY recipe's
# environment, so the tree would be hashed twice on `make test`, `make lint` and everything else.
# Computed in the build recipes only.
#
# THE ASSIGNMENT IS SPLIT FROM THE export ON PURPOSE. `export VAR="$(cmd)"` takes its exit status
# from the export builtin, which is 0 whatever cmd did, so `set -e` would not see the script fail
# and compose would be handed an empty stamp -- an image silently marked unverifiable because of a
# bug, which is the quiet failure this whole epic is about. A plain assignment propagates the
# command substitution's status, so `set -e` fires and the build stops with the script's message.
define WITH_SOURCE_STAMP
set -euo pipefail; \
SOURCE_DIGEST_DATA_STORE="$$(./tools/source_digest.sh data store)"; \
SOURCE_DIGEST_DATA_INGEST="$$(./tools/source_digest.sh data ingest)"; \
export SOURCE_DIGEST_DATA_STORE SOURCE_DIGEST_DATA_INGEST;
endef

.PHONY: prod-build
prod-build: $(VENV_MARKER)  ## Build the production images (:latest), stamped with the source digest
	$(WITH_SOURCE_STAMP) $(PROD_COMPOSE) build

.PHONY: prod-build-clean
prod-build-clean: $(VENV_MARKER)  ## Build the production images from scratch, no cache
	$(WITH_SOURCE_STAMP) $(PROD_COMPOSE) build --no-cache

.PHONY: prod-deps
prod-deps: $(VENV_MARKER)  ## Start the production dependencies (postgres)
	$(PROD_UP) postgres

.PHONY: prod-launch
prod-launch: prod-deps  ## Start the production services, waiting for healthy
	$(PROD_UP) data_store data_ingest

.PHONY: prod-logs
prod-logs:  ## Follow the production service logs
	$(PROD_COMPOSE) logs -f data_store data_ingest

# --remove-orphans, for the reason agent-up carries it (addendum 11 R3, line 633) and
# stack_down_steps carries it: a service DROPPED from the compose file leaves a container this
# project owns and no longer declares, and a plain `down` leaves it running with only a warning.
# That is not hypothetical here -- a kafka container outlived the service's deletion and failed a
# verification run (tj-citjd6). IT DELETES, and on the production path: any container in this
# project whose service is no longer in the file goes, not just the one you were thinking of. That
# is what "removes this stack cleanly" has always promised, and the flag is what makes it true.
.PHONY: prod-down
prod-down:  ## Stop the production stack
	$(PROD_COMPOSE) down --remove-orphans

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
	./server/data/store/run_migrations.sh

# READ-ONLY, and the approval for this target was conditional on staying that way: `current`
# reads the alembic_version table, `history` reads the revision files, and neither writes
# anything. Nothing may be added here that mutates -- a mutating step belongs behind its own
# named target, the way `migrate` is. `alembic check` is NOT read-only and so lives behind
# `migrate-check` below, which says why.
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
	./server/data/store/run_migrations.sh current
	./server/data/store/run_migrations.sh history

# THE DRIFT DIAGNOSTIC, AND IT IS NOT READ-ONLY -- which is the whole reason it is a target of its
# own rather than a third line of `migrate-status`.
#
# What it buys: `alembic check` compares the models with the live catalogue, and so sees what
# `current` structurally cannot. Bug tj-5h30md is the case -- the database sat at head
# eec8f88a7443 with uq_stock_market_activity_natural_key manually dropped, and `current` read
# alembic_version, reported head, and was right, because a manual DROP leaves that table
# untouched. Nothing surfaced the drift until every bar upsert returned 500.
#
# What it costs, MEASURED, not reasoned (validator, on tj-o82yyu): unlike `current`, `check` does
# not pass dont_mutate=True, so MigrationContext.run_migrations reaches _ensure_version_table,
# whose body is `self._version.create(self.connection, checkfirst=True)` -- a CREATE TABLE. On a
# throwaway never-migrated SQLite database, `current` wrote nothing while `check` raised
# CommandError AND created alembic_version. So it is read-only only against a database that has
# already been migrated, and the case where it is not is a first run or a wiped volume -- exactly
# when an operator reaches for a status command. ADR tj-x3ig38's 2026-10-02 addendum item 8 says
# `check` writes nothing; that item is wrong, and this is the measurement that refutes it.
#
# Hence the split. `migrate-status` keeps a promise its approval was conditional on, this target
# states its cost in its own help line, and the allowlist in common/tests/test_ci_invariants.py
# stays as it is -- it names ensure_version among the writers it excludes, so it was right to
# refuse `check`, and widening it would have bought a true-looking label over a false claim.
#
# A non-zero exit means drift, or "Target database is not up to date." when the database is behind
# head. BLIND SPOTS: autogenerate compares neither enum labels nor server defaults, so a clean run
# is no evidence about either (ADR tj-x3ig38 addendum items 2 and 4; migrations/env.py says why).
#
# AND WHICH MODELS IT COMPARES IS NOW ESTABLISHED RATHER THAN ASSUMED (decision tj-yb1bxj). env.py
# imports the models from /code -- in the IMAGE -- while only alembic.ini and migrations/ are bind
# mounted, so `check` has always compared the IMAGE'S models against the live schema, and a stale
# image reports phantom drift. run_migrations.sh refuses, before alembic runs, unless the image
# carries a source stamp equal to this checkout's digest, and prints both on EVERY outcome. The
# guard lives in the script and not in this recipe because the script owns the `-f
# docker-compose.yaml` pin that decides which image the one-off container uses; the reasoning is
# written out there. It is gated on the alembic command being `check`, so `migrate` is untouched.
#
# THE HELP LINE NAMES THE REFUSAL, THE ORDINARY UNSTAMPED CASE AND THE ESCAPE HATCH, AND POINTS AT
# run_migrations.sh FOR THE REST (decision tj-yb1bxj addendum 2, item 2 — amending tj-ymsobh R4's
# PLACEMENT, not its substance). R4 originally asked for all of this in the help line too, and a
# literal reading produced a ~580-character single unwrapped `make help` row, roughly five times
# the next-longest target's — worse for every target's help in order to document one. The limit
# that extra text existed to carry — the stamp covers COPYd source, not the Dockerfile's own
# instructions, so a changed `uv sync` group set still reads as fresh — lives in
# verify_comparison_image's comparison comment and in the guard's own printed output on the MATCH
# outcome (both in run_migrations.sh), which is where a reader meets it at the moment they are
# already puzzled about why a check passed. That was R4's actual purpose, and a help row nobody
# reads documents nothing.
.PHONY: migrate-check
migrate-check:  ## Compare the models with the live schema (writes alembic_version if absent). REFUSES unless the data_store image's source stamp matches this checkout's digest; an unstamped CI or system-launch image is the ordinary case, not a fault -- make prod-build fixes it. What the stamp does and does not cover: see the comment above. Escape hatch: MIGRATE_CHECK_ALLOW_STALE_IMAGE=1
	./server/data/store/run_migrations.sh check

.PHONY: dev-build
dev-build: $(VENV_MARKER)  ## Build the development images (:dev), stamped with the source digest
	$(WITH_SOURCE_STAMP) $(DEV_COMPOSE) build

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
dev-deps: $(VENV_MARKER) dev-network  ## Start the development dependencies (postgres)
	$(DEV_COMPOSE) up -d postgres

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
# That handles the service-in-a-file-you-forgot-to-load case; --remove-orphans handles the other
# one, a service DROPPED from the file entirely, which widening the file list cannot reach. See
# prod-down for what the flag deletes.
#
# The placeholder PGADMIN_* values exist only to get past the ":?" guards, so that stopping the
# stack never requires pgAdmin credentials. They are safe here and ONLY here: `down` creates no
# container, so no pgAdmin account can ever be initialised from them. Shell variables outrank
# .env in compose interpolation, which is why they must never be copied onto an `up`.
.PHONY: dev-down
dev-down:  ## Stop the development stack, pgAdmin included
	PGADMIN_EMAIL=unused PGADMIN_PASS=unused $(TOOLS_COMPOSE) down --remove-orphans

.PHONY: dev-prune
dev-prune: ## Prune development services
	docker container prune -f && docker volume prune -f && docker image prune -f

# Removed spellings. Each one used to resolve a stack its name did not state: `launch` and
# `launch-deps` ran dev while the help text said production, and `build` produced prod
# images no target ever started (tj-6ap2vw). Failing here rather than deleting the names
# outright means muscle memory gets a pointer instead of picking a stack silently -- and
# server/data/store/run_migrations.sh still names `make launch-deps` in its error path, so that
# hint degrades into this message rather than into nothing. No `##`: `make help` lists the
# real targets only.
.PHONY: build build-clean launch launch-deps launch-down
build build-clean launch launch-deps launch-down:
	@echo "'make $@' is gone: it did not state which stack it meant (tj-6ap2vw)." >&2
	@echo "Use the prod-* or dev-* target for the stack you want -- see 'make help'." >&2
	@exit 1

# THE ROOT ENV FILE, NAMED EXPLICITLY, and the reason it has to be. Compose takes its project
# directory from the FIRST compose file's directory, so `-f .devcontainer/compose.yml` makes
# .devcontainer the project directory even though make runs from the repo root -- and the env file
# compose reads by default is therefore .devcontainer/.env, NOT the root .env where the two dev
# credentials that file interpolates actually live (the same reason agent-mcp-up below passes
# --env-file /dev/null: without it compose reads the PROJECT DIRECTORY's env file, whatever that
# happens to be). Without this flag both values would fall back to the sentinel defaults
# .devcontainer/compose.yml documents, which is a working-looking container with a dead password.
#
# ABSOLUTE ($(CURDIR)), because compose has resolved a relative --env-file against the invoking
# directory in some versions and against the project directory in others; an absolute path means
# the same file under either rule. /dev/null when this checkout has no root .env, so agent-down
# and agent-build still work on a checkout nobody has configured -- compose refuses outright when
# a named --env-file is missing, and a devcontainer you cannot stop is a worse failure than a
# credential you do not have.
#
# NOTHING IS READ INTO MAKE'S SHELL. Compose interpolates on the HOST, out of this file, and only
# the keys named one by one in that compose file's environment: block reach the container. Never
# an env_file: entry pointing here -- that injects EVERY key in the root file, ALPACA_API_KEY and
# ALPACA_API_SECRET with it, which the user's ruling keeps out of the agent container entirely
# (decision record tj-izzqub addendum 1, rulings (a) and (d)).
AGENT_ENV_FILE := $(if $(wildcard $(CURDIR)/.env),$(CURDIR)/.env,/dev/null)
AGENT_COMPOSE := docker compose --env-file $(AGENT_ENV_FILE) -f .devcontainer/compose.yml

# The agent config directory holds the session transcripts, bind-mounted from the host.
# devcontainer.json's initializeCommand creates it when an IDE starts the container, but that
# hook does not run for a plain `compose up` — so agent-up creates it too. Docker would otherwise
# create the missing bind source as a root-owned directory the agent user cannot write to.
# Exported so compose.yml resolves the same path this file does.
export AGENT_HOME_PATH ?= $(HOME)/.claude-agent-homes/trader_joe
# The agent-stack MCP's SHARE directory (ADR tj-4rr0la addendum 11 R2): the token file and seed
# output, and nothing else -- never inside AGENT_HOME_PATH, the user's Claude config, which the MCP
# must not see. Bound at /agent_mcp_share in both the devcontainer and agent_mcp.
# TRANSIENT BY DESIGN (the user's ruling on tj-c4mosr.4): $XDG_RUNTIME_DIR (/run/user/<uid>: per-user
# tmpfs, wiped at logout or reboot), or /tmp/trader_joe_agent_mcp-<uid> only when XDG_RUNTIME_DIR is
# unset or empty. The server regenerates the token when it is missing. agent-mcp-share creates it 0700
# and refuses one that is a symlink, not ours or not 0700. One overridable variable; the default is
# spelled the same in three places, which must agree: here, .devcontainer/compose.yml and the
# initializeCommand in .devcontainer/devcontainer.json. Computed once (:=), so `id` runs once.
ifeq ($(strip $(AGENT_MCP_SHARE_PATH)),)
AGENT_MCP_SHARE_PATH := $(if $(XDG_RUNTIME_DIR),$(XDG_RUNTIME_DIR)/trader_joe_agent_mcp,/tmp/trader_joe_agent_mcp-$(shell id -u))
endif
export AGENT_MCP_SHARE_PATH

# Creates the share directory 0700 if it is missing, then REFUSES -- naming the path and what it found
# -- unless it is a real directory (lstat: a symlink is refused, never followed), owned by the invoking
# user, mode exactly 0700. /tmp is shared by every user, so an existing path there is someone's claim
# until proven ours. The same check is inlined in the initializeCommand. Docker-free; GNU stat.
.PHONY: agent-mcp-share
agent-mcp-share:  ## Create the agent-stack MCP's share directory 0700, refusing one not owned by you or not 0700 (no Docker)
	@s="$(AGENT_MCP_SHARE_PATH)"; \
	case "$$s" in /*) ;; *) echo "make agent-mcp-share: AGENT_MCP_SHARE_PATH must be absolute: '$$s'" >&2; exit 1;; esac; \
	[ -e "$$s" ] || [ -L "$$s" ] || { mkdir -p "$$(dirname "$$s")" && mkdir -m 0700 "$$s"; } 2> /dev/null; \
	[ -e "$$s" ] || [ -L "$$s" ] || { echo "make agent-mcp-share: could not create $$s" >&2; exit 1; }; \
	if [ -L "$$s" ] || [ ! -d "$$s" ] || [ "$$(stat -c %u:%a "$$s")" != "$$(id -u):700" ]; then \
		echo "make agent-mcp-share: refusing $$s: it must be a directory (not a symlink) owned by uid $$(id -u) with mode 700; found $$(stat -c '%F, uid %u, mode %a' "$$s")" >&2; \
		exit 1; \
	fi

# Rebuilding the devcontainer also rebuilds the agent-stack MCP (the user's ruling on tj-c4mosr.4,
# F-B option A; ADR tj-4rr0la addendum 9 (2)): its image takes its trusted files from root's working
# tree, and this is the make path's "devcontainer rebuild". The IDE's Rebuild Container cannot be told
# apart from a start on the host (initializeCommand runs before the old container is removed), so after
# one the user runs `make agent-mcp-rebuild` by hand -- no watcher, no next-start check.
# A RECIPE LINE after the devcontainer build, like agent-up's MCP start: a failed devcontainer build
# still stops make, a failed MCP rebuild warns and does not. AGENT_MCP=off skips it, as it does there.
.PHONY: agent-build
agent-build:  ## Build the agent devcontainer image, then rebuild the agent-stack MCP (AGENT_MCP=off skips it)
	$(AGENT_COMPOSE) build
	@if [ "$(AGENT_MCP)" != off ]; then \
		$(MAKE) --no-print-directory agent-mcp-rebuild \
			|| echo "WARNING: the agent-stack MCP was not rebuilt and keeps its previous image. Retry with 'make agent-mcp-rebuild', or skip it with AGENT_MCP=off." >&2; \
	fi

# The agent-stack MCP starts with the devcontainer unless AGENT_MCP=off (make agent-up AGENT_MCP=off,
# or AGENT_MCP=off in the environment) -- the user's rulings, ADR tj-4rr0la addendum 7, not defaults
# open to change. A RECIPE LINE, not a prerequisite: a failed prerequisite stops make, and a failed
# MCP start must warn and let the devcontainer start anyway. agent-mcp-network IS a prerequisite:
# compose.yml joins trader_joe_agent_mcp, so it has to exist even with the MCP off -- and so must the
# share directory it binds.
.PHONY: agent-up
agent-up: dev-network agent-mcp-network agent-mcp-share  ## Start the agent devcontainer and the agent-stack MCP (AGENT_MCP=off skips the MCP; ruled, tj-4rr0la add. 7)
	mkdir -p "$(AGENT_HOME_PATH)"
	@if [ "$(AGENT_MCP)" != off ]; then \
		$(MAKE) --no-print-directory agent-mcp-up \
			|| echo "WARNING: the agent-stack MCP did not start; the devcontainer starts without it. Retry with 'make agent-mcp-up', or skip it with AGENT_MCP=off." >&2; \
	fi
	$(AGENT_COMPOSE) up -d

# Leaves the agent-stack MCP running (ruled, tj-4rr0la addendum 7): the IDE path has no host-side stop
# hook, so the make path does not stop it either. make agent-mcp-down does.
.PHONY: agent-down
agent-down:  ## Stop the agent devcontainer (host config dir is kept; the agent-stack MCP keeps running)
	$(AGENT_COMPOSE) down

.PHONY: agent-attach
agent-attach:  ## Open a shell inside the agent devcontainer
	$(AGENT_COMPOSE) exec agent bash

# THE AGENT-STACK MCP (ADR tj-4rr0la addenda 1, 4, 5, 7, 9 and 11; tj-c4mosr.4):
# docker-compose.agent-mcp.yaml, the server in its own container plus the socket proxy. HOST ONLY: the
# devcontainer has no Docker, by standing rule, so these targets cannot run inside it. No
# $(VENV_MARKER) or uv prerequisite -- they must run on a bare host, because the IDE's
# initializeCommand calls agent-mcp-up on every start.
#
# The compose invocation is pinned to the ROOT checkout, wherever make runs: --project-directory and
# -f name the main worktree (git's common directory, less /.git), so the image is always built from
# root's working tree (the source ruled on tj-h8yf91). --env-file /dev/null: compose would otherwise
# read the project directory's live env file for interpolation; this file needs none of it, and every
# variable it interpolates is set on this line. -p restates the file's own name: for the reader.
AGENT_MCP_PROJECT := trader_joe_agent_mcp
# The devcontainer's route to the MCP. INTERNAL (no gateway), unlike devnet. Its fixed name appears in
# four places, which must agree: this variable, docker-compose.agent-mcp.yaml, .devcontainer/compose.yml
# and the initializeCommand in .devcontainer/devcontainer.json.
AGENT_MCP_NETWORK := trader_joe_agent_mcp
# The root checkout's HOST path: the source of agent_mcp's read-only /workspace bind (addendum 11 R1)
# and the MCP project's own --project-directory. Recursive (=), so git runs only when an MCP recipe
# expands it, never on `make test`.
AGENT_MCP_REPO_HOST_PATH = $(patsubst %/.git,%,$(shell git rev-parse --path-format=absolute --git-common-dir))
# The agent stack's own directory, outside the repository, AGENT_HOME_PATH and the share directory
# (agent-mcp-paths refuses otherwise): generated env files, data, snapshot, audit log.
# tools/agent_mcp/settings.py documents it.
AGENT_MCP_STACK_DIR ?= $(or $(XDG_DATA_HOME),$(HOME)/.local/share)/trader_joe_agent_stack
AGENT_MCP_COMPOSE = AGENT_MCP_REPO_HOST_PATH="$(AGENT_MCP_REPO_HOST_PATH)" \
	AGENT_MCP_STACK_DIR="$(AGENT_MCP_STACK_DIR)" AGENT_MCP_SHARE_PATH="$(AGENT_MCP_SHARE_PATH)" \
	AGENT_MCP_UID="$$(id -u)" AGENT_MCP_GID="$$(id -g)" \
	docker compose -p $(AGENT_MCP_PROJECT) --project-directory "$(AGENT_MCP_REPO_HOST_PATH)" --env-file /dev/null \
	-f "$(AGENT_MCP_REPO_HOST_PATH)/docker-compose.agent-mcp.yaml"

# Idempotent and race-safe the dev-network way (look, else create, else look again), but INTERNAL:
# only the devcontainer and agent_mcp join it, and neither needs a gateway through it. Never removed.
# Whether an existing network really is internal is checked by agent-mcp-up, not here: agent-up needs
# the network to exist even with AGENT_MCP=off, and must not fail on the MCP's account.
.PHONY: agent-mcp-network
agent-mcp-network:  ## Create the internal agent-stack MCP network if it is missing (never removed)
	@docker network inspect $(AGENT_MCP_NETWORK) > /dev/null 2>&1 \
		|| docker network create --driver bridge --internal $(AGENT_MCP_NETWORK) > /dev/null \
		|| docker network inspect $(AGENT_MCP_NETWORK) > /dev/null

# THE HOST PATH CHECK (addendum 11 R2). DOCKER-FREE, so it can be run for real anywhere. With
# the repository seen at /workspace inside agent_mcp, settings.py's own 'stack dir outside the
# repository' checks compare a host path with a container path and prove nothing; this replaces them,
# on the host, before every start and rebuild. The share directory comes from its prerequisite
# agent-mcp-share. It creates the other directories (0700, except the agent home
# directory, which agent-up owns), resolves every path, and refuses -- exit 1, naming both paths --
# unless:
#   AGENT_MCP_STACK_DIR   lies outside, and does not contain, the repository, AGENT_HOME_PATH and
#                         AGENT_MCP_SHARE_PATH (the snapshot's premise: only the MCP writes there);
#   AGENT_MCP_SHARE_PATH  lies outside, and does not contain, the repository and AGENT_HOME_PATH (its
#                         own mount, so the agent cannot swap it for a symlink the daemon follows);
#   AGENT_HOME_PATH       lies outside the repository.
# (The rebuild trigger the user chose, agent-build and agent-mcp-rebuild, keeps no host state, so
# addendum 11 R4's marker directory and its rules are not needed.)
.PHONY: agent-mcp-paths
agent-mcp-paths: agent-mcp-share  ## Check the agent-stack MCP's host paths do not overlap (no Docker; run by agent-mcp-up/-rebuild)
	@mkdir -p "$(AGENT_HOME_PATH)" \
		&& mkdir -p -m 0700 "$(AGENT_MCP_STACK_DIR)" || exit 1; \
	repo=$$(realpath "$(AGENT_MCP_REPO_HOST_PATH)") && home=$$(realpath "$(AGENT_HOME_PATH)") \
		&& share=$$(realpath "$(AGENT_MCP_SHARE_PATH)") && stack=$$(realpath "$(AGENT_MCP_STACK_DIR)") || exit 1; \
	within() { case "$$1/" in "$${2%/}"/*) return 0;; esac; return 1; }; \
	refuse() { echo "make agent-mcp-paths: $$1 ($$2) must lie outside $$3 ($$4)$$5" >&2; bad=1; }; \
	apart() { within "$$2" "$$4" && refuse "$$1" "$$2" "$$3" "$$4"; within "$$4" "$$2" && refuse "$$1" "$$2" "$$3" "$$4" ", and not contain it"; :; }; \
	outside() { within "$$2" "$$4" && refuse "$$1" "$$2" "$$3" "$$4"; :; }; \
	bad=0; \
	apart AGENT_MCP_STACK_DIR "$$stack" "the repository" "$$repo"; \
	apart AGENT_MCP_STACK_DIR "$$stack" AGENT_HOME_PATH "$$home"; \
	apart AGENT_MCP_STACK_DIR "$$stack" AGENT_MCP_SHARE_PATH "$$share"; \
	apart AGENT_MCP_SHARE_PATH "$$share" "the repository" "$$repo"; \
	apart AGENT_MCP_SHARE_PATH "$$share" AGENT_HOME_PATH "$$home"; \
	outside AGENT_HOME_PATH "$$home" "the repository" "$$repo"; \
	exit $$bad

# The MCP project's containers, found by the labels compose puts on them -- no compose file read.
# oneoff=False leaves out any `compose run` container of the project.
AGENT_MCP_CONTAINER = docker ps -aq --filter label=com.docker.compose.project=$(AGENT_MCP_PROJECT) \
	--filter label=com.docker.compose.service=$(1) --filter label=com.docker.compose.oneoff=False

# Shared by agent-mcp-up and agent-mcp-rebuild: the network must really be internal. (The mount
# sources are created by agent-mcp-paths, a prerequisite of both.)
define AGENT_MCP_PREFLIGHT
@[ "$$(docker network inspect -f '{{.Internal}}' $(AGENT_MCP_NETWORK))" = true ] || { \
	echo "make $@: network $(AGENT_MCP_NETWORK) exists but is not internal; remove that network by hand and re-run." >&2; \
	exit 1; }
endef

define AGENT_MCP_ANNOUNCE
@echo "agent-stack MCP: http://agent_mcp:8765/mcp, from the devcontainer over $(AGENT_MCP_NETWORK)"
@echo "bearer token file: $(AGENT_MCP_SHARE_PATH)/agent_mcp_token on the host, /agent_mcp_share/agent_mcp_token in the devcontainer (not printed)"
endef

# THE PLAIN START -- what agent-up and the IDE's initializeCommand run on every start. IDEMPOTENT.
# It NEVER APPLIES docker-compose.agent-mcp.yaml to an existing container (ADR tj-4rr0la addendum 9 (a);
# addendum 11 R3): `compose up` recreates a container whose configuration changed, so an up at every
# start would put an unreviewed edit to that file -- a new mount, the socket on agent_mcp -- live with no
# rebuild. The label lookup must find EXACTLY ONE container per service:
#   one each    `docker start` on both, which reads no compose file at all (stronger than
#               `compose start`, which still parses it), then a bounded wait on agent_mcp's healthcheck
#               (the token gate answering 401). The share directory is transient: if logout wiped it
#               while agent_mcp kept running, its bind still points at the old, unreachable directory
#               and the host's new one has no token. Then agent_mcp is `docker restart`ed: a restart
#               re-resolves the bind source (same config, no compose file read) and the server writes
#               a new token at start (auth.load_or_create_token). A devcontainer already running keeps
#               its own bind to the OLD directory and gets 401 until it is restarted too, so this
#               branch also tells the user so on stderr. It never restarts the devcontainer itself:
#               that would kill live agent sessions (ADR tj-4rr0la addendum 12, F2);
#   any zero    the CREATE branch: `compose up --no-recreate`, from the existing image, building only if
#               that is missing. The first-ever create is inside addendum 9 (3), and only a user action
#               removes these containers (agent-mcp-down keeps them; no verb reaches this project), so it
#               is user-caused. --no-recreate is why a partial state -- one container left -- creates only
#               the missing one and never re-applies the file to the survivor;
#   any two+    refuse and start nothing; agent-mcp-rebuild recreates the project cleanly.
#
# NO REBUILD HERE, and no rebuild detection: a plain start stays start-only (the user's ruling on
# tj-c4mosr.4, F-B option A). The image, with the trusted compose files and Dockerfile baked in, is
# rebuilt only by agent-mcp-rebuild -- run by `make agent-build`, or by hand after the IDE's Rebuild
# Container (tj-h8yf91; addendum 9).
#
# Prints the URL and the token FILE, never the token.
.PHONY: agent-mcp-up
agent-mcp-up: agent-mcp-network agent-mcp-paths  ## Start the agent-stack MCP's existing containers (host only; never rebuilds -- see agent-mcp-rebuild)
	$(AGENT_MCP_PREFLIGHT)
	@proxy=$$($(call AGENT_MCP_CONTAINER,socket_proxy)) && mcp=$$($(call AGENT_MCP_CONTAINER,agent_mcp)) || exit 1; \
	np=$$(echo $$proxy | wc -w); nm=$$(echo $$mcp | wc -w); \
	if [ "$$np" -gt 1 ] || [ "$$nm" -gt 1 ]; then \
		echo "make agent-mcp-up: project $(AGENT_MCP_PROJECT) has $$np socket_proxy and $$nm agent_mcp containers, expected one each; started nothing. Run 'make agent-mcp-rebuild'." >&2; \
		exit 1; \
	elif [ "$$np" -eq 1 ] && [ "$$nm" -eq 1 ]; then \
		healthy() { for i in $$(seq 60); do \
			health=$$(docker inspect -f '{{.State.Health.Status}}' $$mcp); \
			[ "$$health" = healthy ] && return 0; \
			[ "$$i" = 60 ] && { echo "make agent-mcp-up: agent_mcp is still '$$health' after 120s" >&2; return 1; }; \
			sleep 2; \
		done; }; \
		docker start $$proxy $$mcp > /dev/null && healthy || exit 1; \
		if [ ! -e "$(AGENT_MCP_SHARE_PATH)/agent_mcp_token" ]; then \
			echo "make agent-mcp-up: no token in $(AGENT_MCP_SHARE_PATH) (the share directory was wiped under a running MCP); restarting agent_mcp to bind the new directory and generate one." >&2; \
			docker restart $$mcp > /dev/null && healthy || exit 1; \
			echo "make agent-mcp-up: a devcontainer that is already running keeps the old share directory and gets 401 from the MCP until it is restarted: 'make agent-down && make agent-up', or close and reopen it in the IDE." >&2; \
		fi; \
	else \
		$(AGENT_MCP_COMPOSE) up -d --wait --wait-timeout 120 --no-recreate; \
	fi
	$(AGENT_MCP_ANNOUNCE)

# THE FORCED REBUILD (addendum 9 (c)): rebuilds the image from root's WORKING TREE -- the trusted compose
# files, the trusted Dockerfile and tools/agent_mcp as they stand there, trusted without review (the
# residual the user accepted, addendum 9 (3)) -- and recreates both containers from the current compose
# file. The one target that applies docker-compose.agent-mcp.yaml to existing containers.
# --remove-orphans (addendum 11 R3): a service renamed or dropped in the file must not leave its old
# container -- possibly one holding docker.sock -- running beside the new one.
.PHONY: agent-mcp-rebuild
agent-mcp-rebuild: agent-mcp-network agent-mcp-paths  ## Rebuild and recreate the agent-stack MCP from root's working tree: after the IDE's Rebuild Container, or after changing MCP files (host only; agent-build runs it)
	$(AGENT_MCP_PREFLIGHT)
	$(AGENT_MCP_COMPOSE) up -d --wait --wait-timeout 120 --build --force-recreate --remove-orphans
	$(AGENT_MCP_ANNOUNCE)

# STOPS the MCP and its proxy and KEEPS the containers (addendum 9 (a)), so the next plain start
# re-reads no compose file. `docker stop` by label, like the start: no compose file read here either.
# The agent STACK the MCP drives is a separate project: stop it first with the stack_down verb (or
# leave it; it keeps its data). The external network stays.
.PHONY: agent-mcp-down
agent-mcp-down:  ## Stop (not remove) the agent-stack MCP and its socket proxy (the agent stack itself: stack_down verb)
	@ids="$$($(call AGENT_MCP_CONTAINER,agent_mcp)) $$($(call AGENT_MCP_CONTAINER,socket_proxy))"; \
	[ -z "$$(echo $$ids)" ] || docker stop $$ids > /dev/null

# dev-down, not prod-down: this is a workstation target -- it deletes the venv -- and dev-down is
# the one teardown that loads docker-compose.tools.yaml, so going through it leaves no pgAdmin
# behind.
.PHONY: clean
clean: dev-down  ## Clean up the project
	rm -rf $(VENV_DIR)
	[[ -d .pytest_cache ]] && rm -rf .pytest_cache || true
	[[ -d .coverage ]] && rm -rf .coverage || true
	[[ -d coverage.xml ]] && rm -rf coverage.xml || true

# THE ONE LINT ENTRY POINT (tj-3mk3u5.54; the user's ruling on tj-3mk3u5.55). `make lint` lints every
# language PATHS covers, one leg per language, and `make lint-fix` fixes what each leg can fix:
#   lint-python   ruff check and ruff format --check on PATHS: the two lines `lint` always ran.
#   lint-proto    buf lint, buf format --diff --exit-code and buf breaking, on the WHOLE proto module,
#                 when PATHS covers proto/ (the selector below). PATHS decides WHETHER buf runs, never
#                 what it reads: module-level rules (package against directory, import cycles) need
#                 every file in the module.
#   lint-shell    shellcheck over the *.sh files UNDER PATHS (tj-xc6nfv). Unlike proto, PATHS decides
#                 WHICH FILES are read, as it does for ruff: a shell script is checked on its own, so
#                 there is no module whose other files a rule needs. No *.sh under PATHS prints one
#                 'not run' line and passes.
#   lint-ts       NOT YET. PR 4 adds the fourth leg with the UI and its tooling: lint-ts and lint-fix-ts,
#                 selected by web/, as a fourth prerequisite of lint and of lint-fix. Nothing stands in
#                 for it before then: a leg that lints nothing is a green result with nothing behind it.
# buf lint and buf breaking have no autofix, so lint-fix-proto is buf format -w alone. lint-fix-shell
# fixes NOTHING and says so out loud -- see the comment on that target for why shellcheck's
# --format=diff is not a formatter and must not be applied in bulk.
#
# THE PROTO SELECTOR. PATHS is space-separated and relative to the repository root. Each word is
# normalised -- one leading ./ stripped, then every trailing / -- and the proto leg is SELECTED when
# any word is then '.' or empty (PATHS=. or ./), 'proto', or under proto/. An empty PATHS selects it
# too, because ruff given no path lints the whole tree. NOT SELECTED, the leg prints one 'not run'
# line and passes WITHOUT looking for buf, so a component-scoped run (PATHS=common, data/ingest, ...)
# behaves as it did before buf, installed or not. SELECTED, an absent buf or a buf at any other version
# than BUF_VERSION FAILS, naming both remedies. Never a skip, and no variable switches the leg off
# (tj-qenrpk's fail-not-skip rule; pytest.ini). A change to buf.yaml itself is checked with PATHS=.
_lint_strip_slashes = $(if $(filter %/,$(1)),$(call _lint_strip_slashes,$(patsubst %/,%,$(1))),$(1))
_lint_word = $(call _lint_strip_slashes,$(patsubst ./%,%,$(1)))
_lint_selects_proto = $(if $(filter . proto proto/%,$(or $(call _lint_word,$(1)),.)),yes)
LINT_PROTO_SELECTED := $(if $(strip $(PATHS)),$(strip $(foreach word,$(PATHS),$(call _lint_selects_proto,$(word)))),yes)
LINT_PROTO_NOT_RUN = $@: not run: PATHS=$(PATHS) does not cover proto/ (buf runs for PATHS=. or proto/...)

# BUF is looked up on PATH; overridable, e.g. with a stub. BUF_ERROR_FORMAT goes to buf lint and buf
# breaking; CI passes github-actions, so findings become annotations on the pull request.
BUF ?= buf
BUF_ERROR_FORMAT ?= text

# The selected leg needs the pinned buf: another version may lint, format or compare differently.
define BUF_PIN_CHECK
@if ! where="$$(command -v $(BUF))"; then \
	found='none: $(BUF) is not on PATH'; \
elif ! version="$$($(BUF) --version 2>&1)"; then \
	found="$$where, whose --version fails: $$version"; \
elif [ "$$version" != '$(BUF_VERSION)' ]; then \
	found="buf $$version at $$where"; \
else \
	found=''; \
fi; \
if [ -n "$$found" ]; then \
	echo "make $@: PATHS=$(PATHS) covers proto/, which needs buf $(BUF_VERSION) (BUF=$(BUF)); found $$found." >&2; \
	echo "  Install the pin: make buf-install (checksum-verified, into $(BUF_INSTALL_DIR); BUF_INSTALL_DIR= to change it)," >&2; \
	echo "  or rebuild the agent image, which installs it at /usr/local/bin/buf." >&2; \
	exit 1; \
fi
endef

# BREAKING, the last step of lint-proto. The baseline is BUF_AGAINST_REF, by default the LOCAL main
# branch: refs are shared across linked worktrees, so it resolves inside an agent's worktree too. A
# local main can lag origin's, which is acceptable while the check is report-only. CI fetches main into
# origin/main at depth 1 and passes BUF_AGAINST_REF=origin/main.
#
# THE BASELINE IS MATERIALISED WITH git archive: <ref>'s buf.yaml and proto/ are extracted into a
# temporary directory, removed on exit, and buf compares against that directory. Not buf's own
# '.git#branch=main' input: in a linked worktree .git is a file, not the directory buf reads, and CI's
# checkout is shallow with no local main. git archive works the same in the shared checkout, a
# worktree and CI.
#   <ref> does not resolve   FAIL, naming the ref and the fix. A missing baseline is a broken check.
#   <ref> has no proto/      PASS, saying 'nothing to compare': the first-adoption case, since main has
#                            no proto/ until PR 2 merges. Said out loud, never silent.
#   proto/ but no buf.yaml   FAIL: there is no module to compare against.
# BOTH SIDES ARE BUILT FIRST, and either failing to build FAILS. That is what makes the report-only
# downgrade safe: buf exits 100 for a compile error as well as for breaking-change findings, in the
# input and in the baseline alike (seen on buf 1.73.0), so without the builds a baseline that does not
# compile would read as a finding and pass.
#
# REPORT-ONLY (tj-3mk3u5.55, W2) while ADR tj-8konfu D1's compatibility window is open. buf breaking
# exiting 0 prints 'no breaking changes'. Exiting 100, its findings code, prints the findings and a
# banner, then passes while BUF_BREAKING_BLOCKING is 0 and fails when it is 1. ANY OTHER non-zero fails
# in both modes: report-only covers findings, never a broken check, which is why this is not `|| true`.
BUF_AGAINST_REF ?= main
# Flip to 1 at the first SDK release (tj-d2mhru). Until then a break is printed, not failed (tj-3mk3u5.55 W2).
BUF_BREAKING_BLOCKING := 0

define BUF_BREAKING
@set -euo pipefail; \
ref='$(BUF_AGAINST_REF)'; \
if ! git rev-parse --verify --quiet "$$ref^{commit}" > /dev/null; then \
	echo "make $@: breaking: the baseline ref '$$ref' does not resolve to a commit here; failing. Fetch it, or pass BUF_AGAINST_REF=<ref> (CI fetches main as origin/main)." >&2; \
	exit 1; \
fi; \
if ! git cat-file -e "$$ref:proto" 2> /dev/null; then \
	echo "$@: breaking: no proto/ on $$ref: nothing to compare"; \
	exit 0; \
fi; \
if ! git cat-file -e "$$ref:buf.yaml" 2> /dev/null; then \
	echo "make $@: breaking: $$ref has proto/ but no buf.yaml, so there is no module to compare against; failing." >&2; \
	exit 1; \
fi; \
base="$$(mktemp -d)"; \
trap 'rm -rf "$$base"' EXIT; \
git archive "$$ref" -- buf.yaml proto | tar -x -C "$$base"; \
echo "$@: breaking: against $$ref ($$(git rev-parse --short "$$ref^{commit}")), its buf.yaml and proto/ extracted by git archive"; \
$(BUF) build; \
$(BUF) build "$$base" || { status=$$?; echo "make $@: breaking: the baseline on $$ref does not build (exit $$status); failing, because a broken baseline is not a finding." >&2; exit 1; }; \
status=0; \
$(BUF) breaking --error-format=$(BUF_ERROR_FORMAT) --against "$$base" || status=$$?; \
case "$$status" in \
	0) echo "$@: breaking: no breaking changes against $$ref" ;; \
	100) \
		if [ '$(BUF_BREAKING_BLOCKING)' = 0 ]; then \
			echo "$@: breaking: REPORT-ONLY until the first SDK release (tj-d2mhru): the breaking change(s) above are against $$ref; not failing."; \
		else \
			echo "make $@: breaking: BLOCKING (BUF_BREAKING_BLOCKING=$(BUF_BREAKING_BLOCKING)): the breaking change(s) above are against $$ref; failing." >&2; \
			exit 1; \
		fi ;; \
	*) echo "make $@: breaking: buf breaking exited $$status, which is a broken check, not a finding; failing in either mode." >&2; exit "$$status" ;; \
esac
endef

# SHELLCHECK, the shell leg (tj-xc6nfv). SHELLCHECK is looked up on PATH and overridable, e.g. with
# a stub, exactly as BUF is.
#
# SEVERITY IS NAMED RATHER THAN DEFAULTED. `style` is shellcheck 0.11.0's own default and reports
# everything it has; spelling it out means a future shellcheck that narrows its default cannot
# quietly narrow this gate. It FAILS from the first run rather than warning for a branch -- the
# whole existing surface was brought to zero findings in the same change that added the leg, so
# there is no backlog a warning would be covering for, and this project has already established
# that a warning nobody must act on does not work (tj-yb1bxj, "why refuse rather than warn").
#
# OPTIONAL CHECKS ARE OFF, measured rather than assumed. `--enable=all` reports 187 findings on this
# tree, 166 of them pure house style (SC2250 require-variable-braces x137, SC2292
# require-double-brackets x29) -- a wall no one gets to green, and a lint gate nobody can get to
# green is worse than none. The two narrower ones worth wanting, check-extra-masked-returns (SC2312)
# and check-set-e-suppressed (SC2310), report four findings between them and every one sits on a
# construct that is deliberate and commented at its site in run_migrations.sh and source_digest.sh.
# Switching them on would demand either a behavioural edit to a verification guard or four
# suppressions, so they stay off and the four sites are filed for a human to read (tj-7s225q), which
# is where the decision to turn either check on belongs.
#
# THERE IS NO .shellcheckrc, ON PURPOSE. A repo-level rc is the blanket exclusion this work exists
# to avoid: every exception in this tree is a `# shellcheck disable=CODE  # reason` on the line that
# needs it, so the reason travels with the code and dies with it. Two of those already existed in
# tools/source_digest.sh naming SC2254 -- `case`'s code -- for two `[[ ]]` tests whose code is
# SC2053, which is to say they suppressed nothing at all and nobody could have known.
SHELLCHECK ?= shellcheck
SHELLCHECK_FORMAT ?= tty
SHELLCHECK_SEVERITY ?= style

# THE FILE SET. `git ls-files -z --cached --others --exclude-standard` is tracked files PLUS
# untracked-and-not-ignored ones, so a script added to the tree is linted before it is ever
# `git add`ed -- an unlinted new script would be precisely the silent hole this leg is for. It also
# gets the exclusions free and correct: .venv and the rest are ignored, and git does not descend
# into a nested worktree under .claude/worktrees because that directory has its own .git. The
# explicit :(exclude) is belt-and-braces for that last one (tj-aov3ip: a repo-wide scan must never
# read another agent's half-edited copy). PATHS words are git pathspecs relative to the repository
# root, which is why this leg needs none of the ./ and trailing-/ normalisation the proto selector
# does -- git handles `./tools/` itself.
#
# ONLY *.sh. Every shell script in this tree ends in .sh; one that did not would go unlinted, which
# is the known edge of this selector and the reason to keep the convention.
SHELLCHECK_PATHSPEC = $(or $(strip $(PATHS)),.) ':(exclude).claude/worktrees'

# THE PROTO LEG GOES LAST, in `lint` and in `lint-fix` alike. buf breaking is REPORT-ONLY
# (BUF_BREAKING_BLOCKING above), so it is the one leg that can print something a reader must act on
# while the target still exits 0; last means its banner is the last thing on the screen rather than
# scrolled off by another language's output.
.PHONY: lint
lint: lint-python lint-shell lint-proto  ## Lint every language PATHS covers (Python: ruff; shell: shellcheck, for *.sh under PATHS; proto: buf, when PATHS is . or under proto/)

.PHONY: lint-python
lint-python: $(VENV_MARKER)  ## Lint and format-check Python with ruff (scope with PATHS=)
	uv run ruff check $(PATHS)
	uv run ruff format --check $(PATHS)

# THE SELECTION IS MADE IN THE RECIPE, not at parse time with $(shell ...), because make expands a
# parse-time $(shell) on EVERY invocation of EVERY target -- `make test` would run git ls-files too.
#
# IT GOES THROUGH A FILE AND NOT `< <(git ls-files ...)`, deliberately, and this is the one piece of
# shell in this repository that was written from a measured failure rather than from taste: inside
# tools/source_digest.sh the process-substitution form made `exit` kill only the subshell, so the
# script printed a plausible digest and exited 0 on error. Here the same form would hide a FAILING
# git -- no output, an empty array, "no *.sh file", a green lint -- which is the hollow guard this
# whole leg exists to stop being possible. With a file, `set -e` sees git's status and the recipe
# dies on it.
#
# A PATH WITH A SPACE in it would be split by the shell and reach shellcheck as two names that do
# not exist, which shellcheck reports and fails on. Loud and wrong, never silent and green; -z/-0
# would be exact, but `read -d ''` into an array is what makes the message above possible to write.
.PHONY: lint-shell
lint-shell:  ## shellcheck every *.sh under PATHS (nothing under PATHS: one 'not run' line, and passes)
	@set -euo pipefail; \
	list="$$(mktemp)"; \
	trap 'rm -f "$$list"' EXIT; \
	git ls-files -z --cached --others --exclude-standard -- $(SHELLCHECK_PATHSPEC) > "$$list"; \
	files=(); \
	while IFS= read -r -d '' path; do \
		case "$$path" in *.sh) files+=("$$path") ;; esac; \
	done < "$$list"; \
	if [ "$${#files[@]}" -eq 0 ]; then \
		echo "$@: not run: PATHS=$(PATHS) covers no *.sh file (shellcheck reads the *.sh under PATHS)"; \
		exit 0; \
	fi; \
	if ! where="$$(command -v $(SHELLCHECK))"; then \
		found='none: $(SHELLCHECK) is not on PATH'; \
	elif ! version="$$($(SHELLCHECK) --version 2>&1 | awk '/^version:/ {print $$2}')"; then \
		found="$$where, whose --version fails"; \
	elif [ "$$version" != '$(SHELLCHECK_VERSION)' ]; then \
		found="shellcheck $$version at $$where"; \
	else \
		found=''; \
	fi; \
	if [ -n "$$found" ]; then \
		echo "make $@: PATHS=$(PATHS) covers $${#files[@]} *.sh file(s), which need shellcheck $(SHELLCHECK_VERSION) (SHELLCHECK=$(SHELLCHECK)); found $$found." >&2; \
		echo "  Install the pin: make shellcheck-install (checksum-verified, into $(SHELLCHECK_INSTALL_DIR); SHELLCHECK_INSTALL_DIR= to change it)," >&2; \
		echo "  or rebuild the agent image, which installs it at /usr/local/bin/shellcheck." >&2; \
		exit 1; \
	fi; \
	echo "$(SHELLCHECK) --severity=$(SHELLCHECK_SEVERITY) --format=$(SHELLCHECK_FORMAT) ($${#files[@]} file(s): $${files[*]})"; \
	$(SHELLCHECK) --severity=$(SHELLCHECK_SEVERITY) --format=$(SHELLCHECK_FORMAT) -- "$${files[@]}"

.PHONY: lint-proto
lint-proto:  ## buf lint, format check and breaking vs BUF_AGAINST_REF (report-only), when PATHS covers proto/
ifeq ($(LINT_PROTO_SELECTED),)
	@echo "$(LINT_PROTO_NOT_RUN)"
else
	$(BUF_PIN_CHECK)
	$(BUF) lint --error-format=$(BUF_ERROR_FORMAT)
	@echo '$(BUF) format --diff --exit-code'; $(BUF) format --diff --exit-code \
		|| { status=$$?; echo "make $@: the diff above is buf format's; apply it with 'make lint-fix PATHS=proto'." >&2; exit $$status; }
	$(BUF_BREAKING)
endif

.PHONY: lint-fix
lint-fix: lint-fix-python lint-fix-shell lint-fix-proto  ## Apply lint fixes and formatting for every language PATHS covers (scope with PATHS=)

.PHONY: lint-fix-python
lint-fix-python: $(VENV_MARKER)  ## Apply ruff's fixes and formatting (scope with PATHS=)
	uv run ruff check --fix $(PATHS)
	uv run ruff format $(PATHS)

# THE LEG THAT FIXES NOTHING, and says so on every run instead of being absent. ruff and buf each
# have a formatter, so each has a real lint-fix leg; shell has neither half here, for two separate
# reasons worth keeping apart:
#
# 1. NO FORMATTER IS PINNED. shellcheck is a linter and ships no formatter. shfmt is the tool that
#    would be the counterpart to `buf format`, and it is DEFERRED, not forgotten: pointing it at
#    this tree reformats ~770 lines of existing bash, which is a whitespace diff a human has to
#    review by hand to be sure it is only whitespace -- and it buys zero correctness, which is what
#    tj-xc6nfv was raised about. It is filed as its own follow-up (tj-twwed3) so it is reviewed as a
#    formatting change, on its own, rather than riding in under a lint bead.
# 2. shellcheck's `--format=diff` IS NOT A FORMATTER AND MUST NOT BE APPLIED IN BULK. It emits a
#    patch for the subset of findings it can rewrite, and those rewrites are BEHAVIOURAL by
#    construction: its fix for SC2086 is to quote an expansion, and this repository has an expansion
#    that must stay unquoted (entrypoint.sh's $ADDITIONAL_ARGS, which is an argument list that has
#    to word-split and has to vanish when unset -- quoting it hands uvicorn one empty argument and
#    breaks startup in prod). `buf format` moves whitespace; this changes what the program does.
#    Running it from `make lint-fix`, which a developer reasonably expects to be safe, would make a
#    quiet behavioural edit to a container entrypoint and a verification guard look like tidying.
#
# So the shell half of lint-fix is a human reading `make lint`'s findings. The line below is printed
# so that is a stated fact rather than an unexplained gap between the legs.
.PHONY: lint-fix-shell
lint-fix-shell:  ## Fixes nothing, by design: no shell formatter is pinned (see the comment above)
	@echo "$@: nothing to apply: no shell FORMATTER is pinned -- shellcheck is a linter and has none, and its --format=diff rewrites behaviour, not whitespace."
	@echo "$@: run 'make lint PATHS=$(PATHS)' and fix what it reports by hand; an exception is a '# shellcheck disable=CODE  # reason' on the line that needs it."

.PHONY: lint-fix-proto
lint-fix-proto:  ## Apply buf format to proto/, when PATHS covers proto/
ifeq ($(LINT_PROTO_SELECTED),)
	@echo "$(LINT_PROTO_NOT_RUN)"
else
	$(BUF_PIN_CHECK)
	$(BUF) format -w
endif

# THE ONE TARGET THAT DOWNLOADS BUF. `make lint` never touches the network. Linux x86_64 and aarch64
# only: any other platform fails naming itself, never as a checksum mismatch, which would read like a
# corrupted download. The BARE release binary is fetched from BUF_RELEASE_URL (overridable, so a test
# can serve a fake over file://) into a temporary file INSIDE BUF_INSTALL_DIR, checked by sha256sum
# against the pin, and run to check that it reports BUF_VERSION. Either mismatch deletes the download
# and fails, leaving any existing buf untouched. Only then is it renamed over BUF_INSTALL_DIR/buf: a
# rename within one directory is atomic, so agents sharing a container never run a half-written file.
# The default, ~/.local/bin, is first on the agent image's PATH; it is per container, not per worktree,
# and not a mount, so one run serves every agent in the container until it is recreated, and the
# rebuilt image then carries /usr/local/bin/buf itself. CI installs into /usr/local/bin.
BUF_RELEASE_URL ?= https://github.com/bufbuild/buf/releases/download
BUF_INSTALL_DIR ?= $(HOME)/.local/bin

.PHONY: buf-install
buf-install:  ## Install the pinned buf, checksum-verified, into BUF_INSTALL_DIR (default ~/.local/bin; the only target that downloads it)
	@set -euo pipefail; \
	platform="$$(uname -s) $$(uname -m)"; \
	case "$$platform" in \
		'Linux x86_64') asset=buf-Linux-x86_64; sum='$(BUF_SHA256_X86_64)' ;; \
		'Linux aarch64') asset=buf-Linux-aarch64; sum='$(BUF_SHA256_AARCH64)' ;; \
		*) echo "make buf-install: no buf checksum pinned for $$platform; buf is pinned for Linux x86_64 and Linux aarch64 only (BUF_SHA256_* in the Makefile)." >&2; exit 1 ;; \
	esac; \
	url='$(BUF_RELEASE_URL)/v$(BUF_VERSION)/'"$$asset"; \
	dir='$(BUF_INSTALL_DIR)'; \
	mkdir -p "$$dir"; \
	tmp="$$(mktemp "$$dir/.buf-install.XXXXXX")"; \
	trap 'rm -f "$$tmp"' EXIT; \
	echo "make buf-install: fetching $$url"; \
	curl -fsSL -o "$$tmp" "$$url"; \
	if ! printf '%s  %s\n' "$$sum" "$$tmp" | sha256sum -c --status -; then \
		echo "make buf-install: checksum mismatch for $$url: expected $$sum, got $$(sha256sum "$$tmp" | cut -d ' ' -f 1). Deleted the download; $$dir/buf is untouched." >&2; \
		exit 1; \
	fi; \
	chmod 0755 "$$tmp"; \
	found="$$("$$tmp" --version 2>&1)" || found="a failing --version ($$found)"; \
	if [ "$$found" != '$(BUF_VERSION)' ]; then \
		echo "make buf-install: $$url matches its pinned checksum but reports version $$found, not $(BUF_VERSION): BUF_VERSION and BUF_SHA256_* disagree. Deleted the download; $$dir/buf is untouched." >&2; \
		exit 1; \
	fi; \
	mv -f "$$tmp" "$$dir/buf"; \
	trap - EXIT; \
	echo "make buf-install: installed buf $$("$$dir/buf" --version) at $$dir/buf (sha256 $$sum)"; \
	resolved="$$(command -v buf || true)"; \
	[ "$$resolved" = "$$dir/buf" ] || echo "make buf-install: note: 'buf' on PATH is $${resolved:-not found}, not $$dir/buf; make lint runs the first buf on PATH (or BUF=<path>)." >&2

# THE ONE TARGET THAT DOWNLOADS SHELLCHECK (tj-xc6nfv), on buf-install's rule above and with its
# guarantees: `make lint` never touches the network; Linux x86_64 and aarch64 only, and any other
# platform fails naming ITSELF rather than as a checksum mismatch, which would read like a corrupted
# download; the download is deleted and the installed shellcheck left untouched on either a checksum
# or a version mismatch; and the move into place is a rename WITHIN one directory, which is atomic,
# so agents sharing a container never run a half-written binary.
#
# ONE DIFFERENCE FROM buf-install, forced by the release's shape: buf ships a bare binary, shellcheck
# ships a .tar.gz holding shellcheck-v<version>/shellcheck. So the download and the extraction happen
# in a scratch directory and only the extracted binary is copied into SHELLCHECK_INSTALL_DIR, where
# the final rename is still within that one directory. ONE trap covers both, set after both paths are
# named: a second `trap ... EXIT` would silently replace the first and leak the scratch directory.
# .tar.gz and not the smaller .tar.xz because no xz binary exists here (see the pin at the top).
SHELLCHECK_RELEASE_URL ?= https://github.com/koalaman/shellcheck/releases/download
SHELLCHECK_INSTALL_DIR ?= $(HOME)/.local/bin

.PHONY: shellcheck-install
shellcheck-install:  ## Install the pinned shellcheck, checksum-verified, into SHELLCHECK_INSTALL_DIR (default ~/.local/bin; the only target that downloads it)
	@set -euo pipefail; \
	platform="$$(uname -s) $$(uname -m)"; \
	case "$$platform" in \
		'Linux x86_64') asset='shellcheck-v$(SHELLCHECK_VERSION).linux.x86_64.tar.gz'; sum='$(SHELLCHECK_SHA256_X86_64)' ;; \
		'Linux aarch64') asset='shellcheck-v$(SHELLCHECK_VERSION).linux.aarch64.tar.gz'; sum='$(SHELLCHECK_SHA256_AARCH64)' ;; \
		*) echo "make shellcheck-install: no shellcheck checksum pinned for $$platform; shellcheck is pinned for Linux x86_64 and Linux aarch64 only (SHELLCHECK_SHA256_* in the Makefile)." >&2; exit 1 ;; \
	esac; \
	url='$(SHELLCHECK_RELEASE_URL)/v$(SHELLCHECK_VERSION)/'"$$asset"; \
	dir='$(SHELLCHECK_INSTALL_DIR)'; \
	mkdir -p "$$dir"; \
	work="$$(mktemp -d)"; \
	tmp="$$(mktemp "$$dir/.shellcheck-install.XXXXXX")"; \
	trap 'rm -rf "$$work"; rm -f "$$tmp"' EXIT; \
	echo "make shellcheck-install: fetching $$url"; \
	curl -fsSL -o "$$work/release.tar.gz" "$$url"; \
	if ! printf '%s  %s\n' "$$sum" "$$work/release.tar.gz" | sha256sum -c --status -; then \
		echo "make shellcheck-install: checksum mismatch for $$url: expected $$sum, got $$(sha256sum "$$work/release.tar.gz" | cut -d ' ' -f 1). Deleted the download; $$dir/shellcheck is untouched." >&2; \
		exit 1; \
	fi; \
	tar -xzf "$$work/release.tar.gz" -C "$$work"; \
	binary="$$work/shellcheck-v$(SHELLCHECK_VERSION)/shellcheck"; \
	if [ ! -f "$$binary" ]; then \
		echo "make shellcheck-install: $$url matches its pinned checksum but holds no shellcheck-v$(SHELLCHECK_VERSION)/shellcheck; the release layout changed. Deleted the download; $$dir/shellcheck is untouched." >&2; \
		exit 1; \
	fi; \
	cp "$$binary" "$$tmp"; \
	chmod 0755 "$$tmp"; \
	found="$$("$$tmp" --version 2>&1 | awk '/^version:/ {print $$2}')" || found=''; \
	if [ "$$found" != '$(SHELLCHECK_VERSION)' ]; then \
		echo "make shellcheck-install: $$url matches its pinned checksum but reports version $${found:-none}, not $(SHELLCHECK_VERSION): SHELLCHECK_VERSION and SHELLCHECK_SHA256_* disagree. Deleted the download; $$dir/shellcheck is untouched." >&2; \
		exit 1; \
	fi; \
	mv -f "$$tmp" "$$dir/shellcheck"; \
	rm -rf "$$work"; \
	trap - EXIT; \
	echo "make shellcheck-install: installed shellcheck $$("$$dir/shellcheck" --version | awk '/^version:/ {print $$2}') at $$dir/shellcheck (sha256 $$sum)"; \
	resolved="$$(command -v shellcheck || true)"; \
	[ "$$resolved" = "$$dir/shellcheck" ] || echo "make shellcheck-install: note: 'shellcheck' on PATH is $${resolved:-not found}, not $$dir/shellcheck; make lint runs the first shellcheck on PATH (or SHELLCHECK=<path>)." >&2

# ./tools holds the agent-stack MCP server (tools/agent_mcp, ADR tj-4rr0la section 6): build
# tooling, but it holds Docker access, so bandit reads it like production source.
# ./gen/proto/python is generated, but the image copies it and runs it, so bandit reads it too
# (decision tj-3mk3u5.42 F1). CI's SOURCE_PATHS must name the same roots.
# The four service trees live under ./server since epic tj-iontkq; ./tools and ./gen stay at the
# top of the repository. Get a root wrong and bandit scans nothing and exits 0, which is why
# common/tests/test_ci_invariants.py asserts every scanner root here is a real directory.
SOURCE_DIRS := ./server/common ./server/routers ./server/schemas ./server/data ./tools ./gen/proto/python

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
# requirements.txt is removed whatever the export or pip-audit returns (tj-0pobey.6): a bare
# `rm` line after pip-audit never ran when pip-audit found something, leaving the file behind in
# the working tree. The cleanup sits AFTER each command, so the export and pip-audit invocations
# stay CI's; each one's status is captured and re-raised, so a finding still fails this target.
# The export, here and in CI, passes --color never (tj-3mk3u5.43). uv colours its output when the
# CALLER's environment asks for it (FORCE_COLOR, CLICOLOR_FORCE), even into a redirect, and
# pip-audit then reads the escape code as line 1 and fails. The flag outranks every such variable,
# so the file is plain text whoever runs this.
.PHONY: security
security: $(VENV_MARKER)  ## Check security vulnerabilities
	uv run bandit -r $(SOURCE_DIRS) --exclude '*/tests/*'
	uv run semgrep --config=auto --error --exclude=tests/ --exclude=.venv --exclude=docker-compose.override.yaml --exclude=.claude/worktrees .
	uv export --all-groups --no-group dev --no-group testing --no-group security --locked --format requirements-txt --color never > requirements.txt || { status=$$?; rm -f requirements.txt; exit $$status; }
	uv run pip-audit -r requirements.txt --disable-pip; status=$$?; rm -f requirements.txt; exit $$status

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
# without touching this file. `make test PATHS=server/data/store/tests` does the same job today,
# but the path is exactly what the monorepo split (tj-iontkq.4) just rewrote -- markers survived
# that move untouched, PATHS= did not. Component names are the `markers` list in pytest.ini.
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
#   PYTHONPATH                  /code:/code/gen/proto/python, the image's model: the mount root, so
#                               `from data.store.app... import` resolves (tests/system has no
#                               package chain above it), then the generated code's root.
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

# The disposable-database guard itself, ONE definition shared by test-system, system-launch and
# seed-dump, so the three cannot drift apart. $@ names whichever target refused; the text is generic,
# with one line per user saying how that target reaches the database.
define SYSTEM_TEST_DISPOSABLE_GUARD
@if [ "$(SYSTEM_TEST_DISPOSABLE_DB)" != "1" ]; then \
	echo "make $@ REFUSED: this target WRITES to the database it is pointed at -- the one the default" >&2; \
	echo "  compose project's stack holds -- and this host may also run the production deployment, in that" >&2; \
	echo "  same compose project:" >&2; \
	echo "    test-system:   the suite's client joins its networks and writes to its database;" >&2; \
	echo "    system-launch: replaces its data_ingest with the fake, which writes fake bars;" >&2; \
	echo "    seed-dump:     the producer POSTs its scenario into it." >&2; \
	echo "  Run it only against a stack you can wipe: bring one up and migrate it" >&2; \
	echo "  (make system-launch or make dev-launch, then make migrate), then run:" >&2; \
	echo "    make $@ SYSTEM_TEST_DISPOSABLE_DB=1" >&2; \
	exit 1; \
fi
endef

.PHONY: test-system
test-system:  ## Run tests/system from the test_client container against an up, migrated stack (SYSTEM_TEST_DISPOSABLE_DB=1)
	$(SYSTEM_TEST_DISPOSABLE_GUARD)
	@[ -f .env ] || { echo "make test-system: no .env in $(CURDIR); compose interpolates the stack's credentials from it." >&2; exit 1; }
	@echo "System suite from test_client against data_store (service data_store, on store_api) and Postgres (service postgres, on store_db); TZ set in docker-compose.test-client.yaml and checked by the client's entrypoint."
	@echo "The database password and the instance write secret reach the container from .env through compose (values not shown)."
	$(TEST_CLIENT_COMPOSE) run --rm --no-deps --build test_client $(SYSTEM_PATHS)

# THE FAKE-MODE STACK (decision tj-j4wknb R4; tj-vhboky.61): the stack docker-compose.yaml defines,
# from the PROD images, with docker-compose.fake.yaml on top -- data_ingest runs the test-only launcher
# on FakeRead from a read-only mount of tests/fakes, one worker, broker keys blanked. Nothing else
# differs from prod-launch. The system suite runs against it: make system-launch, make migrate, then
# make test-system.
#
# PROD_COMPOSE never loads the fake-mode overlay, just as it never loads the dev override: the prod
# image holds no fakes, and the only way a fake reaches a running service is this file list (or the
# agent stack's, AGENT_STACK_COMPOSE).
SYSTEM_COMPOSE := docker compose -f docker-compose.yaml -f docker-compose.fake.yaml

# Behind the SAME disposable-database guard as test-system: this runs in the default compose project,
# the one prod-launch uses, so on a host that runs the production deployment it would recreate that
# stack's data_ingest as the fake, and fake bars would reach its database. The same --wait as PROD_UP
# (spelled out rather than shared, so PROD_UP stays as it is). No build: like prod-launch it starts the
# images prod-build made, so build first after a change. No $(VENV_MARKER), for the reason test-system
# has none. No stop target of its own: every service here is in docker-compose.yaml, so `make
# prod-down` (or dev-down) stops and removes this stack cleanly.
.PHONY: system-launch
system-launch:  ## Start the stack from the prod images with data_ingest on the fake broker, waiting for healthy (SYSTEM_TEST_DISPOSABLE_DB=1)
	$(SYSTEM_TEST_DISPOSABLE_GUARD)
	$(SYSTEM_COMPOSE) up -d --wait --wait-timeout 300

# THE SEED DUMP (ADR tj-4rr0la addendum 10 (4); decision tj-vhboky.55 S9-S11). Against the stack
# system-launch started -- up, migrated (make migrate) and in fake mode -- with the test-client file
# added, it runs the producer in test_client by the ONE invocation the agent-stack MCP's seed_dump
# verb also uses: `compose run --rm -T --entrypoint /code/.venv/bin/python test_client -m
# data.store.seeds [--date D]`. The producer POSTs its own scenario to data_store (tj-vhboky.55) and
# reads Postgres, over the stack's networks from the client container -- never `docker exec` into a
# service. It writes nothing: its stdout is the one-line bundle, its stderr the messages.
#
# THE HOST WRITER is data.store.seeds.bundle in the host venv, the ONE host-side no-follow writer
# (S11): stdout is piped to `python -m data.store.seeds.bundle --out $(SEED_OUT)`, which writes
# <revision>.sql and <revision>.json there. SEED_OUT defaults to output/seeds, git-ignored, inside
# the repository and never under tests/ (the writer refuses tests/ itself); a person copies a
# reviewed seed into tests/system/seeds/ (tj-vhboky.56). A SEED_OUT OUTSIDE the repository reached
# through a symlinked parent (macOS /tmp, a symlinked checkout path) is refused, exit 3, by design:
# the writer's no-follow walk starts at / for such a path (S11.2). DATE, when given, is forwarded as
# --date; the producer refuses one that is not a real YYYY-MM-DD date.
#
# THE STATUS is the producer's whenever it is non-zero (0, 3 refused, 1 failed), else the writer's.
# Not `set -o pipefail`, which reports the RIGHTMOST failure -- the writer's 'no bundle line' after a
# failed producer. bash's PIPESTATUS (SHELL is /bin/bash) is read straight after the pipeline.
# Behind the SAME disposable-database guard as test-system: the producer writes its scenario into
# whatever database the default compose project's stack holds. $(VENV_MARKER), unlike test-system,
# because the writer runs in the host venv.
#
# ONLY THE WRITER GETS PYTHONPATH, and the asymmetry is the point (tj-iontkq.4). The producer runs
# INSIDE the container, where the image's own ENV already names the import root and the trees sit at
# /code unprefixed -- the move deliberately left the container layout alone. The writer runs in the
# HOST venv, outside pytest, so it gets neither pytest.ini's pythonpath nor the image's ENV, and
# data.store.seeds.bundle now resolves only with server/ on the path. Same roots and order as the
# errors-doc target above.
SEED_OUT ?= output/seeds
SEED_DUMP_COMPOSE := $(SYSTEM_COMPOSE) -f docker-compose.test-client.yaml

.PHONY: seed-dump
seed-dump: $(VENV_MARKER)  ## Dump a seed from the fake-mode stack into SEED_OUT (default output/seeds; DATE=YYYY-MM-DD; SYSTEM_TEST_DISPOSABLE_DB=1). A SEED_OUT outside the repo via a symlinked parent is refused, exit 3
	$(SYSTEM_TEST_DISPOSABLE_GUARD)
	@[ -f .env ] || { echo "make seed-dump: no .env in $(CURDIR); compose interpolates the stack's credentials from it." >&2; exit 1; }
	$(SEED_DUMP_COMPOSE) run --rm -T --entrypoint /code/.venv/bin/python test_client -m data.store.seeds $(if $(DATE),--date "$$SEED_DUMP_DATE") \
		| PYTHONPATH="$(CURDIR)/server:$(CURDIR)" $(VENV_PYTHON) -m data.store.seeds.bundle --out "$$SEED_DUMP_OUT"; \
		status=("$${PIPESTATUS[@]}"); \
		if [ "$${status[0]}" -ne 0 ]; then exit "$${status[0]}"; fi; \
		exit "$${status[1]}"

# DATE and SEED_OUT reach the recipe through the environment, never spliced into its text: a quote in
# either would otherwise break the shell quoting around it (tj-irhy0a.25). Declared after the recipe
# so the rule with the recipe stays the first `seed-dump:` line, the one readers of this file look for.
seed-dump: export SEED_DUMP_DATE := $(DATE)
seed-dump: export SEED_DUMP_OUT := $(SEED_OUT)
