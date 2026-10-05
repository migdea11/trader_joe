#! /bin/bash
set -euo pipefail

# Run an alembic command against the data-store schema on the running stack.
#
# Usage: run_migrations.sh [alembic-command...]   (default: upgrade head)
#
# This is the single spelling of "apply the migrations": the make target and, later, the
# deploy step call this script rather than repeating the compose invocation (tj-oiv075 —
# a deploy-script step is the adopted long-term design, never the entrypoint and never a
# compose dependency). Nothing else in the stack creates the schema: entrypoint.sh runs
# uvicorn only, data_store's lifespan opens the engine without creating tables, and
# docker-compose.yaml has no migration service. A healthy stack has an EMPTY database
# until this runs.
#
# THE REVISIONS ARE NOT IN THE IMAGE, AND THAT MATTERS. The Dockerfile copies pyproject.toml,
# uv.lock, entrypoint.sh, common, routers, schemas, gen/proto/python and the service app dir —
# and nothing else. alembic.ini and migrations/ reach the container as bind mounts from THIS
# CHECKOUT (docker-compose.yaml:112-113). So the code that runs is whatever image is deployed,
# but the revisions that get applied are whatever is checked out here, and the two can
# disagree silently. Shipping the revisions inside the image is a build-infra change, not this
# script's job — tj-y3sj8x.
#
# "Run this from a checkout that matches the running image" is what this header used to say, and
# saying it was all it did. On 2026-10-04 a careful operator did exactly what it warned against
# and read three phantom drift items as real. For `check` — and only for `check` — that sentence
# is now enforced rather than requested; see THE COMPARISON IMAGE IS VERIFIED below.
#
# THE ALEMBIC COMMAND IS AN ARGUMENT because that silent disagreement needs a diagnostic, and
# the diagnostic wants every check below unchanged: the repo root, the pinned compose file, the
# postgres check, and above all the empty-versions guard, since `alembic history` reads the same
# bind-mounted revision files whose absence is the symptom being looked for. The inspection
# targets call this script rather than spelling out a second compose invocation, which is exactly
# what this script's existence is meant to prevent (tj-4yvsb2). The default is `upgrade head`, so
# callers that pass nothing — `make migrate`, the deploy step — are unchanged. Nothing here
# validates the command: this is the plumbing, and it is the CALLER that promises read-only.
# Anything mutating still belongs behind a named, reviewed target.
#
# `check` IS NOT READ-ONLY ON A NEVER-MIGRATED DATABASE. That was measured, not argued:
# `alembic.command.current` passes `dont_mutate=True`; `check` does not. So `check` reaches
# `MigrationContext.run_migrations`, which calls `_ensure_version_table()` →
# `_version.create(self.connection, checkfirst=True)` — a CREATE TABLE. Run against a throwaway
# never-migrated SQLite database, `current` wrote nothing and `check` wrote `alembic_version`.
# It is read-only only against a database ALREADY AT A REVISION, and the case that reaches the
# other branch is a real one: a first run on a new environment, or a wiped volume. There it is
# also meaningless, which is the other half of the same fact — with no schema to compare against
# the models, everything reads as drift. WHICH TARGET may run `check` is settled in the Makefile
# (`migrate-check`, never `migrate-status`); read it there for that half, so this comment cannot
# go stale against it. WHAT MUST BE TRUE BEFORE `check` RUNS is settled here, below.
#
# Where it does run, `check` earns its place — unlike `current` and `history` it EXITS NON-ZERO
# when the models and the live catalogue disagree, which is the point: a database stamped at head
# whose schema has drifted (tj-5h30md) is invisible to `current`. It refuses with "Target database
# is not up to date." when the database is behind head. Its blind spots are enum labels and server
# defaults, neither of which autogenerate compares here — see migrations/env.py.

REPO_ROOT="$(realpath "$(dirname "$0")/../..")"
MIGRATION_DIR="$REPO_ROOT/data/store/migrations/versions"

