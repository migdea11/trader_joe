#! /bin/bash
set -euo pipefail

# The ONE definition of "a digest of exactly the source the Dockerfile COPYs into a service image"
# (decision tj-yb1bxj clauses 1, 5 and 6).
#
# Usage:
#   tools/source_digest.sh <service-path> <service-name>      # print one 64-hex digest
#   tools/source_digest.sh --list-paths <path> <name>         # print the COPY sources it covers
#   tools/source_digest.sh --list-files <path> <name>         # print every file it hashes
#
# e.g.  tools/source_digest.sh data store
#
# WHY IT EXISTS. data/store/migrations/env.py imports the models from /code, which is IN THE IMAGE,
# while only alembic.ini and migrations/ are bind-mounted. So `alembic check` always compares the
# IMAGE'S models against the live schema, never the checkout's. On 2026-10-04 that reported three
# drift items that were all phantoms of a stale prod image, one of them a bug the branch had
# already removed (data/store/app/database/models/store_dataset_entry.py:67). run_migrations.sh's
# header already said "Run this from a checkout that matches the running image." It warned, nothing
# enforced it, and it failed anyway. This digest is what makes the correspondence checkable: the
# build stamps it onto the image as a label and migrate-check (tj-ymsobh) recomputes it here and
# refuses on a mismatch.
#
# THE OUTPUT CONTRACT, which the Dockerfile label and tj-ymsobh both key on: on success this prints
# exactly one line matching ^[0-9a-f]{64}$ and nothing else on stdout. Every diagnostic goes to
# stderr. Any failure exits non-zero and prints NO digest, so a caller that forgets to check the
# status gets an empty stamp -- unverifiable -- rather than a wrong one.
#
# ---------------------------------------------------------------------------------------------
# THE PATH LIST IS NOT MAINTAINED BY HAND, and that is the point of clause 6. It is PARSED out of
# the Dockerfile's own COPY instructions, so adding a COPY grows the digest's coverage in the same
# commit, and coverage cannot silently shrink behind a list someone forgot to update. A COPY the
# parser cannot read is a hard error, never a skipped line -- a parser that quietly finds nothing
# is precisely the hollow guard this whole epic keeps finding.
#
# WHAT IS AND IS NOT COVERED. Exactly the build context the service image is built from: the COPY
# sources, minus what .dockerignore keeps out of the context. NOT covered: the Dockerfile's own
# instructions. An edit that changes the image without changing a COPY source or its contents -- a
# new ENV, a different `uv sync` group set -- does not move this digest. Dependency CHANGES are
# covered, because pyproject.toml and uv.lock are themselves COPY sources. Closing the remaining
# gap would mean hashing the Dockerfile too, which contradicts "exactly the source it COPYs"; it is
# a stated limit, not an oversight.
#
# WHY .dockerignore MUST BE HONOURED, and this is the part that is easy to get wrong in the
# dangerous direction. .dockerignore excludes common/tests/, routers/tests/, schemas/tests/,
# **/__pycache__/ and **/*.py[cod] -- all of them UNDER a COPY source. A digest that hashed them
# would change on every test edit while the image stayed byte-identical, so migrate-check would
# refuse against a perfectly fresh image, every day, until someone set the escape hatch and stopped
# reading the output. tj-yb1bxj names that outcome explicitly. So the exclusions are applied -- but
# EXCLUDING TOO MUCH is the fatal direction (a file in the image that the digest does not cover is
# a false match, i.e. the guard going hollow), so the parser below understands a deliberately small
# set of pattern forms and FAILS on anything else rather than guessing. Erring loud beats erring
# quiet in either direction, and only one of the two directions is recoverable.
#
# DETERMINISM, which is the whole requirement. The file list is sorted bytewise under LC_ALL=C; the
# manifest records the path AND the content of every file, so a rename moves the digest; the
# executable bit is recorded because COPY preserves it; and nothing else about the filesystem is
# read -- no mtime, no uid, no gid, no other permission bit, no directory walk order. Two runs on
# the same tree agree, and `touch`ing a file changes nothing.
#
# THE FORMAT VERSION in the manifest header is load-bearing. Change how this script hashes and the
# digest of an unchanged tree changes, so every image built by the previous version reads as stale
# and gets rebuilt. That is the correct, fail-closed behaviour; bump MANIFEST_VERSION deliberately
# when the format changes, so the reason is in the history.

MANIFEST_VERSION='trader_joe source-digest v1'

