"""run_migrations.sh runs alembic through the definition of the stack that is RUNNING (tj-s6r3rt.1).

THE DESIGN is tj-s6r3rt.1's DETECTION DESIGN, from decision tj-slxynv option (b), the tj-x3ig38 addendum of
2026-10-12 and tj-yb1bxj addendum 4:

* the postgres container's compose label com.docker.compose.project.config_files is read; the stack is DEV iff
  some entry's FILE NAME is exactly docker-compose.override.yaml -- never a full-path comparison, since the label
  holds the paths as whoever ran `up` saw them (a host path, or a devcontainer's /workspace);
* a dev postgres is cross-checked against a running data_store, whose label must say dev too;
* DEV runs `-f docker-compose.yaml -f docker-compose.override.yaml` and nothing else;
* TEST (tj-iee6sn.25; ADR tj-4rr0la addendum 27 (1)-(2)): some entry's file name is exactly
  docker-compose.fake.yaml and none is the override -- the stack `make system-launch` runs -- and it runs
  `-f docker-compose.yaml -f docker-compose.fake.yaml`; a test postgres is cross-checked against a test data_store
  the same way. The override AND the fake together is no launched stack and is production;
* EVERY other case -- prod, both files, no label, empty, malformed, relative, a failed inspect or ps, more than one
  candidate container, a mixed stack -- runs `-f docker-compose.yaml` alone, byte-identical to the command before
  the change, and STILL RUNS ALEMBIC in the default project: a selection problem never refuses `upgrade head`
  there (inside trader_joe_dev every such case is refused, below);
* exactly ONE line on stderr names the definition and why; stdout gains nothing (callers parse `alembic current`);
* the exit status is alembic's, not the detection's;
* `check`'s comparison-image guard asks the SELECTED definition which image runs, and its FIX: line names the
  build for that definition: make dev-build on DEV, make test-build on TEST, make prod-build on PROD.

THE PROJECT (tj-grna9p.162; ADR tj-4rr0la addendum 29 (3); owner ruling tj-oxxl8a option A). Postgres is looked for
in the compose project trader_joe_dev first, then in the default project (prod's, unchanged). The one found is used
for every later compose call: -p trader_joe_dev for the dev project, no -p at all for the default one. Both found
refuses with exit 1 and runs nothing; neither is the old not-running message. The selection line ends by naming
the project. The stub answers each project's ps from its own files, and every table case runs in both projects.

A PRODUCTION-LABELLED POSTGRES INSIDE trader_joe_dev IS REFUSED (tj-grna9p.173; the architect's ruling (1) on
tj-grna9p.162; addendum 29 (3): only DEV and TEST belong in that project). The postgres's label classifies PROD (well
formed, neither the override nor the fake): exit 1, one exact line, and nothing after the two probes and the one
label read -- no cross-check, no image lookup, no alembic. In the default project the same label is the normal
production case and runs alembic with no -p, as before. DEV and TEST labels in trader_joe_dev still run there.

EVERY OTHER UNCLEAR OUTCOME IS REFUSED IN trader_joe_dev TOO (architect option B on the escalation; ADR tj-yb1bxj's
never-block rule now scoped to the default project). No label, malformed, both files, a failed inspect or ps, two
postgres or data_store containers, a mixed stack: exit 1 with one message naming the cause (the same words the
default project's selection line gives) and the fix, after only the reads that cause needed. The default project is
unchanged: those outcomes still run alembic on the production definition there.

IDENTICAL IDS FROM BOTH PROBES ARE ONE PROJECT (tj-grna9p.173; ruling (2)). COMPOSE_PROJECT_NAME=trader_joe_dev in the
caller's env renames the default probe's project, so both probes find the same postgres: that is the dev project,
never 'both running'. Different ids still refuse; two empty answers are still 'not running'. The stub honours
COMPOSE_PROJECT_NAME=trader_joe_dev the way compose does (an explicit -p wins), and the harness strips any inherited
value so no other test depends on the caller's env.

THE STUB'S IMAGE ANSWERS ARE ANCHORED TO THE FILES. Its `config --images` answers :dev for a list with the override,
:test for one with the fake (addendum 27 (2), tj-iee6sn.19) and :latest otherwise; one test below reads the three
compose files and the Makefile and fails if that map, or the test-build target the TEST FIX: line names, drifts.

THE LABEL FORMAT IS ASSUMED, NOT OBSERVED. Absolute paths joined by commas is compose v2's documented behaviour as
the design states it; the owner's confirmation on a real host is tj-s6r3rt.6, open when these were written. If
the host shows a different format, the classifier table below is what changes.

TIER. The real script, run by bash as a subprocess, with a stub `docker` first on PATH (the house pattern:
common/tests/test_roots.py, test_service_pythonpath.py). The stub answers by argv from files in a per-test
directory and journals every call, so each assertion reads the argv compose was actually given. No Docker runs.
The classifier itself is also driven directly, from the block run_migrations.sh marks for extraction.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from common.tests.roots import REPO_ROOT
from data.store.tests.compose_double import (
    ALEMBIC,
    ALEMBIC_RUN,
    ALEMBIC_STDOUT,
    BASH,
    DUMP_LINE,
    IMAGE_MIGRATION_DIR,
    LABEL_READ,
    PG_DUMP,
    PG_RESTORE_LIST,
    PROJECTS,
    REVISIONS_PROBE,
    RUN_MIGRATIONS,
    Harness,
    Outcome,
    Stack,
)


pytestmark = pytest.mark.data_store

SCRIPT = RUN_MIGRATIONS
# The revisions ship in the data_store image (tj-7zjov1.45): the Running line names the image's directory, never a
# path in the host checkout.
RUNNING_REVISIONS = f'against the revisions in the data_store image ({IMAGE_MIGRATION_DIR})'

BASE = 'docker-compose.yaml'
OVERRIDE = 'docker-compose.override.yaml'
FAKE = 'docker-compose.fake.yaml'
PROD_FILES = (BASE,)
DEV_FILES = (BASE, OVERRIDE)
TEST_FILES = (BASE, FAKE)

DEV_LINE = (
    'run_migrations.sh: using the DEV definition (docker-compose.yaml + docker-compose.override.yaml): '
    'the running postgres was started with docker-compose.override.yaml'
)
TEST_LINE = (
    'run_migrations.sh: using the TEST definition (docker-compose.yaml + docker-compose.fake.yaml): '
    'the running postgres was started with docker-compose.fake.yaml'
)
PROD_PREFIX = 'run_migrations.sh: using the PRODUCTION definition (docker-compose.yaml): '

# THE PROJECT (ADR tj-4rr0la addendum 29 (3); owner ruling tj-oxxl8a option A). Dev and test run as the compose
# project trader_joe_dev; prod keeps the default project and its launch. The script probes trader_joe_dev FIRST,
# then the default project; the project it finds postgres in is the one every later compose call addresses:
# -p trader_joe_dev for the dev project, NO -p at all for the default one, so prod's calls are the old ones.
DEV_PROJECT = 'trader_joe_dev'
PROJECT_ARGS = {'default': (), 'dev': ('-p', DEV_PROJECT)}
PROJECT_NAME = {'default': 'default', 'dev': DEV_PROJECT}
PROBE_DEV = ('compose', '-p', DEV_PROJECT, '-f', BASE, 'ps', '-q', 'postgres')
PROBE_DEFAULT = ('compose', '-f', BASE, 'ps', '-q', 'postgres')
BOTH_RUNNING = f'postgres runs in both {DEV_PROJECT} and the default project: stop one, then re-run.\n'
NOT_RUNNING = 'postgres is not running — start it first (make launch-deps), then re-run.\n'


def _named(line: str, project: str) -> str:
    """The selection line as the script prints it: it ends by naming the project postgres was found in."""
    return f'{line} (project: {PROJECT_NAME[project]})'


# Two roots, because the design's whole reason for a basename match is that they differ.
HOST = '/home/owner/src/trader_joe'
DEVCONTAINER = '/workspace'


def _label(root: str, *names: str) -> str:
    return ','.join(f'{root}/{name}' for name in names)


# DEV_COMPOSE (Makefile) loads the base, the override and the two web files; a bare `docker compose up` loads
# the base and the override; PROD_COMPOSE the base and the web file; SYSTEM_COMPOSE the base and the fake.
DEV_LABEL = _label(HOST, BASE, OVERRIDE, 'docker-compose.web.yaml', 'docker-compose.web.dev.yaml')
DEV_LABEL_DEVCONTAINER = _label(DEVCONTAINER, BASE, OVERRIDE, 'docker-compose.web.yaml', 'docker-compose.web.dev.yaml')
BARE_UP_LABEL = _label(HOST, BASE, OVERRIDE)
PROD_LABEL = _label(HOST, BASE, 'docker-compose.web.yaml')
SYSTEM_LABEL = _label(HOST, BASE, FAKE)
SYSTEM_LABEL_DEVCONTAINER = _label(DEVCONTAINER, BASE, FAKE)
# No target launches the override and the fake together (addendum 27 (1)); a label naming both is production.
BOTH_LABEL = _label(HOST, BASE, OVERRIDE, FAKE)

REASON_PROD = 'the running postgres was started without docker-compose.override.yaml'
REASON_NO_LABEL = 'the container carries no compose config_files label'
REASON_INSPECT_FAILED = "could not read the container's compose label"
REASON_BOTH = (
    'both docker-compose.override.yaml and docker-compose.fake.yaml are in the label; no target launches that pair'
)


def _malformed(label: str) -> str:
    return f'malformed label: {label}'


# ------------------------------------------------------------------------------------------------- the stub
# The stub, Stack, Call, Outcome and Harness live in compose_double.py, shared with the restore and rehearsal tests.


def _head(project: str, files: tuple[str, ...]) -> tuple[str, ...]:
    """`compose`, the found project's -p, then the definition's -f list: how every later call begins."""
    return ('compose', *PROJECT_ARGS[project], *[w for f in files for w in ('-f', f)])


def _split_dump_line(outcome: Outcome, invocation: tuple[str, ...]) -> list[str]:
    """Stderr's lines less the dump line, which a schema-moving command prints last and a read-only one never.

    The migration-safety gate (P03) adds exactly that one stderr line; everything these older tests pin about stderr
    is the rest, unchanged.
    """
    lines = outcome.result.stderr.splitlines()
    dumps = [line for line in lines if DUMP_LINE.fullmatch(line)]
    if _is_mutating(invocation):
        assert len(dumps) == 1 and DUMP_LINE.fullmatch(lines[-1]), outcome.explain()
        return lines[:-1]
    assert not dumps, outcome.explain()
    return lines


def _is_mutating(invocation: tuple[str, ...]) -> bool:
    return bool({'upgrade', 'downgrade', 'stamp'} & set(_alembic_args(invocation)))


def _gate(files: tuple[str, ...], invocation: tuple[str, ...]) -> list[tuple[str, ...]]:
    """The gate calls' words after -p/-f, in order.

    The look into the image for revision files, on every command (tj-7zjov1.45); then, for a schema-moving one, the
    label read (production definition only), the dump and its read-back.
    """
    if not _is_mutating(invocation):
        return [REVISIONS_PROBE]
    return [REVISIONS_PROBE, *([LABEL_READ] if files == PROD_FILES else []), PG_DUMP, PG_RESTORE_LIST]


@pytest.fixture(scope='module')
def checkout_digest() -> str:
    """This checkout's source digest, so the stub can play an image whose stamp MATCHES and `check` reaches run."""
    result = subprocess.run(
        [str(REPO_ROOT / 'tools' / 'source_digest.sh'), 'data', 'store'],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f'tools/source_digest.sh failed (gen/ is built by make test):\n{result.stderr}'
    return result.stdout.strip()


@pytest.fixture
def harness(tmp_path: Path, checkout_digest: str) -> Harness:
    assert BASH, 'bash is not on PATH, so the script cannot be exercised'
    return Harness(tmp_path, checkout_digest)


# ------------------------------------------------------------------------------------------------- the table

# (id, stack, the -f list expected, the stderr selection line expected)
CASES = [
    # --- production: base alone, today's command
    ('prod', Stack(('pg',), {'pg': PROD_LABEL}), PROD_FILES, PROD_PREFIX + REASON_PROD),
    ('prod, base file only', Stack(('pg',), {'pg': _label(HOST, BASE)}), PROD_FILES, PROD_PREFIX + REASON_PROD),
    (
        'neither the override nor the fake, nor the base',
        Stack(('pg',), {'pg': _label(HOST, 'docker-compose.web.yaml')}),
        PROD_FILES,
        PROD_PREFIX + REASON_PROD,
    ),
    # --- both the override and the fake: no target launches the pair, so production, with a truthful reason
    ('override and fake together', Stack(('pg',), {'pg': BOTH_LABEL}), PROD_FILES, PROD_PREFIX + REASON_BOTH),
    (
        'override and fake together, fake first, with the web files',
        Stack(('pg',), {'pg': _label(DEVCONTAINER, FAKE, BASE, 'docker-compose.web.yaml', OVERRIDE)}),
        PROD_FILES,
        PROD_PREFIX + REASON_BOTH,
    ),
    # --- unambiguous test: what `make system-launch` runs (base + fake)
    ('system-launch', Stack(('pg',), {'pg': SYSTEM_LABEL}), TEST_FILES, TEST_LINE),
    ('test, devcontainer paths', Stack(('pg',), {'pg': SYSTEM_LABEL_DEVCONTAINER}), TEST_FILES, TEST_LINE),
    ('test, fake file only', Stack(('pg',), {'pg': _label(HOST, FAKE)}), TEST_FILES, TEST_LINE),
    (
        'test postgres, test data_store (each under its own root)',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': SYSTEM_LABEL_DEVCONTAINER}, ('ds',)),
        TEST_FILES,
        TEST_LINE,
    ),
    # --- near misses of the fake: well formed, and no entry's file name is exactly the fake
    *[
        (name, Stack(('pg',), {'pg': label}), PROD_FILES, PROD_PREFIX + REASON_PROD)
        for name, label in [
            ('fake .bak', _label(HOST, BASE, FAKE + '.bak')),
            ('other-docker-compose.fake.yaml', _label(HOST, BASE, 'other-' + FAKE)),
            ('a directory named like the fake', _label(HOST, BASE, f'{FAKE}/{BASE}')),
        ]
    ],
    # --- a malformed or empty label is never test, whatever it names
    *[
        (name, Stack(('pg',), {'pg': label}), PROD_FILES, PROD_PREFIX + _malformed(label))
        for name, label in [
            ('fake, leading comma', ',' + SYSTEM_LABEL),
            ('fake, trailing comma', SYSTEM_LABEL + ','),
            ('fake, double comma', f'{HOST}/{BASE},,{HOST}/{FAKE}'),
            ('relative fake entry alone', FAKE),
            ('relative fake entry', f'{HOST}/{BASE},{FAKE}'),
            ('fake, space after a comma', f'{HOST}/{BASE}, {HOST}/{FAKE}'),
        ]
    ],
    # --- a test postgres cross-checked against data_store: anything but one test data_store (or none) is production
    (
        'two test postgres containers',
        Stack(('pg1', 'pg2'), {'pg1': SYSTEM_LABEL, 'pg2': SYSTEM_LABEL}),
        PROD_FILES,
        PROD_PREFIX + '2 postgres containers in this project; cannot tell which stack',
    ),
    (
        'test postgres, two data_store containers',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds1': SYSTEM_LABEL, 'ds2': SYSTEM_LABEL}, ('ds1', 'ds2')),
        PROD_FILES,
        PROD_PREFIX + '2 data_store containers; cannot tell which stack',
    ),
    (
        'test postgres, prod data_store',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': PROD_LABEL}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + 'postgres was started by the test definition but data_store by PROD: mixed stack',
    ),
    (
        'test postgres, dev data_store',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': DEV_LABEL}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + 'postgres was started by the test definition but data_store by DEV: mixed stack',
    ),
    (
        'test postgres, data_store with both files',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': BOTH_LABEL}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + f'postgres was started by the test definition but data_store by UNKNOWN: {REASON_BOTH}: '
        'mixed stack',
    ),
    (
        'test postgres, data_store with no label',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': ''}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + f'postgres was started by the test definition but data_store by UNKNOWN: {REASON_NO_LABEL}: '
        'mixed stack',
    ),
    (
        'test postgres, data_store inspect fails',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': None}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + 'postgres was started by the test definition but data_store by UNKNOWN: '
        f'{REASON_INSPECT_FAILED}: mixed stack',
    ),
    (
        'test postgres, data_store ps fails',
        Stack(('pg',), {'pg': SYSTEM_LABEL}, data_store_ps_fails=True),
        PROD_FILES,
        PROD_PREFIX + 'could not list the data_store containers to cross-check the stack',
    ),
    (
        'dev postgres, test data_store',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': SYSTEM_LABEL}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + 'postgres was started by the dev definition but data_store by TEST: mixed stack',
    ),
    (
        'both-files postgres, test data_store',
        Stack(('pg',), {'pg': BOTH_LABEL, 'ds': SYSTEM_LABEL}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + REASON_BOTH,
    ),
    # --- unambiguous dev
    ('dev, dev-deps only', Stack(('pg',), {'pg': DEV_LABEL}), DEV_FILES, DEV_LINE),
    ('dev, devcontainer paths', Stack(('pg',), {'pg': DEV_LABEL_DEVCONTAINER}), DEV_FILES, DEV_LINE),
    ('dev, bare compose up', Stack(('pg',), {'pg': BARE_UP_LABEL}), DEV_FILES, DEV_LINE),
    (
        'dev postgres, dev data_store (each under its own root)',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': DEV_LABEL_DEVCONTAINER}, ('ds',)),
        DEV_FILES,
        DEV_LINE,
    ),
    # --- the label is absent, unreadable or malformed: base alone
    ('empty label', Stack(('pg',), {'pg': ''}), PROD_FILES, PROD_PREFIX + REASON_NO_LABEL),
    ('inspect fails', Stack(('pg',), {'pg': None}), PROD_FILES, PROD_PREFIX + REASON_INSPECT_FAILED),
    *[
        (name, Stack(('pg',), {'pg': label}), PROD_FILES, PROD_PREFIX + _malformed(label))
        for name, label in [
            ('leading comma', ',' + DEV_LABEL),
            ('trailing comma', DEV_LABEL + ','),
            ('double comma', f'{HOST}/{BASE},,{HOST}/{OVERRIDE}'),
            ('relative entry alone', OVERRIDE),
            ('relative override entry', f'{HOST}/{BASE},{OVERRIDE}'),
            ('space after a comma', f'{HOST}/{BASE}, {HOST}/{OVERRIDE}'),
        ]
    ],
    # --- near misses: well formed, but no entry's file name is exactly the override
    *[
        (name, Stack(('pg',), {'pg': label}), PROD_FILES, PROD_PREFIX + REASON_PROD)
        for name, label in [
            ('override .bak', _label(HOST, BASE, OVERRIDE + '.bak')),
            ('other-docker-compose.override.yaml', _label(HOST, BASE, 'other-' + OVERRIDE)),
            ('a directory named like the override', _label(HOST, BASE, f'{OVERRIDE}/{BASE}')),
        ]
    ],
    # --- more than one candidate container, or a stack the two containers disagree on: base alone
    (
        'two postgres containers',
        Stack(('pg1', 'pg2'), {'pg1': DEV_LABEL, 'pg2': DEV_LABEL}),
        PROD_FILES,
        PROD_PREFIX + '2 postgres containers in this project; cannot tell which stack',
    ),
    (
        'dev postgres, two data_store containers',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds1': DEV_LABEL, 'ds2': DEV_LABEL}, ('ds1', 'ds2')),
        PROD_FILES,
        PROD_PREFIX + '2 data_store containers; cannot tell which stack',
    ),
    (
        'dev postgres, prod data_store',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': PROD_LABEL}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + 'postgres was started by the dev definition but data_store by PROD: mixed stack',
    ),
    (
        'dev postgres, data_store with no label',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': ''}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + f'postgres was started by the dev definition but data_store by UNKNOWN: {REASON_NO_LABEL}: '
        'mixed stack',
    ),
    (
        'dev postgres, data_store inspect fails',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': None}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + f'postgres was started by the dev definition but data_store by UNKNOWN: {REASON_INSPECT_FAILED}: '
        'mixed stack',
    ),
    (
        'dev postgres, data_store ps fails',
        Stack(('pg',), {'pg': DEV_LABEL}, data_store_ps_fails=True),
        PROD_FILES,
        PROD_PREFIX + 'could not list the data_store containers to cross-check the stack',
    ),
]

INVOCATIONS = [(), ('current',), ('history',), ('check',)]

# THE PROD-IN-DEV REFUSAL (tj-grna9p.173). The table's PROD-verdict rows are exactly those whose selection line is the
# production prefix with the plain production reason: the label is well formed and names neither the override nor
# the fake. In the default project they run as production; in trader_joe_dev they are refused.
PROD_IN_DEV = (
    f'project {DEV_PROJECT} holds a container started from the production files: stop it (it belongs in the default '
    'project); make migrate will not guess\n'
)


def _prod_verdict(line: str) -> bool:
    return line == PROD_PREFIX + REASON_PROD


def _unclear_in_dev(reason: str) -> str:
    """The refusal of every other unclear outcome inside trader_joe_dev (architect option B), naming its cause."""
    return (
        f'project {DEV_PROJECT}: cannot tell that this is a dev or test stack ({reason}): refusing; make migrate '
        'will not guess. Restart the stack with make dev-up or make system-launch, then re-run.\n'
    )


def _refusal_in_dev(line: str) -> str:
    """What a production-prefixed selection line becomes in trader_joe_dev: the prod-label or the unclear refusal."""
    assert line.startswith(PROD_PREFIX), line
    return PROD_IN_DEV if _prod_verdict(line) else _unclear_in_dev(line[len(PROD_PREFIX) :])


PROD_VERDICT_CASES = [c for c in CASES if _prod_verdict(c[3])]
# OPTION B (tj-grna9p.173): inside trader_joe_dev only a positively DEV or TEST stack runs; every row the default
# project runs on the production definition is refused there, the prod label with its own message and the rest
# (no label, malformed, both files, a failed inspect or ps, two containers, a mixed stack) naming their cause.
REFUSED_IN_DEV_CASES = [c for c in CASES if c[3].startswith(PROD_PREFIX)]
UNCLEAR_CASES = [c for c in REFUSED_IN_DEV_CASES if not _prod_verdict(c[3])]
# The table, by project, less the rows the refusals own: every remaining row runs alembic.
SELECTION_PARAMS = [
    pytest.param(stack, files, line, project, id=f'{name}-{project}')
    for name, stack, files, line in CASES
    for project in PROJECTS
    if not (project == 'dev' and line.startswith(PROD_PREFIX))
]


def _alembic_args(invocation: tuple[str, ...]) -> tuple[str, ...]:
    return invocation or ('upgrade', 'head')


def _assert_probed_in_order(outcome: Outcome) -> None:
    """The first two docker calls are the probes, trader_joe_dev first; no other call asks for postgres."""
    assert [c.argv for c in outcome.calls[:2]] == [PROBE_DEV, PROBE_DEFAULT], outcome.explain()
    assert [c.argv for c in outcome.compose('ps', '-q', 'postgres')] == [PROBE_DEV, PROBE_DEFAULT], outcome.explain()


def _assert_every_later_call_addresses(outcome: Outcome, project: str) -> None:
    """After the probes every compose call carries exactly the found project's -p (none for the default one)."""
    later = [c for c in outcome.calls[2:] if c.is_compose]
    assert later, outcome.explain()
    assert {c.project_args for c in later} == {PROJECT_ARGS[project]}, outcome.explain()
    # and the -p, where there is one, comes straight after `compose`, ahead of the literal -f list
    for call in later:
        assert call.argv[1 : 1 + len(PROJECT_ARGS[project])] == PROJECT_ARGS[project], outcome.explain()


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('stack', 'files', 'line', 'project'), SELECTION_PARAMS)
def test_the_running_stack_selects_the_definition_and_alembic_always_runs(
    harness: Harness, stack: Stack, files: tuple[str, ...], line: str, invocation: tuple[str, ...], project: str
):
    """Every case, in either project: the label picks the definition, the project found picks the -p.

    Except a production outcome inside trader_joe_dev, which the refusal tests below own: in the dev project only
    the DEV and TEST rows are here.
    """
    harness.stack(stack, project)
    outcome = harness.run(*invocation)
    args = _alembic_args(invocation)

    assert outcome.result.returncode == 0, outcome.explain()

    # The command itself is unchanged; only the -f list and the project vary. In the default project it is
    # byte-identical to the old spelling: no -p at all.
    run = outcome.alembic()
    assert run.argv == ('compose', *PROJECT_ARGS[project], *[w for f in files for w in ('-f', f)], 'run', '--rm',
                        '--no-deps', 'data_store', ALEMBIC, *args), outcome.explain()  # fmt: skip
    # The migration-safety gate's calls (P03), on the same definition and project as the one-off: a schema-moving
    # command dumps and reads the dump back, after a label read on the production definition only; a read-only one
    # makes none of them.
    assert outcome.gate_calls() == _gate(files, invocation), outcome.explain()
    for call in outcome.calls:
        if call.is_compose and call.rest in (REVISIONS_PROBE, LABEL_READ, PG_DUMP, PG_RESTORE_LIST):
            assert call.argv[: len(_head(project, files))] == _head(project, files), outcome.explain()

    # The postgres probes come first, trader_joe_dev before the default project, each on the base file alone.
    _assert_probed_in_order(outcome)
    _assert_every_later_call_addresses(outcome, project)
    if 'check' in invocation:
        # The guard asks the SELECTED definition, in the found project, which image the one-off container will run.
        config = outcome.only('config', '--images', 'data_store')
        assert config.files == files and config.project_args == PROJECT_ARGS[project], outcome.explain()
    else:
        assert not outcome.compose('config'), outcome.explain()

    # Every compose call names one of the three definitions, and never the override and the fake together.
    assert {call.files for call in outcome.calls if call.is_compose} <= {PROD_FILES, DEV_FILES, TEST_FILES}, (
        outcome.explain()
    )

    # Exactly one selection line on stderr, naming the definition, why, and the project (and, after it, the dump
    # line of a schema-moving command).
    assert _split_dump_line(outcome, invocation) == _named(line, project).splitlines(), outcome.explain()
    assert outcome.result.stderr.count('run_migrations.sh: using the') == 1, outcome.explain()

    # Nothing added to stdout: the existing Running line, then alembic's own output (and, for check, the guard's
    # verified block, which predates this change).
    stdout = outcome.result.stdout.splitlines()
    assert stdout[0] == f"Running 'alembic {' '.join(args)}' {RUNNING_REVISIONS}", outcome.explain()
    assert stdout[-1] == ALEMBIC_STDOUT, outcome.explain()
    if 'check' in invocation:
        assert stdout[1].startswith('Comparison image verified'), outcome.explain()
    else:
        assert len(stdout) == 2, outcome.explain()
    assert 'definition' not in outcome.result.stdout, outcome.explain()


def _inspect(container: str) -> tuple[str, ...]:
    return (
        'inspect', '--format',
        '{{ with .Config.Labels }}{{ index . "com.docker.compose.project.config_files" }}{{ end }}', container,
    )  # fmt: skip


def _assert_refused_prod_in_dev(outcome: Outcome, container: str = 'pg') -> None:
    """Exit 1, the one exact line, stdout only the Running line; the calls are the probes and one label read."""
    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == PROD_IN_DEV, outcome.explain()
    assert len(outcome.result.stdout.splitlines()) == 1, outcome.explain()
    assert outcome.result.stdout.startswith("Running 'alembic "), outcome.explain()
    assert [c.argv for c in outcome.calls] == [PROBE_DEV, PROBE_DEFAULT, _inspect(container)], outcome.explain()


DATA_STORE_CROSS_CHECK = ('compose', '-p', DEV_PROJECT, '-f', BASE, 'ps', '-q', 'data_store')


def _reads_before_the_verdict(stack: Stack) -> list[tuple[str, ...]]:
    """The docker calls the selection makes in trader_joe_dev before it can refuse STACK, and nothing more.

    The probes; with exactly one postgres its label read; when that label is DEV or TEST the data_store listing;
    with exactly one data_store listed, its label read. No run, no config, no image lookup.
    """
    calls = [PROBE_DEV, PROBE_DEFAULT]
    if len(stack.postgres) != 1:
        return calls
    (postgres,) = stack.postgres
    calls.append(_inspect(postgres))
    label = stack.labels.get(postgres, '')
    if label is None or _classify(label).strip() not in ('DEV', 'TEST'):
        return calls
    calls.append(DATA_STORE_CROSS_CHECK)
    if not stack.data_store_ps_fails and len(stack.data_store) == 1:
        calls.append(_inspect(stack.data_store[0]))
    return calls


def _assert_refused_in_dev(outcome: Outcome, stack: Stack, message: str) -> None:
    """Exit 1, exactly MESSAGE on stderr, stdout only the Running line, and the calls stop at the reads."""
    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == message, outcome.explain()
    assert 'run_migrations.sh: using the' not in outcome.result.stderr, outcome.explain()
    assert len(outcome.result.stdout.splitlines()) == 1, outcome.explain()
    assert outcome.result.stdout.startswith("Running 'alembic "), outcome.explain()
    assert [c.argv for c in outcome.calls] == _reads_before_the_verdict(stack), outcome.explain()


def test_the_refused_rows_are_the_ones_expected():
    """Option B's table is not vacuous: every production row of the table, and only those, is refused in dev."""
    assert len(REFUSED_IN_DEV_CASES) == 41 and len(UNCLEAR_CASES) == 32, [c[0] for c in REFUSED_IN_DEV_CASES]
    assert all(not c[3].startswith(PROD_PREFIX) for c in CASES if c not in REFUSED_IN_DEV_CASES)
    assert {c[2] for c in REFUSED_IN_DEV_CASES} == {PROD_FILES}


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('stack', 'line'), [(c[1], c[3]) for c in UNCLEAR_CASES], ids=[c[0] for c in UNCLEAR_CASES])
def test_an_unclear_stack_in_the_dev_project_is_refused_naming_its_cause(
    harness: Harness, stack: Stack, line: str, invocation: tuple[str, ...]
):
    """No label, malformed, both files, a failed inspect or ps, two containers, a mixed stack: refused in dev.

    The message carries the same cause the default project's selection line gives. run_status=3 so that reaching
    alembic would show as 3; check reaches no image lookup.
    """
    harness.stack(stack, 'dev')
    harness.image_mode('missing')
    outcome = harness.run(*invocation, run_status=3)
    _assert_refused_in_dev(outcome, stack, _unclear_in_dev(line[len(PROD_PREFIX) :]))


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('stack', 'line'), [(c[1], c[3]) for c in UNCLEAR_CASES], ids=[c[0] for c in UNCLEAR_CASES])
def test_the_same_unclear_stacks_in_the_default_project_still_run_production(
    harness: Harness, stack: Stack, line: str, invocation: tuple[str, ...]
):
    """The default project is unchanged (tj-yb1bxj's never-block rule, now scoped to it): production, no -p, alembic."""
    harness.stack(stack, 'default')
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    assert 'cannot tell that this is a dev or test stack' not in outcome.result.stderr, outcome.explain()
    assert outcome.alembic().argv == ('compose', '-f', BASE, 'run', '--rm', '--no-deps', 'data_store', ALEMBIC,
                                      *_alembic_args(invocation)), outcome.explain()  # fmt: skip
    assert not [c for c in outcome.calls[1:] if c.is_compose and c.project_args], outcome.explain()
    assert _split_dump_line(outcome, invocation) == _named(line, 'default').splitlines(), outcome.explain()
    # THE BEHAVIOUR CHANGE P03 MADE HERE: an unclear stack in the default project is the production definition, so a
    # schema-moving command now also reads the deployment label (and would refuse a non-empty one) before its dump.
    assert outcome.gate_calls() == _gate(PROD_FILES, invocation), outcome.explain()


