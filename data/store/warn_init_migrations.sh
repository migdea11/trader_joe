#! /bin/bash

# Only use to regenerate migration dir (Including tracked files)
docker-compose run --rm --entrypoint /bin/bash data_store -c "alembic init migrations"
