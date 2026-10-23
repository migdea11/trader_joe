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
# THAT SPLIT EXISTS ON THE PRODUCTION AND TEST DEFINITIONS. TEST runs the :test image, built from
# the production target, with only alembic.ini and migrations/ bind-mounted, so it has the split
# too. On the dev definition the override bind-mounts app/, common/, routers/, schemas/ and
# gen/proto/python over /code as well, so the models, the enums the revisions import and the
# revisions themselves all come from this checkout: there is no image/revision split in dev.
# Which definition runs is chosen from the running stack; see WHICH STACK below.
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

# REPO_ROOT and SERVER_ROOT, not one REPO_ROOT counted by distance (tj-2bsw0k; epic tj-iontkq
# risk R-1). REPO_ROOT is the TRUE repository root -- the directory holding pytest.ini, Makefile,
# the compose files and tools/. SERVER_ROOT is the directory holding common/, routers/, schemas/
# and data/ -- the one MIGRATION_DIR below actually wants. The two are the same directory today,
# which is exactly why a single two-directories-up count read as correct: that count always lands
# on the directory holding data/ (SERVER_ROOT's definition) no matter where this script itself
# sits, so it was SERVER_ROOT computed under REPO_ROOT's name. Once the service trees move under
# server/ (this epic), this script moves to server/data/store/run_migrations.sh, the same count
# lands on server/, and every consumer of the old name that actually wanted the true root --
# the `cd` below, so compose can find docker-compose.yaml, and tools/source_digest.sh further
# down -- would have silently resolved into server/, where neither exists. Each root is now found by
# searching upward for what only it carries, not by counting, so a further move cannot misalign
# the count again; see common/tests/roots.py for the same two searches in Python.
#
# NEITHER SEARCH FALLS BACK: a marker search that quietly returned the filesystem root instead of
# refusing would be the same vacuous-pass failure in a new place, since every path built from it
# would exist nowhere -- the empty-versions guard just below exists to catch precisely that.
find_repo_root() {
    local dir="$1"
    while true; do
        if [ -f "$dir/pytest.ini" ]; then
            printf '%s\n' "$dir"
            return 0
        fi
        if [ "$dir" = / ]; then
            break
        fi
        dir="$(dirname "$dir")"
    done
    echo "no ancestor of $1 carries pytest.ini; cannot locate the repository root" >&2
    exit 1
}

find_server_root() {
    local dir="$1"
    while true; do
        if [ -d "$dir/common" ] && [ -d "$dir/routers" ] && [ -d "$dir/schemas" ]; then
            printf '%s\n' "$dir"
            return 0
        fi
        if [ "$dir" = / ]; then
            break
        fi
        dir="$(dirname "$dir")"
    done
    echo "no ancestor of $1 carries common/, routers/ and schemas/ as directories; cannot locate the server root" >&2
    exit 1
}

SCRIPT_DIR="$(realpath "$(dirname "$0")")"
REPO_ROOT="$(find_repo_root "$SCRIPT_DIR")"
SERVER_ROOT="$(find_server_root "$SCRIPT_DIR")"
MIGRATION_DIR="$SERVER_ROOT/data/store/migrations/versions"

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
# the dev image. THE DEFAULT IS THE PRODUCTION DEFINITION: the dev definition (base + override)
# is selected only when the running stack is unambiguously dev, and the TEST definition (base +
# fake) only when it is unambiguously test, both by select_stack below; everything ambiguous
# in the default project stays on this line, and in trader_joe_dev it is refused (tj-slxynv;
# tj-x3ig38 addendum 2026-10-12; tj-grna9p.173). Keep this
# top-level line literal: test_network_model.py reads the first ^COMPOSE=(...) line.
COMPOSE=(docker compose -f docker-compose.yaml)

# WHICH make TARGET REBUILDS THE IMAGE THE SELECTED DEFINITION RUNS. Only the FIX: lines of the
# `check` guard read it; select_stack sets it to dev-build when it selects the dev definition and
# to test-build when it selects the TEST definition.
BUILD_TARGET=prod-build