def test_the_prod_verdict_rows_are_the_ones_expected():
    """The refusal's table is not vacuous: the nine well-formed labels naming neither the override nor the fake."""
    assert [c[0] for c in PROD_VERDICT_CASES] == [
        'prod', 'prod, base file only', 'neither the override nor the fake, nor the base',
        'fake .bak', 'other-docker-compose.fake.yaml', 'a directory named like the fake',
        'override .bak', 'other-docker-compose.override.yaml', 'a directory named like the override',
    ]  # fmt: skip


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize('stack', [c[1] for c in PROD_VERDICT_CASES], ids=[c[0] for c in PROD_VERDICT_CASES])
def test_a_production_labelled_postgres_in_the_dev_project_is_refused(
    harness: Harness, stack: Stack, invocation: tuple[str, ...]
):
    """A PROD label inside trader_joe_dev (ruling (1) on tj-grna9p.162): refused before anything else runs.

    run_status=3 so that reaching alembic would show as 3, never as the refusal's 1; check reaches no image lookup.
    """
    harness.stack(stack, 'dev')
    harness.image_mode('missing')
    outcome = harness.run(*invocation, run_status=3)
    _assert_refused_prod_in_dev(outcome)


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(
    'data_store',
    [None, DEV_LABEL, SYSTEM_LABEL, PROD_LABEL],
    ids=['no data_store', 'dev data_store', 'test data_store', 'prod data_store'],
)
def test_the_prod_in_dev_refusal_comes_before_the_cross_check(
    harness: Harness, data_store: str | None, invocation: tuple[str, ...]
):
    """Whatever data_store runs beside it, a prod postgres in trader_joe_dev is refused: data_store is never listed."""
    labels = {'pg': PROD_LABEL} | ({'ds': data_store} if data_store is not None else {})
    harness.stack(Stack(('pg',), labels, ('ds',) if data_store is not None else ()), 'dev')
    outcome = harness.run(*invocation, run_status=3)
    _assert_refused_prod_in_dev(outcome)


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize('stack', [c[1] for c in PROD_VERDICT_CASES], ids=[c[0] for c in PROD_VERDICT_CASES])
def test_the_same_labels_in_the_default_project_run_production_with_no_project(
    harness: Harness, stack: Stack, invocation: tuple[str, ...]
):
    """The refusal is the dev project's alone: prod's own project runs the production definition, no -p anywhere."""
    harness.stack(stack, 'default')
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    assert PROD_IN_DEV.strip() not in outcome.result.stderr, outcome.explain()
    run = outcome.alembic()
    assert run.argv == ('compose', '-f', BASE, 'run', '--rm', '--no-deps', 'data_store', ALEMBIC,
                        *_alembic_args(invocation)), outcome.explain()  # fmt: skip
    assert not [c for c in outcome.calls[1:] if c.is_compose and c.project_args], outcome.explain()
    assert _split_dump_line(outcome, invocation) == _named(PROD_PREFIX + REASON_PROD, 'default').splitlines(), (
        outcome.explain()
    )


