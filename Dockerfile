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
# /code/gen/proto/python is the generated gRPC code's import root (trader_joe.proto; decision
# tj-3mk3u5.42 F1), reached by configuration, never by code. pytest.ini's pythonpath mirrors it.
# Under compose this ENV does not hold on its own: the root env file's legacy PYTHONPATH=./ arrives
# through env_file:, which outranks an image's ENV. So docker-compose.yaml sets this same value,
# literally, in the environment: of every service built from the service stages (addendum F1-A),
# and environment: outranks both. Change the copies together.
ENV PYTHONPATH="/code:/code/gen/proto/python"
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

# Add common files. gen/proto/python is the protoc output common/rpc imports; without it the image
# starts until the first servicer that imports generated code is registered, then fails. It is
# GENERATED AND NEVER COMMITTED, so the build context only holds it because the build targets run
# `make proto` on the host first (Makefile prod-build/dev-build, and a step in CI's Image Build and
# System Testing jobs). Generating here instead would ship grpcio-tools in the prod image, because
# base_deploy_image COPYs this stage's whole /code and the venv is /code/.venv -- the Makefile's
# proto block records that measurement and why a stage of its own was rejected too.
#
# THE CONTAINER LAYOUT IS DELIBERATELY NOT THE CHECKOUT LAYOUT (epic tj-iontkq, R-2). The service
# trees live under ./server in the checkout and under /code in the image, so only the LEFT of each
# COPY gained a server/ prefix. Everything downstream -- ENV PYTHONPATH="/code", APP_MODULE's
# ${SERVICE_PATH}.${SERVICE_NAME}.app.main, entrypoint.sh, /code/alembic.ini, /code/migrations and
# every compose mount target -- is unchanged, which is the whole reason the move was shaped this way.
# entrypoint.sh and gen/proto/python stay at the top of the repository and keep their bare sources.
#
# WHY NOT JUST `COPY ./server /code` -- the obvious next question. Because it would put every
# service's app, both test trees, data/store/migrations and data/store/seeds into every image. The
# by-name copies are what keep one service's code out of the other's image, and
# data/*/tests/test_no_production_test_imports.py asserts exactly that from this list.
COPY ./entrypoint.sh /code/entrypoint.sh
COPY ./server/common /code/common
COPY ./server/routers /code/routers
COPY ./server/schemas /code/schemas
COPY ./gen/proto/python /code/gen/proto/python

# Add service-specific files
COPY ./server/${SERVICE_PATH}/${SERVICE_NAME}/app /code/${SERVICE_PATH}/${SERVICE_NAME}/app

# Extending Base Build image to include dev deps
FROM service_build_image AS service_build_image_dev
RUN uv sync --only-group base --only-group ${SERVICE_PATH}-${SERVICE_NAME} --only-group dev --frozen

# System-test client image (tj-q9ae5u addendum 1 item 4', ruling Q2-B on tj-ijpys9.9). The
# test_client service in docker-compose.test-client.yaml runs tests/system from it; only
# `make test-system` and CI's client steps build it. Shaped like the future SDK base image --
# Python plus the client-side dependencies -- with the testing group layered on top. When the
# SDK image exists (tj-d2mhru) this becomes FROM that image instead.
#
# NO SOURCE IS COPIED: compose bind-mounts common, routers, schemas, gen/proto/python, data/store/app,
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

# Setup environment. PYTHONPATH as in base_build_image: the root, then the generated code's root.
WORKDIR /code
ENV PYTHONPATH="/code:/code/gen/proto/python"
ENV PATH="/code/.venv/bin/:${PATH}"

# Setup service execution
COPY --from=service_build_image /home/appuser/.local/share/uv/python /home/appuser/.local/share/uv/python
ENV APP_MODULE="${SERVICE_PATH}.${SERVICE_NAME}.app.main:app"
CMD ["/code/entrypoint.sh"]

# THE SOURCE STAMP (decision tj-yb1bxj clauses 1 and 6), applied to both deploy stages below.
#
# WHAT IT IS FOR. data/store/migrations/env.py imports the models from /code -- IN THE IMAGE --
# while only alembic.ini and migrations/ are bind-mounted, so `alembic check` always compares the
# IMAGE'S models against the live schema and never the checkout's. On 2026-10-04 that reported
# three drift items against a stale prod image that were all phantoms. This label is what lets
# migrate-check (tj-ymsobh) tell a stale image from a fresh one instead of warning about it.
#
# THE VALUE is tools/source_digest.sh's output for this service: a sha256 over exactly the build
# context the COPYs above deliver. That script PARSES ITS PATH LIST OUT OF THIS FILE, so adding a
# COPY here grows what the digest covers in the same commit -- a hand-maintained list would let
# coverage shrink silently and the guard go hollow a second time.
#
# PLACED LAST IN EACH STAGE, ON PURPOSE. An ARG invalidates every layer below it whenever its
# value changes, and this value changes on every source edit. Declared any earlier, each build
# would be a cold build. It feeds nothing but the LABEL, so it is declared and consumed at the
# very bottom and only that one layer is rebuilt.
#
# WHEN IT IS EMPTY, AND WHAT EMPTY MEANS. docker-compose.yaml interpolates the value from the
# environment of whoever runs the build, which the Makefile's prod-build and dev-build set. A
# build started any other way -- the agent stack, CI, a bare `docker compose build` -- leaves it
# unset, and compose then passes an empty string. Compose cannot omit a mapping-form build arg, so
# the label is always present and the EMPTY VALUE is what says "unstamped"; an image built before
# this change has no such label key at all. THE CONTRACT WITH tj-ymsobh IS THEREFORE: the stamp is
# trustworthy only when the label exists AND matches ^[0-9a-f]{64}$. A missing key and an empty
# value both mean "cannot verify", which is NOT "matches" -- conflating them is how this goes
# hollow again. Nothing but a real digest can ever satisfy that pattern, so neither case can be
# mistaken for a match.
#
# WHAT IT DOES NOT COVER: this file's own instructions. A new ENV or a different `uv sync` group
# changes the image without moving the digest. Dependency changes ARE covered, because
# pyproject.toml and uv.lock are COPY sources themselves. Stated limit, not an oversight.

# Dev-specific stage
FROM base_deploy_image AS dev_image
# Copy dev service source and deps from build
COPY --from=service_build_image_dev /code /code

ENV RUN_MODE="dev"
ENV ADDITIONAL_ARGS="--reload"

ARG SOURCE_DIGEST
LABEL trader_joe.source.digest="${SOURCE_DIGEST}"

# Prod-specific stage
FROM base_deploy_image AS prod_image
# Copy service source and deps from build
COPY --from=service_build_image /code /code

ENV RUN_MODE="prod"

ARG SOURCE_DIGEST
LABEL trader_joe.source.digest="${SOURCE_DIGEST}"
