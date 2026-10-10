# Generic bootstrap container for agent-workflow — phase A0.
#
# Its only job is to host an agent that can reach /kit, the project and the bead store. Nothing
# here is project-specific, because at this point nothing about the project has been established.
# The tailored image is generated after discovery and replaces this one.
FROM debian:bookworm-slim

ARG USERNAME=agent
ARG UID=1000
ARG GID=1000

# python3 + python3-yaml are the workflow tooling's runtime. They are installed here so the
# prerequisite is satisfied by construction: the tooling runs inside this container, and nobody
# has to install Python on the host to use it. Distribution packages rather than pip, because a
# bookworm base refuses pip installs into the system environment.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq less openssh-client ripgrep tar \
        python3 python3-yaml \
    && rm -rf /var/lib/apt/lists/*

# bd — pinned and checksum-verified. Never "latest": a release can carry a higher version number
# than the code inside it, so the version is a label, not a capability claim.
#
# The checksum is selected by architecture, because the artifact differs per platform. An
# architecture with no pin fails saying so; a single shared checksum would instead fail as a
# mismatch, which reads like a corrupted download and sends you looking in the wrong place.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum='2f92b904ecf35b607e44dc5c39229173af69c54f1183e8d709f1773540cdcf3b' ;; \
      arm64) sum='4ce9446a68edc13202b76c84a47d66fb3dad2787b6464c9522a108faf9c1c608' ;; \
      *) echo "no bd checksum pinned for ${arch}; add BD_SHA256_* to templates/toolchain.env" >&2; exit 1 ;; \
    esac; \
    [ "$sum" != 'REPLACE_ME' ] || { echo "bd checksum for ${arch} is unpinned" >&2; exit 1; }; \
    curl -fsSL -o /tmp/bd.tar.gz \
      "https://github.com/gastownhall/beads/releases/download/v1.3.0/beads_1.3.0_linux_${arch}.tar.gz"; \
    echo "${sum}  /tmp/bd.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/bd.tar.gz -C /usr/local/bin bd; \
    rm /tmp/bd.tar.gz; \
    bd version

# dolt — the store engine. Same pinning rule, same per-architecture selection.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sum='c49d4c3e004cf1581ba0d4a00c5023a26f84eb2ec15d5fe876eed36d5343f463' ;; \
      arm64) sum='9ce70fc81e50139e97758ef7f4dc57e9583e4e5ef05ad75d7535c30caa161387' ;; \
      *) echo "no dolt checksum pinned for ${arch}; add DOLT_SHA256_* to templates/toolchain.env" >&2; exit 1 ;; \
    esac; \
    [ "$sum" != 'REPLACE_ME' ] || { echo "dolt checksum for ${arch} is unpinned" >&2; exit 1; }; \
    curl -fsSL -o /tmp/dolt.tar.gz \
      "https://github.com/dolthub/dolt/releases/download/v2.3.5/dolt-linux-${arch}.tar.gz"; \
    echo "${sum}  /tmp/dolt.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/dolt.tar.gz -C /tmp; \
    install -m 0755 "/tmp/dolt-linux-${arch}/bin/dolt" /usr/local/bin/dolt; \
    rm -rf /tmp/dolt.tar.gz "/tmp/dolt-linux-${arch}"; \
    dolt version

# Claude Code is installed further down, AS THE AGENT USER, with the native installer.
#
# It was a root `npm install -g` here. That put the package in a root-owned global node_modules
# which the agent user cannot write, so Claude's own updater could not run and updating it meant
# rebuilding this image. npm global is also the legacy install path and does not auto-update.
#
# Not the dev container feature either: this image is built by `make agent-build`, a plain
# `docker compose build`, and devcontainer.json's features are applied only by the Dev Containers
# CLI or IDE extension. Declaring the feature installs nothing here — it leaves no Claude at all.
#
# nodejs and npm left the first apt line with the npm install (tj-3mk3u5.52): nothing else in this
# image runs them, and VS Code brings its own node for its server. Something that does need them,
# such as an MCP server started through npx, is the reason to add them back; name it here if so.
# The reason now is the web chain (npm ci, lint, typecheck, test, build in /web), which builder-ui
# and the web validator run in an agent worktree (tj-grna9p.97). It is the pinned Node tarball
# further down, not the apt packages: the test_no_apt_install_brings_back_nodejs_or_npm pin stands.

# --- tailored for trader_joe from here down -------------------------------------------------
# The project's own toolchain, so an agent can actually run `make test` / `make lint` instead of
# reporting them NOT RUN. Pinned and checksum-verified, on the same rule as bd and dolt above.
#
# uv is pinned here to the same version as the other three pins: Makefile UV_VERSION, the project
# Dockerfile, and .github/workflows/trader_joe_testing.yml UV_VERSION. All four move together.
# They have to: this is the uv on PATH for every agent in this image, the Makefile refuses to run
# when the uv it finds is older than its pin, and an older uv silently rewrites uv.lock rather
# than failing. Changing one pin without the other three breaks `make` here or diverges the lock.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) target='x86_64-unknown-linux-gnu'; sum='23bf5552d220e0842b65c862097b2ebaeba0064b74eda5e565e77fd25969d8c8' ;; \
      arm64) target='aarch64-unknown-linux-gnu'; sum='0804e9b164c64b6914182d5920c08551958a095986f10a3731056df701126436' ;; \
      *) echo "no uv checksum pinned for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/0.12.19/uv-${target}.tar.gz"; \
    echo "${sum}  /tmp/uv.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tar.gz -C /tmp; \
    install -m 0755 "/tmp/uv-${target}/uv" /usr/local/bin/uv; \
    install -m 0755 "/tmp/uv-${target}/uvx" /usr/local/bin/uvx; \
    rm -rf /tmp/uv.tar.gz "/tmp/uv-${target}"; \
    uv --version

# buf, which `make lint` runs on proto/ when PATHS covers it (tj-3mk3u5.54): the bare release binary,
# pinned and checksum-verified on the same rule as bd, dolt and uv. It lints only; protoc generates.
# These values MIRROR the Makefile's BUF_VERSION, BUF_SHA256_X86_64 and BUF_SHA256_AARCH64, which are
# the one authority: this build's context is .devcontainer/, so it cannot read the Makefile.
# common/tests/test_agent_image.py pins them equal, and all of them move together, in one commit.
# Until this image is rebuilt, `make buf-install` puts the same pin in ~/.local/bin.
RUN set -eux; \
    version='1.73.0'; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) asset='buf-Linux-x86_64'; sum='8f2986298ad08f0cc1bf999b9797b7c383adf32d7edf0f73d6f1e1a701baeac1' ;; \
      arm64) asset='buf-Linux-aarch64'; sum='902b75267db7f4391e99b7fa0756050e5354234cc0437ef50eee9c788950c7a3' ;; \
      *) echo "no buf checksum pinned for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/buf \
      "https://github.com/bufbuild/buf/releases/download/v${version}/${asset}"; \
    echo "${sum}  /tmp/buf" | sha256sum -c -; \
    install -m 0755 /tmp/buf /usr/local/bin/buf; \
    rm /tmp/buf; \
    buf --version; \
    [ "$(buf --version)" = "${version}" ]

# shellcheck, which `make lint` runs on every *.sh under PATHS (tj-xc6nfv): pinned and
# checksum-verified on the same rule as bd, dolt, uv and buf. It lints only; nothing formats shell.
# These values MIRROR the Makefile's SHELLCHECK_VERSION, SHELLCHECK_SHA256_X86_64 and
# SHELLCHECK_SHA256_AARCH64, which are the one authority: this build's context is .devcontainer/, so
# it cannot read the Makefile. All of them move together, in one commit.
# A SEPARATE RUN from buf's, not folded into it: common/tests/test_agent_image.py executes the buf RUN
# with dpkg, curl, sha256sum, install, rm and buf stubbed and asserts its exact call sequence, so an
# extra command inside it breaks a test that is checking something else entirely.
# NOT `apt-get install shellcheck`: bookworm ships 0.9.0, and a linter at a version other than the one
# `make lint` pins reports a different set of findings.
# .tar.gz, not the smaller .tar.xz, because the release's .xz needs an xz binary this image does not
# have; tar and gzip are Essential in Debian. The archive holds shellcheck-v${version}/shellcheck.
# Until this image is rebuilt, `make shellcheck-install` puts the same pin in ~/.local/bin.
RUN set -eux; \
    version='0.11.0'; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) asset="shellcheck-v${version}.linux.x86_64.tar.gz"; sum='b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6' ;; \
      arm64) asset="shellcheck-v${version}.linux.aarch64.tar.gz"; sum='68a8133197a50beb8803f8d42f9908d1af1c5540d4bb05fdfca8c1fa47decefc' ;; \
      *) echo "no shellcheck checksum pinned for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/shellcheck.tar.gz \
      "https://github.com/koalaman/shellcheck/releases/download/v${version}/${asset}"; \
    echo "${sum}  /tmp/shellcheck.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/shellcheck.tar.gz -C /tmp; \
    install -m 0755 "/tmp/shellcheck-v${version}/shellcheck" /usr/local/bin/shellcheck; \
    rm -rf /tmp/shellcheck.tar.gz "/tmp/shellcheck-v${version}"; \
    shellcheck --version; \
    [ "$(shellcheck --version | awk '/^version:/ {print $2}')" = "${version}" ]

# Node and npm, which the web chain in /web runs (tj-grna9p.97): the official release tarball, pinned
# and checksum-verified on the same rule as bd, dolt, uv, buf and shellcheck. npm is the one the
# pinned Node bundles. These values MIRROR the Makefile's NODE_VERSION, NODE_SHA256_X86_64 and
# NODE_SHA256_AARCH64, which are the one authority: this build's context is .devcontainer/, so it
# cannot read the Makefile. All of them move together, in one commit.
# NOT `apt-get install nodejs npm`: bookworm ships Node 18, and the version the web chain is built
# and tested with has to equal the one the root Dockerfile and CI use.
# The checksums are the release's own SHASUMS256.txt lines. .tar.gz, not .tar.xz, because this image
# has no xz binary. The archive unpacks to node-v${version}-linux-<arch>/ with bin/, lib/, include/
# and share/; the top-level docs are excluded so they do not land loose in /usr/local.
RUN set -eux; \
    version='24.21.0'; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) asset="node-v${version}-linux-x64.tar.gz"; sum='6e1db87ef58b8819e5d5402eff1536491b18edd8eb7bee5ef7897876e88dc5ff' ;; \
      arm64) asset="node-v${version}-linux-arm64.tar.gz"; sum='724282c3b43aec998aa9527380465b45d229e021b58035f5f4f63095eabfe5d5' ;; \
      *) echo "no node checksum pinned for ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.gz "https://nodejs.org/dist/v${version}/${asset}"; \
    echo "${sum}  /tmp/node.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \
      --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md'; \
    rm /tmp/node.tar.gz; \
    node --version; \
    npm --version; \
    [ "$(node --version)" = "v${version}" ]

# make drives every command binding in the manifest; bookworm ships Python 3.11, so uv supplies
# the 3.12 the project requires. UV_PYTHON_INSTALL_DIR is outside $HOME so it survives the
# config volume being replaced, and is readable by the agent user.
RUN apt-get update && apt-get install -y --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN install -d -o 1000 -g 1000 /opt/uv/python \
    && uv python install 3.12 \
    && chmod -R a+rX /opt/uv/python

# Non-root. The agent must not be able to remove its own controls.
#
# The config directory is created here, owned by the user, so the image is coherent on its own.
# Compose bind-mounts a host directory over it, and a bind mount takes the host directory's
# ownership rather than the image's — which is why devcontainer.json creates that host directory
# before the container starts.
RUN groupadd -g "${GID}" "${USERNAME}" \
    && useradd -u "${UID}" -g "${GID}" -m -s /bin/bash "${USERNAME}" \
    && install -d -o "${UID}" -g "${GID}" "/home/${USERNAME}/.claude"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER ${USERNAME}

# Claude Code, installed by its native installer as the agent user rather than as root.
#
# This is what makes `claude` able to update itself inside a running container: the launcher at
# ~/.local/bin/claude is a symlink into ~/.local/share/claude/versions/<version>/, and both are
# owned by the user running Claude, so the updater swaps versions without sudo and without a
# rebuild. PATH is set explicitly because the entrypoint execs its command — no login shell runs,
# so the line the installer appends to ~/.bashrc is never sourced.
#
# The version argument is the STARTING point, not a ceiling: Claude updates past it in place. A
# rebuild returns to this version, and .claude/workflow.yml's claude_code pin is what to bump.
# To hold a version instead, set DISABLE_AUTOUPDATER=1 in compose.yml.
#
# Deliberately not a named volume over ~/.local/share/claude: Docker seeds an empty volume from
# the image, but never re-seeds a populated one, so after a version bump the volume would still
# hold the old payload while the image's launcher symlink pointed at a version that is not there.
ENV PATH="/home/${USERNAME}/.local/bin:${PATH}"
# No checksum is pinned on this script, unlike bd, dolt and uv above, and on purpose: accepted risk
# tj-y9imnu. The trust root is the vendor origin, which the in-place updater pulls every later
# version from anyway, so a pin here would protect one build and nothing after it; upstream
# publishes no checksum to pin.
RUN curl -fsSL https://claude.ai/install.sh | bash -s 2.1.274 \
    && claude --version

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