@pytest.mark.parametrize('label', [DEV_LABEL, BARE_UP_LABEL, SYSTEM_LABEL], ids=['dev', 'bare up', 'test'])
@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
def test_dev_and_test_labels_in_the_dev_project_still_run_there(
    harness: Harness, label: str, invocation: tuple[str, ...]
):
    """Only DEV and TEST belong in trader_joe_dev, and they still run alembic there, -p on every later call."""
    harness.stack(Stack(('pg',), {'pg': label}), 'dev')
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    assert PROD_IN_DEV.strip() not in outcome.result.stderr, outcome.explain()
    assert outcome.alembic().project_args == ('-p', DEV_PROJECT), outcome.explain()
    _assert_every_later_call_addresses(outcome, 'dev')


# ------------------------------------------------------------------------------------------------- one project
# Both probes returning the SAME non-empty id(s) is one project, the dev one (ruling (2) on tj-grna9p.162).

SAME_ID_RUNS = [
    ('dev', Stack(('pg',), {'pg': DEV_LABEL}), DEV_FILES, DEV_LINE),
    ('test', Stack(('pg',), {'pg': SYSTEM_LABEL}), TEST_FILES, TEST_LINE),
    ('dev, dev data_store', Stack(('pg',), {'pg': DEV_LABEL, 'ds': DEV_LABEL}, ('ds',)), DEV_FILES, DEV_LINE),
]


