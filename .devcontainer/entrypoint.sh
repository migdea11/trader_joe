#!/bin/sh
# Container entrypoint for the agent-workflow bootstrap container.
#
# Container home is not host home, so the machine-level skill symlinks written by
# bin/setup-machine are not here. Without this step the workflow skills simply do not exist
# inside the container, which presents as "the skill isn't there" rather than as a mount problem.
set -eu

SKILL_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"

if [ -d /kit/skills ]; then
    mkdir -p "$SKILL_DIR" || {
        printf 'entrypoint: cannot create %s — the config volume is probably root-owned\n' "$SKILL_DIR" >&2
        exit 1
    }
    for src in /kit/skills/*/; do
        [ -d "$src" ] || continue
        dest="${SKILL_DIR}/$(basename "$src")"
        if [ -L "$dest" ] || [ ! -e "$dest" ]; then
            rm -f "$dest"
            ln -s "${src%/}" "$dest"
        fi
    done
else
    printf 'warning: /kit is not mounted — workflow skills are unavailable\n' >&2
fi

exec "$@"