# "$@" is exempt from `set -u` when empty, so this is safe with no arguments; the array is
# never empty afterwards, which keeps the expansions below safe too.
ALEMBIC_ARGS=("$@")
if [ ${#ALEMBIC_ARGS[@]} -eq 0 ]; then
    ALEMBIC_ARGS=(upgrade head)
fi

# Compose resolves the compose file, .env and every bind-mount source against the project
# directory, and derives the default project name from it. Run from anywhere else, this
# would either not find docker-compose.yaml or stand up a second, differently named
# project beside the running one.
cd "$REPO_ROOT"

# -f docker-compose.yaml explicitly, matching `make build` and the CI migrate step. A bare
# `docker compose` also auto-loads docker-compose.override.yaml, which swaps data_store to
# the dev image; migrations belong against the production service definition.
COMPOSE=(docker compose -f docker-compose.yaml)

# =================================================================================================
# THE COMPARISON IMAGE IS VERIFIED BEFORE `check` RUNS (decision tj-yb1bxj clauses 2-4; tj-ymsobh).
#
# WHY. migrations/env.py imports the models from /code — IN THE IMAGE — while only alembic.ini and
# migrations/ are bind-mounted. So `alembic check` ALWAYS compares the IMAGE'S models against the
# live schema and never this checkout's, and nothing in its output says which image it used. On
# 2026-10-04 that reported three drift items that were all phantoms of a stale prod image, one of
# them a bug the branch had already removed. A drift check whose comparison base is unverified is
# not a weak check, it is an uninterpretable one, and an uninterpretable green is worse than none.
#
# WHY IT LIVES IN THIS SCRIPT RATHER THAN IN THE `migrate-check` TARGET, which is the other place
# it could have gone. The guard has to name the image the one-off container will actually use, and
# this script is the only thing that knows it: the `-f docker-compose.yaml` pin above is what
# excludes the dev override, so the image is a consequence of THIS file's compose invocation.
# Deriving it again in the Makefile would be a second spelling of the thing this script exists to
# be the single spelling of (tj-4yvsb2), and the two would drift. Gating on the COMMAND rather than
# on the caller also means a future caller that passes `check` — a deploy script, a human — gets
# the guard without anyone remembering to add it.
#
# WHY `upgrade head` IS NOT GUARDED, and do not "fix" this by hoisting the call.
#
# What upgrade does NOT do: it never COMPARES the models. target_metadata (migrations/env.py:57)
# and include_object (migrations/env.py:76) are consumed only by autogenerate's comparison, so the
# phantom-drift failure this guard exists to stop — a stale image's models reported as live-schema
# drift — cannot happen on the upgrade path.
#
# What upgrade DOES do, and this is a real exposure rather than an absent one: it applies the
# bind-mounted revision scripts, and a revision script may import image code and derive the DDL it
# emits from it. Today exactly one statement does — FEED_TYPE.create() in
# migrations/versions/eec8f88a7443_per_dataset_identity_and_feed.py, whose CREATE TYPE label list
# is read off the IMAGE's common.enums.data_stock.Feed. A stale image there emits a different
# CREATE TYPE, with no ImportError, and alembic check cannot see it — enum labels are one of the
# two blind spots this file's header already names.
#
# IT IS STILL NOT GUARDED, and the reason is the COST OF REFUSING, not the absence of exposure.
# migrate-check is advisory and re-runnable, so refusing it costs an operator one build.
# `upgrade head` IS the remedy: it is the only way to move a database forward, and it is needed
# most at the moment a deploy is already wrong. A guard that can refuse the remedy leaves the
# override as the only way out, under exactly the pressure that trains people to set it. A check
# may fail closed; a remedy may not. tj-yb1bxj addendum 3 carries the measurement.
#
# ONLY THE LITERAL `1` OPENS THE ESCAPE HATCH, so MIGRATE_CHECK_ALLOW_STALE_IMAGE=0 cannot enable it
# by accident. It is never set by any recipe.
SERVICE=data_store
SERVICE_PATH=data
SERVICE_NAME=store
SOURCE_STAMP_LABEL='trader_joe.source.digest'

# The three facts every outcome prints, in one spelling so no outcome can omit one. A refusal that
# does not say which two things disagreed is the warning this guard replaced.
describe_comparison() {
    echo "  image reference: $1"
    echo "  image stamp:     $2"
    echo "  checkout digest: $3"
}

# Each caller pipes in THE FINDING — what is true of the image, both digests, and the fix. This
# supplies the VERDICT: a refusal, or, with the hatch set, a banner over the identical finding.
# Keeping the two apart is what makes the hatch honest; a banner that quietly said less than the
# refusal would be the warning this guard replaced, wearing a different hat.
#
# Everything that reaches here HAS an answer about the comparison base. The hatch downgrades an
# answer, never a failure to obtain one, which is why the two infrastructure failures below exit
# whatever the hatch says: they cannot print what they compared, and a banner that cannot name both
# sides is worth nothing to the person reading it.
refuse_or_proceed() {
    if [ "${MIGRATE_CHECK_ALLOW_STALE_IMAGE:-}" = 1 ]; then
        echo "===============================================================================" >&2
        echo "MIGRATE_CHECK_ALLOW_STALE_IMAGE=1 — PROCEEDING AGAINST AN UNVERIFIED IMAGE." >&2
        echo "" >&2
        cat >&2
        echo "" >&2
        echo "WHAT YOU ARE GIVING UP: this run answers 'do the DEPLOYED image's models match the" >&2
        echo "live schema', NOT 'do THIS CHECKOUT's models match it'. Any drift it reports may be" >&2
        echo "an artifact of the image, and a clean result is no evidence about the code you are" >&2
        echo "reading." >&2
        echo "===============================================================================" >&2
        return 0
    fi
    echo "REFUSED: not running \`alembic check\` against an unverified comparison image." >&2
    echo "" >&2
    cat >&2
    echo "" >&2
    echo "To check THE DEPLOYED IMAGE against a checkout you know differs — the one case where" >&2
    echo "this refusal is in your way — set MIGRATE_CHECK_ALLOW_STALE_IMAGE=1 and read the" >&2
    echo "banner it prints instead." >&2
    exit 1
}

verify_comparison_image() {
    local image_ref checkout_digest image_stamp compose_status

    # Asked of compose rather than spelled out here: the service's image is compose's answer to
    # give, and hard-coding `trader_joe_data_store:latest` would be a copy that goes stale silently.
    #
    # THE THREE OUTCOMES BELOW ARE DISTINGUISHED, NOT COLLAPSED. A failed call, a successful-but-
    # empty result and a successful multi-image result are three different facts about compose;
    # reporting all three as "returned nothing" is the same conflation R1 forbids one layer up — in
    # the one message R1 itself does not govern — refusing to infer a cause from a signal that
    # cannot carry it. All three still refuse, outside the escape hatch's reach, for the same
    # reason: the guard cannot name what it would compare. Nothing behavioural rides on the split.
    if image_ref="$("${COMPOSE[@]}" config --images "$SERVICE")"; then
        compose_status=0
    else
        compose_status=$?
    fi

    if [ "$compose_status" -ne 0 ]; then
        echo "Could not determine which image '$SERVICE' would run: docker compose config --images" >&2
        echo "$SERVICE failed with status $compose_status. The guard cannot name what it would" >&2
        echo "compare, so it refuses rather than guess. MIGRATE_CHECK_ALLOW_STALE_IMAGE does not" >&2
        echo "cover this: there is no answer here to downgrade." >&2
        exit 1
    fi

    if [ -z "$image_ref" ]; then
        echo "Could not determine which image '$SERVICE' would run: docker compose config --images" >&2
        echo "$SERVICE returned an empty result. The guard cannot name what it would compare, so" >&2
        echo "it refuses rather than guess. MIGRATE_CHECK_ALLOW_STALE_IMAGE does not cover this:" >&2
        echo "there is no answer here to downgrade." >&2
        exit 1
    fi

    if [ "$image_ref" != "${image_ref%%$'\n'*}" ]; then
        echo "Could not determine which image '$SERVICE' would run: docker compose config --images" >&2
        echo "$SERVICE named more than one image:" >&2
        echo "$image_ref" >&2
        echo "The guard cannot name what it would compare, so it refuses rather than guess." >&2
        echo "MIGRATE_CHECK_ALLOW_STALE_IMAGE does not cover this: there is no answer here to" >&2
        echo "downgrade." >&2
        exit 1
    fi

    # The ONE definition of the digest, shared with the build (tj-yb1bxj clause 5). It prints its
    # own diagnostics and prints no digest at all on failure, so an empty value is impossible here.
    if ! checkout_digest="$("$REPO_ROOT/tools/source_digest.sh" "$SERVICE_PATH" "$SERVICE_NAME")"; then
        echo "Could not compute this checkout's source digest (tools/source_digest.sh above)." >&2
        echo "Nothing to compare the image against, so the guard refuses." >&2
        echo "MIGRATE_CHECK_ALLOW_STALE_IMAGE does not cover this: no answer here to downgrade." >&2
        exit 1
    fi

    # EXISTENCE GETS ITS OWN QUESTION, and this is not belt-and-braces. The formatted read below
    # returns ZERO STATUS AND AN EMPTY STRING for a nil Labels map, a present-but-empty value and a
    # missing key alike, so it carries no information about whether the image exists — inferring
    # absence from it would relabel a pre-stamp image as "no such image". Both are refusals either
    # way, so only the DIAGNOSTIC would be wrong, which is precisely the fault this guard is for.
    #
    # STDERR IS SUPPRESSED HERE TOO, so in isolation a docker failure that is NOT absence — an
    # unreachable daemon, a permissions error — would also misread as "NO SUCH IMAGE", the same
    # fault one paragraph up. THAT IS UNREACHABLE TODAY, MEASURED rather than assumed: the postgres
    # check below (`"${COMPOSE[@]}" ps -q postgres`) runs before this function is ever called, and
    # with an unreachable daemon that assignment itself fails under `set -e`, exiting the script
    # there — the operator sees docker's own message instead of this one. The containment rides
    # entirely on CHECK ORDER: moving this guard's call above the postgres check reopens it.
    if ! docker image inspect "$image_ref" > /dev/null 2>&1; then
        refuse_or_proceed << EOF
NO SUCH IMAGE on this host, so there is nothing to compare against.
$(describe_comparison "$image_ref" '<no such image>' "$checkout_digest")

Left to proceed, compose would build this image implicitly as part of \`run\` — from this
checkout, but WITHOUT the stamp, because only the build recipes export it. The next run would
then land in "cannot verify" rather than here.

FIX: make prod-build
EOF
        return 0
    fi

    # THE STAMP IS VALID IFF IT MATCHES ^[0-9a-f]{64}$, and no finer distinction is drawn. The
    # `with` guard is not decoration: .Config.Labels is nil on an image carrying no labels at all,
    # and `index` on a nil map errors without it. A missing key, an empty value and a nil map all
    # render as the empty string through this read, all three fail the pattern, and all three mean
    # the same thing — cannot verify. Which one BuildKit actually produces has never been measured,
    # and the contract is written so that nobody needs to know.
    image_stamp="$(docker image inspect \
        --format "{{ with .Config.Labels }}{{ index . \"$SOURCE_STAMP_LABEL\" }}{{ end }}" \
        "$image_ref" 2> /dev/null || true)"

    if ! [[ $image_stamp =~ ^[0-9a-f]{64}$ ]]; then
        refuse_or_proceed << EOF
CANNOT VERIFY: this image carries no usable source stamp, so what \`alembic check\` would
compare cannot be established. Absent is not a match.
$(describe_comparison "$image_ref" "${image_stamp:-<absent>}" "$checkout_digest")

THIS IS THE ORDINARY CASE AND NOTHING IS WRONG WITH YOUR IMAGE. Only \`make prod-build\` and
\`make dev-build\` stamp one. An image built by CI, by \`make system-launch\` (up -d --wait, no
--build) or by a bare \`docker compose build\` has no stamp — it may well be perfectly current;
it simply cannot be SHOWN to be, and a check nobody can interpret is the thing being replaced.

FIX: make prod-build
EOF
        return 0
    fi

    if [ "$image_stamp" != "$checkout_digest" ]; then
        refuse_or_proceed << EOF
STALE IMAGE: this image was built from DIFFERENT source than this checkout. The two digests
below are both valid and they disagree.
$(describe_comparison "$image_ref" "$image_stamp" "$checkout_digest")

\`alembic check\` would compare models you are NOT reading against the live schema and report
every difference as drift. That is the 2026-10-04 failure verbatim: three drift items, all
phantoms of a stale image, one of them a bug the branch had already removed.

FIX: make prod-build
EOF
        return 0
    fi

    # THE MATCH CASE STATES ITS OWN LIMIT, because this is the one direction of the guard that fails
    # OPEN (tj-yb1bxj addendum 1), and the moment someone needs to know it is the moment they are
    # already puzzled about why a check passed. The digest covers the files the Dockerfile COPYs —
    # not the Dockerfile's own instructions — so an edit that changes the image without changing a
    # COPY source (a different `uv sync` group set, a new ENV) leaves a stale image reading FRESH.
    # Dependency VERSION changes are covered, because pyproject.toml and uv.lock are COPY sources;
    # only a group-SELECTION change escapes. Claim nothing wider than the sentence printed below.
    echo "Comparison image verified: its stamp matches this checkout's source digest."
    describe_comparison "$image_ref" "$image_stamp" "$checkout_digest"
    echo "  This says only: THE SOURCE THIS IMAGE WAS BUILT FROM MATCHES THIS CHECKOUT. The digest"
    echo "  covers the files the Dockerfile COPYs, not the Dockerfile's own instructions, so a"
    echo "  changed \`uv sync\` group set still reads as fresh (tj-yb1bxj addendum 1)."
}
# =================================================================================================

echo "Running 'alembic ${ALEMBIC_ARGS[*]}' against the revisions in $MIGRATION_DIR"

if [ ! -d "$MIGRATION_DIR" ]; then
    echo "Migration directory does not exist: $MIGRATION_DIR" >&2
    exit 1
fi

# An empty versions/ makes `alembic upgrade head` a successful no-op, which is
# indistinguishable from a migration that worked. Given the bind mount above, that is
# exactly the symptom of running from the wrong checkout, so fail loudly instead.
if ! compgen -G "$MIGRATION_DIR/*.py" > /dev/null; then
    echo "No revision files in $MIGRATION_DIR — wrong checkout?" >&2
    exit 1
fi

# --no-deps, so the dependency check is ours to make. Without it compose starts BOTH
# declared dependencies for a container that needs only the database, and kafka's 90s
# start_period is charged to every migration. With it, a stopped database surfaces as a
# name-resolution failure from inside the container; this says the same thing in one line.
# Bringing postgres up here instead was rejected: `up` recreates a container whose config
# hash has changed, and applying migrations must never restart the database it migrates.
postgres_container="$("${COMPOSE[@]}" ps -q postgres)"
if [ -z "$postgres_container" ]; then
    echo "postgres is not running — start it first (make launch-deps), then re-run." >&2
    exit 1
fi

# Last, because the two checks above are cheaper and because an operator with a stopped database
# wants to hear about the database first. Still BEFORE alembic runs, which is the requirement: a
# refusal after the fact would have already written alembic_version and already printed drift.
#
# "ANY argument is literally `check`" rather than "the subcommand is `check`", and the crude test
# is the deliberate one. Picking the subcommand out means skipping alembic's global options, which
# means knowing which of them take a VALUE — get that wrong for `-c alembic.ini check` and the
# value reads as the subcommand, the guard silently does not run, and the guard is hollow. The
# crude test errs the other way: at worst it guards an invocation that merely mentions the word,
# which costs one informative refusal. Only one of those two mistakes is recoverable.
for argument in "${ALEMBIC_ARGS[@]}"; do
    if [ "$argument" = check ]; then
        verify_comparison_image
        break
    fi
done

# No pip install. alembic and psycopg2-binary are both in the data-store dependency group
# the image syncs (pyproject.toml [dependency-groups], Dockerfile:29), so the venv already
# has them — and data_store declares read_only: true, so installing at migration time
# could not have worked even if they were missing. The interpreter is addressed by full
# path because the image sets CMD and not ENTRYPOINT: an argv here replaces entrypoint.sh
# rather than appending to it. `run` publishes no ports, so this cannot collide with the
# data_store container already serving traffic.
"${COMPOSE[@]}" run --rm --no-deps data_store /code/.venv/bin/alembic "${ALEMBIC_ARGS[@]}"
