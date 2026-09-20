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
        ca-certificates curl git jq less openssh-client ripgrep tar nodejs npm \
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

RUN npm install -g @anthropic-ai/claude-code@2.1.274 \
    && npm cache clean --force

# Non-root. The agent must not be able to remove its own controls.
#
# The config directory is created here, owned by the user, because compose mounts a named volume
# on it. Docker creates a missing mount point root-owned; one that exists in the image is copied
# into a new volume with its ownership intact.
RUN groupadd -g "${GID}" "${USERNAME}" \
    && useradd -u "${UID}" -g "${GID}" -m -s /bin/bash "${USERNAME}" \
    && install -d -o "${UID}" -g "${GID}" "/home/${USERNAME}/.claude"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER ${USERNAME}
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
