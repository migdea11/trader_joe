# THE AGENT-STACK MCP IMAGE (ADR tj-4rr0la addendum 1 (a); tj-c4mosr.3 item 11). The server in its own
# container, separate from the devcontainer and from test_client. Not a stage of the service
# Dockerfile: the agent-mcp uv group lands here and in NO service image.
#
# BUILD CONTEXT: the repository root, with this file as the Dockerfile -- it needs pyproject.toml and
# uv.lock. The root .dockerignore does not apply: BuildKit reads Dockerfile.dockerignore beside this
# file instead, an allow-list, so no env file can reach the context at all.
#
# NO SOCKET, NO DOCKER GROUP. The docker CLI and its plugins are here; the daemon is reached only
# through the socket proxy over TCP (DOCKER_HOST, set per subprocess by runner.DOCKER_ENV).
#
# Base images pinned by digest, the exact Python patch in the tag for the reader. The docker CLI,
# compose and buildx are pinned by version AND sha256 per architecture -- the devcontainer
# Dockerfile's rule: an architecture with no pin fails saying so.

# --- the docker CLI and its plugins, downloaded and verified ----------------------------------------
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS docker_cli

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# docker 29.8.1 (download.docker.com static build; no published checksum, so the sums below were
# taken from the archives on 2026-09-30), compose v5.5.1 and buildx v0.37.1 (both matched against
# the release's checksums.txt). compose builds through buildx, so both plugins are needed.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) darch='x86_64'; carch='x86_64'; barch='amd64'; \
             docker_sum='d8db66739d2e28d4933786d73e918d9be643a67fbd835db1bf740d650a259e70'; \
             compose_sum='db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576'; \
             buildx_sum='9447199cdb435f25880548343c128a4b6650e8891ee598905d8d29d39a8e359b' ;; \
      arm64) darch='aarch64'; carch='aarch64'; barch='arm64'; \
             docker_sum='667395fbffab52901b80181dfbb39ea76da2fbd7642c4fbddd24e42146b07b48'; \
             compose_sum='732e3a84c1a0f67256ce80bc2598a24546b10ca05f9faa97efceb1171ece2ef7'; \
             buildx_sum='e5cc9fe3bbff5cbc91230981f7860e06076110730a2db997082652199042a1f2' ;; \
      *) echo "no docker CLI checksums pinned for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/docker.tgz "https://download.docker.com/linux/static/stable/${darch}/docker-29.8.1.tgz"; \
    echo "${docker_sum}  /tmp/docker.tgz" | sha256sum -c -; \
    curl -fsSL -o /tmp/docker-compose "https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-${carch}"; \
    echo "${compose_sum}  /tmp/docker-compose" | sha256sum -c -; \
    curl -fsSL -o /tmp/docker-buildx "https://github.com/docker/buildx/releases/download/v0.37.1/buildx-v0.37.1.linux-${barch}"; \
    echo "${buildx_sum}  /tmp/docker-buildx" | sha256sum -c -; \
    tar -xzf /tmp/docker.tgz -C /tmp docker/docker; \
    install -D -m 0755 /tmp/docker/docker /out/docker; \
    install -D -m 0755 /tmp/docker-compose /out/cli-plugins/docker-compose; \
    install -D -m 0755 /tmp/docker-buildx /out/cli-plugins/docker-buildx; \
    /out/docker --version

# --- the agent-mcp group, from the committed lock ----------------------------------------------------
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS build

# Same uv as Makefile UV_VERSION, the service Dockerfile, the devcontainer and CI.
COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /bin/uv
# The base image's own interpreter, never a download, so the venv's python resolves in the final stage.
ENV UV_PROJECT_ENVIRONMENT=/opt/agent_mcp/.venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock ./
# --only-group agent-mcp: the MCP SDK and what it needs, none of base, dev, testing or a service
# group. --frozen: exactly the committed lock, never a re-resolve.
RUN uv sync --only-group agent-mcp --frozen

# --- the image ----------------------------------------------------------------------------------------
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS agent_mcp_image

# The devcontainer's UID/GID (its Dockerfile's defaults), so the 0600 token file the server writes
# under AGENT_HOME_PATH is readable by the agent user there, and git's ownership check accepts the
# host checkout without a safe.directory exception. make agent-mcp-up passes the host's own ids.
ARG UID=1000
ARG GID=1000

