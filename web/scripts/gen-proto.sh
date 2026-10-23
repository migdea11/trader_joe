#!/bin/sh
# Generate gen/proto/ts AND its content stamp, gen/proto/ts.digest (bead tj-grna9p.186).
#
# Every TypeScript generation goes through here (the `gen:proto` npm script, so the predev, prebuild,
# pretypecheck and pretest hooks and `make gen-proto-ts` alike), so the stamp can never say "current"
# for a tree a different input generated. The protocol is the Makefile's: delete the old stamp FIRST,
# generate, write the stamp LAST. A failed generation or a failed digest leaves NO stamp and exits
# non-zero. The stamp is written to a temp file beside it and renamed, so an interrupted write leaves
# no stamp rather than a partial one.
#
# The digest is the repo script's, tools/proto_digest.sh (one sha256 on success, nothing on failure).
# POSIX sh: npm runs scripts with `sh -c`.

set -eu

# Run from web/ whatever the caller's directory: buf.gen.yaml and ../proto are relative to it.
# shellcheck disable=SC1007 # an empty CDPATH for this one cd is the point
CDPATH= cd -- "$(dirname "$0")/.."

stamp=../gen/proto/ts.digest
tmp="$stamp.tmp.$$"

rm -f "$stamp"
# Cleanup on EXIT only. Each signal gets its own trap that EXITS: a trap that merely returns lets the
# shell carry on after the signal (it would go on to write the stamp), so an interrupted generation
# must end the script, with the conventional 128+signal status.
trap 'rm -f "$tmp"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

buf generate ../proto --template buf.gen.yaml

digest="$(bash ../tools/proto_digest.sh ts)"
[ -n "$digest" ] || { echo "gen-proto: proto_digest.sh printed no digest" >&2; exit 1; }
# Exactly one sha256: 64 lowercase hex characters and nothing else (a stray line fails closed).
case "$digest" in
    *[!0-9a-f]*) echo "gen-proto: proto_digest.sh output is not a sha256" >&2; exit 1 ;;
esac
[ "${#digest}" -eq 64 ] || { echo "gen-proto: proto_digest.sh output is not 64 characters" >&2; exit 1; }

printf '%s\n' "$digest" > "$tmp"
mv "$tmp" "$stamp"
