# EXTERNAL IMAGES ARE DIGEST-PINNED (ADR tj-4rr0la addenda 13-14). Every FROM and COPY --from that
# names a registry image is tag@sha256:<multi-arch INDEX digest> -- the manifest list, not one
# platform's manifest -- and tools/agent_mcp/stack.py BASE_IMAGES lists exactly these refs. The agent
# stack builds this file from inside agent_mcp, which has no egress: the MCP has the DAEMON pull each
# ref first, because buildx would otherwise fetch the registry token itself and fail. A digest bump
# (Dependabot's docker entry, or by hand) updates BASE_IMAGES in the same change.
# Digests taken 2026-10-01 from the registry API (the Docker-Content-Digest of the tag's index):
# debian:bookworm-slim and caddy:2.11.7-alpine (the web image's server, 2026-10-06) from
# registry-1.docker.io, uv:0.12.19 from ghcr.io.

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

# THE WEB IMAGE (decisions tj-grna9p.5, .6, .7; bead tj-grna9p.26): two stages, a Node build and a Caddy
# server, built only by docker-compose.web.yaml and its dev overlay. They sit before the deploy stages
# for the reason system_test_image does: prod_image stays the LAST stage, the one a target-less
# `docker build` makes.
#
# THE DEV SERVICE (bead tj-mcrwrd) USES web_build_image AS ITS TARGET, not a stage of its own: that
# stage already holds the pinned Node, the installed dependencies (/web/node_modules, owned by the
# non-root user) and a baked copy of the source, which is all `vite` needs. docker-compose.web.dev.yaml
# runs vite from it and bind-mounts ./web/src over /web/src, so a host edit hot-reloads. The cost is one
# `vite build` at dev-build time whose dist the dev server never reads. A stage of its own, FROM a
# shared Node-and-dependencies stage, would avoid that and was written first; it moved the Node RUN
# out of web_build_image, which the build-infra tests read it from, so it was folded back.
#
# NO COPY HERE READS THE BUILD CONTEXT, and that is deliberate. tools/source_digest.sh parses every
# COPY source out of this file into every service image's digest, and the reach tests derive their
# roots from the same list, so a `COPY web/...` would stamp each service image with the UI's source
# and put web/ in the Python reach scan. The sources arrive as the NAMED BUILD CONTEXTS web_src
# (./web) and web_deploy (./deploy/web), which docker-compose.web.yaml declares as additional_contexts,
# read through `RUN --mount=type=bind,from=...`. NOT `COPY --from=<context>`: the digest-pin test
# (tools/agent_mcp/tests/test_bases.py) reads every COPY --from operand that is not a stage as a
# registry image and demands a digest of it, and a named context is a local directory, not an image.
# No registry image is left unpinned by this; the two contexts are checkouts of this repository.
# A named context is not filtered by .dockerignore, so the source step drops a host node_modules
# and dist itself, below.
#
# THE NODE PIN. The three NODE_* values MIRROR the Makefile's NODE_VERSION, NODE_SHA256_X86_64 and
# NODE_SHA256_AARCH64, which are the one authority (tj-grna9p.97), exactly as .devcontainer/Dockerfile
# mirrors them: this build cannot read the Makefile, and a build arg fed from it would leave a build
# outside make with no version. A bump is those three values here, in the devcontainer and in the
# Makefile, in one commit. A test pins them equal. npm is whatever the pinned Node bundles.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS web_build_image
ARG NODE_VERSION=24.21.0
ARG NODE_SHA256_X86_64=6e1db87ef58b8819e5d5402eff1536491b18edd8eb7bee5ef7897876e88dc5ff
ARG NODE_SHA256_AARCH64=724282c3b43aec998aa9527380465b45d229e021b58035f5f4f63095eabfe5d5