# git: the read-only plumbing the server runs (worktree list, cat-file). Nothing else.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "${GID}" agent_mcp \
    && useradd -u "${UID}" -g "${GID}" -m -s /usr/sbin/nologin agent_mcp

COPY --from=docker_cli /out/docker /usr/local/bin/docker
COPY --from=docker_cli /out/cli-plugins/ /usr/local/lib/docker/cli-plugins/
COPY --from=build /opt/agent_mcp/.venv /opt/agent_mcp/.venv

# The server, without its tests. Root-owned, so the server cannot rewrite itself.
COPY tools/__init__.py /opt/agent_mcp/tools/__init__.py
COPY tools/agent_mcp/*.py /opt/agent_mcp/tools/agent_mcp/
# THE TRUSTED COMPOSE FILES (stack.TRUSTED_COMPOSE_DIR): the -f list the verbs pass, taken from the
# checkout the image is built from, never from an agent's worktree at run time. A compose change
# reaches the MCP when this image is rebuilt (make agent-mcp-up).
COPY docker-compose.yaml docker-compose.test-client.yaml docker-compose.fake.yaml /opt/agent_mcp/compose/
# THE TRUSTED DOCKERFILE (stack.TRUSTED_DOCKERFILE; ADR tj-4rr0la addendum 5, ruling 2): the service
# Dockerfile, which the MCP-only overlays below (docker-compose.agent-dev.yaml, -test.yaml and -client.yaml)
# name as build.dockerfile for what they build, so the worktree's own Dockerfile is never read and
# FROM / COPY --from cannot reach other images on the host daemon. Same rebuild rule as the compose files.
COPY Dockerfile /opt/agent_mcp/compose/Dockerfile
# THE TRUSTED COPIES FOR THE USER'S DEV PROJECT (stack.DEV_COMPOSE_FILES; ADR tj-4rr0la addendum 23,
# epic tj-iee6sn). The dev set's -f list: the base file (copied above), the override, the web prod
# and web dev files, and the MCP-only overlay that points build.dockerfile at the trusted Dockerfile
# above. docker-compose.tools.yaml (pgAdmin) is for tools. Nothing from the checkout's own files
# is ever passed to compose.
COPY docker-compose.override.yaml docker-compose.web.yaml docker-compose.web.dev.yaml \
    docker-compose.agent-dev.yaml docker-compose.tools.yaml /opt/agent_mcp/compose/
# TEST MODE'S BUILD OVERLAY (stack.TEST_BUILD_COMPOSE_FILES; tj-iee6sn.24): it sets only build.dockerfile
# for data_store and data_ingest, after the base file and docker-compose.fake.yaml (both copied above).
COPY docker-compose.agent-test.yaml /opt/agent_mcp/compose/
# THE SYSTEM-TEST CLIENT'S OVERLAY (stack.SYSTEM_CLIENT_COMPOSE_FILES; tj-iee6sn.20): sets only test_client's
# build.dockerfile, after the base file and docker-compose.test-client.yaml (both copied above), so the
# client built from a worktree's snapshot uses the trusted Dockerfile below, never the snapshot's own.
COPY docker-compose.agent-client.yaml /opt/agent_mcp/compose/
# THE TWO SCRIPTS the dev verbs run from here instead of the checkout's (an agent-writable file run
# inside agent_mcp would hold the Docker API): dev_instance_secret.sh resolves the dev web container's
# INSTANCE_WRITE_SECRET, source_digest.sh stamps the images. In a tools/ directory beside the trusted
# Dockerfile, the repository's own layout, because source_digest.sh finds the Dockerfile at ../Dockerfile
# from itself. They need sh, bash, awk, sed, sort, find, tr, mktemp, realpath and sha256sum, all of
# which the Debian slim base provides (no package is installed for them). Same rebuild rule as the
# compose files.
# proto_digest.sh (tj-iee6sn.33) is the third: it recomputes the content stamp of gen/proto/<kind>, run
# with --context over the repository mount, and needs only bash, find, sort, sha256sum and realpath.
COPY tools/dev_instance_secret.sh tools/source_digest.sh tools/proto_digest.sh /opt/agent_mcp/compose/tools/

ENV PATH="/opt/agent_mcp/.venv/bin:${PATH}" \
    PYTHONPATH=/opt/agent_mcp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER agent_mcp
WORKDIR /home/agent_mcp
CMD ["python", "-m", "tools.agent_mcp"]
