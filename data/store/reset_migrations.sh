#! /bin/bash

SCRIPT_DIR="$(realpath "$(dirname "$0")")"
MIGRATION_DIR="$SCRIPT_DIR/migrations/versions"
echo "Deleting $MIGRATION_DIR"
rm -Rf $MIGRATION_DIR/*.py

# Optional: Reset the database schema (use with caution)
docker-compose run --rm --entrypoint /bin/bash postgres -c "psql -U postgres -h db -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'"

# Reset the alembic_version table
# docker-compose run --rm --entrypoint /bin/bash postgres -c "psql -U postgres -h db -c 'DELETE FROM alembic_version;'"

# Generate the initial migration
docker-compose run --rm --entrypoint /bin/bash data_store -c "alembic revision --autogenerate -m 'Initial migration'"
