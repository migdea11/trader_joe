# EXTERNAL IMAGES ARE DIGEST-PINNED (ADR tj-4rr0la addenda 13-14). Every FROM and COPY --from that
# names a registry image is tag@sha256:<multi-arch INDEX digest> -- the manifest list, not one
# platform's manifest -- and tools/agent_mcp/stack.py BASE_IMAGES lists exactly these refs. The agent
# stack builds this file from inside agent_mcp, which has no egress: the MCP has the DAEMON pull each
# ref first, because buildx would otherwise fetch the registry token itself and fail. A digest bump
# (Dependabot's docker entry, or by hand) updates BASE_IMAGES in the same change.
# Digests taken 2026-10-01 from the registry API (the Docker-Content-Digest of the tag's index):
# debian:bookworm-slim from registry-1.docker.io, uv:0.12.19 from ghcr.io.

# Base Build Image
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS base_build_image
ARG SERVICE_PATH=none
ARG SERVICE_NAME=none

# User setup
RUN addgroup --system appgroup && adduser --ingroup appgroup appuser
USER appuser
WORKDIR /code

# Install common dependencies
# 0.9.17 is a floor, not a preference: older uv silently ignores the `exclude-newer`
# cooldown in pyproject.toml. Matches Makefile UV_VERSION and the CI workflow.
COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /uvx /bin/
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

# NOTE: the build deliberately copies no .env. Configuration arrives at runtime through
# compose's env_file:, and a baked .env would put whatever the build host happened to
# have -- Alpaca keys, database password -- into an image layer that is then pushed to
# GHCR. data/store/migrations/env.py calls load_dotenv('.env'), which is a no-op when the
# file is absent; it reads DATABASE_URI from the process environment either way.

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

# System-test client image (tj-q9ae5u addendum 1 item 4', ruling Q2-B on tj-ijpys9.9). The
# test_client service in docker-compose.test-client.yaml runs tests/system from it; only
# `make test-system` and CI's client steps build it. Shaped like the future SDK base image --
# Python plus the client-side dependencies -- with the testing group layered on top. When the
# SDK image exists (tj-d2mhru) this becomes FROM that image instead.
#
# NO SOURCE IS COPIED: compose bind-mounts common, routers, schemas, data/store/app,
# data/store/migrations, tests/system and pytest.ini read-only from the checkout, so a test edit
# needs no rebuild and a stale image cannot run old tests. Placed before the deploy stages so
# prod_image stays the last stage, the one a target-less `docker build` produces.
FROM base_build_image AS system_test_image

# Root for the package step only. curl for CI's probes from the client; tzdata because the
# suite runs under a non-UTC TZ, and without the zone's data the container silently runs at UTC,
# where a naive-to-timestamptz shift cannot go red.
USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*
USER appuser

RUN uv sync --only-group base --only-group data-store --only-group testing --frozen

# Base Deploy Image
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS base_deploy_image
ARG SERVICE_PATH=none
ARG SERVICE_NAME=none

# User setup
RUN addgroup --system appgroup && adduser --ingroup appgroup appuser
USER appuser

# Setup environment
WORKDIR /code
ENV PYTHONPATH="/code"
ENV PATH="/code/.venv/bin/:${PATH}"

# Setup service execution
COPY --from=service_build_image /home/appuser/.local/share/uv/python /home/appuser/.local/share/uv/python
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