# THE COMPOSE PROJECT THE RUNNING POSTGRES LIVES IN (ADR tj-4rr0la addendum 29; owner ruling tj-oxxl8a
# option A). Dev and test run as the compose project trader_joe_dev; production keeps the default
# project name and its launch. The script only READS labels and execs alembic in the project it
# picked, so it never touches the other project's containers. PROJECT_ARGS is empty for the default
# (production) project and (-p trader_joe_dev) for the dev one; PROJECT_NAME is what the stderr line
# names. Both are set where the probe runs, just before select_stack.
DEV_PROJECT=trader_joe_dev
PROJECT_ARGS=()
PROJECT_NAME=default

# =================================================================================================
# WHICH STACK (decision tj-slxynv option (b); tj-x3ig38 addendum 2026-10-12; tj-yb1bxj addendum 4).
#
# `make dev-build` then dev launch then `make migrate` used to fail from migrations/env.py: this
# script ran the production image (:latest, which only `make prod-build` builds) while the stack up
# was dev (:dev). So the definition follows the stack that is RUNNING, read from the label compose
# stamps on every container it creates, com.docker.compose.project.config_files: the config files
# of the `up` that CREATED the container, as absolute paths joined by commas.
#
#   * The postgres container is the one asked: postgres is already required to be up, it is the
#     one container dev-deps alone starts, and docker-compose.yaml gives it a fixed container_name
#     so a project never has two.
#   * DEV iff some entry's FILE NAME is exactly docker-compose.override.yaml. NEVER a full-path
#     comparison: the label holds the paths as seen by whoever ran `up` (a host path, or a
#     devcontainer's /workspace), so comparing against $REPO_ROOT would misread a correct dev stack.
#   * TEST iff some entry's file name is exactly docker-compose.fake.yaml and none is the override: the
#     stack `make system-launch` runs (base + fake, on ./volume_data/test; ADR tj-4rr0la addendum 27).
#     The fake file sets the :test image tags, so the one-off runs :test and a test stack never
#     builds or runs :latest. DEV and TEST are told apart by basename alone, like the dev rule.
#     BOTH files in one label is no launched stack and is UNKNOWN, so production.
#   * A dev postgres is cross-checked against a running data_store, whose label must say dev too; a
#     test postgres likewise against a test data_store.
#   * IN THE DEFAULT PROJECT, AMBIGUOUS MEANS PRODUCTION AND STILL RUNS ALEMBIC. No label, an empty or
#     malformed label, a failed inspect or ps, more than one postgres or data_store container, a dev
#     postgres with a non-dev data_store (likewise a test postgres with a non-test one). Detection
#     never exits by itself there: `upgrade head` is the remedy and a selection problem must not
#     refuse it (tj-yb1bxj addendum 3 item 6, addendum 4 item 4).
#   * IN trader_joe_dev THE SAME OUTCOMES REFUSE (exit 1, naming the cause): only a positively DEV or
#     TEST stack is accepted there, and a production-labelled container is refused. Nothing in that
#     project is production by construction, so refusing costs a dev restart (tj-grna9p.173,
#     architect option B).
#   * The dev list is -f docker-compose.yaml -f docker-compose.override.yaml and NOTHING else. The
#     web file carries an INSTANCE_WRITE_SECRET:? guard that compose interpolates even for a run of
#     another service, and a dev .env leaves it empty.
#
# ONE line goes to STDERR saying which definition was chosen and why. STDOUT is untouched: CI-style
# callers parse `alembic current` from it.
#
# BEGIN classify_config_files: the whole label -> verdict decision, with no globals and no docker,
# so a test can extract this block and drive it with a label string. Prints one line:
#   DEV                  some entry's file name is exactly docker-compose.override.yaml, and none is
#                        docker-compose.fake.yaml
#   TEST                 some entry's file name is exactly docker-compose.fake.yaml, and none is the
#                        override (what `make system-launch` runs)
#   PROD                 well formed, and neither file name is present
#   UNKNOWN: <reason>    empty, or malformed (an empty or relative entry, or a newline), or BOTH the
#                        override and the fake file (no target launches that pair)
# A path that itself contains a comma lands in UNKNOWN as malformed: fail closed.
classify_config_files() {
    local label="$1" entry has_override=0 has_fake=0
    local -a entries

    if [ -z "$label" ]; then
        echo "UNKNOWN: the container carries no compose config_files label"
        return 0
    fi
    case "$label" in
        ,* | *, | *,,* | *$'\n'*)
            echo "UNKNOWN: malformed label: $label"
            return 0
            ;;
    esac

    IFS=',' read -r -a entries <<< "$label"
    for entry in "${entries[@]}"; do
        if [ -z "$entry" ] || [ "${entry#/}" = "$entry" ]; then
            echo "UNKNOWN: malformed label: $label"
            return 0
        fi
    done
    for entry in "${entries[@]}"; do
        case "${entry##*/}" in
            docker-compose.override.yaml) has_override=1 ;;
            docker-compose.fake.yaml) has_fake=1 ;;
        esac
    done
    if [ "$has_override" -eq 1 ] && [ "$has_fake" -eq 1 ]; then
        echo "UNKNOWN: both docker-compose.override.yaml and docker-compose.fake.yaml are in the label; no target launches that pair"
    elif [ "$has_override" -eq 1 ]; then
        echo DEV
    elif [ "$has_fake" -eq 1 ]; then
        echo TEST
    else
        echo PROD
    fi
}
# END classify_config_files