# Bytewise ordering and C collation everywhere below: `sort` under a UTF-8 locale orders by
# collation rules that differ between hosts, which would make the digest depend on the machine.
export LC_ALL=C

REPO_ROOT="$(realpath "$(dirname "$0")/..")"
DOCKERFILE="$REPO_ROOT/Dockerfile"
DOCKERIGNORE="$REPO_ROOT/.dockerignore"

usage() {
    echo "usage: $(basename "$0") [--list-paths|--list-files] <service-path> <service-name>" >&2
    echo "  e.g. $(basename "$0") data store" >&2
}

MODE=digest
case "${1:-}" in
    --list-paths) MODE=paths; shift ;;
    --list-files) MODE=files; shift ;;
    --help | -h)
        usage
        exit 0
        ;;
    --*)
        echo "$(basename "$0"): unknown option '$1'" >&2
        usage
        exit 2
        ;;
esac

if [ $# -ne 2 ]; then
    usage
    exit 2
fi

SERVICE_PATH="$1"
SERVICE_NAME="$2"

# Compose resolves the build context against the repository root and so must this: every path
# below, in the Dockerfile and in .dockerignore alike, is relative to it.
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------------------------
# The COPY sources, parsed out of the Dockerfile.
#
# A `COPY --from=` reads another stage or a registry image, not the build context, and is skipped;
# everything else contributes every operand but the last (the destination). The build args the
# Dockerfile names in a source -- SERVICE_PATH and SERVICE_NAME, which is why they are arguments
# here -- are expanded, and any OTHER unexpanded `$` is a hard error, because silently dropping
# such a source is exactly how coverage shrinks without anyone noticing.
#
# The accepted spellings are the shell form with plain, unquoted, wildcard-free operands. The JSON
# array form, quoted operands and wildcards all fail loudly: none is used today, and a parser that
# half-understands a new one is worse than a parser that stops.
copy_sources() {
    # Join backslash continuations first, so a wrapped COPY is read whole rather than as two lines
    # neither of which parses.
    sed -e ':a' -e '/\\$/{N;s/\\\n/ /;ba' -e '}' "$DOCKERFILE" |
        awk -v service_path="$SERVICE_PATH" -v service_name="$SERVICE_NAME" '
        function die(msg) { printf("source_digest: %s\n  in: %s\n", msg, line) > "/dev/stderr"; exit 3 }
        {
            line = $0
            sub(/^[ \t]+/, "", line)
            head = toupper(substr(line, 1, 5))
            if (head != "COPY " && substr(head, 1, 4) != "ADD ") next
            if (line ~ /\[/) die("a JSON-array COPY/ADD is not supported by the digest parser")
            if (line ~ /["'"'"']/) die("a quoted COPY/ADD operand is not supported by the digest parser")

            n = split(line, word, /[ \t]+/)
            operands = 0
            for (i = 2; i <= n; i++) {
                if (word[i] == "") continue
                if (substr(word[i], 1, 2) == "--") {
                    if (word[i] ~ /^--from=/) next   # another stage or image, not the context
                    continue                          # --chown / --chmod / --link: no source of its own
                }
                operands++
                operand[operands] = word[i]
            }
            if (operands < 2) die("a COPY/ADD with fewer than two operands is not supported")

            for (i = 1; i < operands; i++) {
                src = operand[i]
                gsub(/\$\{SERVICE_PATH\}|\$SERVICE_PATH/, service_path, src)
                gsub(/\$\{SERVICE_NAME\}|\$SERVICE_NAME/, service_name, src)
                if (src ~ /\$/) die("COPY source \"" src "\" names a build arg the digest cannot expand")
                if (src ~ /[*?[]/) die("a wildcard COPY source (\"" src "\") is not supported by the digest parser")
                sub(/^\.\//, "", src)
                sub(/\/+$/, "", src)
                if (src == "" || src == ".") die("COPY source \"" operand[i] "\" resolves to the whole context")
                print src
            }
        }
    ' | sort -u
}

# ---------------------------------------------------------------------------------------------
# The .dockerignore exclusions, parsed into four rule kinds. Anything else is a hard error.
#
#   seg:<name>    from `**/<name>` with no wildcard -- any path segment equal to <name>
#   base:<glob>   from `**/<glob>` with a wildcard -- any file whose basename matches <glob>
#   lit:<path>    a wildcard-free path -- that exact path, and everything under it
#   root:<glob>   a single wildcard-bearing segment -- a TOP-LEVEL entry matching it, and anything
#                 under it. Top level only, which is Docker's own rule: `*.so` does not match
#                 `common/x.so`. Matching it at any depth would exclude more than the build does,
#                 and over-exclusion is the direction that makes a false match possible.
#
# Negations (`!…`) and wildcards in a multi-segment pattern are refused outright. Both are
# expressible in .dockerignore and neither is used here; a digest that mis-handled one would
# silently stop covering part of the image.
ignore_rules() {
    [ -f "$DOCKERIGNORE" ] || return 0
    local pattern
    while IFS= read -r pattern || [ -n "$pattern" ]; do
        pattern="${pattern%$'\r'}"
        # Trailing whitespace is not significant in .dockerignore and a stray space would
        # otherwise make a literal rule match nothing at all -- silently, which is the bad way.
        pattern="${pattern%"${pattern##*[![:space:]]}"}"
        pattern="${pattern#"${pattern%%[![:space:]]*}"}"
        [ -n "$pattern" ] || continue
        case "$pattern" in '#'*) continue ;; esac

        case "$pattern" in
            '!'*)
                echo "source_digest: .dockerignore negation '$pattern' is not supported." >&2
                echo "  A negation re-includes a path, and guessing wrong would leave part of the" >&2
                echo "  image uncovered by the stamp. Teach this script the rule, deliberately." >&2
                exit 3
                ;;
        esac

        pattern="${pattern#/}"
        pattern="${pattern%/}"
        [ -n "$pattern" ] || continue

        case "$pattern" in
            '**/'*)
                local tail="${pattern#'**/'}"
                case "$tail" in
                    */*)
                        echo "source_digest: .dockerignore pattern '$pattern' is not supported (a" >&2
                        echo "  multi-segment tail after '**/'). Teach this script the rule." >&2
                        exit 3
                        ;;
                    *[\*\?\[]*) printf 'base:%s\n' "$tail" ;;
                    *) printf 'seg:%s\n' "$tail" ;;
                esac
                ;;
            *[\*\?\[]*)
                case "$pattern" in
                    */*)
                        echo "source_digest: .dockerignore pattern '$pattern' is not supported (a" >&2
                        echo "  wildcard inside a multi-segment path). Teach this script the rule." >&2
                        exit 3
                        ;;
                    *) printf 'root:%s\n' "$pattern" ;;
                esac
                ;;
            *) printf 'lit:%s\n' "$pattern" ;;
        esac
    done < "$DOCKERIGNORE"
}

# ---------------------------------------------------------------------------------------------
# NOTHING BELOW USES PROCESS SUBSTITUTION OR A COMMAND SUBSTITUTION TO CARRY A FAILURE, and that is
# not a style preference. The first draft of this script read each stage through `< <(stage)`, so
# every `exit` above killed a subshell and nothing else: an unsupported .dockerignore pattern, a
# COPY source that did not exist, a build arg that would not expand -- each one silently truncated
# the input and the script went on to print a perfectly plausible 64-hex digest and exit 0. A
# stamp that is wrong in silence is worse than no stamp, and it is the exact failure this epic
# exists to stop. Every stage now writes to a file in the current shell, where `set -e` and
# `pipefail` can see it fail.
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

ignore_rules > "$WORK_DIR/rules"
IGNORE_RULES=()
while IFS= read -r rule; do
    [ -n "$rule" ] && IGNORE_RULES+=("$rule")
done < "$WORK_DIR/rules"

is_excluded() {
    local path="$1" rule kind body first segment rest
    first="${path%%/*}"
    for rule in ${IGNORE_RULES[@]+"${IGNORE_RULES[@]}"}; do
        kind="${rule%%:*}"
        body="${rule#*:}"
        case "$kind" in
            seg)
                rest="$path"
                while :; do
                    segment="${rest%%/*}"
                    [ "$segment" = "$body" ] && return 0
                    [ "$rest" = "$segment" ] && break
                    rest="${rest#*/}"
                done
                ;;
            base)
                # shellcheck disable=SC2053  # $body is a glob on purpose; SC2053 is [[ ]]'s code (SC2254 is `case`'s)
                [[ "${path##*/}" == $body ]] && return 0
                ;;
            lit)
                [ "$path" = "$body" ] && return 0
                case "$path" in "$body"/*) return 0 ;; esac
                ;;
            root)
                # shellcheck disable=SC2053  # $body is a glob on purpose; SC2053 is [[ ]]'s code (SC2254 is `case`'s)
                [[ "$first" == $body ]] && return 0
                ;;
        esac
    done
    return 1
}

# ---------------------------------------------------------------------------------------------
copy_sources > "$WORK_DIR/sources"
SOURCES=()
while IFS= read -r source; do
    [ -n "$source" ] && SOURCES+=("$source")
done < "$WORK_DIR/sources"

if [ ${#SOURCES[@]} -eq 0 ]; then
    echo "source_digest: the Dockerfile COPY parse found no build-context source at all." >&2
    echo "  That is a parser failure, not an empty Dockerfile: refusing to print a digest" >&2
    echo "  that would cover nothing while looking like a real one." >&2
    exit 3
fi

if [ "$MODE" = paths ]; then
    printf '%s\n' "${SOURCES[@]}"
    exit 0
fi

# Every file the build context delivers under each COPY source, sorted bytewise. A source that
# survives the ignore rules with no file left is a parse failure, not a legitimately empty tree:
# an over-broad .dockerignore rule that erased a whole package would otherwise shrink the digest's
# coverage in silence, which is the one failure mode that makes a stale image read as fresh.
collect_files() {
    local source kept path
    for source in "${SOURCES[@]}"; do
        if [ -d "$source" ]; then
            find "$source" \( -type f -o -type l \) -print0 > "$WORK_DIR/walk"
        elif [ -f "$source" ] || [ -L "$source" ]; then
            printf '%s\0' "$source" > "$WORK_DIR/walk"
        else
            echo "source_digest: COPY source '$source' does not exist in $REPO_ROOT." >&2
            echo "  The Dockerfile COPYs it, so this build context could not be sent either." >&2
            exit 4
        fi

        kept=0
        while IFS= read -r -d '' path; do
            case "$path" in
                *$'\n'*)
                    echo "source_digest: '$path' contains a newline, which this manifest cannot encode." >&2
                    exit 4
                    ;;
            esac
            is_excluded "$path" && continue
            printf '%s\0' "$path"
            kept=1
        done < "$WORK_DIR/walk"

        if [ "$kept" -eq 0 ]; then
            echo "source_digest: COPY source '$source' contributed no file to the digest." >&2
            echo "  Either it is empty, or a .dockerignore rule excluded all of it. Both leave" >&2
            echo "  the stamp covering less than the image holds; failing instead." >&2
            exit 4
        fi
    done
}

# The manifest, which IS the digest's definition: a version line, the covered path list (so adding
# or removing a COPY source moves the digest even when no file content changed), then one line per
# file. `f` records the executable bit, which COPY preserves and which `chmod +x entrypoint.sh`
# would otherwise change invisibly; `l` records the link target rather than following it.
build_manifest() {
    printf '%s\n' "$MANIFEST_VERSION"
    printf 'paths %s\n' "$(printf '%s ' "${SOURCES[@]}")"
    local path hash mode target
    while IFS= read -r -d '' path; do
        if [ -L "$path" ]; then
            target="$(readlink "$path")"
            hash="$(printf '%s' "$target" | sha256sum)"
            printf 'l %s %s\n' "${hash%% *}" "$path"
        else
            if [ -x "$path" ]; then mode=755; else mode=644; fi
            hash="$(sha256sum < "$path")"
            printf 'f %s %s %s\n' "$mode" "${hash%% *}" "$path"
        fi
    done < "$WORK_DIR/files"
}

collect_files > "$WORK_DIR/files.unsorted"
sort -z < "$WORK_DIR/files.unsorted" > "$WORK_DIR/files"

if [ "$MODE" = files ]; then
    tr '\0' '\n' < "$WORK_DIR/files"
    exit 0
fi

# Each stage lands in a file and is checked, so a run that went wrong cannot reach this line and
# print a plausible-looking stamp -- see the note above WORK_DIR for the draft where it could.
build_manifest > "$WORK_DIR/manifest"
DIGEST="$(sha256sum < "$WORK_DIR/manifest")"
DIGEST="${DIGEST%% *}"

case "$DIGEST" in
    [0-9a-f]*) ;;
    *)
        echo "source_digest: sha256sum produced '$DIGEST', which is not a digest." >&2
        exit 5
        ;;
esac
if [ ${#DIGEST} -ne 64 ]; then
    echo "source_digest: sha256sum produced a ${#DIGEST}-character value, expected 64." >&2
    exit 5
fi

printf '%s\n' "$DIGEST"