def _assert_one_dev_project(outcome: Outcome, files: tuple[str, ...], line: str, invocation: tuple[str, ...]) -> None:
    assert outcome.result.returncode == 0, outcome.explain()
    assert _split_dump_line(outcome, invocation) == _named(line, 'dev').splitlines(), outcome.explain()
    assert 'runs in both' not in outcome.result.stderr, outcome.explain()
    _assert_probed_in_order(outcome)
    _assert_every_later_call_addresses(outcome, 'dev')
    run = outcome.alembic()
    assert run.argv == ('compose', '-p', DEV_PROJECT, *[w for f in files for w in ('-f', f)], 'run', '--rm',
                        '--no-deps', 'data_store', ALEMBIC, *_alembic_args(invocation)), outcome.explain()  # fmt: skip


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('stack', 'files', 'line'), [c[1:] for c in SAME_ID_RUNS], ids=[c[0] for c in SAME_ID_RUNS])
def test_identical_ids_from_both_probes_are_the_dev_project(
    harness: Harness, stack: Stack, files: tuple[str, ...], line: str, invocation: tuple[str, ...]
):
    """The stub's two projects answer the same postgres id: one project, trader_joe_dev, and alembic runs."""
    harness.stack(stack, 'dev', other=Stack(stack.postgres))
    outcome = harness.run(*invocation)
    _assert_one_dev_project(outcome, files, line, invocation)


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('stack', 'files', 'line'), [c[1:] for c in SAME_ID_RUNS], ids=[c[0] for c in SAME_ID_RUNS])
def test_compose_project_name_set_to_the_dev_project_is_one_project(
    harness: Harness, stack: Stack, files: tuple[str, ...], line: str, invocation: tuple[str, ...]
):
    """The motivating case: COMPOSE_PROJECT_NAME=trader_joe_dev makes the default probe ask trader_joe_dev too."""
    harness.stack(stack, 'dev')
    outcome = harness.run(*invocation, extra_env={'COMPOSE_PROJECT_NAME': DEV_PROJECT})
    _assert_one_dev_project(outcome, files, line, invocation)


@pytest.mark.parametrize('extra_env', [{}, {'COMPOSE_PROJECT_NAME': DEV_PROJECT}], ids=['same ids', 'env'])
def test_one_project_holding_a_prod_label_is_still_refused(harness: Harness, extra_env: dict[str, str]):
    """The collapse makes it the dev project, so a prod label there is the prod-in-dev refusal, not 'both running'."""
    harness.stack(Stack(('pg',), {'pg': PROD_LABEL}), 'dev', other=Stack(('pg',)) if not extra_env else None)
    outcome = harness.run('upgrade', 'head', run_status=3, extra_env=extra_env)
    _assert_refused_prod_in_dev(outcome)


def test_prod_in_its_own_project_is_invisible_under_the_dev_project_name(harness: Harness):
    """COMPOSE_PROJECT_NAME=trader_joe_dev hides prod's default project from both probes: not running, nothing run."""
    harness.stack(Stack(('pgp',), {'pgp': PROD_LABEL}), 'default')
    outcome = harness.run(extra_env={'COMPOSE_PROJECT_NAME': DEV_PROJECT})

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == NOT_RUNNING, outcome.explain()
    assert [c.argv for c in outcome.calls] == [PROBE_DEV, PROBE_DEFAULT], outcome.explain()


