# Base Image
FROM python:3.12-slim AS base_image
WORKDIR /code
ENV PYTHONPATH="/code"

# Install common dependencies
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY ./entrypoint.sh /code/entrypoint.sh

# Install dependencies for specific services
ARG SERVICE_PATH=none
ARG SERVICE_NAME=none
COPY ./${SERVICE_PATH}/${SERVICE_NAME}/requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Add common files
COPY ./common /code/common
COPY ./routers /code/routers
COPY ./schemas /code/schemas

# Add service-specific files
COPY ./${SERVICE_PATH}/${SERVICE_NAME}/app /code/data/${SERVICE_NAME}/app

# Set entrypoint and environment variable
COPY ./.env /code/.env
ENV APP_MODULE="data.${SERVICE_NAME}.app.main:app"
ENTRYPOINT ["/code/entrypoint.sh"]

# Dev-specific stage
FROM base_image AS dev_image
ARG SERVICE_PATH
ARG SERVICE_NAME
RUN pip install --no-cache-dir --upgrade debugpy
ENV RUN_MODE="dev"
ENV ADDITIONAL_ARGS="--reload"

# Prod-specific stage
FROM base_image AS prod_image
ARG SERVICE_PATH
ARG SERVICE_NAME
ENV RUN_MODE="prod"
