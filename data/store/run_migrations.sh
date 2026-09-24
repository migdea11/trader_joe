#! /bin/bash
set -euo pipefail

# Apply the data-store migrations to the running stack.
#
# This is the single spelling of "apply the migrations": the make target and, later, the
# deploy step call this script rather than repeating the compose invocation (tj-oiv075 —
# a deploy-script step is the adopted long-term design, never the entrypoint and never a
# compose dependency). Nothing else in the stack creates the schema: entrypoint.sh runs
# uvicorn only, data_store's lifespan opens the engine without creating tables, and
# docker-compose.yaml has no migration service. A healthy stack has an EMPTY database
# until this runs.
#
# THE REVISIONS ARE NOT IN THE IMAGE, AND THAT MATTERS. The Dockerfile copies only
# entrypoint.sh, common, routers, schemas and the service app dir. alembic.ini and
# migrations/ reach the container as bind mounts from THIS CHECKOUT
# (docker-compose.yaml:112-113). So the code that runs is whatever image is deployed, but
# the revisions that get applied are whatever is checked out here, and the two can
# disagree silently. Run this from a checkout that matches the running image. Shipping the
# revisions inside the image is a build-infra change, not this script's job — tj-y3sj8x.

REPO_ROOT="$(realpath "$(dirname "$0")/../..")"
MIGRATION_DIR="$REPO_ROOT/data/store/migrations/versions"

# Compose resolves the compose file, .env and every bind-mount source against the project
# directory, and derives the default project name from it. Run from anywhere else, this
# would either not find docker-compose.yaml or stand up a second, differently named
# project beside the running one.
cd "$REPO_ROOT"

# -f docker-compose.yaml explicitly, matching `make build` and the CI migrate step. A bare
# `docker compose` also auto-loads docker-compose.override.yaml, which swaps data_store to
# the dev image; migrations belong against the production service definition.
COMPOSE=(docker compose -f docker-compose.yaml)

echo "Applying migrations from $MIGRATION_DIR"

if [ ! -d "$MIGRATION_DIR" ]; then
    echo "Migration directory does not exist: $MIGRATION_DIR" >&2
    exit 1
fi

# An empty versions/ makes `alembic upgrade head` a successful no-op, which is
# indistinguishable from a migration that worked. Given the bind mount above, that is
# exactly the symptom of running from the wrong checkout, so fail loudly instead.
if ! compgen -G "$MIGRATION_DIR/*.py" > /dev/null; then
    echo "No revision files in $MIGRATION_DIR — wrong checkout?" >&2
    exit 1
fi

# --no-deps, so the dependency check is ours to make. Without it compose starts BOTH
# declared dependencies for a container that needs only the database, and kafka's 90s
# start_period is charged to every migration. With it, a stopped database surfaces as a
# name-resolution failure from inside the container; this says the same thing in one line.
# Bringing postgres up here instead was rejected: `up` recreates a container whose config
# hash has changed, and applying migrations must never restart the database it migrates.
postgres_container="$("${COMPOSE[@]}" ps -q postgres)"
if [ -z "$postgres_container" ]; then
    echo "postgres is not running — start it first (make launch-deps), then re-run." >&2
    exit 1
fi

# No pip install. alembic and psycopg2-binary are both in the data-store dependency group
# the image syncs (pyproject.toml [dependency-groups], Dockerfile:29), so the venv already
# has them — and data_store declares read_only: true, so installing at migration time
# could not have worked even if they were missing. The interpreter is addressed by full
# path because the image sets CMD and not ENTRYPOINT: an argv here replaces entrypoint.sh
# rather than appending to it. `run` publishes no ports, so this cannot collide with the
# data_store container already serving traffic.
"${COMPOSE[@]}" run --rm --no-deps data_store /code/.venv/bin/alembic upgrade head
