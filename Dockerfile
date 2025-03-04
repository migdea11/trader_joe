# Base Build Image
FROM debian:bookworm-slim AS base_build_image
ARG SERVICE_PATH=none
ARG SERVICE_NAME=none
WORKDIR /code

# Install common dependencies
COPY --from=ghcr.io/astral-sh/uv:0.6.4. /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV PYTHONPATH="/code"
ENV PATH="/code/.venv/bin:${PATH}"

COPY ./pyproject.toml /code/pyproject.toml
COPY ./uv.lock /code/uv.lock

# Install dependencies for specific services
RUN uv venv
RUN uv sync --only-group base --frozen

FROM base_build_image AS service_build_image
ARG SERVICE_PATH=none
ARG SERVICE_NAME=none

# Install service-specific dependencies
RUN uv sync --only-group base --only-group ${SERVICE_PATH}-${SERVICE_NAME} --frozen

# Set entrypoint and environment variable
COPY ./.env /code/.env

# Add common files
COPY ./entrypoint.sh /code/entrypoint.sh
COPY ./common /code/common
COPY ./routers /code/routers
COPY ./schemas /code/schemas

# Add service-specific files
COPY ./${SERVICE_PATH}/${SERVICE_NAME}/app /code/${SERVICE_PATH}/${SERVICE_NAME}/app

# Extending Base Build image to include dev deps
FROM service_build_image AS service_build_image_dev
RUN uv sync --only-group base --only-group ${SERVICE_PATH}-${SERVICE_NAME} --only-group dev --frozen

# Base Deploy Image
FROM debian:bookworm-slim AS base_deploy_image
ARG SERVICE_PATH=none
ARG SERVICE_NAME=none

# Setup environment
WORKDIR /code
ENV PYTHONPATH="/code"
ENV PATH="/code/.venv/bin/:${PATH}"

# Setup service execution
COPY --from=service_build_image /root/.local/share/uv/python /root/.local/share/uv/python
ENV APP_MODULE="${SERVICE_PATH}.${SERVICE_NAME}.app.main:app"
CMD ["/code/entrypoint.sh"]

# Dev-specific stage
FROM base_deploy_image AS dev_image
# Copy dev service source and deps from build
COPY --from=service_build_image_dev /code /code

ENV RUN_MODE="dev"
ENV ADDITIONAL_ARGS="--reload"

# Prod-specific stage
FROM base_deploy_image AS prod_image
# Copy service source and deps from build
COPY --from=service_build_image /code /code

ENV RUN_MODE="prod"
