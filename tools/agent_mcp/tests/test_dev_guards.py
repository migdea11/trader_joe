"""The dev lifecycle's checks (tj-iee6sn.6; ADR tj-4rr0la addendum 23; owner rulings D5-D7 on tj-iee6sn.1).

The verbs (B4 down/restart, B5 build, B6 up, B7 migrate, B8 tools) come with
their own beads and their own tests; B4's, which call the guards in order, are test_dev_label_verbs.py.
What is pinned here is each check on its own, so that gutting one turns ITS tests red (addendum 19 (A)):

  * check_dev_lifecycle_argv: one whole row of a fixed table under its own prefix, and the
    forbidden words named in every spelling;
  * the mode guard (D7 generalised, tj-iee6sn.24): its one `docker ps` over the non-one-off
    containers, and the DEV/TEST/PROD classification of its output, which must agree with
    run_migrations.sh's classify_config_files as the script itself answers, fake-file labels included;
  * the two TEST-mode file sets and the rows only they accept (tj-iee6sn.24);
  * the D6 symlink check over the bind sources parsed from the trusted dev files;
  * the D5 env-file preflight, which lstats and never opens;
  * the pinned and placeholder step environments, derived from the compose files' own defaults;
  * the S6 stale-copy refusal; the S10 budget;
  * Step.env: it reaches the subprocess and nothing that is recorded or audited.

Expected values are literals or are parsed from the files they mirror, never read from stack.py.
"""

import asyncio
import inspect
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 -- a fixed bash invocation of the repository's own classifier block
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from common.tests import compose_model
from common.tests.test_ci_invariants import _make_recipe, _make_variable
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import REPO_ROOT, SERVER_ROOT, make_rig
from tools.agent_mcp.tests.test_commands import EXPECTED_DEV_BAKES, _baked_into_trusted


pytestmark = pytest.mark.build_infra