# Classify one container id: reads its label and hands it to classify_config_files. A failed
# inspect is UNKNOWN, never an exit. The `with` guard matters: .Config.Labels is nil on a
# container with no labels at all, and `index` on a nil map errors without it.
classify_container() {
    local label
    if ! label="$(docker inspect \
        --format '{{ with .Config.Labels }}{{ index . "com.docker.compose.project.config_files" }}{{ end }}' \
        "$1" 2> /dev/null)"; then
        echo "UNKNOWN: could not read the container's compose label"
        return 0
    fi
    classify_config_files "$label"
}

# Number of lines in a possibly empty `ps -q` result.
count_lines() {
    if [ -z "$1" ]; then
        echo 0
    else
        local newlines="${1//[!$'\n']/}"
        echo $((${#newlines} + 1))
    fi
}

# Chooses COMPOSE and BUILD_TARGET and prints the one stderr line. $1 is postgres's container id(s),
# already captured by the caller. Run AFTER the postgres-is-running check and BEFORE the `check`
# guard, which asks the selected definition which image the one-off container will run.
select_stack() {
    local postgres_ids="$1" reason verdict data_store_ids count chosen="" name data_store_verdict

    reason="the running postgres was started without docker-compose.override.yaml"
    count="$(count_lines "$postgres_ids")"
    if [ "$count" -ne 1 ]; then
        reason="$count postgres containers in this project; cannot tell which stack"
    else
        verdict="$(classify_container "$postgres_ids")"
        case "$verdict" in
            DEV | TEST)
                # A dev postgres wants a dev data_store, a test postgres a test one; none running
                # is fine. Anything else is a mixed stack, and mixed means production.
                name=dev
                if [ "$verdict" = TEST ]; then
                    name="test"
                fi
                if ! data_store_ids="$("${COMPOSE[@]}" ps -q data_store 2> /dev/null)"; then
                    reason="could not list the data_store containers to cross-check the stack"
                else
                    count="$(count_lines "$data_store_ids")"
                    if [ "$count" -eq 0 ]; then
                        reason=""
                        chosen="$verdict"
                    elif [ "$count" -ne 1 ]; then
                        reason="$count data_store containers; cannot tell which stack"
                    else
                        data_store_verdict="$(classify_container "$data_store_ids")"
                        if [ "$data_store_verdict" = "$verdict" ]; then
                            reason=""
                            chosen="$verdict"
                        else
                            reason="postgres was started by the $name definition but data_store by ${data_store_verdict}: mixed stack"
                        fi
                    fi
                fi
                ;;
            PROD)
                # Only DEV and TEST definitions belong in the dev project (ADR tj-4rr0la addendum 29
                # (3)); a container started from the production files there is refused, as the MCP
                # guard refuses it. The default project's production postgres is the normal case.
                if [ ${#PROJECT_ARGS[@]} -gt 0 ]; then
                    echo "project $PROJECT_NAME holds a container started from the production files: stop it (it belongs in the default project); make migrate will not guess" >&2
                    exit 1
                fi
                ;;
            *) reason="${verdict#UNKNOWN: }" ;;
        esac
    fi

    case "$chosen" in
        DEV)
            COMPOSE=(docker compose -f docker-compose.yaml -f docker-compose.override.yaml)
            BUILD_TARGET=dev-build
            echo "run_migrations.sh: using the DEV definition (docker-compose.yaml + docker-compose.override.yaml): the running postgres was started with docker-compose.override.yaml (project: $PROJECT_NAME)" >&2
            ;;
        TEST)
            COMPOSE=(docker compose -f docker-compose.yaml -f docker-compose.fake.yaml)
            BUILD_TARGET=test-build
            echo "run_migrations.sh: using the TEST definition (docker-compose.yaml + docker-compose.fake.yaml): the running postgres was started with docker-compose.fake.yaml (project: $PROJECT_NAME)" >&2
            ;;
        *)
            # Nothing in the dev project is production by construction, so an unclear outcome there
            # is refused rather than run against the production definition (tj-grna9p.173, architect
            # option B). Costs a dev restart at most. Only the default project falls through.
            if [ ${#PROJECT_ARGS[@]} -gt 0 ]; then
                echo "project $PROJECT_NAME: cannot tell that this is a dev or test stack ($reason): refusing; make migrate will not guess. Restart the stack with make dev-up or make system-launch, then re-run." >&2
                exit 1
            fi
            # Literal like the other arms: the single project splice below then treats all three alike.
            COMPOSE=(docker compose -f docker-compose.yaml)
            echo "run_migrations.sh: using the PRODUCTION definition (docker-compose.yaml): $reason (project: $PROJECT_NAME)" >&2
            ;;
    esac

    # The definition lines above stay literal; the project the postgres was found in is spliced in
    # after `docker compose`, so every later call (cross-check, guard, one-off) addresses that
    # project alone. Empty for the default (production) project.
    if [ ${#PROJECT_ARGS[@]} -gt 0 ]; then
        local -a definition=("${COMPOSE[@]:2}")
        unset COMPOSE
        COMPOSE+=(docker compose "${PROJECT_ARGS[@]}" "${definition[@]}")
    fi
}
# =================================================================================================

# =================================================================================================
# THE COMPARISON IMAGE IS VERIFIED BEFORE `check` RUNS (decision tj-yb1bxj clauses 2-4; tj-ymsobh).
#
# WHY. migrations/env.py imports the models from /code. On the PRODUCTION definition /code is the
# IMAGE's, and only alembic.ini and migrations/ are bind-mounted, so there `alembic check` compares
# the IMAGE'S models against the live schema and never this checkout's, and nothing in its output
# says which image it used. On the DEV definition the override also bind-mounts app/, common/,
# routers/, schemas/ and gen/proto/python over /code, so the models come from this checkout and
# that image/revision split does not arise. The guard below still runs there, and deliberately:
# the stamp compares what is baked into the :dev image (the dependencies, which no bind mount
# replaces), and a :dev image whose stamp does not match the checkout is refused (tj-yb1bxj
# addendum 4 item 3; do not relax it). On the TEST definition the image is :test, which bakes the
# code like production's, so the split is the production one; the guard compares the :test image
# (compose's answer, not a spelled tag) and its FIX line names make test-build. On 2026-10-04 the
# production case reported three drift items that were all phantoms of a stale prod image, one of
# them a bug the branch had already removed. A drift check whose comparison base is unverified is
# not a weak check, it is an uninterpretable one, and an uninterpretable green is worse than none.
#
# WHY IT LIVES IN THIS SCRIPT RATHER THAN IN THE `migrate-check` TARGET, which is the other place
# it could have gone. The guard has to name the image the one-off container will actually use, and
# this script is the only thing that knows it: select_stack above decides which definition is
# loaded (base, base + override or base + fake), so the image (:dev, :test or :latest) is a
# consequence of THIS file's compose invocation, and the guard follows that selection rather than assuming the production image.
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
# is read off the IMAGE's common.enums.data_stock.Feed on the PRODUCTION definition (on the dev
# definition common/ is bind-mounted, so it is read off this checkout). A stale image there emits
# a different CREATE TYPE, with no ImportError, and alembic check cannot see it — enum labels are
# one of the two blind spots this file's header already names.
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
    # check below (the two project probes `docker compose ... ps -q postgres`) runs before this function is ever called, and
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

FIX: make $BUILD_TARGET
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

THIS IS THE ORDINARY CASE AND NOTHING IS WRONG WITH YOUR IMAGE. Only \`make prod-build\`,
\`make dev-build\` and \`make test-build\` stamp one. An image built by CI, pulled, or built by a
bare \`docker compose build\` has no stamp — it may well be perfectly current; it simply cannot
be SHOWN to be, and a check nobody can interpret is the thing being replaced. \`make
system-launch\` (up -d --wait, no --build) runs an image that is already there as it is (stamped
only if one of the three targets above built it), and builds a missing one implicitly, unstamped.

FIX: make $BUILD_TARGET
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

FIX: make $BUILD_TARGET
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
#
# THE PROJECT IS FOUND FIRST. Postgres is looked for in trader_joe_dev, then in the default project.
# Exactly one holds a running postgres: that project is used for every compose call from here on.
# Both do: refuse, since there is no telling which one is meant and a guess could migrate the
# production database. Neither: the not-running message, as before.
# If COMPOSE_PROJECT_NAME is set (to trader_joe_dev) it renames the default probe's project, so both
# probes resolve to the same project and return the SAME container id(s): that is one project, not
# two, and it is used as the dev project (the explicit -p probe is the one that names it). Two
# DIFFERENT ids are still ambiguous and refuse.
dev_postgres="$(docker compose -p "$DEV_PROJECT" -f docker-compose.yaml ps -q postgres)"
default_postgres="$(docker compose -f docker-compose.yaml ps -q postgres)"
if [ "$dev_postgres" = "$default_postgres" ]; then
    default_postgres=""
fi
if [ -n "$dev_postgres" ] && [ -n "$default_postgres" ]; then
    echo "postgres runs in both $DEV_PROJECT and the default project: stop one, then re-run." >&2
    exit 1
fi
postgres_container="$default_postgres"
if [ -n "$dev_postgres" ]; then
    postgres_container="$dev_postgres"
    PROJECT_ARGS=(-p "$DEV_PROJECT")
    PROJECT_NAME="$DEV_PROJECT"
    COMPOSE=(docker compose -p "$DEV_PROJECT" -f docker-compose.yaml)
fi
if [ -z "$postgres_container" ]; then
    echo "postgres is not running — start it first (make launch-deps), then re-run." >&2
    exit 1
fi

# AFTER the postgres check and BEFORE the guard below: the guard asks the selected definition which
# image will run, and verify_comparison_image's containment comment depends on the postgres check
# having run first. In the default project an unclear outcome means the production definition and
# never exits. In trader_joe_dev select_stack exits 1 on anything but a positively DEV or TEST stack
# (a production-labelled container, an unreadable label, a mixed stack and the rest).
select_stack "$postgres_container"

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