# Root for the package step and the Node install only. The official tarball, checksum-verified; .tar.gz
# because it needs nothing beyond tar and gzip.
USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) asset="node-v${NODE_VERSION}-linux-x64.tar.gz"; sum="${NODE_SHA256_X86_64}" ;; \
      arm64) asset="node-v${NODE_VERSION}-linux-arm64.tar.gz"; sum="${NODE_SHA256_AARCH64}" ;; \
      *) echo "no node checksum pinned for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.gz "https://nodejs.org/dist/v${NODE_VERSION}/${asset}"; \
    echo "${sum}  /tmp/node.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \
      --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md'; \
    rm /tmp/node.tar.gz; \
    [ "$(node --version)" = "v${NODE_VERSION}" ]

RUN addgroup --system appgroup && adduser --ingroup appgroup appuser \
    && install -d -o appuser -g appgroup /web
USER appuser
WORKDIR /web

# The lockfile first, so the dependency layer is reused until it changes. npm ci installs exactly the
# lockfile and fails when package.json disagrees with it. Two single-file bind mounts, so this layer's
# cache key is those two files and not the whole of web/.
RUN --mount=type=bind,from=web_src,source=package.json,target=/web/package.json \
    --mount=type=bind,from=web_src,source=package-lock.json,target=/web/package-lock.json \
    npm ci --no-audit --no-fund

# The source, streamed through tar so that whatever node_modules or dist the host had is left behind:
# the host's platform-specific binaries must never replace the ones npm ci just installed here.
RUN --mount=type=bind,from=web_src,target=/mnt/web_src \
    tar -C /mnt/web_src --exclude=./node_modules --exclude=./dist -cf - . | tar -C /web -xf -
# The PrimeUI Community licence key, read by Vite at build time as import.meta.env.VITE_PRIMEUI_LICENSE_KEY
# (web/src/config/licence.ts) from the process environment, hence ENV and not just ARG. Empty by default:
# the app builds and works without it and shows a red "Invalid PrimeUI License" notice at runtime.
# ENV records the value in this stage's image metadata, and the value ends up in the public JS bundle
# anyway. That is acceptable ONLY because the Community licence allows the key in the shipped bundle. It
# follows that an image built with a key must NEVER be pushed to a public registry unless the owner
# decides so. Declared after the dependency layers so changing the key does not invalidate npm ci.
ARG VITE_PRIMEUI_LICENSE_KEY=
ENV VITE_PRIMEUI_LICENSE_KEY=$VITE_PRIMEUI_LICENSE_KEY

# npm run build writes /web/dist, WITH --ignore-scripts: the `prebuild` pre-script is `npm run gen:proto`,
# i.e. buf generate, and this image has no buf and no proto/. Nothing generated is committed (gen/ is
# gitignored), so the host writes gen/proto/ts (make gen-proto-ts, a prerequisite of prod-build and
# dev-build) and compose hands it over as the named context web_gen, mounted at /gen/proto/ts: with
# WORKDIR /web, that is the ../gen/proto/ts the @generated alias in vite.config.ts points at. A bind
# mount, so it adds no layer and the generated code is not copied into the image; the dist it helped
# produce is. The build script itself is just `vite build`, which does not type-check.
RUN --mount=type=bind,from=web_gen,target=/gen/proto/ts \
    npm run build --ignore-scripts

# The server. Caddy serves /srv and proxies the one data_store route (deploy/web/Caddyfile). Non-root
# on the unprivileged port 8080. XDG_* point at /tmp, which compose mounts as tmpfs, because the root
# filesystem is read-only there and Caddy writes a little state under them.
FROM caddy:2.11.7-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f AS web_image
COPY --from=web_build_image /web/dist /srv
RUN --mount=type=bind,from=web_deploy,source=Caddyfile,target=/mnt/Caddyfile cp /mnt/Caddyfile /etc/caddy/Caddyfile
ENV XDG_DATA_HOME=/tmp/caddy-data XDG_CONFIG_HOME=/tmp/caddy-config
USER 65532:65532
EXPOSE 8080

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