DIFFERENT_IDS = [
    ('one each, different', ('pgd',), ('pgp',)),
    ('the default probe a superset', ('pg',), ('pg', 'pgx')),
    ('the dev probe a superset', ('pg', 'pgx'), ('pg',)),
]


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('dev', 'default'), [c[1:] for c in DIFFERENT_IDS], ids=[c[0] for c in DIFFERENT_IDS])
def test_different_ids_from_the_two_probes_still_refuse_as_both(
    harness: Harness, dev: tuple[str, ...], default: tuple[str, ...], invocation: tuple[str, ...]
):
    """Only IDENTICAL answers collapse: any difference is two projects, refused after the probes and nothing else."""
    labels = dict.fromkeys({*dev, *default}, DEV_LABEL)
    harness.stack(Stack(dev, labels), 'dev', other=Stack(default, labels))
    outcome = harness.run(*invocation, run_status=3)

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == BOTH_RUNNING, outcome.explain()
    assert [c.argv for c in outcome.calls] == [PROBE_DEV, PROBE_DEFAULT], outcome.explain()


@pytest.mark.parametrize('extra_env', [{}, {'COMPOSE_PROJECT_NAME': DEV_PROJECT}], ids=['default env', 'env'])
def test_two_empty_answers_are_not_a_collapse(harness: Harness, extra_env: dict[str, str]):
    """Equal but EMPTY answers are neither project running: the old message, nothing after the probes."""
    harness.stack(Stack(()))
    outcome = harness.run(extra_env=extra_env)

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == NOT_RUNNING, outcome.explain()
    assert [c.argv for c in outcome.calls] == [PROBE_DEV, PROBE_DEFAULT], outcome.explain()


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
def test_the_test_one_off_is_the_literal_base_plus_fake_run(harness: Harness, invocation: tuple[str, ...]):
    """The one-off on a test stack, spelled out once in full: the :test image runs, never :latest (addendum 27 (2)).

    A test stack runs in trader_joe_dev (tj-grna9p.161), so the literal carries -p trader_joe_dev.
    """
    harness.stack(Stack(('pg',), {'pg': SYSTEM_LABEL}), 'dev')
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    head = ('compose', '-p', 'trader_joe_dev', '-f', 'docker-compose.yaml', '-f', 'docker-compose.fake.yaml')
    assert outcome.alembic().argv == (
        *head, 'run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', *_alembic_args(invocation),
    ), outcome.explain()  # fmt: skip
    # The look into the image, on every command, then the dump and its read-back, spelled out in full once: -T, in
    # postgres, on the same project and definition. No label read: the test definition is supposed to carry a label.
    gate = [c.argv for c in outcome.calls if c.is_compose and (c.rest[:1] == ('exec',) or '--entrypoint' in c.rest)]
    probe = (*head, 'run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'data_store', '-c',
             'set -- /code/migrations/versions/*.py; [ -e "$1" ] || exit 3')  # fmt: skip
    expected = [probe, *([
        (*head, 'exec', '-T', 'postgres', 'sh', '-c',
         'exec pg_dump --format=custom --user "$POSTGRES_USER" --dbname "$POSTGRES_DB"'),
        (*head, 'exec', '-T', 'postgres', 'pg_restore', '--list'),
    ] if _is_mutating(invocation) else [])]  # fmt: skip
    assert gate == expected, outcome.explain()
    assert outcome.result.stderr.splitlines()[0] == TEST_LINE + ' (project: trader_joe_dev)', outcome.explain()


# The literal one-off argv per (definition, project), spelled out in full once: the dev project's carries
# -p trader_joe_dev straight after `compose`; the default project's is the pre-change command word for word.
LITERAL_RUNS = [
    ('dev', DEV_LABEL, 'dev', DEV_LINE,
     ('compose', '-p', 'trader_joe_dev', '-f', 'docker-compose.yaml', '-f', 'docker-compose.override.yaml')),
    ('test', SYSTEM_LABEL, 'dev', TEST_LINE,
     ('compose', '-p', 'trader_joe_dev', '-f', 'docker-compose.yaml', '-f', 'docker-compose.fake.yaml')),
    # A prod-labelled postgres INSIDE trader_joe_dev has no one-off: it is refused (tj-grna9p.173), pinned by
    # test_a_production_labelled_postgres_in_the_dev_project_is_refused above.
    ('prod', PROD_LABEL, 'default', PROD_PREFIX + REASON_PROD, ('compose', '-f', 'docker-compose.yaml')),
    ('dev label in the default project', DEV_LABEL, 'default', DEV_LINE,
     ('compose', '-f', 'docker-compose.yaml', '-f', 'docker-compose.override.yaml')),
    ('test label in the default project', SYSTEM_LABEL, 'default', TEST_LINE,
     ('compose', '-f', 'docker-compose.yaml', '-f', 'docker-compose.fake.yaml')),
]  # fmt: skip


@pytest.mark.parametrize(('label', 'project', 'line', 'head'), [c[1:] for c in LITERAL_RUNS],
                         ids=[c[0] for c in LITERAL_RUNS])  # fmt: skip
def test_the_one_off_is_spelled_out_per_definition_and_project(
    harness: Harness, label: str, project: str, line: str, head: tuple[str, ...]
):
    harness.stack(Stack(('pg',), {'pg': label}), project)
    outcome = harness.run()

    run = (*head, 'run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', 'upgrade', 'head')
    # DEV and TEST cross-check data_store before the selection, on the base file, in the found project.
    cross_check = [('compose', *PROJECT_ARGS[project], '-f', 'docker-compose.yaml', 'ps', '-q', 'data_store')]
    # Then the look into the image for revision files (tj-7zjov1.45), on the selected definition; then the
    # migration-safety gate (P03): the production definition reads the deployment label first; every definition
    # dumps and reads the dump back; then alembic.
    probe = (*head, 'run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'data_store', '-c',
             'set -- /code/migrations/versions/*.py; [ -e "$1" ] || exit 3')  # fmt: skip
    label_read =(*head, 'run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'data_store', '-c',
                  'printf %s "${DEPLOYMENT_LABEL-}"')  # fmt: skip
    dump = (*head, 'exec', '-T', 'postgres', 'sh', '-c',
            'exec pg_dump --format=custom --user "$POSTGRES_USER" --dbname "$POSTGRES_DB"')  # fmt: skip
    read_back = (*head, 'exec', '-T', 'postgres', 'pg_restore', '--list')
    dev_or_test = line in (DEV_LINE, TEST_LINE)
    expected = [PROBE_DEV, PROBE_DEFAULT, *(cross_check if dev_or_test else []), probe,
                *([] if dev_or_test else [label_read]), dump, read_back, run]  # fmt: skip

    assert outcome.result.returncode == 0, outcome.explain()
    assert [c.argv for c in outcome.calls if c.is_compose] == expected, outcome.explain()
    assert _split_dump_line(outcome, ()) == [f'{line} (project: {PROJECT_NAME[project]})'], outcome.explain()


def test_the_default_projects_calls_are_the_pre_change_commands_exactly(harness: Harness):
    """Prod's path, every docker call in order: the old postgres check, the label read, the gate, the old one-off.

    The only additions are the trader_joe_dev probe, which finds nothing, the look into the image for revision files
    (tj-7zjov1.45), and the migration-safety gate (P03): the DEPLOYMENT_LABEL read, the dump and its read-back.
    Nothing names a project.
    """
    harness.stack(Stack(('pg',), {'pg': PROD_LABEL}), 'default')
    outcome = harness.run()

    assert outcome.result.returncode == 0, outcome.explain()
    assert [c.argv for c in outcome.calls] == [
        PROBE_DEV,
        ('compose', '-f', 'docker-compose.yaml', 'ps', '-q', 'postgres'),
        ('inspect', '--format',
         '{{ with .Config.Labels }}{{ index . "com.docker.compose.project.config_files" }}{{ end }}', 'pg'),
        ('compose', '-f', 'docker-compose.yaml', 'run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh',
         'data_store', '-c', 'set -- /code/migrations/versions/*.py; [ -e "$1" ] || exit 3'),
        ('compose', '-f', 'docker-compose.yaml', 'run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh',
         'data_store', '-c', 'printf %s "${DEPLOYMENT_LABEL-}"'),
        ('compose', '-f', 'docker-compose.yaml', 'exec', '-T', 'postgres', 'sh', '-c',
         'exec pg_dump --format=custom --user "$POSTGRES_USER" --dbname "$POSTGRES_DB"'),
        ('compose', '-f', 'docker-compose.yaml', 'exec', '-T', 'postgres', 'pg_restore', '--list'),
        ('compose', '-f', 'docker-compose.yaml', 'run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic',
         'upgrade', 'head'),
    ], outcome.explain()  # fmt: skip
    assert not [c for c in outcome.calls[1:] if c.is_compose and c.project_args], outcome.explain()


# Both projects hold a running postgres, whatever their labels: refuse before reading any label or running alembic.
BOTH_PROJECTS = [
    ('dev and prod', Stack(('pgd',), {'pgd': DEV_LABEL}), Stack(('pgp',), {'pgp': PROD_LABEL})),
    ('test and prod', Stack(('pgd',), {'pgd': SYSTEM_LABEL}), Stack(('pgp',), {'pgp': PROD_LABEL})),
    ('dev and dev', Stack(('pgd',), {'pgd': DEV_LABEL}), Stack(('pgp',), {'pgp': DEV_LABEL})),
    ('prod and prod', Stack(('pgd',), {'pgd': PROD_LABEL}), Stack(('pgp',), {'pgp': PROD_LABEL})),
    ('two in dev, one in default', Stack(('pgd1', 'pgd2')), Stack(('pgp',), {'pgp': PROD_LABEL})),
    ('no labels', Stack(('pgd',), {'pgd': None}), Stack(('pgp',), {'pgp': ''})),
]


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('dev', 'default'), [c[1:] for c in BOTH_PROJECTS], ids=[c[0] for c in BOTH_PROJECTS])
def test_postgres_in_both_projects_refuses_and_runs_nothing(
    harness: Harness, dev: Stack, default: Stack, invocation: tuple[str, ...]
):
    """Both up is ambiguous (addendum 29 (3)): exit 1, the exact message, and nothing after the two probes."""
    harness.stack(dev, 'dev', other=default)
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == BOTH_RUNNING, outcome.explain()
    assert [c.argv for c in outcome.calls] == [PROBE_DEV, PROBE_DEFAULT], outcome.explain()
    assert not outcome.compose('run'), outcome.explain()
    assert 'run_migrations.sh: using the' not in outcome.result.stderr, outcome.explain()


