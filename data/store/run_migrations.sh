#! /bin/bash

SCRIPT_DIR="$(realpath "$(dirname "$0")")"
MIGRATION_DIR="$SCRIPT_DIR/migrations/versions"
echo "Applying migrations from $MIGRATION_DIR"

# Ensure the migrations directory exists
if [ ! -d "$MIGRATION_DIR" ]; then
    echo "Migration directory does not exist: $MIGRATION_DIR"
    exit 1
fi

# Apply migrations up to head
docker-compose run --rm --entrypoint /bin/bash data_store -c "alembic upgrade head"