EXPECTED_DOCKER = '/usr/local/bin/docker'
EXPECTED_PROJECT = 'trader_joe'
EXPECTED_TRUSTED_DIR = '/opt/agent_mcp/compose'
EXPECTED_DEV_COMPOSE_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
    'docker-compose.agent-dev.yaml',
)
HOST = Path('/home/someone/repos/trader_joe')
LABEL_PREFIX = [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_PROJECT]
FILE_PREFIX = [
    *LABEL_PREFIX,
    '--project-directory',
    str(HOST),
    *(word for name in EXPECTED_DEV_COMPOSE_FILES for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')),
]

# tools' prefix (tj-iee6sn.14): the dev files with the trusted docker-compose.tools.yaml inserted
# BEFORE the MCP-only overlay, which stays last (the Makefile's TOOLS_COMPOSE plus the overlay).
EXPECTED_TOOLS_COMPOSE_FILES = (
    *EXPECTED_DEV_COMPOSE_FILES[:-1],
    'docker-compose.tools.yaml',
    EXPECTED_DEV_COMPOSE_FILES[-1],
)
TOOLS_PREFIX = [
    *LABEL_PREFIX,
    '--project-directory',
    str(HOST),
    *(word for name in EXPECTED_TOOLS_COMPOSE_FILES for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')),
]

# TEST mode's two sets (tj-iee6sn.24; ADR tj-4rr0la addendum 27): test_up's is the Makefile's SYSTEM_COMPOSE
# (base, then the fake overlay that names the :test tags); test_build's adds the MCP-only overlay LAST.
EXPECTED_TEST_COMPOSE_FILES = ('docker-compose.yaml', 'docker-compose.fake.yaml')
EXPECTED_TEST_BUILD_COMPOSE_FILES = (*EXPECTED_TEST_COMPOSE_FILES, 'docker-compose.agent-test.yaml')
TEST_PREFIX = [
    *LABEL_PREFIX,
    '--project-directory',
    str(HOST),
    *(word for name in EXPECTED_TEST_COMPOSE_FILES for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')),
]
TEST_BUILD_PREFIX = [
    *LABEL_PREFIX,
    '--project-directory',
    str(HOST),
    *(word for name in EXPECTED_TEST_BUILD_COMPOSE_FILES for word in ('-f', f'{EXPECTED_TRUSTED_DIR}/{name}')),
]

LABEL = 'label-only'
FILE = 'file-based'
TOOLS = 'file-based+tools'
TEST = 'file-based+test'
TEST_BUILD = 'file-based+test-build'
KINDS = (LABEL, FILE, TOOLS, TEST, TEST_BUILD)
# make test-build's build and make system-launch's up, word for word (no service names on the up).
TEST_BUILD_TAIL = ('build', 'data_store', 'data_ingest')
TEST_UP_TAIL = ('up', '-d', '--wait', '--wait-timeout', '300')
ALEMBIC = '/code/.venv/bin/alembic'
UP_TAIL = ('up', '-d', '--wait', '--wait-timeout', '300', 'postgres', 'data_store', 'data_ingest', 'web')
# make dev-tools' `up -d pgadmin` plus --wait, the one documented deviation (the 18:37 readiness note (2)).
TOOLS_TAIL = ('up', '-d', '--wait', 'pgadmin')
# Every command the dev lifecycle may run, whole, with the prefix it runs under (addendum 23 (2); the
# 22:05/22:08 notes on the bead: label-only down, label-only restart of the three apps, the file-based
# build, the up of make dev-up, and make migrate's alembic run; tj-iee6sn.10 removed make migrate-check's).
MIGRATE_TAIL = ('run', '--rm', '--no-deps', 'data_store', ALEMBIC, 'upgrade', 'head')
EXPECTED_ROWS = {
    ('down',): LABEL,
    ('restart', 'data_store'): LABEL,
    ('restart', 'data_ingest'): LABEL,
    ('restart', 'web'): LABEL,
    ('build', 'data_store', 'data_ingest', 'web'): FILE,
    UP_TAIL: FILE,
    MIGRATE_TAIL: FILE,
    # tj-iee6sn.14: make dev-tools' up of pgAdmin, under the tools prefix and no other.
    TOOLS_TAIL: TOOLS,
    # tj-iee6sn.24: make test-build's build ONLY under the test build set (the one set that can build
    # :test and nothing else), make system-launch's up ONLY under the test set.
    TEST_BUILD_TAIL: TEST_BUILD,
    TEST_UP_TAIL: TEST,
}
ROW_IDS = [' '.join(tail[:2]) + (' ...' if len(tail) > 2 else '') for tail in EXPECTED_ROWS]
# tj-iee6sn.10: the SAME migrate words are also accepted under the TEST set (make migrate's TEST definition:
# base + docker-compose.fake.yaml), from a second table, so one tail is accepted under exactly two prefixes.
EXPECTED_TEST_ONEOFF_ROWS = {MIGRATE_TAIL: TEST}


def _accepted_kinds(tail) -> list[str]:
    """The kinds TAIL must be accepted under, in KINDS order: its row's, plus its TEST one-off row's."""
    wanted = {EXPECTED_ROWS[tail], EXPECTED_TEST_ONEOFF_ROWS.get(tail)}
    return [kind for kind in KINDS if kind in wanted]


EXPECTED_FORBIDDEN = {
    '-v',
    '-V',
    '--volumes',
    '--renew-anon-volumes',
    '--rmi',
    '--remove-orphans',
    'prune',
    '-f',
    '--file',
    '-p',
    '--project-name',
    '--project-directory',
    '--env-file',
    '--profile',
}


def _argv(tail, kind: str = LABEL) -> list[str]:
    prefixes = {
        LABEL: LABEL_PREFIX,
        FILE: FILE_PREFIX,
        TOOLS: TOOLS_PREFIX,
        TEST: TEST_PREFIX,
        TEST_BUILD: TEST_BUILD_PREFIX,
    }
    return [*prefixes[kind], *tail]


def _check(argv, host: Path | None = HOST) -> None:
    stack.check_dev_lifecycle_argv(argv, host)


# ===============================================================================================
# THE ARGV TABLE
# ===============================================================================================


def test_the_table_holds_exactly_these_rows_each_with_its_kind():
    """Membership: a row added (stop, rm, a restart of postgres) or a kind swapped is red here."""
    kinds = {
        stack._DEV_LABEL_ONLY: LABEL,
        stack._DEV_FILE_BASED: FILE,
        stack._DEV_TOOLS_FILE_BASED: TOOLS,
        stack._TEST_FILE_BASED: TEST,
        stack._TEST_BUILD_FILE_BASED: TEST_BUILD,
    }
    assert {tail: kinds[kind] for tail, kind in stack.DEV_LIFECYCLE_COMMANDS.items()} == EXPECTED_ROWS
    assert {tail: kinds[kind] for tail, kind in stack.TEST_ONEOFF_COMMANDS.items()} == EXPECTED_TEST_ONEOFF_ROWS
    assert (
        stack._DEV_LABEL_ONLY,
        stack._DEV_FILE_BASED,
        stack._DEV_TOOLS_FILE_BASED,
        stack._TEST_FILE_BASED,
        stack._TEST_BUILD_FILE_BASED,
    ) == KINDS


def test_the_forbidden_set_is_exactly_the_data_losing_and_steering_words():
    assert set(stack.DEV_LIFECYCLE_FORBIDDEN) == EXPECTED_FORBIDDEN


@pytest.mark.parametrize(('tail', 'kind'), list(EXPECTED_ROWS.items()), ids=ROW_IDS)
def test_each_row_is_accepted_under_its_own_prefix(tail, kind):
    _check(_argv(tail, kind))
    _check(tuple(_argv(tail, kind)))


@pytest.mark.parametrize(('tail', 'kind'), list(EXPECTED_ROWS.items()), ids=ROW_IDS)
def test_each_row_is_refused_under_every_other_prefix(tail, kind):
    """Wrong kind, every way, for each row.

    A label-only down never loads files, a file-based up never runs without them, the pgAdmin up never
    runs without the tools file, and no other row runs with it (tj-iee6sn.14). The migrate one-off is the one
    tail with a second accepted prefix, the TEST set (tj-iee6sn.10); every other prefix still refuses it.
    """
    for other in KINDS:
        if other not in _accepted_kinds(tail):
            with pytest.raises(stack.Refused, match='one of its fixed commands'):
                _check(_argv(tail, other))


def test_every_row_is_accepted_exactly_once_over_the_three_prefixes():
    """Each tail is accepted under one prefix and only one -- the migrate one-off under DEV's and TEST's."""
    for tail in EXPECTED_ROWS:
        accepted = []
        for kind in KINDS:
            try:
                _check(_argv(tail, kind))
                accepted.append(kind)
            except stack.Refused:
                pass
        assert accepted == _accepted_kinds(tail), (tail, accepted)
    assert _accepted_kinds(MIGRATE_TAIL) == [FILE, TEST], 'non-vacuity: the one tail with two prefixes'


@pytest.mark.parametrize(
    'tail',
    [TEST_UP_TAIL, TEST_BUILD_TAIL, UP_TAIL, TOOLS_TAIL, ('build', 'data_store', 'data_ingest', 'web')],
    ids=['test up', 'test build', 'dev up', 'tools up', 'dev build'],
)
def test_the_test_oneoff_table_admits_no_other_row_under_the_test_prefix(tail):
    """TEST_ONEOFF_COMMANDS widens the TEST prefix by the migrate words alone: a DEV row stays refused under it.

    And the TEST up row stays refused under the dev prefix: the second table is consulted for the TEST kind only.
    """
    accepted = [kind for kind in KINDS if _try(_argv(tail, kind))]
    assert accepted == [EXPECTED_ROWS[tail]], (tail, accepted)


def _try(argv) -> bool:
    try:
        _check(argv)
    except stack.Refused:
        return False
    return True


@pytest.mark.parametrize(
    'tail',
    [
        (*MIGRATE_TAIL[:-1], 'heads'),
        (*MIGRATE_TAIL[:-2], 'check'),
        (*MIGRATE_TAIL[:-2], 'downgrade', 'base'),
        (*MIGRATE_TAIL, '--sql'),
        ('run', '--rm', '--no-deps', '--build', 'data_store', ALEMBIC, 'upgrade', 'head'),
        ('run', '--rm', 'data_store', ALEMBIC, 'upgrade', 'head'),
        ('run', '--rm', '--no-deps', 'data_ingest', ALEMBIC, 'upgrade', 'head'),
    ],
    ids=['heads', 'check', 'downgrade', '--sql', '--build', 'no --no-deps', 'data_ingest'],
)
def test_no_neighbour_of_the_migrate_words_is_accepted_under_any_prefix(tail):
    assert not [kind for kind in KINDS if _try(_argv(tail, kind))], tail


# Every spelling of every forbidden word: separated, `=` attached, short attached, short cluster.
_SPELLINGS = {
    'separated -v': ['-v'],
    'separated -V': ['-V'],
    'separated --volumes': ['--volumes'],
    'separated --renew-anon-volumes': ['--renew-anon-volumes'],
    'separated --rmi': ['--rmi', 'all'],
    'separated --remove-orphans': ['--remove-orphans'],
    'separated prune': ['prune'],
    'separated -f': ['-f', 'docker-compose.yaml'],
    'separated --file': ['--file', 'docker-compose.yaml'],
    'separated -p': ['-p', 'other'],
    'separated --project-name': ['--project-name', 'other'],
    'separated --project-directory': ['--project-directory', '/tmp'],
    'separated --env-file': ['--env-file', '/agent_mcp_share/token'],
    'separated --profile': ['--profile', 'tools'],
    'attached --volumes=true': ['--volumes=true'],
    'attached --volumes=false': ['--volumes=false'],
    'attached --renew-anon-volumes=true': ['--renew-anon-volumes=true'],
    'attached --rmi=all': ['--rmi=all'],
    'attached --rmi=local': ['--rmi=local'],
    'attached --remove-orphans=true': ['--remove-orphans=true'],
    'attached -v=true': ['-v=true'],
    'attached --file=x': ['--file=docker-compose.yaml'],
    'attached --project-name=x': ['--project-name=other'],
    'attached --project-directory=x': ['--project-directory=/tmp'],
    'attached --env-file=x': ['--env-file=/agent_mcp_share/token'],
    'attached --profile=x': ['--profile=tools'],
    'short attached -fx': ['-fx'],
    'short attached -pfoo': ['-pfoo'],
    'short cluster -dv': ['-dv'],
    'short cluster -vd': ['-vd'],
    'short cluster -dV': ['-dV'],
    'short cluster -tv': ['-tv'],
}


@pytest.mark.parametrize('position', ['before', 'after'])
@pytest.mark.parametrize('spelling', list(_SPELLINGS.values()), ids=list(_SPELLINGS))
@pytest.mark.parametrize(('tail', 'kind'), list(EXPECTED_ROWS.items()), ids=ROW_IDS)
def test_a_forbidden_word_in_any_spelling_is_refused_and_named(tail, kind, spelling, position):
    """Anywhere after the prefix, every row: refused by the forbidden scan, which names the word.

    The table would refuse these anyway (none is a row); matching the scan's own message is what keeps
    the scan's naming honest, so gutting it is red here and nowhere else.
    """
    words = [*spelling, *tail] if position == 'before' else [*tail, *spelling]
    if kind == LABEL and words[0] == '--project-directory':
        # Straight after the label prefix it reads as the start of a FILE prefix, which then does not match.
        with pytest.raises(stack.Refused, match='names exactly the trusted dev compose files'):
            _check(_argv(words, kind))
        return
    with pytest.raises(stack.Refused, match='removes no volume, image or orphan') as refused:
        _check(_argv(words, kind))
    assert spelling[0] in str(refused.value), refused.value


# Tails with no forbidden word that are still not a row: the TABLE is the only thing refusing these.
_NOT_A_ROW = {
    'up without -d': ('up', '--wait', '--wait-timeout', '300', 'postgres', 'data_store', 'data_ingest', 'web'),
    'up without postgres': ('up', '-d', '--wait', '--wait-timeout', '300', 'data_store', 'data_ingest', 'web'),
    'up with pgadmin': (*UP_TAIL, 'pgadmin'),
    'up with --no-deps': (*UP_TAIL[:5], '--no-deps', *UP_TAIL[5:]),
    'up reordered services': (
        'up',
        '-d',
        '--wait',
        '--wait-timeout',
        '300',
        'web',
        'postgres',
        'data_store',
        'data_ingest',
    ),
    'up reordered flags': (
        'up',
        '--wait',
        '-d',
        '--wait-timeout',
        '300',
        'postgres',
        'data_store',
        'data_ingest',
        'web',
    ),
    'up another timeout': (
        'up',
        '-d',
        '--wait',
        '--wait-timeout',
        '3000',
        'postgres',
        'data_store',
        'data_ingest',
        'web',
    ),
    'up --build': (*UP_TAIL[:1], '--build', *UP_TAIL[1:]),
    'up --force-recreate': (*UP_TAIL[:1], '--force-recreate', *UP_TAIL[1:]),
    'up duplicated word': (*UP_TAIL, 'web'),
    'bare up': ('up',),
    'restart postgres': ('restart', 'postgres'),
    'restart pgadmin': ('restart', 'pgadmin'),
    'restart all': ('restart',),
    'restart two': ('restart', 'data_store', 'web'),
    'stop': ('stop',),
    'start': ('start',),
    'rm': ('rm',),
    'rm --stop --force': ('rm', '--stop', '--force'),
    'kill': ('kill',),
    'config': ('config',),
    'exec': ('exec', 'data_store', 'sh'),
    'down with a timeout': ('down', '--timeout', '1'),
    'down twice': ('down', 'down'),
    'build one service': ('build', 'data_store'),
    'build --no-cache': ('build', '--no-cache', 'data_store', 'data_ingest', 'web'),
    'run another program': ('run', '--rm', '--no-deps', 'data_store', 'sh'),
    'run alembic downgrade': ('run', '--rm', '--no-deps', 'data_store', ALEMBIC, 'downgrade', 'base'),
    'run without --rm': ('run', '--no-deps', 'data_store', ALEMBIC, 'upgrade', 'head'),
    'run with deps': ('run', '--rm', 'data_store', ALEMBIC, 'upgrade', 'head'),
    'run on ingest': ('run', '--rm', '--no-deps', 'data_ingest', ALEMBIC, 'upgrade', 'head'),
    'empty tail': (),
    # tj-iee6sn.14: the pgAdmin up, exactly, and nothing near it.
    "make's own up -d pgadmin, no --wait": ('up', '-d', 'pgadmin'),
    'pgadmin with --no-deps': ('up', '-d', '--wait', '--no-deps', 'pgadmin'),
    'pgadmin and postgres': (*TOOLS_TAIL, 'postgres'),
    'postgres and pgadmin': ('up', '-d', '--wait', 'postgres', 'pgadmin'),
    'pgadmin with a wait timeout': ('up', '-d', '--wait', '--wait-timeout', '300', 'pgadmin'),
    'pgadmin --force-recreate': ('up', '-d', '--wait', '--force-recreate', 'pgadmin'),
    'pgadmin --build': ('up', '-d', '--wait', '--build', 'pgadmin'),
    'pgadmin reordered flags': ('up', '--wait', '-d', 'pgadmin'),
    'pgadmin attached, no -d': ('up', '--wait', 'pgadmin'),
    'stop pgadmin': ('stop', 'pgadmin'),
    'rm pgadmin': ('rm', '-s', 'pgadmin'),
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('tail', list(_NOT_A_ROW.values()), ids=list(_NOT_A_ROW))
def test_a_tail_that_is_not_a_whole_row_is_refused(tail, kind):
    with pytest.raises(stack.Refused, match='one of its fixed commands'):
        _check(_argv(tail, kind))


def test_a_file_based_argv_without_the_host_path_is_refused():
    """The file prefix cannot be checked without the path it names, so it is never accepted unchecked."""
    with pytest.raises(stack.Refused, match='needs the repository host path'):
        _check(_argv(UP_TAIL, FILE), None)


def test_a_label_only_row_needs_no_host_path():
    _check(_argv(('down',)), None)


def test_the_tools_row_without_the_host_path_is_refused():
    with pytest.raises(stack.Refused, match='needs the repository host path'):
        _check(_argv(TOOLS_TAIL, TOOLS), None)


_TOOLS_INDEX = TOOLS_PREFIX.index(f'{EXPECTED_TRUSTED_DIR}/docker-compose.tools.yaml')
# The tools prefix with its own file moved, doubled or swapped: never the tools row (tj-iee6sn.14).
_BAD_TOOLS_PREFIXES = {
    'the tools file after the overlay': [
        *TOOLS_PREFIX[: _TOOLS_INDEX - 1],
        *TOOLS_PREFIX[_TOOLS_INDEX + 1 :],
        '-f',
        TOOLS_PREFIX[_TOOLS_INDEX],
    ],
    'the tools file first': [*TOOLS_PREFIX[:6], *TOOLS_PREFIX[_TOOLS_INDEX - 1 : _TOOLS_INDEX + 1], *FILE_PREFIX[6:]],
    "the checkout's tools file": [
        *TOOLS_PREFIX[:_TOOLS_INDEX],
        f'{HOST}/docker-compose.tools.yaml',
        *TOOLS_PREFIX[_TOOLS_INDEX + 1 :],
    ],
    'the tools file twice': [*TOOLS_PREFIX[: _TOOLS_INDEX + 1], *TOOLS_PREFIX[_TOOLS_INDEX - 1 :]],
    'the overlay missing': TOOLS_PREFIX[:-2],
    'another host path': [*LABEL_PREFIX, '--project-directory', '/srv/other', *TOOLS_PREFIX[6:]],
}


@pytest.mark.parametrize('prefix', list(_BAD_TOOLS_PREFIXES.values()), ids=list(_BAD_TOOLS_PREFIXES))
def test_the_pgadmin_up_under_any_other_file_list_is_refused(prefix):
    assert prefix != TOOLS_PREFIX and prefix != FILE_PREFIX
    with pytest.raises(stack.Refused):
        _check([*prefix, *TOOLS_TAIL])


def test_the_tools_prefix_is_the_dev_one_with_the_tools_file_before_the_overlay():
    """stack.dev_tools_prefix, spelled from literals; and DEV_TOOLS_COMPOSE_FILES is the first four, tools, overlay."""
    assert stack.dev_tools_prefix(HOST) == TOOLS_PREFIX
    assert stack.DEV_TOOLS_COMPOSE_FILES == EXPECTED_TOOLS_COMPOSE_FILES
    assert stack.DEV_TOOLS_COMPOSE_FILES[:4] == EXPECTED_DEV_COMPOSE_FILES[:4]
    assert stack.DEV_TOOLS_COMPOSE_FILES[-1] == 'docker-compose.agent-dev.yaml'
    # dev_file_prefix is untouched: no other verb loads the tools file (its `:?` guards would tax them).
    assert stack.dev_file_prefix(HOST) == FILE_PREFIX
    assert 'docker-compose.tools.yaml' not in stack.DEV_COMPOSE_FILES


def _trusted(*names: str) -> list[str]:
    return [
        *LABEL_PREFIX,
        '--project-directory',
        str(HOST),
        *(w for n in names for w in ('-f', f'{EXPECTED_TRUSTED_DIR}/{n}')),
    ]


# Every near miss of the two TEST sets (tj-iee6sn.24 TESTS: "the lifecycle check accepts the build row
# ONLY under that file set (refuses it under the base file alone so :latest cannot be built, and refuses
# `up` under the build set)"). The base file alone names :latest (docker-compose.yaml l.133, l.222).
_BAD_TEST_PREFIXES = {
    'the base file alone (would build :latest)': _trusted('docker-compose.yaml'),
    'the base and the test overlay, no fake file (would build :latest)': _trusted(
        'docker-compose.yaml', 'docker-compose.agent-test.yaml'
    ),
    'the fake file first': _trusted('docker-compose.fake.yaml', 'docker-compose.yaml'),
    'the test overlay before the fake file': _trusted(
        'docker-compose.yaml', 'docker-compose.agent-test.yaml', 'docker-compose.fake.yaml'
    ),
    'the fake file twice': _trusted('docker-compose.yaml', 'docker-compose.fake.yaml', 'docker-compose.fake.yaml'),
    'the override added': _trusted('docker-compose.yaml', 'docker-compose.override.yaml', 'docker-compose.fake.yaml'),
    'the dev overlay in place of the test one': _trusted(
        'docker-compose.yaml', 'docker-compose.fake.yaml', 'docker-compose.agent-dev.yaml'
    ),
    "the checkout's fake file": [*TEST_PREFIX[:-1], f'{HOST}/docker-compose.fake.yaml'],
    "the checkout's test overlay": [*TEST_BUILD_PREFIX[:-1], f'{HOST}/docker-compose.agent-test.yaml'],
    'another host path': [*LABEL_PREFIX, '--project-directory', '/srv/other', *TEST_BUILD_PREFIX[6:]],
}


@pytest.mark.parametrize('tail', [TEST_BUILD_TAIL, TEST_UP_TAIL], ids=['test build', 'test up'])
@pytest.mark.parametrize('prefix', list(_BAD_TEST_PREFIXES.values()), ids=list(_BAD_TEST_PREFIXES))
def test_the_test_rows_under_any_other_file_list_are_refused(prefix, tail):
    assert prefix not in (TEST_PREFIX, TEST_BUILD_PREFIX)
    with pytest.raises(stack.Refused):
        _check([*prefix, *tail])


def test_the_test_build_row_is_refused_under_the_test_up_set_and_the_up_under_the_build_set():
    """Spelled out beside the sweep: neither TEST row runs under the other TEST set, nor the build under dev's.

    A build without the overlay would read the checkout's Dockerfile; the up never runs with the build
    overlay (test_up never builds).
    """
    with pytest.raises(stack.Refused, match='one of its fixed commands'):
        _check([*TEST_PREFIX, *TEST_BUILD_TAIL])
    with pytest.raises(stack.Refused, match='one of its fixed commands'):
        _check([*TEST_BUILD_PREFIX, *TEST_UP_TAIL])
    with pytest.raises(stack.Refused, match='one of its fixed commands'):
        _check([*FILE_PREFIX, *TEST_BUILD_TAIL])


def test_the_test_prefixes_are_the_makefiles_system_compose_plus_the_overlay():
    """stack.testmode_prefix / testmode_build_prefix from literals; the sets agree with SYSTEM_COMPOSE.

    The Makefile's SYSTEM_COMPOSE -f list (the overlay's own header says a validator pins this), with
    the MCP-only overlay unknown to the Makefile.
    """
    assert stack.testmode_prefix(HOST) == TEST_PREFIX
    assert stack.testmode_build_prefix(HOST) == TEST_BUILD_PREFIX
    assert stack.TEST_COMPOSE_FILES == EXPECTED_TEST_COMPOSE_FILES
    assert stack.TEST_BUILD_COMPOSE_FILES == EXPECTED_TEST_BUILD_COMPOSE_FILES
    words = shlex.split(_make_variable('SYSTEM_COMPOSE'))
    files = tuple(value for flag, value in itertools.pairwise(words) if flag in ('-f', '--file'))
    assert files == EXPECTED_TEST_COMPOSE_FILES, words
    assert 'docker-compose.agent-test.yaml' not in (REPO_ROOT / 'Makefile').read_text(encoding='utf-8')


def test_the_agent_test_overlay_sets_only_build_dockerfile_for_the_two_test_services():
    """tj-iee6sn.24 WHAT (3): build.dockerfile (the trusted Dockerfile) for data_store and data_ingest only.

    Nothing else: no image tag (the fake overlay names :test), no mount, no network, no environment, no web.
    """
    model = compose_model.load(REPO_ROOT / 'docker-compose.agent-test.yaml')
    assert set(model) == {'services'}, model
    assert model['services'] == {
        'data_store': {'build': {'dockerfile': f'{EXPECTED_TRUSTED_DIR}/Dockerfile'}},
        'data_ingest': {'build': {'dockerfile': f'{EXPECTED_TRUSTED_DIR}/Dockerfile'}},
    }
    # The services it names are exactly the ones make test-build builds (its recipe's last words).
    recipe = _make_recipe('test-build')
    assert len(recipe) == 1 and recipe[0].endswith('$(SYSTEM_COMPOSE) build data_store data_ingest'), recipe
    assert tuple(model['services']) == tuple(recipe[0].split()[-2:]) == TEST_BUILD_TAIL[1:]


def test_a_project_dev_tools_started_still_passes_the_prod_guard():
    """The config_files label of a container tools started classifies DEV.

    pgAdmin's container carries the tools -f list as that label; were it not DEV, the next dev verb
    after a tools would refuse the project as a prod launch.
    """
    label = ','.join(f'{EXPECTED_TRUSTED_DIR}/{name}' for name in EXPECTED_TOOLS_COMPOSE_FILES)
    assert stack.classify_config_files(label) == 'DEV'
    assert stack.check_mode_guard(0, f'{label}\n'.encode()) == 'DEV'


_BAD_FILE_PREFIXES = {
    'another host path': [*LABEL_PREFIX, '--project-directory', '/srv/other', *FILE_PREFIX[6:]],
    "the checkout's own compose file": [*FILE_PREFIX[:7], f'{HOST}/docker-compose.yaml', *FILE_PREFIX[8:]],
    'one -f missing': FILE_PREFIX[:-2],
    'one -f extra': [*FILE_PREFIX, '-f', f'{EXPECTED_TRUSTED_DIR}/docker-compose.tools.yaml'],
    'files reordered': [*FILE_PREFIX[:6], *FILE_PREFIX[8:10], *FILE_PREFIX[6:8], *FILE_PREFIX[10:]],
}


@pytest.mark.parametrize('prefix', list(_BAD_FILE_PREFIXES.values()), ids=list(_BAD_FILE_PREFIXES))
def test_a_file_prefix_other_than_the_trusted_one_is_refused(prefix):
    with pytest.raises(stack.Refused):
        _check([*prefix, *UP_TAIL])


_BAD_LABEL_PREFIXES = {
    'the agent stack project': [EXPECTED_DOCKER, 'compose', '-p', 'trader_joe_agent_stack'],
    'attached project': [EXPECTED_DOCKER, 'compose', f'-p{EXPECTED_PROJECT}'],
    '= attached project': [EXPECTED_DOCKER, 'compose', f'--project-name={EXPECTED_PROJECT}'],
    'no project': [EXPECTED_DOCKER, 'compose'],
    'another docker': ['docker', 'compose', '-p', EXPECTED_PROJECT],
    'compose v1': ['/usr/local/bin/docker-compose', '-p', EXPECTED_PROJECT],
    'nothing': [],
}


@pytest.mark.parametrize('prefix', list(_BAD_LABEL_PREFIXES.values()), ids=list(_BAD_LABEL_PREFIXES))
def test_a_wrong_project_or_program_is_refused(prefix):
    with pytest.raises(stack.Refused):
        _check([*prefix, 'down'])


# ===============================================================================================
# THE MODE GUARD (D7 generalised; ADR tj-4rr0la addenda 26 (2) and 27 (1); tj-iee6sn.24 WHAT (1))
# ===============================================================================================

EXPECTED_GUARD_ARGV = (
    EXPECTED_DOCKER,
    'ps',
    '-a',
    '--filter',
    f'label=com.docker.compose.project={EXPECTED_PROJECT}',
    # Addendum 26 (2): one-off containers (oneoff=True: alembic runs, test_client) are ignored by the
    # classification, as the lookups already ignore them.
    '--filter',
    'label=com.docker.compose.oneoff=False',
    '--format',
    '{{.Label "com.docker.compose.project.config_files"}}',
)


def test_the_guard_argv_is_exactly_one_docker_ps_of_the_non_one_off_containers_printing_one_label():
    """Ruling D7 on tj-iee6sn.1, with addendum 26 (2)'s one-off filter, spelled word for word."""
    assert stack.dev_prod_guard_argv() == EXPECTED_GUARD_ARGV
    step = stack.dev_prod_guard_step(Path('/stack'))
    assert step.argv == EXPECTED_GUARD_ARGV and step.cwd == Path('/stack') and step.env == ()
    assert step.builds is False


_BAD_GUARD_ARGVS = {
    'running only': EXPECTED_GUARD_ARGV[:2] + EXPECTED_GUARD_ARGV[3:],
    'another project': (*EXPECTED_GUARD_ARGV[:4], 'label=com.docker.compose.project=other', *EXPECTED_GUARD_ARGV[5:]),
    'one-offs included (no oneoff filter)': (*EXPECTED_GUARD_ARGV[:5], *EXPECTED_GUARD_ARGV[7:]),
    'one-offs only': (*EXPECTED_GUARD_ARGV[:6], 'label=com.docker.compose.oneoff=True', *EXPECTED_GUARD_ARGV[7:]),
    'another format': (*EXPECTED_GUARD_ARGV[:8], '{{.ID}}'),
    'an extra word': (*EXPECTED_GUARD_ARGV, '--no-trunc'),
    'empty': (),
}


@pytest.mark.parametrize('argv', list(_BAD_GUARD_ARGVS.values()), ids=list(_BAD_GUARD_ARGVS))
def test_the_guard_argv_check_refuses_any_other_argv(argv):
    with pytest.raises(stack.Refused, match='the mode guard is'):
        stack.check_dev_prod_guard_argv(argv)


DEV_LINE = '/r/docker-compose.yaml,/r/docker-compose.override.yaml,/r/docker-compose.web.yaml'
MCP_LINE = ','.join(f'{EXPECTED_TRUSTED_DIR}/{name}' for name in EXPECTED_DEV_COMPOSE_FILES)
TOOLS_LINE = f'{DEV_LINE},/r/docker-compose.web.dev.yaml,/r/docker-compose.tools.yaml'
PROD_LINE = '/r/docker-compose.yaml,/r/docker-compose.web.yaml'
# TEST mode (addendum 27 (1)): make system-launch's set, the MCP's test_up set (trusted copies), and
# test_build's set (the MCP-only overlay last), which a built-but-never-started container never carries
# but which must classify TEST all the same.
TEST_LINE = '/r/docker-compose.yaml,/r/docker-compose.fake.yaml'
MCP_TEST_LINE = f'{EXPECTED_TRUSTED_DIR}/docker-compose.yaml,{EXPECTED_TRUSTED_DIR}/docker-compose.fake.yaml'
MCP_TEST_BUILD_LINE = f'{MCP_TEST_LINE},{EXPECTED_TRUSTED_DIR}/docker-compose.agent-test.yaml'
# Both files: no target launches that pair, so it is not a mode at all (PROD, fail closed).
BOTH_LINE = '/r/docker-compose.yaml,/r/docker-compose.override.yaml,/r/docker-compose.fake.yaml'

EXPECTED_MODES_ANY = frozenset({'DEV', 'TEST', None})
EXPECTED_MODES_DEV_OR_EMPTY = frozenset({'DEV', None})


def test_the_mode_constants_and_the_accepted_sets_are_these_literals():
    assert (stack.MODE_DEV, stack.MODE_TEST, stack.MODE_PROD) == ('DEV', 'TEST', 'PROD')
    assert stack.MODES_ANY == EXPECTED_MODES_ANY
    assert stack.MODES_DEV_OR_EMPTY == EXPECTED_MODES_DEV_OR_EMPTY
    assert 'PROD' not in stack.MODES_ANY | stack.MODES_DEV_OR_EMPTY
    # The default accepted set is the widest one that is still never PROD.
    assert inspect.signature(stack.check_mode_guard).parameters['accepted'].default == EXPECTED_MODES_ANY


@pytest.mark.parametrize(
    ('stdout', 'mode'),
    [
        (b'', None),
        (f'{DEV_LINE}\n'.encode(), 'DEV'),
        (f'{DEV_LINE}\n{MCP_LINE}\n{TOOLS_LINE}\n'.encode(), 'DEV'),
        (f'{TEST_LINE}\n'.encode(), 'TEST'),
        (f'{TEST_LINE}\n{MCP_TEST_LINE}\n{MCP_TEST_BUILD_LINE}\n'.encode(), 'TEST'),
    ],
    ids=[
        'an empty project',
        'one dev container',
        'make, MCP and pgAdmin containers',
        'one test container',
        'make and MCP test containers',
    ],
)
def test_the_guard_returns_the_mode_of_an_empty_an_all_dev_or_an_all_test_project(stdout, mode):
    assert stack.check_mode_guard(0, stdout) == mode
    assert stack.check_mode_guard(0, stdout, EXPECTED_MODES_ANY) == mode


_GUARD_REFUSALS = {
    'one prod container': f'{PROD_LINE}\n',
    'mixed dev and prod': f'{DEV_LINE}\n{PROD_LINE}\n',
    'prod then dev': f'{PROD_LINE}\n{DEV_LINE}\n',
    'test and prod': f'{TEST_LINE}\n{PROD_LINE}\n',
    'dev, test and prod: PROD answers, not the mix': f'{DEV_LINE}\n{TEST_LINE}\n{PROD_LINE}\n',
    'both the override and the fake file': f'{BOTH_LINE}\n',
    'both files beside a dev container': f'{DEV_LINE}\n{BOTH_LINE}\n',
    'a relative entry': '/r/docker-compose.yaml,docker-compose.override.yaml\n',
    'a trailing comma': f'{DEV_LINE},\n',
    'a leading comma': f',{DEV_LINE}\n',
    'a double comma': '/r/docker-compose.yaml,,/r/docker-compose.override.yaml\n',
    'a blank line (a container with no label)': f'{DEV_LINE}\n\n{DEV_LINE}\n',
    'a blank line among test containers': f'{TEST_LINE}\n\n',
    'only a blank line': '\n',
    'no final newline': DEV_LINE,
    'no final newline, test': TEST_LINE,
    'a carriage return': '/r/docker-compose.yaml,/r/docker-compose.override.yaml\r\n',
    'the override as a directory': '/r/docker-compose.override.yaml/x.yaml\n',
    'the override with a suffix': '/r/docker-compose.override.yaml.bak\n',
    'the fake file as .yml': '/r/docker-compose.yaml,/r/docker-compose.fake.yml\n',
    'the fake file with a suffix': '/r/docker-compose.yaml,/r/docker-compose.fake.yaml.bak\n',
    'the fake file as a directory': '/r/docker-compose.yaml,/r/docker-compose.fake.yaml/x.yaml\n',
    'the fake file relative': '/r/docker-compose.yaml,docker-compose.fake.yaml\n',
}


# The PROD refusal, tj-iee6sn.24 WHAT (1)'s text exactly.
EXPECTED_GUARD_REFUSAL = (
    'project trader_joe has containers not started from the dev or test files (a prod launch?): '
    'stop them with make prod-down; tj-oxxl8a'
)
EXPECTED_MIXED_REFUSAL = (
    'project trader_joe holds dev and test containers together, so which mode is up cannot be told: '
    'stop them with make dev-down'
)


@pytest.mark.parametrize('accepted', [EXPECTED_MODES_ANY, EXPECTED_MODES_DEV_OR_EMPTY], ids=['any', 'dev or empty'])
@pytest.mark.parametrize('stdout', list(_GUARD_REFUSALS.values()), ids=list(_GUARD_REFUSALS))
def test_the_guard_refuses_any_prod_container_whatever_the_verb_accepts(stdout, accepted):
    with pytest.raises(stack.Refused) as refused:
        stack.check_mode_guard(0, stdout.encode(), accepted, 'the verb hint')
    assert str(refused.value) == EXPECTED_GUARD_REFUSAL


@pytest.mark.parametrize(
    'stdout',
    [f'{DEV_LINE}\n{TEST_LINE}\n', f'{MCP_TEST_LINE}\n{MCP_LINE}\n', f'{TOOLS_LINE}\n{TEST_LINE}\n{DEV_LINE}\n'],
    ids=['dev then test', 'test then dev (MCP files)', 'pgAdmin among them'],
)
@pytest.mark.parametrize('accepted', [EXPECTED_MODES_ANY, EXPECTED_MODES_DEV_OR_EMPTY], ids=['any', 'dev or empty'])
def test_dev_and_test_containers_together_refuse_whatever_the_verb_accepts(stdout, accepted):
    with pytest.raises(stack.Refused) as refused:
        stack.check_mode_guard(0, stdout.encode(), accepted, 'the verb hint')
    assert str(refused.value) == EXPECTED_MIXED_REFUSAL


@pytest.mark.parametrize(('stdout', 'mode'), [(b'', None), (f'{DEV_LINE}\n'.encode(), 'DEV')], ids=['empty', 'dev'])
def test_a_dev_only_verb_passes_dev_and_an_empty_project(stdout, mode):
    assert stack.check_mode_guard(0, stdout, EXPECTED_MODES_DEV_OR_EMPTY, 'test mode is up: call up first') == mode


def test_a_dev_only_verb_refuses_test_mode_with_its_own_hint():
    for line in (TEST_LINE, MCP_TEST_LINE):
        with pytest.raises(stack.Refused) as refused:
            stack.check_mode_guard(
                0, f'{line}\n'.encode(), EXPECTED_MODES_DEV_OR_EMPTY, 'test mode is up: call up first'
            )
        assert str(refused.value) == 'test mode is up: call up first'


def test_a_mode_outside_the_accepted_set_without_a_hint_names_the_mode():
    with pytest.raises(stack.Refused) as refused:
        stack.check_mode_guard(0, f'{TEST_LINE}\n'.encode(), frozenset({'DEV'}))
    assert str(refused.value) == 'TEST mode is up, which this verb does not act on'
    with pytest.raises(stack.Refused):
        stack.check_mode_guard(0, b'', frozenset({'DEV', 'TEST'}))


def test_the_guard_refuses_bytes_that_are_not_utf8():
    for stdout in (b'/r/docker-compose.override.yaml\xff\n', b'/r/docker-compose.fake.yaml\xff\n'):
        with pytest.raises(stack.Refused) as refused:
            stack.check_mode_guard(0, stdout)
        assert str(refused.value) == EXPECTED_GUARD_REFUSAL


@pytest.mark.parametrize('exit_status', [1, 125, -9, None])
def test_the_guard_refuses_when_the_listing_failed_whatever_it_printed(exit_status):
    """Fail closed: a failed `docker ps` with an empty stdout must not read as an empty project."""
    for stdout in (b'', f'{DEV_LINE}\n'.encode(), f'{TEST_LINE}\n'.encode()):
        with pytest.raises(stack.Refused, match='could not list the containers'):
            stack.check_mode_guard(exit_status, stdout)


# THE CLASSIFIER TABLE (tj-iee6sn.24 TESTS): classify_config_files is run_migrations.sh's port
# (UNKNOWN for both files), and classify_mode is what the guard acts on (UNKNOWN, malformed and
# missing all become PROD). Literal rows; the agreement with the script is the test below.
_CLASSIFIER_TABLE = {
    'dev (make)': (DEV_LINE, 'DEV', 'DEV'),
    'dev (MCP, trusted copies)': (MCP_LINE, 'DEV', 'DEV'),
    'dev with pgAdmin': (TOOLS_LINE, 'DEV', 'DEV'),
    'test (make system-launch)': (TEST_LINE, 'TEST', 'TEST'),
    'test (MCP test_up, trusted copies)': (MCP_TEST_LINE, 'TEST', 'TEST'),
    'test (MCP test_build set)': (MCP_TEST_BUILD_LINE, 'TEST', 'TEST'),
    'the fake file alone': ('/opt/agent_mcp/compose/docker-compose.fake.yaml', 'TEST', 'TEST'),
    'the fake file first': ('/r/docker-compose.fake.yaml,/r/docker-compose.yaml', 'TEST', 'TEST'),
    'both files': (BOTH_LINE, 'UNKNOWN', 'PROD'),
    'both files, fake first': ('/r/docker-compose.fake.yaml,/r/docker-compose.override.yaml', 'UNKNOWN', 'PROD'),
    'neither file (prod)': (PROD_LINE, 'PROD', 'PROD'),
    'the base file alone': ('/r/docker-compose.yaml', 'PROD', 'PROD'),
    'missing (empty label)': ('', 'UNKNOWN', 'PROD'),
    'malformed (relative)': ('docker-compose.fake.yaml', 'UNKNOWN', 'PROD'),
    'malformed (trailing comma)': (f'{TEST_LINE},', 'UNKNOWN', 'PROD'),
    'malformed (newline)': ('/r/a\n/docker-compose.fake.yaml', 'UNKNOWN', 'PROD'),
    'near miss: fake.yml': ('/r/docker-compose.yaml,/r/docker-compose.fake.yml', 'PROD', 'PROD'),
    'near miss: fake.yaml.bak': ('/r/docker-compose.yaml,/r/docker-compose.fake.yaml.bak', 'PROD', 'PROD'),
    'near miss: fake.yaml as a directory': ('/r/docker-compose.fake.yaml/x.yaml', 'PROD', 'PROD'),
    'near miss: prefixed fake': ('/r/xdocker-compose.fake.yaml', 'PROD', 'PROD'),
}


@pytest.mark.parametrize(('label', 'verdict', 'mode'), list(_CLASSIFIER_TABLE.values()), ids=list(_CLASSIFIER_TABLE))
def test_the_classifier_table(label, verdict, mode):
    assert stack.classify_config_files(label) == verdict
    assert stack.classify_mode(label) == mode


# THE CLASSIFIER AGREES WITH THE SCRIPT. Expected verdicts come from run_migrations.sh's own
# classify_config_files block, run by bash, not from a restatement. Labels are built from atoms that
# hit each branch of the script (empty, the leading/trailing/double comma patterns, the newline, an
# empty or relative entry, the exact basename and near misses), combined one to three entries deep.
RUN_MIGRATIONS = SERVER_ROOT / 'data' / 'store' / 'run_migrations.sh'
BASH = shutil.which('bash')
_ATOMS = (
    '',
    '/r/docker-compose.override.yaml',
    '/r/docker-compose.yaml',
    '/docker-compose.override.yaml',
    'docker-compose.override.yaml',
    'r/docker-compose.override.yaml',
    '/r/docker-compose.override.yaml ',
    ' /r/docker-compose.override.yaml',
    '/r/docker-compose.override.yaml/',
    '/r/docker-compose.override.yaml.bak',
    '/r/xdocker-compose.override.yaml',
    '/r/docker-compose.override.yml',
    '/r/a\n/docker-compose.override.yaml',
    '/r/*',
    '/r/$(x)\\',
    '/',
)
# THE FAKE-FILE LABELS (the architect's 09:30 note (B) on tj-iee6sn.24): without them the MCP port's
# missing TEST passed this test unnoticed. The fake file under a checkout, the devcontainer path and
# the MCP image's trusted copy, beside the base file and the override (so base + fake and override +
# fake both occur), and its near misses (.yml, .bak, a directory, relative, prefixed).
_MODE_ATOMS = (
    '',
    '/r/docker-compose.yaml',
    '/r/docker-compose.override.yaml',
    '/r/docker-compose.fake.yaml',
    '/workspace/docker-compose.fake.yaml',
    '/opt/agent_mcp/compose/docker-compose.fake.yaml',
    '/opt/agent_mcp/compose/docker-compose.agent-test.yaml',
    '/r/docker-compose.fake.yml',
    '/r/docker-compose.fake.yaml.bak',
    '/r/docker-compose.fake.yaml/',
    'docker-compose.fake.yaml',
    '/r/xdocker-compose.fake.yaml',
)
# The script's verdict -> the MCP's mode: the script's UNKNOWN is production to its own selection
# (fail closed), and PROD to the MCP's guard.
SCRIPT_TO_MODE = {'DEV': 'DEV', 'TEST': 'TEST', 'PROD': 'PROD', 'UNKNOWN': 'PROD'}


def _labels() -> list[str]:
    labels = set()
    for atom_set in (_ATOMS, _MODE_ATOMS):
        for depth in (1, 2, 3):
            for atoms in itertools.product(atom_set, repeat=depth):
                labels.add(','.join(atoms))
    return sorted(labels)


def _classifier_block() -> str:
    lines = RUN_MIGRATIONS.read_text(encoding='utf-8').splitlines()
    begin = [i for i, line in enumerate(lines) if line.startswith('# BEGIN classify_config_files')]
    end = [i for i, line in enumerate(lines) if line.startswith('# END classify_config_files')]
    assert len(begin) == 1 and len(end) == 1 and begin[0] < end[0]
    return '\n'.join(lines[begin[0] : end[0] + 1])


def _script_verdicts(labels: list[str]) -> list[str]:
    script = (
        f'set -euo pipefail\n{_classifier_block()}\n'
        'while IFS= read -r -d \'\' label; do v="$(classify_config_files "$label")"; printf \'%s\\0\' "${v%%:*}"; done'
    )
    result = subprocess.run(  # nosec B603 -- fixed argv; the script is the repository's own block
        [BASH, '-c', script],
        input=''.join(f'{label}\0' for label in labels).encode(),
        capture_output=True,
        check=True,
        timeout=120,
    )
    verdicts = result.stdout.decode().split('\0')[:-1]
    assert len(verdicts) == len(labels)
    return verdicts


@pytest.mark.skipif(BASH is None, reason='bash is needed to run the script classifier')
def test_classify_config_files_agrees_with_run_migrations_sh_on_every_case():
    labels = _labels()
    verdicts = _script_verdicts(labels)
    assert set(verdicts) == {'DEV', 'TEST', 'PROD', 'UNKNOWN'}, 'the cases must reach every verdict of the script'
    disagreements = [
        (label, script, stack.classify_config_files(label))
        for label, script in zip(labels, verdicts, strict=True)
        if stack.classify_config_files(label) != script
    ]
    assert not disagreements, disagreements[:10]
    # And the guard's mode is the script's verdict with UNKNOWN as PROD, on every case.
    mode_disagreements = [
        (label, script, stack.classify_mode(label))
        for label, script in zip(labels, verdicts, strict=True)
        if stack.classify_mode(label) != SCRIPT_TO_MODE[script]
    ]
    assert not mode_disagreements, mode_disagreements[:10]


@pytest.mark.skipif(BASH is None, reason='bash is needed to run the script classifier')
def test_the_classifier_tables_rows_are_the_scripts_own_verdicts():
    """The architect's named rows (his 09:30 note (B)), through the SCRIPT itself.

    base + fake is TEST in both; override + fake is UNKNOWN (production) in the script and PROD in the
    MCP; the trusted-copy path; the near misses.
    """
    labels = [label for label, _, _ in _CLASSIFIER_TABLE.values()]
    script = _script_verdicts(labels)
    assert script == [verdict for _, verdict, _ in _CLASSIFIER_TABLE.values()], list(zip(labels, script, strict=True))
    assert [SCRIPT_TO_MODE[verdict] for verdict in script] == [mode for _, _, mode in _CLASSIFIER_TABLE.values()]
    named = dict(zip(labels, script, strict=True))
    assert named[TEST_LINE] == 'TEST' and stack.classify_mode(TEST_LINE) == 'TEST'
    assert named[BOTH_LINE] == 'UNKNOWN' and stack.classify_mode(BOTH_LINE) == 'PROD'
    assert named['/opt/agent_mcp/compose/docker-compose.fake.yaml'] == 'TEST'


# ===============================================================================================
# THE D6 SYMLINK CHECK
# ===============================================================================================


def _parsed_bind_sources() -> tuple[set[str], set[str]]:
    """(relative in-repo bind sources, everything else that is a bind) over the five trusted dev files."""
    relative, other = set(), set()
    for name in EXPECTED_DEV_COMPOSE_FILES:
        for service in (compose_model.load(REPO_ROOT / name).get('services') or {}).values():
            for entry in service.get('volumes') or []:
                mount = compose_model.volume(entry)
                if mount['type'] != 'bind':
                    continue
                source = mount['source']
                if source.startswith('./'):
                    relative.add(source[2:].rstrip('/'))
                else:
                    other.add(source)
    return relative, other


def test_the_bind_source_list_is_the_relative_binds_of_the_trusted_dev_files():
    """Ruling D6 MINIMAL: the runtime keeps a plain list, and this pins it against the parsed files."""
    relative, _ = _parsed_bind_sources()
    assert len(set(stack.DEV_BIND_SOURCES)) == len(stack.DEV_BIND_SOURCES), stack.DEV_BIND_SOURCES
    assert set(stack.DEV_BIND_SOURCES) == relative, sorted(relative ^ set(stack.DEV_BIND_SOURCES))


def test_the_only_bind_the_check_cannot_see_is_data_dir():
    """D6's recorded residual is DATA_DIR alone. A new interpolated or absolute bind source is unchecked: red."""
    _, other = _parsed_bind_sources()
    assert other == {'${DATA_DIR:-./volume_data}/postgres'}, other


def _bind_tree(root: Path) -> Path:
    for relative in stack.DEV_BIND_SOURCES:
        path = root / relative
        if relative.endswith('.ini'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('[alembic]\n')
        else:
            path.mkdir(parents=True, exist_ok=True)
    return root


def test_a_real_tree_passes(tmp_path: Path):
    stack.check_dev_bind_sources(_bind_tree(tmp_path / 'repo'))


def test_an_empty_checkout_and_a_missing_source_pass(tmp_path: Path):
    """A missing path is not refused (behaviour unchanged): no server/, and one source gone."""
    empty = tmp_path / 'empty'
    empty.mkdir()
    stack.check_dev_bind_sources(empty)
    repo = _bind_tree(tmp_path / 'repo')
    shutil.rmtree(repo / 'web' / 'src')
    stack.check_dev_bind_sources(repo)


def _components() -> list[tuple[str, str]]:
    """(source, the component of it to replace) for every component of every source."""
    cases = []
    for relative in stack.DEV_BIND_SOURCES:
        parts = relative.split('/')
        cases += [(relative, '/'.join(parts[: depth + 1])) for depth in range(len(parts))]
    return cases


def _symlink_refusal(link: str, source: str) -> str:
    """F4 (tj-iee6sn.6 gate, fixed on tj-iee6sn.7): name the LINK, and the source it was on the way to.

    A link at the leaf is the source itself, named once.
    """
    on_the_way = '' if link == source else f' (on the way to {source})'
    return f'a symlink at {link}{on_the_way}: remove the link, or start the stack yourself'


def _first_source_through(component: str) -> str:
    """The first bind source, in the check's order, whose path passes through COMPONENT."""
    return next(s for s in stack.DEV_BIND_SOURCES if s == component or s.startswith(f'{component}/'))


@pytest.mark.parametrize(('source', 'component'), _components(), ids=[c for _, c in _components()])
def test_a_symlink_at_any_component_of_any_source_is_refused(tmp_path: Path, source: str, component: str):
    """The planted link points at a real directory with the same layout, so stat() would see nothing wrong.

    The refusal is the WHOLE message, naming the component that is the link -- never a real source
    directory below it, which the reader would be told to remove -- and the first source the check
    met on the way through it.
    """
    repo = _bind_tree(tmp_path / 'repo')
    elsewhere = _bind_tree(tmp_path / 'elsewhere')
    target = elsewhere / component
    shutil.move(str(repo / component), str(tmp_path / 'moved'))
    (repo / component).symlink_to(target, target_is_directory=target.is_dir())
    expected = _symlink_refusal(component, _first_source_through(component))
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_bind_sources(repo)
    assert str(refused.value) == expected


def test_a_symlinked_server_names_server_and_the_source_and_a_leaf_link_is_named_once(tmp_path: Path):
    """The two cases the 14:59 note on tj-iee6sn.7 names, spelled out as literals."""
    repo = _bind_tree(tmp_path / 'repo')
    elsewhere = _bind_tree(tmp_path / 'elsewhere')
    shutil.move(str(repo / 'server'), str(tmp_path / 'moved-server'))
    (repo / 'server').symlink_to(elsewhere / 'server', target_is_directory=True)
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_bind_sources(repo)
    assert str(refused.value) == (
        'a symlink at server (on the way to server/data/store/app): remove the link, or start the stack yourself'
    )
    repo = _bind_tree(tmp_path / 'repo2')
    shutil.rmtree(repo / 'web' / 'src')
    (repo / 'web' / 'src').symlink_to(elsewhere / 'web' / 'src', target_is_directory=True)
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_bind_sources(repo)
    assert str(refused.value) == 'a symlink at web/src: remove the link, or start the stack yourself'


def test_a_dangling_symlink_at_a_source_is_refused(tmp_path: Path):
    repo = _bind_tree(tmp_path / 'repo')
    shutil.rmtree(repo / 'server' / 'common')
    (repo / 'server' / 'common').symlink_to(tmp_path / 'nowhere')
    with pytest.raises(stack.Refused) as refused:
        stack.check_dev_bind_sources(repo)
    assert str(refused.value) == _symlink_refusal('server/common', 'server/common')


def test_a_regular_file_in_place_of_a_directory_reads_as_missing(tmp_path: Path):
    """ENOTDIR below a regular file: the source names nothing, so it is not refused (as a missing one)."""
    repo = _bind_tree(tmp_path / 'repo')
    shutil.rmtree(repo / 'server' / 'data')
    (repo / 'server' / 'data').write_text('not a directory\n')
    stack.check_dev_bind_sources(repo)


def test_an_lstat_error_other_than_missing_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fail closed: a component that cannot be checked is not guessed past."""
    repo = _bind_tree(tmp_path / 'repo')
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path).endswith('server/routers'):
            raise PermissionError(13, 'Permission denied', str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, 'lstat', lstat)
    with pytest.raises(stack.Refused, match='server/routers could not be checked'):
        stack.check_dev_bind_sources(repo)


# ===============================================================================================
# THE D5 ENV-FILE PREFLIGHT
# ===============================================================================================

EXPECTED_ENV_FILES = ('.env', 'server/data/store/.env', 'server/data/ingest/.env')


def test_the_preflight_covers_the_three_in_repo_env_files():
    assert tuple(stack.DEV_ENV_FILES) == EXPECTED_ENV_FILES


def _env_tree(root: Path) -> Path:
    for relative in EXPECTED_ENV_FILES:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text('POSTGRES_PASS=secret_value_123\n')
    return root


def test_regular_env_files_pass_and_missing_ones_are_allowed(tmp_path: Path):
    stack.env_file_preflight(_env_tree(tmp_path / 'repo'))
    empty = tmp_path / 'empty'
    empty.mkdir()
    stack.env_file_preflight(empty)


@pytest.mark.parametrize('relative', EXPECTED_ENV_FILES)
def test_a_symlinked_env_file_is_refused(tmp_path: Path, relative: str):
    repo = _env_tree(tmp_path / 'repo')
    (repo / relative).unlink()
    (repo / relative).symlink_to('/agent_mcp_share/token')
    with pytest.raises(stack.Refused) as refused:
        stack.env_file_preflight(repo)
    assert str(refused.value) == _symlink_refusal(relative, relative)


@pytest.mark.parametrize('relative', EXPECTED_ENV_FILES)
def test_a_fifo_env_file_is_refused(tmp_path: Path, relative: str):
    repo = _env_tree(tmp_path / 'repo')
    (repo / relative).unlink()
    os.mkfifo(repo / relative)
    with pytest.raises(stack.Refused, match=f'{re.escape(relative)} is not a regular file'):
        stack.env_file_preflight(repo)


@pytest.mark.parametrize('relative', EXPECTED_ENV_FILES)
def test_a_directory_env_file_is_refused(tmp_path: Path, relative: str):
    repo = _env_tree(tmp_path / 'repo')
    (repo / relative).unlink()
    (repo / relative).mkdir()
    with pytest.raises(stack.Refused, match=f'{re.escape(relative)} is not a regular file'):
        stack.env_file_preflight(repo)


@pytest.mark.parametrize('parent', ['server', 'server/data', 'server/data/store'])
def test_a_symlinked_parent_of_an_env_file_is_refused(tmp_path: Path, parent: str):
    repo = _env_tree(tmp_path / 'repo')
    elsewhere = _env_tree(tmp_path / 'elsewhere')
    shutil.move(str(repo / parent), str(tmp_path / 'moved'))
    (repo / parent).symlink_to(elsewhere / parent, target_is_directory=True)
    with pytest.raises(stack.Refused) as refused:
        stack.env_file_preflight(repo)
    # The link is the parent, not the env file below it (F4): the first file met through it is the store's.
    assert str(refused.value) == _symlink_refusal(parent, 'server/data/store/.env')


def test_the_preflight_never_opens_an_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D5: compose reads them, the MCP's Python never does. Every way to open a file is made to fail."""
    repo = _env_tree(tmp_path / 'repo')
    for relative in EXPECTED_ENV_FILES:
        (repo / relative).chmod(0)

    def refuse(*args, **kwargs):
        raise AssertionError(f'the preflight opened a file: {args!r}')

    with monkeypatch.context() as patched:
        patched.setattr(os, 'open', refuse)
        patched.setattr('builtins.open', refuse)
        patched.setattr(Path, 'open', refuse)
        patched.setattr(Path, 'read_bytes', refuse)
        patched.setattr(Path, 'read_text', refuse)
        stack.env_file_preflight(repo)


# ===============================================================================================
# THE STEP ENVIRONMENTS
# ===============================================================================================

EXPECTED_STEERING = (
    ('COMPOSE_PROJECT_NAME', EXPECTED_PROJECT),
    ('COMPOSE_FILE', ''),
    ('COMPOSE_PATH_SEPARATOR', ':'),
    ('COMPOSE_ENV_FILES', ''),
    ('COMPOSE_PROFILES', ''),
    ('COMPOSE_REMOVE_ORPHANS', 'false'),
    ('COMPOSE_IGNORE_ORPHANS', 'false'),
    # F7 (the 14:59 note (3) on tj-iee6sn.8): the build engine, which a project .env could otherwise
    # set and compose would promote. false is the classic path make dev-build takes.
    ('COMPOSE_BAKE', 'false'),
)
# D5 for build (the 17:25 note (B)): in the PLACEHOLDER env only, COMPOSE_ENV_FILES is /dev/null,
# so compose reads no <project-dir>/.env; it overrides the shared steering value IN PLACE, once.
EXPECTED_PLACEHOLDER_STEERING = tuple(
    (name, '/dev/null' if name == 'COMPOSE_ENV_FILES' else value) for name, value in EXPECTED_STEERING
)
EXPECTED_ENV_FILE_PINS = (
    ('ROOT_ENV_FILE', '.env'),
    ('STORE_ENV_FILE', './server/data/store/.env'),
    ('INGEST_ENV_FILE', './server/data/ingest/.env'),
)
EXPECTED_PINNED = (*EXPECTED_ENV_FILE_PINS, *EXPECTED_STEERING)
EXPECTED_PLACEHOLDER = (
    ('POSTGRES_PASS', 'unused'),
    ('INSTANCE_WRITE_SECRET', 'unused'),
    # The container half of the override's publish entries (e6aa688, the validator's RE: on tj-iee6sn.8):
    # empty, compose refuses '127.0.0.1::'. .env.default's value, pinned against that file below.
    ('APP_INTERNAL_PORT', '80'),
    # Referenced in fields compose validates when it loads the files (tj-iee6sn.30, found live at the
    # tj-iee6sn.11 sitting): postgres container_name (empty is refused by the schema pattern) and the
    # override's published host ports; the last two only warn when unset. .env.default's values, pinned
    # against that file below.
    ('DATABASE_NAME', 'db'),
    ('DATABASE_PORT', '5432'),
    ('DATA_STORE_PORT', '8080'),
    ('DATA_INGEST_PORT', '8081'),
    ('POSTGRES_USER', 'postgres'),
    ('POSTGRES_DB_NAME', 'postgres'),
    ('ROOT_ENV_FILE', '/dev/null'),
    ('STORE_ENV_FILE', '/dev/null'),
    ('INGEST_ENV_FILE', '/dev/null'),
    *EXPECTED_PLACEHOLDER_STEERING,
)


def _dev_file_texts() -> str:
    return '\n'.join((REPO_ROOT / name).read_text(encoding='utf-8') for name in EXPECTED_DEV_COMPOSE_FILES)


def test_the_pinned_env_is_exactly_the_env_file_defaults_and_the_steering_variables():
    assert stack.dev_pinned_step_env() == EXPECTED_PINNED


def test_the_placeholder_env_is_exactly_the_guards_the_env_files_at_dev_null_and_the_steering():
    assert stack.dev_placeholder_step_env() == EXPECTED_PLACEHOLDER


def test_the_placeholder_env_names_one_env_file_set_and_it_is_dev_null():
    """COMPOSE_ENV_FILES once, as /dev/null: a duplicate key would leave the winner to dict order."""
    names = [name for name, _ in stack.dev_placeholder_step_env()]
    assert names.count('COMPOSE_ENV_FILES') == 1, names
    assert dict(stack.dev_placeholder_step_env())['COMPOSE_ENV_FILES'] == '/dev/null'
    # Only the placeholder env: up and friends let compose read the root file (D5), so '' stays.
    assert dict(stack.dev_pinned_step_env())['COMPOSE_ENV_FILES'] == ''


def test_every_dev_step_env_pins_the_classic_build_engine():
    for env in (stack.dev_pinned_step_env(), stack.dev_placeholder_step_env(), stack.dev_label_only_step_env()):
        assert [value for name, value in env if name == 'COMPOSE_BAKE'] == ['false'], env


def test_the_pinned_env_file_values_are_the_compose_files_own_defaults():
    """Parsed from `${ROOT_ENV_FILE:-.env}` and its siblings, so the pin cannot drift from the files."""
    defaults = {}
    for name, default in re.findall(r'\$\{([A-Z_]+_ENV_FILE):-([^}]*)\}', _dev_file_texts()):
        defaults.setdefault(name, set()).add(default)
    assert defaults == {name: {value} for name, value in EXPECTED_ENV_FILE_PINS}, defaults
    pinned = dict(stack.dev_pinned_step_env())
    assert {name: pinned[name] for name in defaults} == dict(EXPECTED_ENV_FILE_PINS)


def test_every_env_file_variable_the_dev_files_read_is_pinned_in_both_envs():
    """A new `${X_ENV_FILE...}` in a dev file would otherwise be redirectable from the root .env."""
    referenced = set(re.findall(r'\$\{([A-Z_]*ENV_FILE)\b', _dev_file_texts()))
    for env in (stack.dev_pinned_step_env(), stack.dev_placeholder_step_env()):
        assert referenced <= set(dict(env)), referenced - set(dict(env))


def test_the_placeholders_cover_every_required_variable_of_the_dev_files():
    """Every `${NAME:?...}` must be set for a build to interpolate the files without reading an env file."""
    required = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*):?\?', _dev_file_texts()))
    placeholders = dict(stack.dev_placeholder_step_env())
    assert required == {'POSTGRES_PASS', 'INSTANCE_WRITE_SECRET'}, required
    assert all(placeholders[name] == 'unused' for name in required), placeholders


def test_the_placeholder_container_port_is_the_committed_default():
    """APP_INTERNAL_PORT, the container port, must be .env.default's (the rest: the test below).

    The services listen on the value their env file gives them, so a build that interpolated a
    different container port would load a project whose publish entries point nowhere. A port bakes
    nothing into an image, so the committed default is the only value that is right without reading
    an env file (D5).
    """
    assignments = re.findall(
        r'^APP_INTERNAL_PORT=(.*)$', (REPO_ROOT / '.env.default').read_text(encoding='utf-8'), re.MULTILINE
    )
    assert len(assignments) == 1, assignments
    assert dict(stack.dev_placeholder_step_env())['APP_INTERNAL_PORT'] == assignments[0].strip()
    # Only the placeholder env: up and friends read the real value from the env files.
    assert 'APP_INTERNAL_PORT' not in dict(stack.dev_pinned_step_env())


def test_every_non_guard_placeholder_is_the_committed_default_and_never_a_secret():
    """tj-iee6sn.30: each placeholder that is not a `:?` guard or an env-file path is .env.default's value.

    .env.default is committed and public, so a value equal to it is no secret by construction; the two
    guards are the throwaway word 'unused' (pinned above), never .env.default's empty value. A value
    that differed from the committed default would load a project naming a container or a port the
    owner's stack does not use (D5: the build reads no env file to learn the real one).
    """
    text = (REPO_ROOT / '.env.default').read_text(encoding='utf-8')
    defaults = dict(re.findall(r'^([A-Z_][A-Z0-9_]*)=(.*)$', text, re.MULTILINE))
    guards = {'POSTGRES_PASS', 'INSTANCE_WRITE_SECRET'}
    env_files = {name for name, _ in EXPECTED_ENV_FILE_PINS}
    steering = {name for name, _ in EXPECTED_PLACEHOLDER_STEERING}
    placeholder = dict(stack.dev_placeholder_step_env())
    values = {name: value for name, value in placeholder.items() if name not in guards | env_files | steering}
    assert set(values) == {
        'APP_INTERNAL_PORT',
        'DATABASE_NAME',
        'DATABASE_PORT',
        'DATA_STORE_PORT',
        'DATA_INGEST_PORT',
        'POSTGRES_USER',
        'POSTGRES_DB_NAME',
    }, values
    for name, value in values.items():
        assert name in defaults, f'{name} has no .env.default line'
        assert len(re.findall(rf'^{name}=', text, re.MULTILINE)) == 1, name
        assert value == defaults[name].strip(), (name, value, defaults[name])
    assert {placeholder[name] for name in guards} == {'unused'}
    # None of them is in the pinned env: up and friends read the real values from the env files.
    assert not set(values) & set(dict(stack.dev_pinned_step_env()))


def test_no_step_env_overrides_the_runners_fixed_environment():
    """Merged OVER DOCKER_ENV: a DOCKER_HOST, PATH or HOME in a step env would redirect the docker CLI."""
    for env in (stack.dev_pinned_step_env(), stack.dev_placeholder_step_env()):
        keys = [name for name, _ in env]
        assert len(keys) == len(set(keys)), keys
        assert not set(keys) & set(runner.DOCKER_ENV), set(keys) & set(runner.DOCKER_ENV)
        assert not [name for name in keys if name.startswith('DOCKER_')], keys


def test_the_env_builders_take_nothing_so_no_value_can_reach_them():
    """Acceptance: no secret value reaches argv by construction; the env is constants, never an argument."""
    for builder in (stack.dev_pinned_step_env, stack.dev_placeholder_step_env):
        assert list(inspect.signature(builder).parameters) == [], builder


def test_an_env_file_setting_the_redirecting_variables_cannot_change_the_step_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The root .env setting ROOT_ENV_FILE (and the steering keys), and the MCP's own environment too."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.env').write_text(
        'ROOT_ENV_FILE=/agent_mcp_share/token\nCOMPOSE_FILE=/tmp/evil.yaml\nCOMPOSE_PROFILES=tools\n'
        'COMPOSE_REMOVE_ORPHANS=true\n'
    )
    monkeypatch.chdir(repo)
    for name in ('ROOT_ENV_FILE', 'STORE_ENV_FILE', 'COMPOSE_FILE', 'COMPOSE_PROFILES', 'COMPOSE_REMOVE_ORPHANS'):
        monkeypatch.setenv(name, '/agent_mcp_share/token')
    assert stack.dev_pinned_step_env() == EXPECTED_PINNED
    assert stack.dev_placeholder_step_env() == EXPECTED_PLACEHOLDER


# ===============================================================================================
# S6, STALE TRUSTED COPIES
# ===============================================================================================

# BUILT FROM THE MCP DOCKERFILE'S BAKE LIST, not spelled here (the architect's 14:59 and 17:25 notes
# (A) on tj-iee6sn.8): every dev file the image bakes is compared, so a file a later bead bakes and S6
# forgets turns the membership pin and its one-byte, missing and symlink cases red. EXPECTED_DEV_BAKES
# is test_commands.py's literal list of what tj-iee6sn.5 bakes, itself pinned there against the
# Dockerfile's COPY lines; docker-compose.yaml and the Dockerfile are the base file and build file of
# every set. docker-compose.fake.yaml is loaded by TEST mode's test_up and test_build (tj-iee6sn.24 WHAT
# (3): "add it and fake.yaml to S6"); docker-compose.agent-test.yaml arrives through EXPECTED_DEV_BAKES.
# tj-iee6sn.20 adds the system-test client's two: docker-compose.test-client.yaml (loaded by
# run_system_tests and seed_dump) and docker-compose.agent-client.yaml, through EXPECTED_DEV_BAKES.
EXPECTED_COPY_NAMES = frozenset(
    {
        'docker-compose.yaml',
        'docker-compose.fake.yaml',
        'docker-compose.test-client.yaml',
        'Dockerfile',
        *EXPECTED_DEV_BAKES,
    }
)
# The retired agent stack's overlay: the image no longer bakes it and S6 does not compare it (tj-iee6sn.21).
RETIRED_OVERLAY = 'docker-compose.agent-stack.yaml'
COPY_NAMES = sorted(EXPECTED_COPY_NAMES)


def test_the_compared_copies_are_every_dev_file_the_mcp_image_bakes():
    """Membership from the bake list; each pair names the same file on both sides (the repository's layout)."""
    checkout_names = [checkout for checkout, _ in stack.DEV_TRUSTED_COPIES]
    assert len(checkout_names) == len(set(checkout_names)), checkout_names
    assert set(checkout_names) == EXPECTED_COPY_NAMES, set(checkout_names) ^ EXPECTED_COPY_NAMES
    assert all(checkout == baked for checkout, baked in stack.DEV_TRUSTED_COPIES), stack.DEV_TRUSTED_COPIES


def test_every_compared_copy_is_read_where_the_mcp_dockerfile_puts_it():
    """The baked side of each pair, under the trusted directory, is exactly the path the image COPYs it to."""
    landed = _baked_into_trusted()
    for checkout, baked in stack.DEV_TRUSTED_COPIES:
        assert landed.get(checkout) == f'{EXPECTED_TRUSTED_DIR}/{baked}', (checkout, landed.get(checkout))


def test_the_bake_list_is_the_dev_files_the_tools_file_the_test_overlay_and_the_two_scripts():
    """Non-vacuity for the derived pin: the list still names what tj-iee6sn.5 and tj-iee6sn.24 baked."""
    assert set(EXPECTED_DEV_BAKES) == {
        *EXPECTED_DEV_COMPOSE_FILES[1:],
        'docker-compose.tools.yaml',
        'docker-compose.agent-test.yaml',
        'docker-compose.agent-client.yaml',
        'tools/dev_instance_secret.sh',
        'tools/source_digest.sh',
    }


def test_the_compared_copies_are_exactly_what_the_mcp_dockerfile_bakes():
    """tj-iee6sn.20, tightened by tj-iee6sn.21: the S6 list EQUALS the Dockerfile's bake list, with NO exception.

    Read from the COPY lines, so a file baked and not compared (S6 would let it go stale) or compared and not
    baked (S6 would refuse every call) is red. The one exception there was, the agent-stack overlay, is gone
    from the bake, so restoring it to the COPY without S6 is red here as well as in test_commands.py.
    """
    baked = set(_baked_into_trusted())
    compared = {checkout for checkout, _ in stack.DEV_TRUSTED_COPIES}
    assert RETIRED_OVERLAY not in baked and RETIRED_OVERLAY not in compared
    assert compared == baked, compared ^ baked
    assert compared == EXPECTED_COPY_NAMES, compared ^ EXPECTED_COPY_NAMES
    assert {'docker-compose.test-client.yaml', 'docker-compose.agent-client.yaml'} <= compared


def test_s6_covers_both_system_client_files():
    """tj-iee6sn.20: the client file and the MCP-only client overlay are compared like the dev files."""
    checkout_names = {checkout for checkout, _ in stack.DEV_TRUSTED_COPIES}
    assert {'docker-compose.test-client.yaml', 'docker-compose.agent-client.yaml'} <= checkout_names, checkout_names


def test_s6_covers_both_test_mode_files():
    """tj-iee6sn.24 WHAT (3): the fake overlay and the MCP-only test overlay are compared like the dev files."""
    checkout_names = {checkout for checkout, _ in stack.DEV_TRUSTED_COPIES}
    assert {'docker-compose.fake.yaml', 'docker-compose.agent-test.yaml'} <= checkout_names, checkout_names


def _copies(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout and a trusted directory holding identical copies of the real files."""
    checkout, trusted = tmp_path / 'checkout', tmp_path / 'trusted'
    for name in COPY_NAMES:
        for root in (checkout, trusted):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / name, root / name)
    return checkout, trusted


def test_identical_copies_pass(tmp_path: Path):
    stack.check_trusted_copies_current(*_copies(tmp_path))


@pytest.mark.parametrize('side', ['checkout', 'trusted'])
@pytest.mark.parametrize('name', COPY_NAMES)
def test_a_one_byte_difference_in_any_copy_refuses_naming_it(tmp_path: Path, name: str, side: str):
    checkout, trusted = _copies(tmp_path)
    path = (checkout if side == 'checkout' else trusted) / name
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(stack.Refused, match=f'the MCP image is stale: {re.escape(name)} differs') as refused:
        stack.check_trusted_copies_current(checkout, trusted)
    assert 'make agent-mcp-rebuild' in str(refused.value)


@pytest.mark.parametrize('side', ['checkout', 'trusted'])
@pytest.mark.parametrize('name', COPY_NAMES)
def test_a_missing_copy_on_either_side_refuses(tmp_path: Path, name: str, side: str):
    checkout, trusted = _copies(tmp_path)
    ((checkout if side == 'checkout' else trusted) / name).unlink()
    with pytest.raises(stack.Refused, match=re.escape(name)):
        stack.check_trusted_copies_current(checkout, trusted)


def test_both_sides_missing_refuses(tmp_path: Path):
    checkout, trusted = _copies(tmp_path)
    (checkout / 'Dockerfile').unlink()
    (trusted / 'Dockerfile').unlink()
    with pytest.raises(stack.Refused, match='Dockerfile'):
        stack.check_trusted_copies_current(checkout, trusted)


@pytest.mark.parametrize('name', COPY_NAMES)
def test_a_symlink_in_the_checkout_refuses_even_to_identical_bytes(tmp_path: Path, name: str):
    checkout, trusted = _copies(tmp_path)
    (checkout / name).unlink()
    (checkout / name).symlink_to(trusted / name)
    with pytest.raises(stack.Refused, match=re.escape(name)):
        stack.check_trusted_copies_current(checkout, trusted)


def test_a_fifo_in_the_checkout_refuses_without_blocking(tmp_path: Path):
    """O_NONBLOCK: a FIFO with no writer must not hang the server (the check runs in its thread)."""
    checkout, trusted = _copies(tmp_path)
    (checkout / 'Dockerfile').unlink()
    os.mkfifo(checkout / 'Dockerfile')
    outcome: list[BaseException | None] = []

    def run():
        try:
            stack.check_trusted_copies_current(checkout, trusted)
            outcome.append(None)
        except BaseException as error:  # carried back to the test thread
            outcome.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), 'the stale check blocked on a FIFO'
    assert isinstance(outcome[0], stack.Refused), outcome


def test_identical_files_over_one_mib_refuse_and_exactly_one_mib_passes(tmp_path: Path):
    checkout, trusted = _copies(tmp_path)
    for root in (checkout, trusted):
        (root / 'Dockerfile').write_bytes(b'#' * (1024 * 1024))
    stack.check_trusted_copies_current(checkout, trusted)
    for root in (checkout, trusted):
        (root / 'Dockerfile').write_bytes(b'#' * (1024 * 1024 + 1))
    with pytest.raises(stack.Refused, match='Dockerfile'):
        stack.check_trusted_copies_current(checkout, trusted)


def test_the_default_trusted_directory_is_the_images():
    signature = inspect.signature(stack.check_trusted_copies_current)
    assert str(signature.parameters['trusted_dir'].default) == EXPECTED_TRUSTED_DIR


# ===============================================================================================
# S10, THE BUDGET
# ===============================================================================================


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_budget_constants_are_twelve_per_rolling_hour():
    assert stack.DEV_BUDGET_LIMIT == 12
    assert stack.DEV_BUDGET_WINDOW_SECONDS == 3600.0


def test_the_thirteenth_call_in_the_hour_refuses():
    clock = Clock()
    budget = stack.DevBudget(clock)
    for _ in range(12):
        budget.charge()
        clock.now += 1
    with pytest.raises(stack.Refused, match='12 state-changing dev verbs in the last hour'):
        budget.charge()


def test_a_refused_call_is_not_counted():
    """Twelve at t=0, refusals at t=1800: had they counted, fewer than twelve would fit at t=3600."""
    clock = Clock()
    budget = stack.DevBudget(clock)
    start = clock.now
    for _ in range(12):
        budget.charge()
    clock.now = start + 1800
    for _ in range(5):
        with pytest.raises(stack.Refused):
            budget.charge()
    clock.now = start + 3600
    for _ in range(12):
        budget.charge()
    with pytest.raises(stack.Refused):
        budget.charge()


def test_entries_expire_one_by_one_on_a_rolling_window():
    clock = Clock()
    budget = stack.DevBudget(clock)
    start = clock.now
    for second in range(12):
        clock.now = start + second
        budget.charge()
    clock.now = start + 3599.999
    with pytest.raises(stack.Refused):
        budget.charge()
    clock.now = start + 3600
    budget.charge()
    with pytest.raises(stack.Refused):
        budget.charge()
    clock.now = start + 3601
    budget.charge()


def test_the_budget_defaults_to_the_monotonic_clock():
    assert inspect.signature(stack.DevBudget).parameters['clock'].default is time.monotonic


# ===============================================================================================
# Step.env THROUGH THE RUNNER
# ===============================================================================================

SENTINEL = 'STEP_ENV_SENTINEL_7f3a9c'


def test_a_step_without_env_runs_in_exactly_the_fixed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('ROOT_ENV_FILE', '/agent_mcp_share/token')
    result = asyncio.run(runner.run_process(stack.Step(('/usr/bin/env',), tmp_path)))
    assert result.exit_status == 0
    assert dict(line.split('=', 1) for line in result.stdout.decode().splitlines()) == runner.DOCKER_ENV


def test_a_steps_env_reaches_its_subprocess_over_the_fixed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The pinned env lands in the child, DOCKER_ENV is kept, and the server's own environment is not inherited."""
    monkeypatch.setenv('COMPOSE_FILE', '/tmp/evil.yaml')
    step = stack.Step(('/usr/bin/env',), tmp_path, env=stack.dev_pinned_step_env())
    result = asyncio.run(runner.run_process(step))
    assert result.exit_status == 0
    child = dict(line.split('=', 1) for line in result.stdout.decode().splitlines())
    assert child == {**runner.DOCKER_ENV, **dict(EXPECTED_PINNED)}, child


def test_a_steps_env_appears_in_no_recorded_command_and_no_audit_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Through the public call path: the subprocess gets the env; steps[].command and the audit never show it."""
    rig = make_rig(tmp_path, monkeypatch)
    real_steps = stack.dev_ps_steps

    def with_env(stack_dir):
        return [replace(step, env=(('INSTANCE_WRITE_SECRET', SENTINEL),)) for step in real_steps(stack_dir)]

    monkeypatch.setattr(stack, 'dev_ps_steps', with_env)
    result = rig.call('ps', {})
    assert result['status'] == 'ok', result
    assert rig.docker.steps and all(dict(step.env) == {'INSTANCE_WRITE_SECRET': SENTINEL} for step in rig.docker.steps)
    assert SENTINEL not in json.dumps(result), result
    assert all(SENTINEL not in step['command'] for step in result['steps'])
    assert rig.audit_lines() and not [line for line in rig.audit_lines() if SENTINEL in line]


def test_a_step_with_env_stays_hashable():
    step = stack.Step(('/usr/bin/true',), Path('/'), env=stack.dev_pinned_step_env())
    assert hash(step) == hash(replace(step))