@pytest.mark.parametrize('project', PROJECTS)
@pytest.mark.parametrize('label', [BOTH_LABEL, _label(HOST, FAKE, OVERRIDE)], ids=['override first', 'fake first'])
def test_both_files_select_production_with_a_reason_that_says_so(harness: Harness, label: str, project: str):
    """Override plus fake is production (addendum 27 (1)), and the line says WHY truthfully.

    The production fallback's usual reason, 'started without docker-compose.override.yaml', would be false here:
    the override IS in the label. The reason must name both files instead. In trader_joe_dev the same reason is the
    cause the refusal names (option B).
    """
    stack = Stack(('pg',), {'pg': label})
    harness.stack(stack, project)
    outcome = harness.run('current')
    if project == 'dev':
        _assert_refused_in_dev(outcome, stack, _unclear_in_dev(REASON_BOTH))
        assert 'started without' not in outcome.result.stderr, outcome.explain()
        return

    assert outcome.result.returncode == 0, outcome.explain()
    run = outcome.alembic()
    assert run.files == PROD_FILES and run.project_args == PROJECT_ARGS[project], outcome.explain()
    assert outcome.result.stderr == _named(PROD_PREFIX + REASON_BOTH, project) + '\n', outcome.explain()
    assert 'started without' not in outcome.result.stderr, outcome.explain()


@pytest.mark.parametrize('project', PROJECTS)
def test_a_newline_in_the_label_is_malformed_and_the_stack_stays_production(harness: Harness, project: str):
    """A newline in the label is malformed: production in the default project, refused in trader_joe_dev.

    Its reason echoes the label verbatim, so the one selection line spans two physical lines here -- the only
    case where it does (non-blocking finding on tj-s6r3rt.1). The project suffix ends the second physical line;
    in the dev project the refusal carries the same two-line cause.
    """
    label = f'{HOST}/{BASE}\n{HOST}/{OVERRIDE}'
    stack = Stack(('pg',), {'pg': label})
    harness.stack(stack, project)
    outcome = harness.run('current')
    if project == 'dev':
        _assert_refused_in_dev(outcome, stack, _unclear_in_dev(_malformed(label)))
        return

    assert outcome.result.returncode == 0, outcome.explain()
    run = outcome.alembic()
    assert run.files == PROD_FILES and run.project_args == PROJECT_ARGS[project], outcome.explain()
    assert outcome.result.stderr == _named(PROD_PREFIX + _malformed(label), project) + '\n', outcome.explain()
    assert 'definition' not in outcome.result.stdout


# (id, stack, what trader_joe_dev answers: None runs alembic there, else the exact refusal)
PATH_STACKS = [
    ('dev', Stack(('pg',), {'pg': DEV_LABEL}), None),
    ('test', Stack(('pg',), {'pg': SYSTEM_LABEL}), None),
    ('prod', Stack(('pg',), {'pg': PROD_LABEL}), PROD_IN_DEV),
    ('both files', Stack(('pg',), {'pg': BOTH_LABEL}), _unclear_in_dev(REASON_BOTH)),
    ('inspect fails', Stack(('pg',), {'pg': None}), _unclear_in_dev(REASON_INSPECT_FAILED)),
    (
        'two postgres',
        Stack(('pg1', 'pg2')),
        _unclear_in_dev('2 postgres containers in this project; cannot tell which stack'),
    ),
    (
        'mixed',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': PROD_LABEL}, ('ds',)),
        _unclear_in_dev('postgres was started by the dev definition but data_store by PROD: mixed stack'),
    ),
    (
        'test mixed',
        Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': DEV_LABEL}, ('ds',)),
        _unclear_in_dev('postgres was started by the test definition but data_store by DEV: mixed stack'),
    ),
]


@pytest.mark.parametrize(('stack', 'dev_refusal'), [c[1:] for c in PATH_STACKS], ids=[c[0] for c in PATH_STACKS])
@pytest.mark.parametrize('status', [1, 3])
@pytest.mark.parametrize('project', PROJECTS)
def test_the_exit_status_is_alembics_on_every_path(
    harness: Harness, stack: Stack, dev_refusal: str | None, status: int, project: str
):
    """Every path that reaches alembic exits with alembic's status; a dev-project refusal never reaches it."""
    if project == 'dev' and dev_refusal is not None:
        harness.stack(stack, project)
        _assert_refused_in_dev(harness.run('upgrade', 'head', run_status=status), stack, dev_refusal)
        return
    harness.stack(stack, project)
    outcome = harness.run('upgrade', 'head', run_status=status)
    assert outcome.result.returncode == status, outcome.explain()
    assert outcome.alembic().project_args == PROJECT_ARGS[project], outcome.explain()
    # The gate ran first and passed: the status is alembic's, not the dump's or the label read's.
    assert outcome.gate_calls()[-2:] == [PG_DUMP, PG_RESTORE_LIST], outcome.explain()


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
def test_a_stopped_postgres_is_reported_before_any_selection(harness: Harness, invocation: tuple[str, ...]):
    """Neither project holds postgres: the old message, after both probes and before any selection.

    The postgres check precedes the selection (the guard's containment depends on that order).
    """
    harness.stack(Stack(()))
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == NOT_RUNNING, outcome.explain()
    assert [c.argv for c in outcome.calls] == [PROBE_DEV, PROBE_DEFAULT], outcome.explain()
    assert not outcome.compose('run')


# ------------------------------------------------------------------------------------------------- the check guard


@pytest.mark.parametrize('image_mode', ['missing', 'unstamped', 'stale'])
@pytest.mark.parametrize(
    ('stack', 'files', 'build', 'image'),
    [
        (Stack(('pg',), {'pg': DEV_LABEL}), DEV_FILES, 'dev-build', 'trader_joe_data_store:dev'),
        (Stack(('pg',), {'pg': SYSTEM_LABEL}), TEST_FILES, 'test-build', 'trader_joe_data_store:test'),
        (Stack(('pg',), {'pg': PROD_LABEL}), PROD_FILES, 'prod-build', 'trader_joe_data_store:latest'),
        (Stack(('pg',), {'pg': BOTH_LABEL}), PROD_FILES, 'prod-build', 'trader_joe_data_store:latest'),
        (Stack(('pg',), {'pg': None}), PROD_FILES, 'prod-build', 'trader_joe_data_store:latest'),
        (
            Stack(('pg',), {'pg': DEV_LABEL, 'ds': PROD_LABEL}, ('ds',)),
            PROD_FILES,
            'prod-build',
            'trader_joe_data_store:latest',
        ),
        (
            Stack(('pg',), {'pg': SYSTEM_LABEL, 'ds': DEV_LABEL}, ('ds',)),
            PROD_FILES,
            'prod-build',
            'trader_joe_data_store:latest',
        ),
    ],
    ids=['dev', 'test', 'prod', 'both files', 'ambiguous', 'mixed', 'test mixed'],
)
@pytest.mark.parametrize('project', PROJECTS)
def test_the_check_guard_names_the_selected_image_and_its_build(
    harness: Harness, stack: Stack, files: tuple[str, ...], build: str, image: str, image_mode: str, project: str
):
    """tj-yb1bxj addendum 4: the refusal reads as before against the selected image; only FIX: follows the stack.

    On TEST the image asked about is compose's answer for base + fake (:test by design), never the :latest the
    production definition names, and the fix is make test-build (addendum 27 (2)). The guard asks in the project
    postgres was found in. In trader_joe_dev every production outcome is refused before the guard is reached.
    """
    harness.stack(stack, project)
    harness.image_mode(image_mode)
    outcome = harness.run('check')
    if project == 'dev' and files == PROD_FILES:
        (dev_refusal,) = [c[2] for c in PATH_STACKS if c[1] == stack]
        assert dev_refusal is not None
        _assert_refused_in_dev(outcome, stack, dev_refusal)
        return

    assert outcome.result.returncode == 1, outcome.explain()
    assert not outcome.compose(*ALEMBIC_RUN), 'a refused check must never reach alembic'
    config = outcome.only('config', '--images', 'data_store')
    assert config.files == files and config.project_args == PROJECT_ARGS[project], outcome.explain()
    stderr = outcome.result.stderr
    assert stderr.startswith('run_migrations.sh: using the'), outcome.explain()
    assert 'REFUSED' in stderr, outcome.explain()
    assert f'image reference: {image}' in stderr, outcome.explain()
    fixes = [line for line in stderr.splitlines() if line.startswith('FIX:')]
    assert fixes == [f'FIX: make {build}'], outcome.explain()


def test_the_stubs_image_map_and_the_fix_targets_are_the_repositorys():
    """The stub's config --images answers, and the three FIX: targets, are read off the real files.

    The guard asks compose, so its refusals name whatever the files say; the stub stands in for compose, so this pins
    that it says the same: base alone is :latest, the override makes :dev, the test-mode overlay makes :test, and
    make prod-build, dev-build and test-build exist.
    """

    def data_store_image(name: str) -> str | None:
        services = yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8'))['services']
        return (services.get('data_store') or {}).get('image')

    assert data_store_image(BASE) == 'trader_joe_data_store:latest'
    assert data_store_image(OVERRIDE) == 'trader_joe_data_store:dev'
    assert data_store_image(FAKE) == 'trader_joe_data_store:test'

    makefile = (REPO_ROOT / 'Makefile').read_text(encoding='utf-8')
    for target in ('prod-build', 'dev-build', 'test-build'):
        assert re.search(rf'^{target}:', makefile, re.MULTILINE), f'the FIX: line names make {target}; no such target'


