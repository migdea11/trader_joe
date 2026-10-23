#! /bin/bash
set -euo pipefail

# The ONE definition of "a digest of the inputs the generated protobuf code is built from" (bead
# tj-grna9p.164; it supersedes the mtime guard of tj-iee6sn.29).
#
# Usage:
#   tools/proto_digest.sh [--context <dir>] python   # print one 64-hex digest over proto/**/*.proto
#   tools/proto_digest.sh [--context <dir>] ts       # the same, plus buf.yaml and web/buf.gen.yaml
#
# --context <dir>, which must come first, digests ANOTHER checkout with THIS script (tj-iee6sn.33). The
# agent MCP runs a TRUSTED copy of this script baked into its image, beside which there is no proto/;
# without the flag that copy would digest its own directory. Everything below is unchanged under the
# flag: the same refusals, the same manifest, so the digest of a checkout is the same either way.
#
# WHY. A generated-code guard has to ask "was gen/proto/<language> generated from THESE protos", and
# only content answers that: a timestamp moves on a checkout, a rebase or a touch with the protos
# unchanged, and stays put across an edit that preserves mtime. `make proto` and `make gen-proto-ts`
# write this script's output to gen/proto/<language>.digest as their last step; a reader (the agent
# MCP, which runs its own trusted copy of this script) recomputes it and compares. Writer and reader
# run the SAME algorithm, so they cannot disagree.
#
# WHAT IS COVERED. Every *.proto under proto/, by relative path and content. For ts also the two files
# `buf generate ../proto --template buf.gen.yaml` (run from web/) reads: web/buf.gen.yaml, whose plugins
# and options change the generated output as surely as a .proto does, and the repo-root buf.yaml, which
# buf reads to resolve the module (its path, and any future excludes or dependencies). The python target
# covers .proto files only: protoc reads no buf.yaml and no template.
# NOT covered: the toolchain (grpcio-tools, buf, protoc-gen-es versions; the plugin is pinned in
# web/package.json and its lockfile, and a bump that changes the output without changing an input does
# not move this digest; the pins are the control for that, and hashing the whole lockfile would mark
# every ts tree stale on an unrelated dependency bump). There is no buf.lock (buf.yaml declares no deps);
# if it ever gains deps, buf.lock becomes an input and belongs here.
#
# THE OUTPUT CONTRACT, as tools/source_digest.sh: on success exactly one line matching ^[0-9a-f]{64}$
# and nothing else on stdout; every diagnostic goes to stderr; any failure exits non-zero and prints
# NO digest, so a caller that forgets the status gets an empty stamp, never a wrong one.
#
# DETERMINISM. Paths are sorted bytewise (LC_ALL=C) and the manifest records the path AND a hash of the
# content of every file, so a rename, an add, a remove or any edit moves the digest. No mtime, no
# mode, no owner, no directory walk order, no git. Run from any directory: it resolves the repository
# from its own location.
#
# REFUSALS. A symlink anywhere under proto/ (or proto/ itself, or buf.yaml or web/buf.gen.yaml being
# one, or web/) and a path with a newline are errors, not skipped entries: this script is trusted code
# that runs over an agent-writable checkout, and following a link would hash something outside the tree.
# For ts, buf.yaml or web/buf.gen.yaml missing, unreadable or not a regular file is an error as well. An
# empty set of .proto files is an error too -- a digest that covers nothing looks like a real one.
#
# For ts, a buf.yaml, buf.work.yaml or buf.lock ANYWHERE under proto/ (any depth, a symlink to one
# included) is refused as well, with the path named on stderr: `buf generate ../proto` obeys them and the
# digest hashes none of them. The python digest ignores them, as protoc does. This is an input check, so
# the manifest is unchanged and the digests of valid trees are byte-identical to before it.
#
# KNOWN LIMITS. The stamp answers "generated from these protos and this config". The generator versions
# (protoc via uv.lock, @bufbuild/protoc-gen-es via the web lockfile) and python's protoc flags (the
# Makefile's proto recipe) are controlled by those lockfiles and by every make proto / make gen-proto-ts
# regenerating; they are deliberately outside the digest. buf.lock becomes an input if buf.yaml ever
# declares dependencies.
#
# THE FORMAT VERSION is load-bearing: changing how this hashes moves every digest, so every stamp
# reads as stale and is regenerated. That is the correct fail-closed behaviour; bump it deliberately.
# v2 (bead tj-grna9p.185): the ts manifest gains buf.yaml, so the ts digest changes meaning; the python
# digest is computed from the same files as in v1, but the version line is part of the manifest, so it
# moves too. Both digests differ from v1 for that reason alone.

MANIFEST_VERSION='trader_joe proto-digest v2'

# Bytewise ordering everywhere: sort under a UTF-8 locale differs between hosts.
export LC_ALL=C

NAME="$(basename "$0")"

usage() {
    echo "usage: $NAME [--context <dir>] <python|ts>" >&2
}

