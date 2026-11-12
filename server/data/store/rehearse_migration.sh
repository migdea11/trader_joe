#! /bin/bash
set -euo pipefail

# Rehearse a migration on a copy of real data (migration-safety gate m5, epic tj-7zjov1, bead P03).
#
# Usage: SYSTEM_TEST_DISPOSABLE_DB=1 rehearse_migration.sh <dump-file>
#
# Restores a dump (typically production's, copied to this machine) into the TEST stack's database, applies
# this checkout's migrations to it, and reports health. Three steps, each stopping the run on failure:
#   1. restore_dump.sh <dump>: refuses unless the running stack is the test stack and attested disposable;
#   2. run_migrations.sh (upgrade head), which follows the running stack (so the TEST definition) and
#      takes its own pre-migration dump first, like any migrate;
#   3. health: `alembic current` must be at head, and the data_store container's own health, when it
#      is running, must not be unhealthy.
# It prints one REHEARSAL line per fact and ends with REHEARSAL OK, or exits non-zero without it. The
# test stack's data_store keeps whatever schema it had open; restart it (make dev-down, make
# system-launch) to see the service itself run against the rehearsed schema. This script does not.

SCRIPT_DIR="$(realpath "$(dirname "$0")")"

# The repository root (the directory holding pytest.ini): compose finds docker-compose.yaml and .env
# there, as in the sibling scripts.
REPO_ROOT="$SCRIPT_DIR"
while [ ! -f "$REPO_ROOT/pytest.ini" ]; do
    if [ "$REPO_ROOT" = / ]; then
        echo "no ancestor of $SCRIPT_DIR carries pytest.ini; cannot locate the repository root" >&2
        exit 1
    fi
    REPO_ROOT="$(dirname "$REPO_ROOT")"
done
cd "$REPO_ROOT"

if [ $# -ne 1 ]; then
    echo "usage: rehearse_migration.sh <dump-file>" >&2
    exit 2
fi
DUMP="$1"

"$SCRIPT_DIR/restore_dump.sh" "$DUMP"
echo "REHEARSAL: restored $DUMP"

"$SCRIPT_DIR/run_migrations.sh"
echo "REHEARSAL: migrated to head"

current="$("$SCRIPT_DIR/run_migrations.sh" current)"
echo "REHEARSAL: $(printf '%s\n' "$current" | tail -n 1)"
case "$current" in
    *"(head)"*) echo "REHEARSAL: database is at head" ;;
    *)
        echo "REHEARSAL FAILED: the database is not at head after the migration." >&2
        exit 1
        ;;
esac

data_store_id="$(docker compose -p trader_joe_dev -f docker-compose.yaml ps -q data_store 2> /dev/null || true)"
if [ -z "$data_store_id" ]; then
    echo "REHEARSAL: data_store container not running; its health was not checked"
else
    health="$(docker inspect --format '{{ with .State.Health }}{{ .Status }}{{ end }}' "$data_store_id")"
    echo "REHEARSAL: data_store health: ${health:-none}"
    if [ "$health" = unhealthy ]; then
        echo "REHEARSAL FAILED: data_store reports unhealthy." >&2
        exit 1
    fi
fi
echo "REHEARSAL OK"
