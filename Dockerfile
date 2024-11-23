# Creating Base Image
FROM python:3.12-slim AS base_image

EXPOSE 80
WORKDIR /code
ENV PYTHONPATH="/code"

COPY ./.env /code/.env
COPY ./entrypoint.sh /code/entrypoint.sh

#
# Creating Data Store Service
#
FROM base_image AS data_store
COPY ./data/store/requirements.txt /code/
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Add Service
COPY ./data/store/app /code/data/store/app
# Add Internal Deps
COPY ./common /code/common
COPY ./routers /code/routers
COPY ./schemas /code/schemas

# Set the entrypoint script as the entrypoint
ENV APP_MODULE="data.store.app.main:app"
ENTRYPOINT ["/code/entrypoint.sh"]

#
# Creating Data Ingest Service
#
FROM base_image AS data_ingest
COPY ./data/ingest/requirements.txt /code/
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Add Service
COPY ./data/ingest/app /code/data/ingest/app
# Add Internal Deps
COPY ./common /code/common
COPY ./routers /code/routers
COPY ./schemas /code/schemas

# Set the entrypoint script as the entrypoint
ENV APP_MODULE="data.ingest.app.main:app"
ENTRYPOINT ["/code/entrypoint.sh"]