@pytest.mark.parametrize('project', PROJECTS)
@pytest.mark.parametrize('label', [DEV_LABEL, SYSTEM_LABEL, PROD_LABEL], ids=['dev', 'test', 'prod'])
def test_upgrade_head_is_never_guarded_whatever_the_image(harness: Harness, label: str, project: str):
    """A remedy may not be refused (tj-yb1bxj addendum 3 item 6): no image lookup at all on upgrade.

    The one refusal of upgrade is not the image guard's: a prod label in trader_joe_dev (tj-grna9p.173).
    """
    harness.stack(Stack(('pg',), {'pg': label}), project)
    harness.image_mode('missing')
    outcome = harness.run()
    if project == 'dev' and label == PROD_LABEL:
        _assert_refused_prod_in_dev(outcome)
        return
    assert outcome.result.returncode == 0, outcome.explain()
    assert not outcome.compose('config') and not [c for c in outcome.calls if c.argv[:1] == ('image',)]


def test_the_definition_lines_stay_literal_and_the_project_is_spliced_in():
    """The three -f definition lines are spelled literally in every arm, with no project in them (addendum 29 (3)).

    The project is added after the selection, not written into each arm; the behaviour that results is pinned by
    the table above. This pins the spelling other readers of the file rely on (test_network_model.py and
    test_agent_stack_files_retired.py read COMPOSE=(...) lines).
    """
    lines = [line.strip() for line in SCRIPT.read_text(encoding='utf-8').splitlines()]
    definitions = (
        'COMPOSE=(docker compose -f docker-compose.yaml)',
        'COMPOSE=(docker compose -f docker-compose.yaml -f docker-compose.override.yaml)',
        'COMPOSE=(docker compose -f docker-compose.yaml -f docker-compose.fake.yaml)',
    )
    # The top-level line is the base literal (test_network_model.py reads the first ^COMPOSE= line).
    top_level = (line for line in SCRIPT.read_text(encoding='utf-8').splitlines() if line.startswith('COMPOSE=('))
    assert next(top_level) == definitions[0]
    # Exactly these assignments, each its exact number of times: the base literal at the top and again in the
    # PRODUCTION arm (so the one project splice treats all three arms alike), the DEV and TEST arms once each,
    # and the one base-file line naming the dev project where the probe finds it.
    assignments = [line for line in lines if line.startswith('COMPOSE=(')]
    assert sorted(assignments) == sorted([
        definitions[0],
        definitions[0],
        definitions[1],
        definitions[2],
        'COMPOSE=(docker compose -p "$DEV_PROJECT" -f docker-compose.yaml)',
    ]), assignments  # fmt: skip
    assert 'DEV_PROJECT=trader_joe_dev' in lines


# ------------------------------------------------------------------------------------------------- the empty-versions guard
# THE GUARD READS THE IMAGE (tj-7zjov1.45; design tj-26jt9w addendum 5 on tj-yb1bxj). An empty versions/ makes
# `alembic upgrade head` a successful no-op, so the script looks for revision files in the image the one-off will run,
# in a one-off of its own on the selected definition and project, before the dump and before alembic, for every
# command. Exit 3 from that look is "none there"; any other failure is "could not look". Both refuse, with exit 1,
# and nothing is dumped or migrated. The host checkout's migrations/ is not consulted at all.

NO_REVISIONS = f'No revision files in the data_store image ({IMAGE_MIGRATION_DIR}) — a broken or wrong image. Nothing was migrated.'
COULD_NOT_LOOK = (
    f'REFUSED: could not look for revision files in the data_store image ({IMAGE_MIGRATION_DIR}). Nothing was migrated.'
)

# Each definition in the project it runs in, plus the production definition chosen for an unclear default-project
# stack, which is where a mislabelled host would land.
GUARD_STACKS = [
    pytest.param(Stack(('pg',), {'pg': PROD_LABEL}), 'default', PROD_FILES, id='prod'),
    pytest.param(Stack(('pg',), {'pg': DEV_LABEL}), 'dev', DEV_FILES, id='dev'),
    pytest.param(Stack(('pg',), {'pg': SYSTEM_LABEL}), 'dev', TEST_FILES, id='test'),
    pytest.param(Stack(('pg',), {'pg': ''}), 'default', PROD_FILES, id='unclear, default project'),
]
GUARD_INVOCATIONS = [
    (), ('upgrade', 'head'), ('downgrade', '-1'), ('stamp', 'head'), ('current',), ('history',), ('heads',), ('check',),
]  # fmt: skip
READ_ONLY = [('current',), ('history',), ('heads',), ('check',)]
MUTATING = [(), ('upgrade', 'head'), ('downgrade', '-1'), ('stamp', 'head')]


def _invocation_id(invocation: tuple[str, ...]) -> str:
    return ' '.join(invocation) or 'no-args'


def _assert_refused_by_the_guard(
    outcome: Outcome, harness: Harness, project: str, files: tuple[str, ...], message: str
):
    """Exit 1, MESSAGE last on stderr, the look the only gate call, and nothing dumped, read back or migrated."""
    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr.splitlines()[-1] == message, outcome.explain()
    assert outcome.gate_calls() == [REVISIONS_PROBE], outcome.explain()
    assert outcome.only(*REVISIONS_PROBE).argv[: len(_head(project, files))] == _head(project, files), outcome.explain()
    assert not outcome.compose(*ALEMBIC_RUN), outcome.explain()
    assert not [c for c in outcome.calls if c.is_compose and c.rest[:1] == ('exec',)], outcome.explain()
    assert harness.backup_files() == [] and not harness.backups.exists(), outcome.explain()
    # stdout is the Running line alone (and, for check, the guard's verified block, which may precede the look).
    assert outcome.result.stdout.splitlines()[0].startswith("Running 'alembic "), outcome.explain()
    assert ALEMBIC_STDOUT not in outcome.result.stdout, outcome.explain()


def test_the_host_checkout_holds_revisions_so_the_empty_image_case_is_not_vacuous():
    """The refusals below run from this checkout, whose migrations/versions is NOT empty: the image alone decides."""
    assert list((REPO_ROOT / 'server' / 'data' / 'store' / 'migrations' / 'versions').glob('*.py'))


@pytest.mark.parametrize('invocation', GUARD_INVOCATIONS, ids=_invocation_id)
@pytest.mark.parametrize(('stack', 'project', 'files'), GUARD_STACKS)
def test_an_image_with_no_revision_files_refuses_before_the_dump_and_alembic(
    harness: Harness, stack: Stack, project: str, files: tuple[str, ...], invocation: tuple[str, ...]
):
    """The look exits 3: exit 1, the no-revisions message, no dump, no alembic, read-only commands included.

    run_status=3 too, so that reaching alembic would show as 3, never as the refusal's 1.
    """
    harness.stack(stack, project)
    harness.revisions(3)
    outcome = harness.run(*invocation, run_status=3)
    _assert_refused_by_the_guard(outcome, harness, project, files, NO_REVISIONS)


@pytest.mark.parametrize('status', [1, 2, 125, 127])
@pytest.mark.parametrize('invocation', [(), ('current',)], ids=_invocation_id)
@pytest.mark.parametrize(('stack', 'project', 'files'), GUARD_STACKS)
def test_a_look_that_fails_refuses_as_could_not_look(
    harness: Harness, stack: Stack, project: str, files: tuple[str, ...], invocation: tuple[str, ...], status: int
):
    """Any other status from the look is a failure to look, never a pass: refused with could not look."""
    harness.stack(stack, project)
    harness.revisions(status)
    outcome = harness.run(*invocation, run_status=3)
    _assert_refused_by_the_guard(outcome, harness, project, files, COULD_NOT_LOOK)
    assert NO_REVISIONS not in outcome.result.stderr, outcome.explain()


@pytest.mark.parametrize('invocation', READ_ONLY, ids=_invocation_id)
@pytest.mark.parametrize(('stack', 'project', 'files'), GUARD_STACKS)
def test_the_read_only_commands_look_too(
    harness: Harness, stack: Stack, project: str, files: tuple[str, ...], invocation: tuple[str, ...]
):
    """current, history, heads and check over an empty versions/ would print nothing and read as success: they look.

    Spelled in full: the selected definition and project, --no-deps, -T, /bin/sh, the image path. It is the only
    --entrypoint one-off a read-only command makes (no label read), and alembic runs after it.
    """
    harness.stack(stack, project)
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    probe = outcome.only(*REVISIONS_PROBE)
    assert probe.argv == (*_head(project, files), 'run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh',
                          'data_store', '-c', 'set -- /code/migrations/versions/*.py; [ -e "$1" ] || exit 3'), (
        outcome.explain()
    )  # fmt: skip
    assert [c for c in outcome.calls if '--entrypoint' in c.argv] == [probe], outcome.explain()
    assert outcome.calls.index(probe) < outcome.calls.index(outcome.alembic()), outcome.explain()


@pytest.mark.parametrize('invocation', MUTATING, ids=_invocation_id)
@pytest.mark.parametrize(('stack', 'project', 'files'), GUARD_STACKS)
def test_the_look_comes_before_the_label_read_the_dump_and_alembic(
    harness: Harness, stack: Stack, project: str, files: tuple[str, ...], invocation: tuple[str, ...]
):
    """A schema-moving command looks first, once: then the label read (production), the dump, the read-back, alembic."""
    harness.stack(stack, project)
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    rests = [c.rest for c in outcome.calls if c.is_compose]
    after = [LABEL_READ] if files == PROD_FILES else []
    order = [REVISIONS_PROBE, *after, PG_DUMP, PG_RESTORE_LIST, outcome.alembic().rest]
    assert [r for r in rests if r in order] == order, outcome.explain()


