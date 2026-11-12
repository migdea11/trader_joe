#! /bin/bash
set -euo pipefail

# Restore a data-store dump into the TEST stack's database (migration-safety gate m4/m5, epic
# tj-7zjov1, bead P03).
#
# Usage: SYSTEM_TEST_DISPOSABLE_DB=1 restore_dump.sh <dump-file>
#
# The dump is one run_migrations.sh took (pg_dump, custom format). THIS DESTROYS THE TARGET'S DATA: it
# drops the public schema and loads the dump over it, so it runs only against the stack `make
# system-launch` started (compose project trader_joe_dev, base + docker-compose.fake.yaml, on
# ./volume_data/test), never dev's data and never production's. Three refusals, all before anything is
# touched:
#   * SYSTEM_TEST_DISPOSABLE_DB is not the literal 1 (the same attestation `make test-system` asks for);
#   * the running postgres in trader_joe_dev does not carry docker-compose.fake.yaml (and not the
#     override) in its compose config_files label -- the full rule run_migrations.sh's
#     classify_config_files applies, restated here because that script runs on source and cannot be
#     sourced; the parity tests in test_migration_safety_scripts.py pin the two against each other; the production
#     project is never even probed;
#   * the dump does not read back (pg_restore --list), so a bad file cannot cost the target's data.
#
# Run in the postgres container (its client matches its server; POSTGRES_USER and POSTGRES_DB are the
# container's own variables, so no credential is read or spelled here). Exit status is non-zero on any
# failure; a failed load leaves the target half-loaded, which is fine for a disposable database and is
# why it is refused anywhere else.

find_repo_root() {
    local dir="$1"
    while true; do
        if [ -f "$dir/pytest.ini" ]; then
            printf '%s\n' "$dir"
            return 0
        fi
        if [ "$dir" = / ]; then
            break
        fi
        dir="$(dirname "$dir")"
    done
    echo "no ancestor of $1 carries pytest.ini; cannot locate the repository root" >&2
    exit 1
}

if [ $# -ne 1 ]; then
    echo "usage: restore_dump.sh <dump-file>" >&2
    exit 2
fi
DUMP="$1"
if [ ! -f "$DUMP" ] || [ ! -s "$DUMP" ]; then
    echo "REFUSED: $DUMP is not a non-empty file." >&2
    exit 1
fi
if [ "${SYSTEM_TEST_DISPOSABLE_DB:-}" != 1 ]; then
    echo "REFUSED: this replaces the target database's contents. Set SYSTEM_TEST_DISPOSABLE_DB=1 to attest that the trader_joe_dev test stack holds nothing you need." >&2
    exit 1
fi

REPO_ROOT="$(find_repo_root "$(realpath "$(dirname "$0")")")"
cd "$REPO_ROOT"

DEV_PROJECT=trader_joe_dev
COMPOSE=(docker compose -p "$DEV_PROJECT" -f docker-compose.yaml -f docker-compose.fake.yaml)

postgres_ids="$(docker compose -p "$DEV_PROJECT" -f docker-compose.yaml ps -q postgres)"
if [ -z "$postgres_ids" ] || [ "$postgres_ids" != "${postgres_ids%%$'\n'*}" ]; then
    echo "REFUSED: expected exactly one running postgres in the $DEV_PROJECT project (make system-launch starts it)." >&2
    exit 1
fi
if ! label="$(docker inspect \
    --format '{{ with .Config.Labels }}{{ index . "com.docker.compose.project.config_files" }}{{ end }}' \
    "$postgres_ids")"; then
    echo "REFUSED: could not read the compose config_files label of the running postgres in $DEV_PROJECT (docker inspect failed, see above), so it cannot be shown to be the test stack; the target was not touched." >&2
    exit 1
fi
# The label must be well formed exactly as classify_config_files in run_migrations.sh requires: not
# empty, no empty entry (leading, trailing or doubled comma), no newline, every entry an absolute path
# (so a relative entry or a space after a comma is malformed). Anything else is refused, not guessed at.
if [ -z "$label" ]; then
    echo "REFUSED: the running postgres in $DEV_PROJECT is not the test stack (its compose label must name docker-compose.fake.yaml and not docker-compose.override.yaml). Start it with make system-launch." >&2
    exit 1
fi
case "$label" in
    ,* | *, | *,,* | *$'\n'*)
        echo "REFUSED: the compose config_files label of the running postgres in $DEV_PROJECT is malformed ($label). Start it with make system-launch." >&2
        exit 1
        ;;
esac
has_fake=0
has_override=0
IFS=',' read -r -a entries <<< "$label"
for entry in "${entries[@]}"; do
    if [ -z "$entry" ] || [ "${entry#/}" = "$entry" ]; then
        echo "REFUSED: the compose config_files label of the running postgres in $DEV_PROJECT is malformed ($label). Start it with make system-launch." >&2
        exit 1
    fi
done
for entry in "${entries[@]}"; do
    case "${entry##*/}" in
        docker-compose.fake.yaml) has_fake=1 ;;
        docker-compose.override.yaml) has_override=1 ;;
    esac
done
if [ "$has_fake" -ne 1 ] || [ "$has_override" -ne 0 ]; then
    echo "REFUSED: the running postgres in $DEV_PROJECT is not the test stack (its compose label must name docker-compose.fake.yaml and not docker-compose.override.yaml). Start it with make system-launch." >&2
    exit 1
fi

if ! "${COMPOSE[@]}" exec -T postgres pg_restore --list < "$DUMP" > /dev/null; then
    echo "REFUSED: $DUMP does not read back (pg_restore --list failed); the target was not touched." >&2
    exit 1
fi

# The single quotes are the point: the variables are the container's, expanded by its shell.
# shellcheck disable=SC2016
"${COMPOSE[@]}" exec -T postgres sh -c \
    'exec psql --user "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"' > /dev/null
# shellcheck disable=SC2016
"${COMPOSE[@]}" exec -T postgres sh -c \
    'exec pg_restore --user "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-owner --exit-on-error --single-transaction' < "$DUMP"

echo "restore_dump.sh: restored $DUMP into the $DEV_PROJECT test database"
