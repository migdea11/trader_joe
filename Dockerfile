# Base Image
FROM python:3.12-slim AS base_image
WORKDIR /code
ENV PYTHONPATH="/code"
ENV PATH="/code/.venv/bin:${PATH}"

# Install common dependencies
# RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
#     && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY ./entrypoint.sh /code/entrypoint.sh
COPY ./pyproject.toml /code/pyproject.toml
COPY ./uv.lock /code/uv.lock

# Install dependencies for specific services
ARG SERVICE_PATH=none
ARG SERVICE_NAME=none
RUN uv sync --group base --group ${SERVICE_PATH}-${SERVICE_NAME} --frozen --no-cache

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
RUN uv sync --group base --group ${SERVICE_PATH}-${SERVICE_NAME} --group dev --frozen --no-cache
ENV RUN_MODE="dev"
ENV ADDITIONAL_ARGS="--reload"

# Prod-specific stage
FROM base_image AS prod_image
ARG SERVICE_PATH
ARG SERVICE_NAME
ENV RUN_MODE="prod"
