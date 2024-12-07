#! /bin/bash

docker-compose run --rm --entrypoint /bin/bash data_store -c "alembic init migrations"