# A REFUSED `check` MUST START NO CONTAINER BEFORE THE IMAGE GUARD HAS SPOKEN. data_store has a build: section
# (docker-compose.yaml), so a `compose run` against a missing image builds it implicitly and unstamped; the guard's
# own NO SUCH IMAGE text says exactly that ("Left to proceed, compose would build this image implicitly as part of
# `run`"). The look is a `compose run`, so where it precedes verify_comparison_image a missing image is built, without
# its stamp, before the guard can refuse it, and the next check reads CANNOT VERIFY instead. The look therefore runs
# after the check guard and before the label read and the dump (tj-7zjov1.45, fixed at 84fc53e); this pins that order.
@pytest.mark.parametrize('image_mode', ['missing', 'unstamped', 'stale', 'match'])
@pytest.mark.parametrize(('stack', 'project', 'files'), GUARD_STACKS)
def test_check_starts_no_container_before_the_image_guard(
    harness: Harness, stack: Stack, project: str, files: tuple[str, ...], image_mode: str
):
    """Refused: no `compose run` at all. Verified: the look and alembic only after the last image inspect."""
    harness.stack(stack, project)
    harness.image_mode(image_mode)
    outcome = harness.run('check')

    if image_mode != 'match':
        assert outcome.result.returncode == 1, outcome.explain()
        assert not outcome.compose('run'), outcome.explain()
        return
    assert outcome.result.returncode == 0, outcome.explain()
    inspects = [i for i, c in enumerate(outcome.calls) if c.argv[:2] == ('image', 'inspect')]
    runs = [i for i, c in enumerate(outcome.calls) if c.is_compose and c.rest[:1] == ('run',)]
    assert inspects and runs and max(inspects) < min(runs), outcome.explain()


def _checkout_without_migrations(root: Path) -> Path:
    """The least of a checkout run_migrations.sh needs: pytest.ini, the server tree, the script.

    No migrations/ and no alembic.ini anywhere: the revisions are the image's.
    """
    root.mkdir()
    (root / 'pytest.ini').write_text('', encoding='utf-8')
    for name in ('common', 'routers', 'schemas'):
        (root / 'server' / name).mkdir(parents=True)
    script = root / 'server' / 'data' / 'store' / 'run_migrations.sh'
    script.parent.mkdir(parents=True)
    script.write_bytes(RUN_MIGRATIONS.read_bytes())
    script.chmod(0o755)
    return script


@pytest.mark.parametrize('invocation', [(), ('current',), ('history',)], ids=_invocation_id)
@pytest.mark.parametrize(('stack', 'project', 'files'), GUARD_STACKS)
def test_a_checkout_with_no_migrations_directory_still_migrates(
    harness: Harness, tmp_path: Path, stack: Stack, project: str, files: tuple[str, ...], invocation: tuple[str, ...]
):
    """P04's acceptance from this side: make migrate works with no host copy of the revisions (tj-7zjov1.45)."""
    script = _checkout_without_migrations(tmp_path / 'checkout')
    checkout = tmp_path / 'checkout'
    assert not list(checkout.rglob('migrations')) and not list(checkout.rglob('alembic.ini'))
    harness.stack(stack, project)
    outcome = harness.run(*invocation, script=script)

    assert outcome.result.returncode == 0, outcome.explain()
    assert outcome.gate_calls() == _gate(files, invocation), outcome.explain()
    assert outcome.alembic().argv == (*_head(project, files), *ALEMBIC_RUN, ALEMBIC, *_alembic_args(invocation)), (
        outcome.explain()
    )
    stdout = outcome.result.stdout.splitlines()
    assert stdout[0] == f"Running 'alembic {' '.join(_alembic_args(invocation))}' {RUNNING_REVISIONS}", (
        outcome.explain()
    )
    assert str(tmp_path / 'checkout') not in outcome.result.stdout, outcome.explain()


def test_the_running_line_names_the_image_not_the_checkout(harness: Harness):
    """The banner: the image's directory, in so many words, and no path of this checkout."""
    harness.stack(Stack(('pg',), {'pg': PROD_LABEL}), 'default')
    outcome = harness.run('current')

    assert outcome.result.stdout.splitlines()[0] == (
        "Running 'alembic current' against the revisions in the data_store image (/code/migrations/versions)"
    ), outcome.explain()
    assert str(REPO_ROOT) not in outcome.result.stdout, outcome.explain()


# ------------------------------------------------------------------------------------------------- the classifier


def _classifier_block() -> str:
    lines = SCRIPT.read_text(encoding='utf-8').splitlines()
    begin = [i for i, line in enumerate(lines) if line.startswith('# BEGIN classify_config_files')]
    end = [i for i, line in enumerate(lines) if line.startswith('# END classify_config_files')]
    assert len(begin) == 1 and len(end) == 1 and begin[0] < end[0], (
        f'{SCRIPT.name} must mark classify_config_files with one BEGIN and one END line'
    )
    return '\n'.join(lines[begin[0] : end[0] + 1])


def _classify(label: str) -> str:
    result = subprocess.run(
        [BASH, '-c', f'set -euo pipefail\n{_classifier_block()}\nclassify_config_files "$1"', 'classify', label],
        capture_output=True,
        text=True,
        check=False,
        env={'PATH': os.environ['PATH']},
    )
    assert result.returncode == 0 and result.stderr == '', result
    return result.stdout


@pytest.mark.parametrize(
    ('label', 'verdict'),
    [
        (DEV_LABEL, 'DEV'),
        (DEV_LABEL_DEVCONTAINER, 'DEV'),
        (BARE_UP_LABEL, 'DEV'),
        (f'/{OVERRIDE}', 'DEV'),
        (_label(HOST, OVERRIDE, BASE), 'DEV'),
        # TEST: the fake by exact file name, the override absent, under any root and in any position
        (SYSTEM_LABEL, 'TEST'),
        (SYSTEM_LABEL_DEVCONTAINER, 'TEST'),
        (f'/{FAKE}', 'TEST'),
        (_label(HOST, FAKE), 'TEST'),
        (_label(HOST, FAKE, BASE), 'TEST'),
        (_label(HOST, BASE, FAKE, 'docker-compose.test-client.yaml'), 'TEST'),
        # both files: no target launches the pair; UNKNOWN, so production
        (BOTH_LABEL, f'UNKNOWN: {REASON_BOTH}'),
        (_label(HOST, FAKE, OVERRIDE), f'UNKNOWN: {REASON_BOTH}'),
        (_label(DEVCONTAINER, OVERRIDE, BASE, FAKE), f'UNKNOWN: {REASON_BOTH}'),
        (f'{HOST}/{OVERRIDE},{DEVCONTAINER}/{FAKE}', f'UNKNOWN: {REASON_BOTH}'),
        # fake near misses: PROD
        (_label(HOST, BASE, FAKE + '.bak'), 'PROD'),
        (_label(HOST, BASE, 'other-' + FAKE), 'PROD'),
        (_label(HOST, BASE, 'docker-compose.fake.yml'), 'PROD'),
        (_label(HOST, BASE, FAKE.upper()), 'PROD'),
        (_label(HOST, BASE, f'{FAKE}/{BASE}'), 'PROD'),
        # a near-miss fake beside the real override is still DEV; a near-miss override beside the real fake is TEST
        (_label(HOST, BASE, OVERRIDE, FAKE + '.bak'), 'DEV'),
        (_label(HOST, BASE, OVERRIDE + '.bak', FAKE), 'TEST'),
        # malformed labels naming the fake: UNKNOWN as malformed, never TEST
        (',' + SYSTEM_LABEL, 'UNKNOWN: ' + _malformed(',' + SYSTEM_LABEL)),
        (SYSTEM_LABEL + ',', 'UNKNOWN: ' + _malformed(SYSTEM_LABEL + ',')),
        (FAKE, 'UNKNOWN: ' + _malformed(FAKE)),
        (f'{HOST}/{BASE},{FAKE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE},{FAKE}')),
        (f'{HOST}/{BASE}\n{HOST}/{FAKE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE}\n{HOST}/{FAKE}')),
        # PROD: neither file name present
        (PROD_LABEL, 'PROD'),
        (f'{HOST}/{BASE}', 'PROD'),
        (f'{HOST}/docker-compose.web.yaml', 'PROD'),
        (_label(HOST, BASE, OVERRIDE + '.bak'), 'PROD'),
        (_label(HOST, BASE, 'other-' + OVERRIDE), 'PROD'),
        (_label(HOST, BASE, 'my-' + OVERRIDE), 'PROD'),
        (_label(HOST, BASE, 'docker-compose.override.yml'), 'PROD'),
        (_label(HOST, BASE, OVERRIDE.upper()), 'PROD'),
        (_label(HOST, BASE, f'{OVERRIDE}/{BASE}'), 'PROD'),
        (f'{HOST}/{BASE},{HOST}/', 'PROD'),
        ('', f'UNKNOWN: {REASON_NO_LABEL}'),
        (',' + DEV_LABEL, 'UNKNOWN: ' + _malformed(',' + DEV_LABEL)),
        (DEV_LABEL + ',', 'UNKNOWN: ' + _malformed(DEV_LABEL + ',')),
        (f'{HOST}/{BASE},,{HOST}/{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE},,{HOST}/{OVERRIDE}')),
        (',', 'UNKNOWN: ' + _malformed(',')),
        (OVERRIDE, 'UNKNOWN: ' + _malformed(OVERRIDE)),
        (f'{HOST}/{BASE},{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE},{OVERRIDE}')),
        (f'./{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'./{OVERRIDE}')),
        (f'{HOST}/{BASE}, {HOST}/{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE}, {HOST}/{OVERRIDE}')),
        (f'{HOST}/{BASE}\n{HOST}/{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE}\n{HOST}/{OVERRIDE}')),
    ],
)
def test_the_classifier_reads_the_label_by_file_name_and_fails_closed(label: str, verdict: str):
    assert _classify(label) == verdict + '\n'