CONTEXT=""
if [ "${1:-}" = --context ]; then
    if [ $# -lt 2 ] || [ ! -d "$2" ]; then
        echo "$NAME: --context needs a directory" >&2
        usage
        exit 2
    fi
    CONTEXT="$2"
    shift 2
fi

if [ $# -ne 1 ]; then
    usage
    exit 2
fi

TARGET="$1"
case "$TARGET" in
    python | ts) ;;
    *)
        echo "$NAME: unknown target '$TARGET'" >&2
        usage
        exit 2
        ;;
esac

if [ -n "$CONTEXT" ]; then
    REPO_ROOT="$(realpath "$CONTEXT")"
else
    REPO_ROOT="$(realpath "$(dirname "$0")/..")"
fi
cd "$REPO_ROOT"

if [ -L proto ] || [ ! -d proto ]; then
    echo "$NAME: proto/ is missing, or is a symlink or not a directory, in $REPO_ROOT; refusing to digest" >&2
    exit 4
fi

# Nothing below carries a failure through a command or process substitution: each stage writes to a
# file in this shell, where set -e sees it fail (see the note in tools/source_digest.sh).
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# ts only: `buf generate ../proto` obeys a buf.yaml, buf.work.yaml or buf.lock found ANYWHERE under
# proto/, and none of them is hashed, so one would change generation without moving the digest. Refuse,
# whatever the entry's type (a symlink to one included). python is unaffected: protoc reads none.
if [ "$TARGET" = ts ]; then
    find proto \( -name buf.yaml -o -name buf.work.yaml -o -name buf.lock \) -print > "$WORK_DIR/bufconfigs"
    if [ -s "$WORK_DIR/bufconfigs" ]; then
        echo "$NAME: refusing the ts digest; buf reads these under proto/ and they would change generation without changing the digest:" >&2
        cat "$WORK_DIR/bufconfigs" >&2
        exit 4
    fi
fi

# A symlink anywhere under proto/ is refused, whatever its name.
find proto -type l -print > "$WORK_DIR/links"
if [ -s "$WORK_DIR/links" ]; then
    echo "$NAME: refusing to digest; symlinks under proto/ are not followed:" >&2
    cat "$WORK_DIR/links" >&2
    exit 4
fi

# Every *.proto must be a regular file (a fifo or device named *.proto would hang or lie).
find proto -name '*.proto' ! -type f ! -type d -print > "$WORK_DIR/irregular"
if [ -s "$WORK_DIR/irregular" ]; then
    echo "$NAME: refusing to digest; not a regular file:" >&2
    cat "$WORK_DIR/irregular" >&2
    exit 4
fi

find proto -type f -name '*.proto' -print0 > "$WORK_DIR/files.unsorted"
if [ "$TARGET" = ts ]; then
    if [ -L buf.yaml ] || [ ! -f buf.yaml ] || [ ! -r buf.yaml ]; then
        echo "$NAME: buf.yaml is missing, unreadable, or is a symlink or not a regular file; refusing to digest" >&2
        exit 4
    fi
    if [ -L web ] || [ -L web/buf.gen.yaml ] || [ ! -f web/buf.gen.yaml ] || [ ! -r web/buf.gen.yaml ]; then
        echo "$NAME: web/buf.gen.yaml is missing, unreadable, or is a symlink or not a regular file; refusing to digest" >&2
        exit 4
    fi
    printf '%s\0' buf.yaml web/buf.gen.yaml >> "$WORK_DIR/files.unsorted"
fi
sort -z < "$WORK_DIR/files.unsorted" > "$WORK_DIR/files"

proto_count=0
while IFS= read -r -d '' path; do
    case "$path" in
        *$'\n'*)
            echo "$NAME: '$path' contains a newline, which this manifest cannot encode." >&2
            exit 4
            ;;
        proto/*) proto_count=$((proto_count + 1)) ;;
    esac
done < "$WORK_DIR/files"
if [ "$proto_count" -eq 0 ]; then
    echo "$NAME: no .proto file under proto/; refusing to print a digest that covers nothing." >&2
    exit 4
fi

build_manifest() {
    printf '%s\n' "$MANIFEST_VERSION"
    printf 'target %s\n' "$TARGET"
    local path hash
    while IFS= read -r -d '' path; do
        hash="$(sha256sum < "$path")"
        printf 'f %s %s\n' "${hash%% *}" "$path"
    done < "$WORK_DIR/files"
}

build_manifest > "$WORK_DIR/manifest"
DIGEST="$(sha256sum < "$WORK_DIR/manifest")"
DIGEST="${DIGEST%% *}"

if [ ${#DIGEST} -ne 64 ]; then
    echo "$NAME: sha256sum produced a ${#DIGEST}-character value, expected 64." >&2
    exit 5
fi
case "$DIGEST" in
    *[!0-9a-f]*)
        echo "$NAME: sha256sum produced '$DIGEST', which is not a digest." >&2
        exit 5
        ;;
esac

printf '%s\n' "$DIGEST"
