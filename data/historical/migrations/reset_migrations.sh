#! /bin/bash

SCRIPT_DIR="$(realpath "$(dirname "$0")")"
MIGRATION_DIR="$SCRIPT_DIR/versions"
echo "Deleting $MIGRATION_DIR"
rm -Rf $MIGRATION_DIR/*.py
docker-compose run --rm --entrypoint /bin/bash data_hist -c "alembic revision --autogenerate -m 'Initial migration'"
