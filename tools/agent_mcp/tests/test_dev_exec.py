"""The dev EXEC family's containment, and db_tables: fixed argv, one running target, a constant script.

tj-iee6sn.13.4 (slice 1 of epic tj-iee6sn). Design: ADR tj-4rr0la addendum 22 (4) and (7), with the
plan-revision-2 ruling on tj-iee6sn.13 (no DEV classifier here: D7 applies only to state changes, so
the target resolver is exactly one RUNNING container of the project's service, and nothing more).

WHAT IS PINNED, in the addendum's order:
    the exact argv     the label lookup and the exec, word for word, from literals
    the guards         check_dev_lookup_argv and check_dev_exec_argv refuse every forbidden shape
    the target         pick_dev_target refuses none, several and a non-id -- and then NO exec runs
    the SQL            a module constant, built from the eight names, that no argument reaches
    the script         psql -X -At -v ON_ERROR_STOP=1, the five redaction names, and its BEHAVIOUR:
                       run under sh with a fake psql, no fake secret value survives to the output
    the verb           closed empty schema, 60 s budget, the {table: present} map and the revision

EVERY CONSTANT IS SPELLED FROM LITERALS, never read from stack.py, for the reason test_dev_project.py
gives: a test that read the module's own constants would agree with any change to them. The one
exception is the script text itself, DEV_DB_TABLES_SCRIPT: it is pinned by its parts (flags, names,
queries) and by RUNNING it, which is stronger than a second copy of the text would be.

NO DOCKER RUNS (tj-j4wknb R4). The behavioural redaction test runs only `sh` and a fake `psql`
written into tmp_path, with an environment built here from FAKE values. The real environment is
never handed to the child and never printed.
"""

import inspect
import itertools
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import FakeDocker, default_response, forbid_real_subprocesses, make_rig


pytestmark = pytest.mark.build_infra

# --- the constants, from literals --------------------------------------------------------------
EXPECTED_DOCKER = '/usr/local/bin/docker'
EXPECTED_DEV_PROJECT = 'trader_joe'
EXPECTED_AGENT_PROJECT = 'trader_joe_agent_stack'
EXPECTED_DEV_SERVICES = ('postgres', 'data_store', 'data_ingest')
# Read-only for every statement of the session: libpq hands PGOPTIONS to the server at connect.
EXPECTED_PGOPTIONS = 'PGOPTIONS=-c default_transaction_read_only=on'
# The tables db_tables reports, in the order of the models (addendum 22 (4)).
EXPECTED_TABLES = (
    'dataset_read',
    'dataset_read_daily',
    'dataset_override_log',
    'dataset_deletion',
    'ingest_run',
    'store_dataset_entry',
    'stock_market_activity',
    'alembic_version',
)
EXPECTED_TABLES_QUERY = ' UNION ALL '.join(
    f"SELECT '{name}', to_regclass('public.{name}') IS NOT NULL" for name in EXPECTED_TABLES
)
EXPECTED_VERSION_QUERY = "SELECT 'version_num', version_num FROM public.alembic_version ORDER BY version_num"
# Addendum 22 (4) SCRUBBING: the container's own secret variables, by name.
EXPECTED_REDACT_NAMES = (
    'INSTANCE_WRITE_SECRET',
    'POSTGRES_PASS',
    'POSTGRES_PASSWORD',
    'ALPACA_API_KEY',
    'ALPACA_API_SECRET',
)
EXPECTED_TIMEOUT_SECONDS = 60
CONTAINER_ID = 'c0ffee000001'


def expected_lookup(service: str, project: str = EXPECTED_DEV_PROJECT) -> list[str]:
    """The label lookup of addendum 22 (4): running, project, service, not a one-off; ids only."""
    return [
        EXPECTED_DOCKER,
        'ps',
        '--filter',
        'status=running',
        '--filter',
        f'label=com.docker.compose.project={project}',
        '--filter',
        f'label=com.docker.compose.service={service}',
        '--filter',
        'label=com.docker.compose.oneoff=False',
        '--format',
        '{{.ID}}',
    ]


def expected_exec(container_id: str = CONTAINER_ID) -> list[str]:
    """db_tables' exec: user postgres, the read-only PGOPTIONS, the id, then sh -c <the script>."""
    return [
        EXPECTED_DOCKER,
        'exec',
        '-u',
        'postgres',
        '-e',
        EXPECTED_PGOPTIONS,
        container_id,
        'sh',
        '-c',
        stack.DEV_DB_TABLES_SCRIPT,
    ]


def expected_migrate_exec(container_id: str = CONTAINER_ID) -> list[str]:
    """migrate_status' exec (tj-iee6sn.13.5): in /code, as the image's user, then sh -c <its script>."""
    return [EXPECTED_DOCKER, 'exec', '-w', '/code', container_id, 'sh', '-c', stack.DEV_MIGRATE_STATUS_SCRIPT]


SCRIPT = stack.DEV_DB_TABLES_SCRIPT
STACK_DIR = Path('/stack')


# ===============================================================================================
# THE EXACT ARGV.
# ===============================================================================================


@pytest.mark.parametrize('service', EXPECTED_DEV_SERVICES)
def test_the_lookup_argv_is_exactly_the_label_filter_of_one_dev_service(service: str):
    """status=running AND project AND service AND oneoff=False (label filters AND together), ids only.

    The service label is the compose service KEY (`postgres`), not container_name (`${DATABASE_NAME}`,
    `db` in the dev env): compose labels every container it creates with its project, its service key
    and oneoff=False, and a `compose run` container with oneoff=True -- which is why that filter is here.
    """
    assert list(stack.dev_lookup_argv(service)) == expected_lookup(service)
    step = stack.dev_lookup_step(STACK_DIR, service)
    assert list(step.argv) == expected_lookup(service) and step.cwd == STACK_DIR and step.builds is False


def test_the_db_tables_exec_argv_is_exact():
    """Addendum 22 (4): docker exec -u postgres -e 'PGOPTIONS=-c default_transaction_read_only=on' <id> sh -c <script>."""
    steps = stack.dev_db_tables_steps(STACK_DIR, CONTAINER_ID)
    assert [list(step.argv) for step in steps] == [expected_exec()]
    assert steps[0].cwd == STACK_DIR and steps[0].builds is False and steps[0].stdout_cap is None


def test_the_read_only_option_is_one_word_with_its_value_attached_to_the_variable():
    """-e takes NAME=VALUE as ONE word: the value carries a space, so a split would break the option."""
    argv = list(stack.dev_db_tables_steps(STACK_DIR, CONTAINER_ID)[0].argv)
    assert argv[argv.index('-e') + 1] == EXPECTED_PGOPTIONS
    assert argv.count('-e') == 1 and argv.count('-u') == 1 and '-w' not in argv


def test_the_whole_verb_runs_the_lookup_then_the_exec_into_the_id_it_found(tmp_path: Path, monkeypatch):
    """Through AgentStack: the two argv, in order, the exec naming exactly the id the lookup printed."""
    found = 'abcdef0123456789'

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1] == 'ps':
            return runner.ProcessResult(0, f'{found}\n'.encode(), b'')
        return default_response(step)

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=respond))
    result = rig.call('db_tables', {})
    assert result['status'] == 'ok', result
    assert [list(step.argv) for step in rig.docker.steps] == [expected_lookup('postgres'), expected_exec(found)]
    assert all(step.cwd == rig.layout.stack_dir for step in rig.docker.steps)


# ===============================================================================================
# check_dev_lookup_argv REFUSES anything but the lookup of one dev service, word for word.
# ===============================================================================================

_GOOD_LOOKUP = expected_lookup('postgres')


def _without(words: list[str], *drop: str) -> list[str]:
    """WORDS with each `--filter <drop>` pair removed."""
    out = list(words)
    for value in drop:
        index = out.index(value)
        del out[index - 1 : index + 1]
    return out


_BAD_LOOKUPS = {
    'another project': expected_lookup('postgres', 'someone_else'),
    'the agent stack project': expected_lookup('postgres', EXPECTED_AGENT_PROJECT),
    'a project with the dev name as prefix': expected_lookup('postgres', 'trader_joe_prod'),
    'a service outside the enum': expected_lookup('pgadmin'),
    'the container name rather than the service': expected_lookup('db'),
    'status=running dropped': _without(_GOOD_LOOKUP, 'status=running'),
    'oneoff=False dropped': _without(_GOOD_LOOKUP, 'label=com.docker.compose.oneoff=False'),
    'oneoff=True': [w.replace('oneoff=False', 'oneoff=True') for w in _GOOD_LOOKUP],
    'the project filter dropped': _without(_GOOD_LOOKUP, f'label=com.docker.compose.project={EXPECTED_DEV_PROJECT}'),
    'the service filter dropped': _without(_GOOD_LOOKUP, 'label=com.docker.compose.service=postgres'),
    'all containers, stopped too': [*_GOOD_LOOKUP[:2], '--all', *_GOOD_LOOKUP[2:]],
    'an extra word at the end': [*_GOOD_LOOKUP, '--no-trunc'],
    'an extra filter': [*_GOOD_LOOKUP[:-2], '--filter', 'name=db', *_GOOD_LOOKUP[-2:]],
    'a format printing labels': [*_GOOD_LOOKUP[:-1], '{{.ID}} {{.Labels}}'],
    'no format': _GOOD_LOOKUP[:-2],
    'the filters reordered': [*_GOOD_LOOKUP[:2], *_GOOD_LOOKUP[4:6], *_GOOD_LOOKUP[2:4], *_GOOD_LOOKUP[6:]],
    'another binary': ['docker', *_GOOD_LOOKUP[1:]],
    'not ps': [EXPECTED_DOCKER, 'exec', *_GOOD_LOOKUP[2:]],
    'empty': [],
}


@pytest.mark.parametrize('argv', list(_BAD_LOOKUPS.values()), ids=list(_BAD_LOOKUPS))
def test_check_dev_lookup_argv_refuses_any_other_lookup(monkeypatch: pytest.MonkeyPatch, argv: list[str]):
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused, match='a dev lookup is'):
        stack.check_dev_lookup_argv(argv)


@pytest.mark.parametrize('service', EXPECTED_DEV_SERVICES)
def test_check_dev_lookup_argv_accepts_the_lookup_of_each_dev_service(service: str):
    stack.check_dev_lookup_argv(expected_lookup(service))
    stack.check_dev_lookup_argv(tuple(expected_lookup(service)))


@pytest.mark.parametrize('service', ['pgadmin', 'db', 'postgres ', '', 'postgres;ls', None, 5])
def test_the_lookup_step_refuses_a_service_outside_the_enum(service: object):
    with pytest.raises(stack.Refused, match='service must be one of'):
        stack.dev_lookup_step(STACK_DIR, service)  # type: ignore[arg-type]


def test_the_lookup_step_runs_check_dev_lookup_argv(monkeypatch: pytest.MonkeyPatch):
    """The call site, not the function: a builder that dropped the re-check is caught here."""
    seen: list[tuple[str, ...]] = []

    def spy(argv):
        seen.append(tuple(argv))
        raise stack.Refused('spy refusal')

    monkeypatch.setattr(stack, 'check_dev_lookup_argv', spy)
    with pytest.raises(stack.Refused, match='spy refusal'):
        stack.dev_lookup_step(STACK_DIR, 'postgres')
    assert seen == [tuple(expected_lookup('postgres'))]


# ===============================================================================================
# check_dev_exec_argv REFUSES every forbidden option, value, program and script.
# ===============================================================================================

_GOOD = expected_exec()
_OPTS, _ID_AND_TAIL = _GOOD[:6], _GOOD[6:]  # docker exec -u postgres -e PGOPTIONS ; <id> sh -c <script>


def _with_options(*extra: str) -> list[str]:
    """The good argv with EXTRA option words inserted before the container id."""
    return [*_OPTS, *extra, *_ID_AND_TAIL]


_BAD_EXECS = {
    # interactive, tty, privilege
    '-it': _with_options('-it'),
    '-i': _with_options('-i'),
    '-t': _with_options('-t'),
    '--interactive': _with_options('--interactive'),
    '--tty': _with_options('--tty'),
    '--privileged': _with_options('--privileged'),
    '--detach': _with_options('-d'),
    # environment
    '--env-file': _with_options('--env-file', '/workspace/.env'),
    '--env-file=': _with_options('--env-file=/workspace/.env'),
    '--env': _with_options('--env', 'X=1'),
    '--env with the fixed value': _with_options('--env', EXPECTED_PGOPTIONS),
    'a second -e, another value': _with_options('-e', 'X=1'),
    'a second -e, the same value': _with_options('-e', EXPECTED_PGOPTIONS),
    '-e with another PGOPTIONS': [*_GOOD[:5], 'PGOPTIONS=-c default_transaction_read_only=off', *_ID_AND_TAIL],
    '-e with PGOPTIONS widened': [*_GOOD[:5], EXPECTED_PGOPTIONS + ' -c search_path=x', *_ID_AND_TAIL],
    '-e attached': [*_GOOD[:4], f'-e{EXPECTED_PGOPTIONS}', *_ID_AND_TAIL],
    '-e with no value before the id': [*_GOOD[:4], '-e', *_ID_AND_TAIL],
    # user
    '-u root': [_GOOD[0], _GOOD[1], '-u', 'root', *_GOOD[4:]],
    '-u 0': [_GOOD[0], _GOOD[1], '-u', '0', *_GOOD[4:]],
    '-uroot': [_GOOD[0], _GOOD[1], '-uroot', *_GOOD[4:]],
    '-upostgres attached': [_GOOD[0], _GOOD[1], '-upostgres', *_GOOD[4:]],
    '--user postgres': [_GOOD[0], _GOOD[1], '--user', 'postgres', *_GOOD[4:]],
    'a second -u': _with_options('-u', 'postgres'),
    # workdir
    '-w /other': _with_options('-w', '/other'),
    '-w /': _with_options('-w', '/'),
    '-w /code twice': _with_options('-w', '/code', '-w', '/code'),
    '--workdir /code': _with_options('--workdir', '/code'),
    # end of options
    '--': _with_options('--'),
    'an option with no value at the end': [*_OPTS, '-w'],
    # the container id
    'no id': [*_OPTS],
    'no id, tail only': [*_OPTS, 'sh', '-c', SCRIPT],
    'a container name': [*_OPTS, 'db', *_ID_AND_TAIL[1:]],
    'an uppercase id': [*_OPTS, CONTAINER_ID.upper(), *_ID_AND_TAIL[1:]],
    'an 11-digit id': [*_OPTS, CONTAINER_ID[:11], *_ID_AND_TAIL[1:]],
    'a 65-digit id': [*_OPTS, 'a' * 65, *_ID_AND_TAIL[1:]],
    'an id with a newline': [*_OPTS, CONTAINER_ID + '\n', *_ID_AND_TAIL[1:]],
    'an id with a shell tail': [*_OPTS, CONTAINER_ID + ';id', *_ID_AND_TAIL[1:]],
    # the program and its arguments
    'bash with the fixed script': [*_OPTS, CONTAINER_ID, 'bash', '-c', SCRIPT],
    '/bin/sh with the fixed script': [*_OPTS, CONTAINER_ID, '/bin/sh', '-c', SCRIPT],
    'psql directly': [*_OPTS, CONTAINER_ID, 'psql', '-c', EXPECTED_TABLES_QUERY],
    'sh with another script': [*_OPTS, CONTAINER_ID, 'sh', '-c', 'cat /proc/1/environ'],
    'sh with the script plus a word': [*_OPTS, CONTAINER_ID, 'sh', '-c', SCRIPT + '; id'],
    'sh with the script and an extra argument': [*_OPTS, CONTAINER_ID, 'sh', '-c', SCRIPT, 'x'],
    'sh -c with no script': [*_OPTS, CONTAINER_ID, 'sh', '-c'],
    'sh alone': [*_OPTS, CONTAINER_ID, 'sh'],
    'no program': [*_OPTS, CONTAINER_ID],
    'sh -ec with the fixed script': [*_OPTS, CONTAINER_ID, 'sh', '-ec', SCRIPT],
    # not docker exec
    'another binary': ['docker', *_GOOD[1:]],
    'docker run': [EXPECTED_DOCKER, 'run', *_GOOD[2:]],
    'compose exec': [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT, 'exec', 'postgres', 'sh', '-c', SCRIPT],
    'empty': [],
}


@pytest.mark.parametrize('argv', list(_BAD_EXECS.values()), ids=list(_BAD_EXECS))
def test_check_dev_exec_argv_refuses(monkeypatch: pytest.MonkeyPatch, argv: list[str]):
    """Addendum 22 (4): -i, -t, --privileged, --env-file, any other -e, any unlisted program -- and more."""
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv(argv)


def test_check_dev_exec_argv_accepts_exactly_the_two_rows_of_the_table():
    """The other side of the gate: a check that refused everything would pass every case above.

    The 21:47 tightening on tj-iee6sn.13.5 replaced the free `[-u] [-w] [-e]` grammar with a table:
    each whole tail runs with its ONE option set, in order. Those two argv, as a list or a tuple and
    with any container id the lookup can print, are accepted; every looser form is refused below.
    """
    stack.check_dev_exec_argv(_GOOD)
    stack.check_dev_exec_argv(tuple(_GOOD))
    stack.check_dev_exec_argv([*_OPTS, 'f' * 64, *_ID_AND_TAIL[1:]])
    stack.check_dev_exec_argv(expected_migrate_exec())
    stack.check_dev_exec_argv(tuple(expected_migrate_exec('f' * 64)))


_MIGRATE = expected_migrate_exec()
_MIGRATE_TAIL = _MIGRATE[-3:]
_DB_TAIL = _ID_AND_TAIL[1:]
_EXEC = [EXPECTED_DOCKER, 'exec']

# The forms the free grammar accepted and the table refuses, and each tail with the OTHER tail's
# options (the architect's 21:47 list): the database script without -u, without -e, with -w /code, as
# root; the alembic wrapper with -u postgres or -e PGOPTIONS.
_LOOSE_EXECS = {
    # the database tail
    'db: no options at all': [*_EXEC, CONTAINER_ID, *_DB_TAIL],
    'db: without -u': [*_EXEC, '-e', EXPECTED_PGOPTIONS, CONTAINER_ID, *_DB_TAIL],
    'db: without -e': [*_EXEC, '-u', 'postgres', CONTAINER_ID, *_DB_TAIL],
    'db: plus -w /code': [*_OPTS, '-w', '/code', CONTAINER_ID, *_DB_TAIL],
    'db: -w /code first': [*_EXEC, '-w', '/code', *_OPTS[2:], CONTAINER_ID, *_DB_TAIL],
    'db: options reordered': [*_EXEC, '-e', EXPECTED_PGOPTIONS, '-u', 'postgres', CONTAINER_ID, *_DB_TAIL],
    'db: as root': [*_EXEC, '-u', 'root', '-e', EXPECTED_PGOPTIONS, CONTAINER_ID, *_DB_TAIL],
    'db: as root, no -e': [*_EXEC, '-u', 'root', CONTAINER_ID, *_DB_TAIL],
    'db: the alembic option set': [*_EXEC, '-w', '/code', CONTAINER_ID, *_DB_TAIL],
    # the alembic tail
    'migrate: no options at all': [*_EXEC, CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: with -u postgres': [*_EXEC, '-u', 'postgres', '-w', '/code', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: -w then -u postgres': [*_EXEC, '-w', '/code', '-u', 'postgres', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: with -e PGOPTIONS': [*_EXEC, '-w', '/code', '-e', EXPECTED_PGOPTIONS, CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: the database option set': [*_OPTS, CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: as root': [*_EXEC, '-u', 'root', '-w', '/code', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: -w /': [*_EXEC, '-w', '/', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: -w /code twice': [*_EXEC, '-w', '/code', '-w', '/code', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: --workdir /code': [*_EXEC, '--workdir', '/code', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: -w/code attached': [*_EXEC, '-w/code', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: -it': [*_EXEC, '-it', '-w', '/code', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: --privileged': [*_EXEC, '-w', '/code', '--privileged', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: --env-file': [*_EXEC, '-w', '/code', '--env-file', '/workspace/.env', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: -e another': [*_EXEC, '-w', '/code', '-e', 'X=1', CONTAINER_ID, *_MIGRATE_TAIL],
    'migrate: an extra argument': [*_MIGRATE, 'x'],
    'migrate: no id': [*_EXEC, '-w', '/code', *_MIGRATE_TAIL],
    'migrate: a container name': [*_EXEC, '-w', '/code', 'data_store', *_MIGRATE_TAIL],
    'migrate: bash': [*_EXEC, '-w', '/code', CONTAINER_ID, 'bash', '-c', _MIGRATE_TAIL[2]],
    # no upgrade, stamp, downgrade or check is constructible: any edit of the wrapper is another tail
    'migrate: upgrade for current': [*_MIGRATE[:-1], _MIGRATE_TAIL[2].replace('current history', 'upgrade history')],
    'migrate: upgrade head': [*_MIGRATE[:-1], _MIGRATE_TAIL[2].replace('current history', 'upgrade head')],
    'migrate: stamp head': [*_MIGRATE[:-1], _MIGRATE_TAIL[2].replace('current history', 'stamp head')],
    'migrate: downgrade': [*_MIGRATE[:-1], _MIGRATE_TAIL[2].replace('current history', 'downgrade base')],
    'migrate: check': [*_MIGRATE[:-1], _MIGRATE_TAIL[2].replace('current history', 'check')],
    'migrate: alembic upgrade head': [
        *_EXEC,
        '-w',
        '/code',
        CONTAINER_ID,
        '/code/.venv/bin/alembic',
        'upgrade',
        'head',
    ],
    'migrate: alembic current directly': [*_EXEC, '-w', '/code', CONTAINER_ID, '/code/.venv/bin/alembic', 'current'],
    'migrate: the script plus a word': [*_MIGRATE[:-1], _MIGRATE_TAIL[2] + '\n/code/.venv/bin/alembic upgrade head'],
}


@pytest.mark.parametrize('argv', list(_LOOSE_EXECS.values()), ids=list(_LOOSE_EXECS))
def test_check_dev_exec_argv_refuses_a_tail_with_any_option_set_but_its_own(monkeypatch: pytest.MonkeyPatch, argv):
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused):
        stack.check_dev_exec_argv(argv)


def test_the_upgrade_substitution_really_changes_the_tail():
    """The mutation cases above are only meaningful if the replacement happened."""
    assert _MIGRATE_TAIL[2].count('current history') == 1
    assert _LOOSE_EXECS['migrate: upgrade for current'] != _MIGRATE


def test_the_exec_command_table_holds_exactly_these_rows():
    """Membership, not just refusal: a row that grew, or an option set that widened, passes every refusal."""
    # tj-iee6sn.13.6: three more rows, one per http_get name, each with NO option at all (no user,
    # no workdir, no variable). Their scripts are pinned by their parts and by running them, in
    # test_dev_http_get.py. reaper_pending was DROPPED (ADR tj-4rr0la addendum 24): its items carry
    # owner strings (owner, last_run.actor, usage.readByClients30d) that redact cannot know.
    assert {
        ('sh', '-c', stack.DEV_DB_TABLES_SCRIPT): ('-u', 'postgres', '-e', EXPECTED_PGOPTIONS),
        ('sh', '-c', stack.DEV_MIGRATE_STATUS_SCRIPT): ('-w', '/code'),
        ('sh', '-c', stack.DEV_HTTP_GET_TAILS['ping'][2]): (),
        ('sh', '-c', stack.DEV_HTTP_GET_TAILS['ui_config'][2]): (),
        ('sh', '-c', stack.DEV_HTTP_GET_TAILS['usage_summary'][2]): (),
    } == stack.DEV_EXEC_COMMANDS
    assert len(stack.DEV_EXEC_COMMANDS) == 5, 'two http_get names share one script'
    assert 'reaper_pending' not in stack.DEV_HTTP_GET_TAILS
    assert stack.DEV_EXEC_PGOPTIONS == EXPECTED_PGOPTIONS
    assert stack.DEV_DB_TABLES_TAIL == ('sh', '-c', SCRIPT)
    assert stack.DEV_MIGRATE_STATUS_TAIL == ('sh', '-c', stack.DEV_MIGRATE_STATUS_SCRIPT)
    removed = ('DEV_EXEC_USERS', 'DEV_EXEC_WORKDIRS', 'DEV_EXEC_PROGRAMS', 'DEV_EXEC_TAILS')
    assert [name for name in removed if hasattr(stack, name)] == [], 'a free-grammar allowlist came back'


def test_the_exec_builders_run_check_dev_exec_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The call site: with the check replaced by a refusing spy, the verb ends 'refused' with NO exec run."""
    seen: list[tuple[str, ...]] = []

    def spy(argv):
        seen.append(tuple(argv))
        raise stack.Refused('spy refusal')

    monkeypatch.setattr(stack, 'check_dev_exec_argv', spy)
    with pytest.raises(stack.Refused, match='spy refusal'):
        stack.dev_db_tables_steps(STACK_DIR, CONTAINER_ID)
    assert seen == [tuple(expected_exec())]
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('db_tables', {})
    assert result['status'] == 'refused' and result['message'] == 'spy refusal', result
    assert [step.argv[1] for step in rig.docker.steps] == ['ps'], 'an exec ran past a refusing check'


@pytest.mark.parametrize('container_id', ['db', 'C0FFEE000001', 'c0ffee00000', '', 'c0ffee000001 x', None, 12])
def test_dev_exec_steps_refuses_a_non_id_before_building_anything(container_id: object):
    with pytest.raises(stack.Refused, match='not a container id'):
        stack.dev_db_tables_steps(STACK_DIR, container_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    'tail',
    [('bash', '-c', SCRIPT), ('sh', '-c', 'id'), ('sh', '-c', SCRIPT, 'x'), ('psql',), ()],
    ids=['bash', 'another script', 'an extra argument', 'psql', 'empty'],
)
def test_dev_exec_steps_refuses_any_tail_but_a_listed_one(tail: tuple[str, ...]):
    with pytest.raises(stack.Refused, match='fixed programs'):
        stack.dev_exec_steps(STACK_DIR, CONTAINER_ID, tail)


def test_dev_exec_steps_takes_no_option_argument():
    """The options come from the table: a caller cannot choose a user, a working directory or -e."""
    assert list(inspect.signature(stack.dev_exec_steps).parameters) == ['stack_dir', 'container_id', 'tail']


@pytest.mark.parametrize(
    'options',
    [{'user': 'root'}, {'workdir': '/'}, {'user': 'postgres', 'workdir': '/tmp'}, {'read_only_postgres': False}],
    ids=['user root', 'workdir /', 'workdir /tmp', 'read-only off'],
)
def test_dev_exec_steps_refuses_an_option_keyword(options: dict[str, object]):
    with pytest.raises(TypeError):
        stack.dev_exec_steps(STACK_DIR, CONTAINER_ID, stack.DEV_DB_TABLES_TAIL, **options)  # type: ignore[call-arg]


@pytest.mark.parametrize('tail', ['db', 'migrate'])
def test_dev_exec_steps_builds_each_tail_with_its_own_option_set(tail: str):
    """Through the generic builder: the table row's options, whichever tail is named."""
    if tail == 'db':
        steps = stack.dev_exec_steps(STACK_DIR, CONTAINER_ID, ('sh', '-c', SCRIPT))
        assert [list(step.argv) for step in steps] == [expected_exec()]
    else:
        steps = stack.dev_exec_steps(STACK_DIR, CONTAINER_ID, ('sh', '-c', stack.DEV_MIGRATE_STATUS_SCRIPT))
        assert [list(step.argv) for step in steps] == [expected_migrate_exec()]


# ===============================================================================================
# pick_dev_target: exactly one running container, else refused -- and then NO exec.
# ===============================================================================================


@pytest.mark.parametrize(
    ('stdout', 'match'),
    [
        (b'', 'no running postgres container'),
        (b'\n\n  \n', 'no running postgres container'),
        (b'c0ffee000001\nc0ffee000002\n', '2 running postgres containers'),
        (b'c0ffee000001\nc0ffee000002\nc0ffee000003\n', '3 running postgres containers'),
        (b'c0ffee000001\nc0ffee000001\n', '2 running postgres containers'),
        (b'db\n', 'not a container id'),
        (b'C0FFEE000001\n', 'not a container id'),
        (b'c0ffee000001 trader_joe\n', 'not a container id'),
        (b'Error response from daemon: 403 Forbidden\n', 'not a container id'),
    ],
    ids=['none', 'blank lines', 'two', 'three', 'the same id twice', 'a name', 'uppercase', 'two words', 'an error'],
)
def test_pick_dev_target_refuses_anything_but_exactly_one_id(stdout: bytes, match: str):
    with pytest.raises(stack.Refused, match=match) as refusal:
        stack.pick_dev_target('postgres', stdout)
    assert 'c0ffee' not in str(refusal.value), 'a refusal lists the ids it found'


@pytest.mark.parametrize('stdout', [b'c0ffee000001\n', b'c0ffee000001', b'\nc0ffee000001\n\n', b'  c0ffee000001  \n'])
def test_pick_dev_target_returns_the_one_id(stdout: bytes):
    assert stack.pick_dev_target('postgres', stdout) == CONTAINER_ID


_LOOKUP_ANSWERS = {
    'no running container': runner.ProcessResult(0, b'', b''),
    'two running containers': runner.ProcessResult(0, b'c0ffee000001\nc0ffee000002\n', b''),
    'a non-hex answer': runner.ProcessResult(0, b'trader_joe-postgres-1\n', b''),
    'the lookup failed': runner.ProcessResult(1, b'', b'Error response from daemon: 403 Forbidden'),
    'the lookup failed but printed one id': runner.ProcessResult(1, b'c0ffee000001\n', b''),
}


@pytest.mark.parametrize('answer', list(_LOOKUP_ANSWERS.values()), ids=list(_LOOKUP_ANSWERS))
def test_dev_db_tables_refuses_without_exec_unless_the_lookup_found_exactly_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: runner.ProcessResult
):
    """Addendum 22 (4)/(7): zero, two, a non-id or a failed lookup -> 'refused', and ZERO exec subprocesses."""

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1] == 'ps':
            return answer
        return default_response(step)

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=respond))
    result = rig.call('db_tables', {})
    assert result['status'] == 'refused', result
    assert [list(step.argv) for step in rig.docker.steps] == [expected_lookup('postgres')], 'an exec ran'
    assert 'details' not in result
    audit = json.loads(rig.audit_lines()[-1])
    assert audit['verb'] == 'db_tables' and audit['status'] == 'refused', audit


# ===============================================================================================
# THE VERB: closed schema, budget, result.
# ===============================================================================================


def test_dev_db_tables_takes_nothing_and_has_a_sixty_second_budget():
    schema = runner.VERB_SCHEMAS['db_tables']
    assert schema == {'properties': {}, 'required': [], 'type': 'object', 'additionalProperties': False}
    assert runner.VERB_TIMEOUT_SECONDS['db_tables'] == EXPECTED_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    'arguments',
    [{'sql': 'SELECT 1'}, {'container': CONTAINER_ID}, {'service': 'data_store'}, {'table': 'x'}, 'SELECT 1', None],
    ids=['sql', 'container', 'service', 'table', 'positional', 'none'],
)
def test_no_argument_reaches_dev_db_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: object):
    """The SQL, the container and the program are constants: any argument at all is refused before docker."""
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('db_tables', arguments)
    assert result['status'] == 'refused', result
    assert rig.docker.steps == []


def test_the_handler_and_the_builder_take_no_caller_string():
    """The only parameter the exec builder takes beyond the directory is the id, and that is checked hex."""
    assert list(inspect.signature(stack.dev_db_tables_steps).parameters) == ['stack_dir', 'container_id']
    assert list(inspect.signature(runner.AgentStack._dev_db_tables).parameters) == ['self', 'call']


def test_dev_db_tables_reports_the_table_map_and_the_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('db_tables', {})
    assert result['status'] == 'ok', result
    assert result['details'] == {'tables': dict.fromkeys(EXPECTED_TABLES, True), 'alembic_revision': 'd7e2a9c35b18'}
    assert result['message'] == '8 of 8 tables present; alembic revision: d7e2a9c35b18'
    audit = json.loads(rig.audit_lines()[-1])
    assert audit['status'] == 'ok' and audit['arguments'] == {}, audit


def test_dev_db_tables_reports_absent_tables_and_no_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stdout = ''.join(f'{name}|{"t" if index % 2 else "f"}\n' for index, name in enumerate(EXPECTED_TABLES[:-1]))
    stdout += 'alembic_version|f\n'

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1] == 'exec':
            return runner.ProcessResult(0, stdout.encode(), b'')
        return default_response(step)

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=respond))
    result = rig.call('db_tables', {})
    assert result['status'] == 'ok', result
    tables = result['details']['tables']
    assert tables == {name: bool(index % 2) for index, name in enumerate(EXPECTED_TABLES[:-1])} | {
        'alembic_version': False
    }
    assert result['details']['alembic_revision'] is None
    assert result['message'] == '3 of 8 tables present; alembic revision: none (no alembic_version row)'


def test_a_failed_exec_is_failed_and_reports_no_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv[1] == 'exec':
            return runner.ProcessResult(1, b'psql: error: connection refused\n', b'')
        return default_response(step)

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=respond))
    result = rig.call('db_tables', {})
    assert result['status'] == 'failed' and 'details' not in result, result
    assert result['exit_status'] == 1


# ===============================================================================================
# parse_db_tables: only the eight names with t/f, and a well-formed revision; everything else ignored.
# ===============================================================================================


def test_parse_db_tables_reads_every_listed_name_and_the_revision():
    lines = [f'{name}|t' for name in EXPECTED_TABLES] + ['version_num|b3d9e1a47c62']
    assert stack.parse_db_tables('\n'.join(lines).encode()) == (dict.fromkeys(EXPECTED_TABLES, True), 'b3d9e1a47c62')


@pytest.mark.parametrize(
    'line',
    [
        'pg_authid|t',
        'users|t',
        'public.dataset_read|t',
        'DATASET_READ|t',
        ' dataset_read|t',
        'dataset_read|true',
        'dataset_read|T',
        'dataset_read|1',
        'dataset_read|',
        'dataset_read| t',
        'dataset_read|t|extra',
        'dataset_read',
        '<redacted>|t',
        'ERROR:  permission denied for table dataset_read',
    ],
)
def test_parse_db_tables_ignores_unknown_names_and_values_other_than_t_and_f(line: str):
    assert stack.parse_db_tables(f'{line}\n'.encode()) == ({}, None)


@pytest.mark.parametrize(
    'value',
    ['', 'b3d9-e1a4', 'b3d9 e1a4', 'b3d9e1a47c62;DROP', '<redacted>', 'a' * 65, 'b3d9e1a47c62\x00', 'é1'],
    ids=['empty', 'a dash', 'a space', 'a semicolon', 'redacted', '65 chars', 'a nul', 'non-ascii'],
)
def test_parse_db_tables_ignores_a_malformed_revision(value: str):
    assert stack.parse_db_tables(f'version_num|{value}\n'.encode()) == ({}, None)


def test_parse_db_tables_tolerates_bytes_that_are_not_utf8():
    assert stack.parse_db_tables(b'\xff\xfe\ndataset_read|f\n') == ({'dataset_read': False}, None)


# ===============================================================================================
# THE SQL AND THE SCRIPT ARE CONSTANTS.
# ===============================================================================================


def test_the_table_set_and_the_sql_are_module_constants_built_from_the_eight_names():
    """Addendum 22 (4): ONE constant query; the second statement only reads alembic_version."""
    assert stack.DEV_DB_TABLES == EXPECTED_TABLES
    assert stack.DEV_DB_VERSION_TABLE == 'alembic_version'
    assert stack._DEV_DB_TABLES_QUERY == EXPECTED_TABLES_QUERY
    assert stack._DEV_DB_VERSION_QUERY == EXPECTED_VERSION_QUERY


def test_the_tables_are_the_store_models_tables():
    """The eight are the seven store models' TABLE_NAME values plus alembic's own version table."""
    models = Path(__file__).resolve().parents[3] / 'server' / 'data' / 'store' / 'app' / 'database' / 'models'
    declared = set()
    for path in models.glob('*.py'):
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('TABLE_NAME = '):
                declared.add(line.split('=', 1)[1].strip().strip('\'"'))
    assert declared == set(EXPECTED_TABLES) - {'alembic_version'}, sorted(declared)


def test_the_script_carries_both_queries_verbatim_and_no_placeholder():
    """Nothing is interpolated into the SQL: both statements appear whole, and no template marker survives."""
    assert f'query "{EXPECTED_TABLES_QUERY}"' in SCRIPT
    assert f'query "{EXPECTED_VERSION_QUERY}"' in SCRIPT
    assert '@' not in SCRIPT.replace('://', ''), 'a template marker was left unreplaced'
    # printf '%s\n' is the script's own output form; a Python-side format marker or extra shell
    # parameter would be the sign of an argument reaching it.
    for marker in ('{}', '{0}', '%(', '$2', '$@', '$*', '`'):
        assert marker not in SCRIPT, marker
    # Only the container's own variables and the script's own locals are expanded.
    assert [var for var in ('$POSTGRES_USER', '$POSTGRES_DB', '$tables', '$version', '$1') if var not in SCRIPT] == []


def test_the_script_runs_psql_unattended_unaligned_and_stopping_on_error():
    """-X (no psqlrc), -At (tuples only, unaligned), -v ON_ERROR_STOP=1, the container's own user and db."""
    assert 'psql -X -At -F \'|\' -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1" 2>&1' in SCRIPT
    assert SCRIPT.count('psql ') == 1, 'a second psql invocation bypasses the query() wrapper'


def test_the_script_names_the_five_redaction_variables():
    assert stack._DEV_REDACT_NAMES.split() == list(EXPECTED_REDACT_NAMES)
    assert f'split("{" ".join(EXPECTED_REDACT_NAMES)}", names, " ")' in SCRIPT
    assert SCRIPT.count('| redact') == 4, 'every printf of query output goes through redact'


# ===============================================================================================
# THE REDACTION, BEHAVIOURALLY: the real script under sh, a FAKE psql, FAKE secrets.
#
# The fake psql prints the value of every secret variable it can see, as an error message from a
# compromised or verbose psql would, then the shape the script parses. If any fake value survives
# to the script's stdout or stderr, the scrub has failed. awk is the system's (mawk on Debian, as in
# the postgres:17-trixie image); the environment is built here and never inherits the real one.
# ===============================================================================================

_FAKE_PSQL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_PSQL_LOG"
for name in INSTANCE_WRITE_SECRET POSTGRES_PASS POSTGRES_PASSWORD ALPACA_API_KEY ALPACA_API_SECRET FAKE_DSN FAKE_ASYNC_DSN; do
  eval "printf 'leak %s=%s\n' \"\$name\" \"\$$name\""
done
case "$*" in
  *version_num*) [ "$FAKE_FAIL_ON" = version ] && { echo 'ERROR: boom' >&2; exit 3; }
                 echo 'version_num|d7e2a9c35b18' ;;
  *) [ "$FAKE_FAIL_ON" = tables ] && { echo 'ERROR: boom' >&2; exit 3; }
     echo 'dataset_read|t'; echo 'alembic_version|t' ;;
esac
"""

_FAKE_VALUES = {
    'plain': 'dummy-not-a-secret',
    'regex metacharacters': 'dummy.*[a-z]+^$(x)|?{2}\\1',
    'awk specials': 'dummy"\\&\\\\%s&\\n',
    'shell specials': "dummy'$(id)`id`;|>",
    'surrounding spaces': '  dummy spaced  ',
    'a slash and an equals': 'dummy/a=b/c',
    'non-ascii': 'dummy-ünïcødé-€',
}


def _run_script(tmp_path: Path, secret: str, fail_on: str = '') -> tuple[subprocess.CompletedProcess, Path]:
    sh = shutil.which('sh')
    if sh is None:
        pytest.skip('no sh on PATH')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    psql = bin_dir / 'psql'
    psql.write_text(_FAKE_PSQL)
    psql.chmod(0o755)
    log = tmp_path / 'psql.log'
    env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'FAKE_PSQL_LOG': str(log),
        'FAKE_FAIL_ON': fail_on,
        'POSTGRES_USER': 'dummy_user',
        'POSTGRES_DB': 'dummy_db',
        'INSTANCE_WRITE_SECRET': secret,
        'POSTGRES_PASS': 'dummy-pass-value',
        'POSTGRES_PASSWORD': 'dummy-password-value',
        'ALPACA_API_KEY': 'dummy-alpaca-key',
        'ALPACA_API_SECRET': 'dummy-alpaca-secret',
        'FAKE_DSN': 'postgresql://dummy_user:dummy-dsn-pw@db:5432/dummy_db',
        'FAKE_ASYNC_DSN': 'postgresql+asyncpg://dummy_user:dummy-async-pw@db/dummy_db',
    }
    completed = subprocess.run([sh, '-c', SCRIPT], env=env, capture_output=True, timeout=30, check=False)
    return completed, log


def _secrets(secret: str) -> list[str]:
    return [
        secret,
        'dummy-pass-value',
        'dummy-password-value',
        'dummy-alpaca-key',
        'dummy-alpaca-secret',
        'dummy-dsn-pw',
        'dummy-async-pw',
    ]


@pytest.mark.parametrize('fail_on', ['', 'tables', 'version'], ids=['success', 'first query fails', 'second fails'])
@pytest.mark.parametrize('secret', list(_FAKE_VALUES.values()), ids=list(_FAKE_VALUES))
def test_no_fake_secret_value_survives_the_script(tmp_path: Path, secret: str, fail_on: str):
    """Addendum 22 (4) SCRUBBING, run: every secret value, and every postgres:// value, is <redacted>.

    On the success path and on both psql-error exit paths (exit 1), whatever the value's characters.
    """
    completed, _ = _run_script(tmp_path, secret, fail_on)
    printed = completed.stdout.decode('utf-8', errors='replace') + completed.stderr.decode('utf-8', errors='replace')
    leaked = [value for value in _secrets(secret) if value.strip() in printed]
    assert leaked == [], f'the script printed fake secret values: {leaked}'
    assert 'leak INSTANCE_WRITE_SECRET=<redacted>' in printed, printed
    assert 'leak FAKE_DSN=<redacted>' in printed and 'leak FAKE_ASYNC_DSN=<redacted>' in printed, printed
    assert completed.returncode == (1 if fail_on else 0), (completed.returncode, printed)
    if not fail_on:
        assert 'dataset_read|t' in printed and 'version_num|d7e2a9c35b18' in printed, printed


def test_the_script_hands_psql_the_fixed_flags_and_the_two_constant_queries(tmp_path: Path):
    """What psql actually receives: the container's own user and db, the flags, and only the constant SQL."""
    completed, log = _run_script(tmp_path, 'dummy-not-a-secret')
    assert completed.returncode == 0, completed
    flags = '-X -At -F | -v ON_ERROR_STOP=1 -U dummy_user -d dummy_db -c'
    assert log.read_text().splitlines() == [f'{flags} {EXPECTED_TABLES_QUERY}', f'{flags} {EXPECTED_VERSION_QUERY}']


def test_the_script_skips_the_version_query_when_alembic_version_is_absent(tmp_path: Path):
    """The second statement runs only when the first reported `alembic_version|t`."""
    sh = shutil.which('sh')
    if sh is None:
        pytest.skip('no sh on PATH')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'psql.log'
    (bin_dir / 'psql').write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FAKE_PSQL_LOG"\necho alembic_version\\|f\n')
    (bin_dir / 'psql').chmod(0o755)
    env = {'PATH': f'{bin_dir}:/usr/bin:/bin', 'FAKE_PSQL_LOG': str(log), 'POSTGRES_USER': 'u', 'POSTGRES_DB': 'd'}
    completed = subprocess.run([sh, '-c', SCRIPT], env=env, capture_output=True, timeout=30, check=False)
    assert completed.returncode == 0 and completed.stdout == b'alembic_version|f\n', completed
    assert len(log.read_text().splitlines()) == 1


def test_the_real_environment_is_never_handed_to_the_script_under_test(tmp_path: Path, monkeypatch):
    """A guard on this module itself: a variable set in the test process does not reach the child."""
    monkeypatch.setenv('INSTANCE_WRITE_SECRET', 'dummy-from-the-parent-process')
    completed, _ = _run_script(tmp_path, 'dummy-not-a-secret')
    assert b'dummy-from-the-parent-process' not in completed.stdout + completed.stderr
    assert os.environ['INSTANCE_WRITE_SECRET'] == 'dummy-from-the-parent-process'


# ===============================================================================================
# THE SCRUB CONTRACT (tj-4kyqk9), which slices 2 and 3 reuse over alembic and HTTP output:
#
#   secrets   every non-empty value of the five names and of every DB-URL variable, PLUS each
#             non-empty line of a multi-line value as its own secret (dedupe optional, unpinned)
#   union     (tj-iee6sn.13.9) EVERY occurrence of EVERY secret over the ORIGINAL output, stepping
#             one character past each match start; overlapping AND abutting spans merge; each merged
#             span prints as ONE <redacted>. Order of secrets is irrelevant. Over-redact, never under.
#   windows   the scan runs in windows of 4096 (or the longest secret) carrying maxlen-1 characters,
#             so a secret straddling a window boundary, or spans touching across one, still merge
#   matching  literal (index), never a regex: a value's metacharacters mean nothing
#   empty     an empty value is never a secret
#   truncated REDACT_TRUNCATED=1, a constant the caller sets, also redacts the longest trailing proper
#             prefix of any secret (the cut of a byte bound); off by default; db_tables never sets it
#
# Each test hands the script's ONE query an exact output through a fake psql and pins the exact bytes
# printed. Exact, not `not in`: a partial leak (the rest of a longer value around <redacted>) is the
# failure these exist for, and only the whole output shows it. FAKE values only, as above.
# ===============================================================================================

# The redact() function alone: its own module constant, which every exec wrapper embeds verbatim.
REDACT_FUNCTION = stack._DEV_REDACT_FUNCTION
_SCRUB_TIMEOUT = 10

_FAKE_PSQL_FIXED = r"""#!/bin/sh
for arg in "$@"; do printf '%s\n' "$arg"; done >> "$FAKE_PSQL_LOG"
cat "$FAKE_PSQL_OUT"
exit "$FAKE_PSQL_RC"
"""


def _scrub(
    tmp_path: Path, secrets: dict[str, str], output: str | bytes, rc: int = 0, awk_log: bool = False
) -> subprocess.CompletedProcess:
    """Run the whole script with SECRETS as its only secret variables and OUTPUT as psql's.

    OUTPUT carries no `alembic_version|t` line, so exactly one query runs and its output is printed
    once, through redact. AWK_LOG puts a shim ahead of the system awk that records its argv.
    """
    sh = shutil.which('sh')
    if sh is None:
        pytest.skip('no sh on PATH')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / 'psql').write_text(_FAKE_PSQL_FIXED)
    (bin_dir / 'psql').chmod(0o755)
    if awk_log:
        real_awk = shutil.which('awk', path='/usr/bin:/bin')
        assert real_awk is not None
        (bin_dir / 'awk').write_text(
            f'#!/bin/sh\nfor arg in "$@"; do printf \'%s\\n\' "$arg"; done >> "$FAKE_AWK_LOG"\nexec {real_awk} "$@"\n'
        )
        (bin_dir / 'awk').chmod(0o755)
    out_file = tmp_path / 'psql.out'
    out_file.write_bytes(output.encode() if isinstance(output, str) else output)
    env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'FAKE_PSQL_LOG': str(tmp_path / 'psql.log'),
        'FAKE_PSQL_OUT': str(out_file),
        'FAKE_PSQL_RC': str(rc),
        'FAKE_AWK_LOG': str(tmp_path / 'awk.log'),
        'POSTGRES_USER': 'dummy_user',
        'POSTGRES_DB': 'dummy_db',
        **secrets,
    }
    return subprocess.run([sh, '-c', SCRIPT], env=env, capture_output=True, timeout=_SCRUB_TIMEOUT, check=False)


def _redact_only(stdin: bytes, secrets: dict[str, str]) -> subprocess.CompletedProcess:
    """The script's own redact() over STDIN, with no psql at all."""
    sh = shutil.which('sh')
    if sh is None:
        pytest.skip('no sh on PATH')
    env = {'PATH': '/usr/bin:/bin', **secrets}
    return subprocess.run(
        [sh, '-c', REDACT_FUNCTION + '\nredact'],
        input=stdin,
        env=env,
        capture_output=True,
        timeout=_SCRUB_TIMEOUT,
        check=False,
    )


def test_a_multi_line_secret_value_is_redacted(tmp_path: Path):
    completed, _ = _run_script(tmp_path, 'dummy-first-line\ndummy-second-line')
    printed = completed.stdout.decode()
    assert 'dummy-first-line' not in printed and 'dummy-second-line' not in printed, printed


def test_a_secret_contained_in_another_does_not_expose_the_rest_of_it(tmp_path: Path):
    # INSTANCE_WRITE_SECRET, first in the list, is 'dummy': a prefix of every other fake value.
    completed, _ = _run_script(tmp_path, 'dummy')
    printed = completed.stdout.decode()
    assert '-password-value' not in printed and '-alpaca-key' not in printed, printed


def test_a_value_that_is_both_a_prefix_and_a_suffix_of_another_leaves_nothing_of_the_longer_one(tmp_path: Path):
    """The union of spans, whatever the list order.

    The short value is listed FIRST, so list order would replace it inside the long one twice and
    print `<redacted>-mid-<redacted>`: the long value's middle in clear. The two short occurrences
    that touch (`dummy-abdummy-ab`) are abutting spans, which merge into ONE <redacted> (tj-iee6sn.13.9).
    """
    secrets = {'INSTANCE_WRITE_SECRET': 'dummy-ab', 'ALPACA_API_SECRET': 'dummy-ab-mid-dummy-ab'}
    completed = _scrub(tmp_path, secrets, 'x dummy-ab-mid-dummy-ab y\ndummy-ab\nz dummy-abdummy-ab')
    assert completed.stdout == b'x <redacted> y\n<redacted>\nz <redacted>\n', completed
    assert completed.returncode == 0 and completed.stderr == b'', completed


@pytest.mark.parametrize(
    'secrets',
    [
        {'INSTANCE_WRITE_SECRET': 'dummy-line-one\ndummy-line-two'},
        # Each line is ALSO another variable's whole value: the duplicates collapse, the result is the same.
        {
            'INSTANCE_WRITE_SECRET': 'dummy-line-one\ndummy-line-two',
            'POSTGRES_PASS': 'dummy-line-one',
            'ALPACA_API_KEY': 'dummy-line-two',
        },
    ],
    ids=['lines only within the value', 'each line is also another value'],
)
def test_each_line_of_a_multi_line_value_is_a_secret_and_so_is_the_whole_value(tmp_path: Path, secrets: dict):
    """The whole value is one secret and each of its lines is another.

    Matched across the line break, the whole value is ONE <redacted>; its lines printed apart, or in
    the other order, are each redacted on their own.
    """
    output = 'a dummy-line-one\ndummy-line-two b\nc dummy-line-two\ndummy-line-one d\ne dummy-line-two'
    completed = _scrub(tmp_path, secrets, output)
    assert completed.stdout == b'a <redacted> b\nc <redacted>\n<redacted> d\ne <redacted>\n', completed
    assert completed.returncode == 0, completed


@pytest.mark.parametrize(
    'secrets',
    [
        {},
        dict.fromkeys(EXPECTED_REDACT_NAMES, ''),
        # An empty line inside a multi-line value is not a secret either.
        {'INSTANCE_WRITE_SECRET': 'dummy-a\n\ndummy-b', 'POSTGRES_PASS': ''},
    ],
    ids=['every name unset', 'every name set but empty', 'a multi-line value with an empty line'],
)
def test_an_empty_secret_variable_does_not_redact_everything(tmp_path: Path, secrets: dict):
    """An empty value is never a secret.

    Under mawk and gawk index(s, "") is 1, so an empty secret would loop for ever (the timeout
    catches that); under busybox it would match nothing.
    """
    completed = _scrub(tmp_path, secrets, 'dataset_read|t\n\nkept as printed')
    assert completed.stdout == b'dataset_read|t\n\nkept as printed\n', completed
    assert completed.returncode == 0 and completed.stderr == b'', completed


def test_duplicate_values_are_redacted_once_each_occurrence(tmp_path: Path):
    secrets = {
        'POSTGRES_PASS': 'dummy-dup',
        'POSTGRES_PASSWORD': 'dummy-dup',
        'FAKE_DSN': 'postgresql://dummy_user:dummy-dup@db/dummy_db',
        'FAKE_DSN_TWICE': 'postgresql://dummy_user:dummy-dup@db/dummy_db',
    }
    output = 'dummy-dup|dummy-dup\npostgresql://dummy_user:dummy-dup@db/dummy_db'
    completed = _scrub(tmp_path, secrets, output)
    assert completed.stdout == b'<redacted>|<redacted>\n<redacted>\n', completed


# Each value, with a DECOY the value would match if it were read as a regex but does not contain
# literally: the decoy must print unchanged. A value that is not a valid regex at all (the unbalanced
# `[`) would make a regex scrub fail outright.
_SPECIAL_VALUES = [
    ('dummy.*[]\\()&$^"/ ', 'dummy'),
    ('dummy.*', 'dummy-decoy-kept'),
    ('du.my', 'duXmy'),
    ('^dummy$', 'dummy'),
    ('dummy[a-z]+', 'dummyabc'),
    ('dummy(a|b)', 'dummya'),
    ('dummy&\\1\\\\&', 'dummy&'),
    ('dummy/"x"/', 'dummy/'),
]


@pytest.mark.parametrize(('value', 'decoy'), _SPECIAL_VALUES, ids=[value for value, _ in _SPECIAL_VALUES])
def test_a_value_with_regex_or_awk_special_characters_is_matched_literally(tmp_path: Path, value: str, decoy: str):
    assert value not in decoy
    output = f'pre {value} mid {decoy} post\n{value}'
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': value}, output)
    # str.replace is literal: the oracle for ONE secret, which is all this test hands the script.
    assert completed.stdout == (output.replace(value, '<redacted>') + '\n').encode(), completed
    assert decoy.encode() in completed.stdout
    assert completed.returncode == 0 and completed.stderr == b'', completed


def test_a_multi_line_value_in_psql_error_output_is_redacted_and_the_exit_is_one(tmp_path: Path):
    """The error path prints the captured output through the same scrub, then exits 1."""
    secrets = {'INSTANCE_WRITE_SECRET': 'dummy-err-1\ndummy-err-2', 'ALPACA_API_KEY': 'dummy-err-key'}
    output = 'ERROR: dummy-err-1\ndummy-err-2\nDETAIL: dummy-err-2 dummy-err-key'
    completed = _scrub(tmp_path, secrets, output, rc=3)
    assert completed.returncode == 1, completed
    assert completed.stdout == b'ERROR: <redacted>\nDETAIL: <redacted> <redacted>\n', completed


def test_empty_output_prints_nothing(tmp_path: Path):
    """redact() over no input prints no bytes at all.

    The script over an empty psql result prints no text: only the newline of its own printf,
    unchanged since before tj-4kyqk9.
    """
    alone = _redact_only(b'', {'INSTANCE_WRITE_SECRET': 'dummy-sec'})
    assert (alone.returncode, alone.stdout, alone.stderr) == (0, b'', b''), alone
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': 'dummy-sec'}, '')
    assert completed.returncode == 0 and completed.stdout.strip() == b'' and completed.stderr == b'', completed


def test_no_secret_value_is_on_any_command_line(tmp_path: Path):
    """The values are read from ENVIRON inside awk, never expanded into an argv.

    Not into the script text, not into psql's arguments, not into awk's (its program text included).
    """
    names = (*EXPECTED_REDACT_NAMES, 'FAKE_DSN')
    for name in names:
        assert f'${name}' not in SCRIPT and f'${{{name}' not in SCRIPT, name
    secrets = {
        'INSTANCE_WRITE_SECRET': 'dummy-argv-1\ndummy-argv-2',
        'POSTGRES_PASSWORD': 'dummy-argv-pw',
        'FAKE_DSN': 'postgresql://dummy_user:dummy-argv-dsn@db/dummy_db',
    }
    completed = _scrub(tmp_path, secrets, 'dummy-argv-pw', awk_log=True)
    assert completed.stdout == b'<redacted>\n', completed
    psql_argv = (tmp_path / 'psql.log').read_text()
    awk_argv = (tmp_path / 'awk.log').read_text()
    assert 'ENVIRON' in awk_argv, 'the awk shim did not see the redact program'
    for value in ('dummy-argv-1', 'dummy-argv-2', 'dummy-argv-pw', 'dummy-argv-dsn'):
        assert value not in psql_argv and value not in awk_argv and value not in SCRIPT, value


def test_a_raw_soh_byte_prints_as_itself_and_a_value_containing_it_is_still_redacted(tmp_path: Path):
    r"""There is no in-flight marker any more (tj-iee6sn.13.9).

    A \001 already in the output is not a secret and prints AS ITSELF (gate ruling on tj-4kyqk9); a
    value containing \001 is matched like any other.
    """
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': 'dummy\x01mid'}, 'a\x01b\np dummy\x01mid q')
    assert completed.stdout == b'a\x01b\np <redacted> q\n', completed
    assert '\\001' not in REDACT_FUNCTION and '\x01' not in REDACT_FUNCTION, 'the marker came back'


def test_a_secret_cannot_match_the_placeholder_another_secret_left(tmp_path: Path):
    """Every match is found over the ORIGINAL output, and the placeholder is written only on output.

    So a value that is part of the word `<redacted>` does not turn the placeholder into
    `<<redacted>>`.
    """
    secrets = {'INSTANCE_WRITE_SECRET': 'dummy-long-value', 'POSTGRES_PASS': 'redacted', 'ALPACA_API_KEY': '>'}
    completed = _scrub(tmp_path, secrets, 'dummy-long-value')
    assert completed.stdout == b'<redacted>\n', completed


# ===============================================================================================
# THE UNION OF SPANS (tj-iee6sn.13.9; fixes tj-u0l5ec): every occurrence of every secret, merged
# when they overlap or touch, one <redacted> per merged span.
# ===============================================================================================


def test_overlapping_secrets_do_not_expose_the_rest_of_the_shorter_one(tmp_path: Path):
    """tj-u0l5ec: longest-first printed `<redacted>-tail`, the shorter secret's non-overlapped part."""
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': 'dummy-long', 'POSTGRES_PASS': 'g-tail'}, 'dummy-long-tail')
    assert completed.stdout == b'<redacted>\n', completed
    assert completed.returncode == 0 and completed.stderr == b'', completed


def test_a_chain_of_three_overlapping_secrets_is_one_placeholder(tmp_path: Path):
    """A overlaps B overlaps C, and A and C do not touch: the merge is transitive."""
    secrets = {'INSTANCE_WRITE_SECRET': 'dummy-AAAb', 'POSTGRES_PASS': 'bBBBc', 'ALPACA_API_KEY': 'cCCC-tail'}
    completed = _scrub(tmp_path, secrets, '<dummy-AAAbBBBcCCC-tail>')
    assert completed.stdout == b'<<redacted>>\n', completed


def test_abutting_secrets_are_one_placeholder(tmp_path: Path):
    """A immediately followed by B: two placeholders would show where one secret ends; the rule merges them."""
    secrets = {'INSTANCE_WRITE_SECRET': 'dummy-first', 'POSTGRES_PASS': 'dummy-second'}
    output = 'x dummy-firstdummy-second y\ndummy-secondXdummy-first\ndummy-seconddummy-first'
    completed = _scrub(tmp_path, secrets, output)
    assert completed.stdout == b'x <redacted> y\n<redacted>X<redacted>\n<redacted>\n', completed


@pytest.mark.parametrize(
    ('value', 'output', 'expected'),
    [
        # 'ab-dummy-ab' ends with what it starts with: printed twice sharing that part, the second
        # occurrence starts INSIDE the first. Stepping past the whole match would miss it.
        ('ab-dummy-ab', 'q ab-dummy-ab-dummy-ab r', 'q <redacted> r'),
        ('dummy-aba-dummy-aba-', 'q dummy-aba-dummy-aba-dummy-aba- r', 'q <redacted> r'),
        ('aba', 'xababay', 'x<redacted>y'),
        ('aa', 'aaa', '<redacted>'),
    ],
    ids=['shared prefix and suffix', 'periodic value', 'aba in ababa', 'aa in aaa'],
)
def test_self_overlapping_occurrences_are_all_found(tmp_path: Path, value: str, output: str, expected: str):
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': value}, output)
    assert completed.stdout == (expected + '\n').encode(), completed


def test_a_one_character_secret_is_redacted_everywhere_it_occurs(tmp_path: Path):
    """Every character a match: each run merges into one placeholder, the rest prints, and it terminates."""
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': 'a'}, 'banana\naardvark\n' + 'a' * 10000)
    assert completed.stdout == b'b<redacted>n<redacted>n<redacted>\n<redacted>rdv<redacted>rk\n<redacted>\n', completed


def test_a_value_of_only_newlines_terminates_and_its_empty_lines_are_not_secrets(tmp_path: Path):
    r"""'\n\n' is a non-empty value (so a secret); its three lines are empty (so not secrets)."""
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': '\n\n'}, 'a\n\nb\nc\n\n\nd')
    # 'c\n\n\nd': the two occurrences of '\n\n' overlap, so all three newlines are one span.
    assert completed.stdout == b'a<redacted>b\nc<redacted>d\n', completed


_ORDER_CASES = {
    'contained': (('dummy', 'dummy-pass-value', 'pass'), 'x dummy-pass-value y dummy z', 'x <redacted> y <redacted> z'),
    'overlap': (('dummy-long', 'g-tail', 'tail-end'), 'dummy-long-tail-end.', '<redacted>.'),
    'prefix and suffix': (
        ('dummy-ab', 'dummy-ab-mid-dummy-ab', 'mid'),
        'dummy-ab-mid-dummy-ab|mid',
        '<redacted>|<redacted>',
    ),
}


@pytest.mark.parametrize('order', list(itertools.permutations(range(3))), ids=lambda order: ''.join(map(str, order)))
@pytest.mark.parametrize('case', list(_ORDER_CASES), ids=list(_ORDER_CASES))
def test_the_result_does_not_depend_on_which_variable_holds_which_secret(tmp_path: Path, case: str, order: tuple):
    """Every permutation of three values over three of the names prints the same bytes."""
    values, output, expected = _ORDER_CASES[case]
    names = ('INSTANCE_WRITE_SECRET', 'POSTGRES_PASSWORD', 'ALPACA_API_SECRET')
    secrets = {names[slot]: values[index] for slot, index in enumerate(order)}
    completed = _scrub(tmp_path, secrets, output)
    assert completed.stdout == (expected + '\n').encode(), completed


def _union_oracle(text: str, values: list[str], truncated: bool = False) -> str:
    """The contract, stated as a reference over a plain list of booleans: covered or not, per character.

    A second, independent statement of the rule (Python, by character), so that the awk is checked
    against what the rule says, not against a copy of itself.
    """
    secrets = set()
    for value in values:
        if value:
            secrets.add(value)
            lines = value.split('\n')
            if len(lines) > 1:
                secrets.update(line for line in lines if line)
    covered = [False] * len(text)
    for secret in secrets:
        start = text.find(secret)
        while start >= 0:
            covered[start : start + len(secret)] = [True] * len(secret)
            start = text.find(secret, start + 1)
    if truncated:
        tail = max((k for s in secrets for k in range(1, len(s)) if text.endswith(s[:k])), default=0)
        covered[len(text) - tail :] = [True] * tail
    out, i = [], 0
    while i < len(text):
        if covered[i]:
            while i < len(text) and covered[i]:
                i += 1
            out.append('<redacted>')
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def test_the_oracle_states_the_rule():
    """The reference itself, on the cases above whose bytes are written out by hand."""
    assert _union_oracle('dummy-long-tail', ['dummy-long', 'g-tail']) == '<redacted>'
    assert _union_oracle('x dummy-firstdummy-second y', ['dummy-first', 'dummy-second']) == 'x <redacted> y'
    assert _union_oracle('xababay', ['aba']) == 'x<redacted>y'
    assert _union_oracle('a-b', ['a', 'b']) == '<redacted>-<redacted>'
    assert _union_oracle('head dummy-tru', ['dummy-truncated'], truncated=True) == 'head <redacted>'
    assert _union_oracle('head dummy-tru', ['dummy-truncated']) == 'head dummy-tru'


def _fuzz_cases(count: int = 40) -> list[tuple[list[str], str]]:
    """Seeded: short secrets over a two-letter alphabet, so overlaps, abutments and chains are dense."""
    rng = random.Random(1309)
    cases = []
    for _ in range(count):
        values = [''.join(rng.choice('ab') for _ in range(rng.randint(1, 4))) for _ in range(rng.randint(1, 3))]
        text = ''.join(rng.choice('ab.') for _ in range(rng.randint(0, 30)))
        cases.append((values, text))
    return cases


@pytest.mark.parametrize(('values', 'text'), _fuzz_cases())
def test_the_scrub_is_the_union_of_every_match_span(values: list[str], text: str):
    names = ('INSTANCE_WRITE_SECRET', 'POSTGRES_PASS', 'ALPACA_API_KEY')
    completed = _redact_only(text.encode() + b'\n', dict(zip(names, values, strict=False)))
    assert completed.stdout == (_union_oracle(text, values) + '\n').encode(), (values, text, completed)


# Windows. The scan looks at 4096 characters at a time (or the longest secret, if longer), carrying
# maxlen-1 characters into the next so a match that starts in one window may end in the next.
_WINDOW = 4096


def _dots_with(length: int, *placed: tuple[int, str]) -> str:
    """LENGTH dots with each (1-based start, text) written over them."""
    body = list('.' * length)
    for start, text in placed:
        body[start - 1 : start - 1 + len(text)] = text
    return ''.join(body)


_STRADDLE = 'dummy-straddle'
_STARTS = [
    1,
    _WINDOW - len(_STRADDLE) + 1,
    _WINDOW - 3,
    _WINDOW,
    _WINDOW + 1,
    2 * _WINDOW - 5,
    2 * _WINDOW,
    2 * _WINDOW + 1,
]


@pytest.mark.parametrize('start', [*_STARTS, 3 * _WINDOW + 50 - len(_STRADDLE) + 1], ids=lambda s: f'at {s}')
def test_a_secret_is_found_wherever_it_falls_against_the_window_boundaries(start: int):
    """At the very start, ending exactly on 4096, straddling 4096/4097 and 8192/8193, and at the very end."""
    text = _dots_with(3 * _WINDOW + 50, (start, _STRADDLE))
    completed = _redact_only(text.encode(), {'INSTANCE_WRITE_SECRET': _STRADDLE})
    assert completed.stdout == (text.replace(_STRADDLE, '<redacted>') + '\n').encode()
    assert completed.returncode == 0


@pytest.mark.parametrize(
    ('values', 'start', 'printed'),
    [
        # A ends on 4096 and B starts on 4097; the same on 8192/8193.
        (('dummy-A', 'dummy-B'), _WINDOW - 6, 'dummy-Adummy-B'),
        (('dummy-A', 'dummy-B'), 2 * _WINDOW - 6, 'dummy-Adummy-B'),
        # A straddles 4096/4097 and B, starting in window two, abuts it.
        (('dummy-A', 'dummy-B'), _WINDOW - 2, 'dummy-Adummy-B'),
        # A starts in window one, B overlaps it and starts in window two.
        (('dummy-AAAA', 'AAA-B'), _WINDOW - 4, 'dummy-AAAA-B'),
        # Abutting runs of a short secret through the boundary: one span from window one to two.
        (('dummy-r',), _WINDOW - 13, 'dummy-rdummy-rdummy-r'),
    ],
    ids=['abut on 4096', 'abut on 8192', 'straddle then abut', 'overlap across 4096', 'run across 4096'],
)
def test_spans_that_touch_across_a_window_boundary_merge(values: tuple, start: int, printed: str):
    """The merge state carries from one window to the next: one placeholder, not two."""
    text = _dots_with(3 * _WINDOW, (start, printed))
    secrets = dict(zip(('INSTANCE_WRITE_SECRET', 'POSTGRES_PASS'), values, strict=False))
    completed = _redact_only(text.encode(), secrets)
    assert completed.stdout == (text.replace(printed, '<redacted>') + '\n').encode(), completed.stdout[
        _WINDOW - 40 : 2 * _WINDOW + 40
    ]


def test_a_run_of_matches_across_several_windows_is_one_placeholder():
    """A one-character secret over three windows and a bit, then alternating with a kept character."""
    alternating = 'ab' * (2 * _WINDOW)
    completed = _redact_only(('a' * (3 * _WINDOW + 7) + '\n' + alternating).encode(), {'INSTANCE_WRITE_SECRET': 'a'})
    assert completed.stdout == b'<redacted>\n' + b'<redacted>b' * (2 * _WINDOW) + b'\n'


def test_a_secret_longer_than_the_window_is_found_wherever_it_falls():
    """The window grows to the longest secret, so a 5000-character value is matched whole, twice."""
    long_value = 'dummy-' + 'L' * 5000
    text = _dots_with(4 * _WINDOW, (3000, long_value), (_WINDOW * 3 - 10, long_value))
    completed = _redact_only(text.encode(), {'INSTANCE_WRITE_SECRET': long_value, 'POSTGRES_PASS': 'dummy-x'})
    assert completed.stdout == (text.replace(long_value, '<redacted>') + '\n').encode()


# ===============================================================================================
# TRUNCATED-INPUT MODE (tj-iee6sn.13.9 (D)): REDACT_TRUNCATED=1, a constant the wrapper sets when it
# cut its input at a byte bound, also redacts the longest trailing proper prefix of any secret.
# ===============================================================================================

_TRUNC = {'REDACT_TRUNCATED': '1'}


@pytest.mark.parametrize(
    ('secrets', 'output', 'expected'),
    [
        ({'INSTANCE_WRITE_SECRET': 'dummy-truncated'}, 'head dummy-tru', 'head <redacted>'),
        ({'INSTANCE_WRITE_SECRET': 'dummy-truncated'}, 'head d', 'head <redacted>'),
        # Not a prefix of any secret: untouched.
        ({'INSTANCE_WRITE_SECRET': 'dummy-truncated'}, 'head dummy-trx', 'head dummy-trx'),
        # The whole secret at the end is a match, not a partial: one placeholder either way.
        ({'INSTANCE_WRITE_SECRET': 'dummy-whole'}, 'head dummy-whole', 'head <redacted>'),
        # The longest partial wins, and merges with a match it touches.
        ({'INSTANCE_WRITE_SECRET': 'dummy-ab', 'POSTGRES_PASS': 'dummy-abcdef'}, 'x dummy-abc', 'x <redacted>'),
        # Both 'dummyX' and 'dummyXdummyX' end the output and start the secret: the LONGER is the cut.
        ({'INSTANCE_WRITE_SECRET': 'dummyXdummyXend'}, 'z dummyXdummyX', 'z <redacted>'),
        ({'INSTANCE_WRITE_SECRET': 'dummy-w'}, 'x dummy-wdummy-', 'x <redacted>'),
        # A multi-line value cut after its first line, and a cut inside a later line of one.
        ({'INSTANCE_WRITE_SECRET': 'dummy-one\ndummy-two'}, 'x dummy-one\ndummy-t', 'x <redacted>'),
        ({'INSTANCE_WRITE_SECRET': 'dummy-one\ndummy-two'}, 'x\ny dummy-tw', 'x\ny <redacted>'),
        # Only the TAIL: the same partial earlier in the output is plain text.
        ({'INSTANCE_WRITE_SECRET': 'dummy-truncated'}, 'dummy-tru mid dummy-tru', 'dummy-tru mid <redacted>'),
        # The signal is not a secret: its value '1' prints.
        ({'INSTANCE_WRITE_SECRET': 'dummy-x'}, 'v1 1', 'v1 1'),
    ],
    ids=[
        'a cut secret',
        'one character',
        'not a prefix',
        'whole secret',
        'longest partial merges',
        'longest of two partials',
        'partial abuts a match',
        'multi-line value',
        'line of a multi-line value',
        'only the tail',
        'signal is not a secret',
    ],
)
def test_a_trailing_partial_secret_is_redacted_when_the_input_was_truncated(secrets: dict, output: str, expected: str):
    completed = _redact_only(output.encode(), {**secrets, **_TRUNC})
    assert completed.stdout == (expected + '\n').encode(), completed
    assert completed.returncode == 0 and completed.stderr == b'', completed


@pytest.mark.parametrize('signal', [None, '', '0', 'yes', ' 1', '11', 'dummy-truncated'], ids=repr)
def test_the_truncated_mode_is_off_unless_the_signal_is_exactly_one(signal: str | None):
    secrets = {'INSTANCE_WRITE_SECRET': 'dummy-truncated'}
    if signal is not None:
        secrets['REDACT_TRUNCATED'] = signal
    completed = _redact_only(b'head dummy-tru', secrets)
    assert completed.stdout == b'head dummy-tru\n', completed


def test_the_truncated_signal_is_a_constant_never_a_secret_value():
    """It is read once, compared to the constant "1", and is not one of the names whose values are secrets."""
    assert REDACT_FUNCTION.count('REDACT_TRUNCATED') == 1
    assert 'ENVIRON["REDACT_TRUNCATED"] == "1"' in REDACT_FUNCTION
    assert 'REDACT_TRUNCATED' not in stack._DEV_REDACT_NAMES.split()


def test_dev_db_tables_never_sets_the_truncated_signal(tmp_path: Path):
    """Its output is not cut, so a trailing partial secret is ordinary text and prints."""
    assert SCRIPT.count('REDACT_TRUNCATED') == REDACT_FUNCTION.count('REDACT_TRUNCATED')
    completed = _scrub(tmp_path, {'INSTANCE_WRITE_SECRET': 'dummy-truncated'}, 'head dummy-tru')
    assert completed.stdout == b'head dummy-tru\n', completed


# ===============================================================================================
# ONE CONSTANT, AND THE BUILD COST (tj-iee6sn.13.9 (A), (C); fixes tj-1dgfpi).
# ===============================================================================================


def test_the_db_tables_script_embeds_the_redact_constant_verbatim():
    """Slices 2 and 3 pipe through the same constant; db_tables carries it, not a copy."""
    assert stack._DEV_DB_TABLES_TEMPLATE.startswith(stack._DEV_REDACT_FUNCTION + '\n')
    assert SCRIPT.startswith(stack._DEV_REDACT_FUNCTION + '\n')
    assert SCRIPT.count('redact() {') == 1
    assert '@NAMES@' not in stack._DEV_REDACT_FUNCTION


@pytest.mark.parametrize(('megabytes', 'bound'), [(1, 3.0), (4, 10.0)], ids=['1 MB', '4 MB'])
def test_a_large_output_with_a_few_secrets_is_scrubbed_in_time(megabytes: int, bound: float):
    """A regression bound, not a benchmark: about 0.03 s and 0.3 s under mawk 1.3.4.

    The per-line concatenating build of tj-1dgfpi took about 38 s for 4 MB; the bound has wide slack.
    """
    line = 'x' * 60 + ' plain text ' + 'y' * 20 + '\n'
    filler = line * (megabytes * 1024 * 1024 // len(line) // 2)
    output = filler + 'k dummy-perf-key\ndummy-perf-ml-1\ndummy-perf-ml-2 tail\n' + filler + 'end dummy-perf-key'
    secrets = {'ALPACA_API_KEY': 'dummy-perf-key', 'INSTANCE_WRITE_SECRET': 'dummy-perf-ml-1\ndummy-perf-ml-2'}
    started = time.monotonic()
    completed = _redact_only(output.encode(), secrets)
    elapsed = time.monotonic() - started
    expected = filler + 'k <redacted>\n<redacted> tail\n' + filler + 'end <redacted>\n'
    assert completed.returncode == 0 and completed.stdout == expected.encode()
    assert elapsed < bound, f'{megabytes} MB took {elapsed:.2f} s'


# ===============================================================================================
# migrate_status' exec (tj-iee6sn.13.5, folded into migrate_status by tj-iee6sn.10): alembic current, then
# history, by exec in the RUNNING data_store, -w /code, through the shared redact constant; refused, naming
# db_tables, when data_store is not running. Design: ADR tj-4rr0la addendum 22 (4); the bead's 21:47
# and 23:33 notes.
# ===============================================================================================

MIGRATE_SCRIPT = stack.DEV_MIGRATE_STATUS_SCRIPT
# The image's venv (Dockerfile: WORKDIR /code, `uv venv`, then COPY --from the build stage's /code).
EXPECTED_ALEMBIC = '/code/.venv/bin/alembic'
# Every alembic subcommand that writes, or that has no business in a status verb.
_ALEMBIC_FORBIDDEN = ('upgrade', 'downgrade', 'stamp', 'check', 'revision', 'merge', 'edit', 'init', 'ensure_version')


def test_the_migrate_status_lookup_argv_is_the_label_filter_of_data_store():
    assert list(stack.dev_lookup_argv('data_store')) == expected_lookup('data_store')


def test_the_migrate_status_exec_argv_is_exact():
    """The exec is `docker exec -w /code <id> sh -c <DEV_MIGRATE_STATUS_SCRIPT>`: no -u, no -e, nothing else."""
    steps = stack.dev_migrate_status_steps(STACK_DIR, CONTAINER_ID)
    assert [list(step.argv) for step in steps] == [expected_migrate_exec()]
    assert steps[0].cwd == STACK_DIR and steps[0].builds is False and steps[0].stdout_cap is None


def test_the_migrate_status_builder_runs_check_dev_exec_argv(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, ...]] = []

    def spy(argv):
        seen.append(tuple(argv))
        raise stack.Refused('spy refusal')

    monkeypatch.setattr(stack, 'check_dev_exec_argv', spy)
    with pytest.raises(stack.Refused, match='spy refusal'):
        stack.dev_migrate_status_steps(STACK_DIR, CONTAINER_ID)
    assert seen == [tuple(expected_migrate_exec())]


@pytest.mark.parametrize('container_id', ['data_store', 'C0FFEE000001', 'c0ffee00000', '', 'c0ffee000001\n', None])
def test_the_migrate_status_builder_refuses_a_non_id(container_id: object):
    with pytest.raises(stack.Refused, match='not a container id'):
        stack.dev_migrate_status_steps(STACK_DIR, container_id)  # type: ignore[arg-type]


# THE VERB. dev_migrate_status was folded into migrate_status by tj-iee6sn.10 (the mode guard first, then this
# lookup and this exec). Its verb-level tests -- the whole run, each lookup refusal running no exec, a failed
# alembic, the schema, the timeout and the handler signature -- live with migrate's in test_dev_migrate.py.
# The builder, its argv and its script stay pinned here.


# --- the script, by its parts ----------------------------------------------------------------------


def _migrate_body() -> str:
    """The script after the embedded redact constant: the only part this bead wrote."""
    assert MIGRATE_SCRIPT.startswith(REDACT_FUNCTION + '\n')
    return MIGRATE_SCRIPT[len(REDACT_FUNCTION) :]


def test_the_migrate_script_embeds_the_redact_constant_verbatim_once():
    """One implementation, tested once above: embedded as text, never a copy or a port; not truncated mode."""
    assert MIGRATE_SCRIPT.startswith(stack._DEV_REDACT_FUNCTION + '\n')
    assert MIGRATE_SCRIPT.count('redact() {') == 1 and MIGRATE_SCRIPT.count('awk ') == REDACT_FUNCTION.count('awk ')
    assert 'REDACT_TRUNCATED' not in _migrate_body(), 'alembic output is not truncated before the scrub'
    assert '@NAMES@' not in MIGRATE_SCRIPT


def test_the_migrate_script_runs_only_current_then_history_from_the_image_venv():
    body = _migrate_body()
    assert 'for sub in current history; do' in body
    assert body.count(EXPECTED_ALEMBIC) == 1 and f'{EXPECTED_ALEMBIC} "$sub" 2>&1' in body
    assert body.count('| redact') == 1 and 'printf \'%s\\n\' "$out" | redact' in body
    words = set(body.replace('"', ' ').replace(';', ' ').split())
    assert [word for word in _ALEMBIC_FORBIDDEN if word in words] == []
    assert '$1' not in body and '$@' not in body and '$*' not in body, 'the script reads an argument'


# --- the script, run: a FAKE alembic in place of the image's ------------------------------------------

# printf, never echo: dash's echo interprets backslashes, so it would print a value other than the one
# in the environment and a `not in` check would pass for the wrong reason.
_FAKE_ALEMBIC = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_ALEMBIC_LOG"
printf 'rev-for-%s (head)\n' "$1"
printf 'url=%s\n' "$DATABASE_URI"
printf 'pw=%s\n' "$POSTGRES_PASS" >&2
printf 'key=%s\n' "$INSTANCE_WRITE_SECRET" >&2
eval "exit \"\${FAKE_RC_$1:-0}\""
"""
_FAKE_DSN = 'postgresql://user:dummy-not-a-secret@db/x'


def _run_migrate(
    tmp_path: Path, rc_current: int = 0, rc_history: int = 0, secret: str = 'dummy-instance-secret'
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """The real script under sh, with /code/.venv/bin/alembic replaced by a fake that prints FAKE values."""
    sh = shutil.which('sh')
    if sh is None:
        pytest.skip('no sh on PATH')
    alembic = tmp_path / 'alembic'
    alembic.write_text(_FAKE_ALEMBIC)
    alembic.chmod(0o755)
    assert MIGRATE_SCRIPT.count(EXPECTED_ALEMBIC) == 1
    script = MIGRATE_SCRIPT.replace(EXPECTED_ALEMBIC, str(alembic))
    log = tmp_path / 'alembic.log'
    env = {
        'PATH': '/usr/bin:/bin',
        'FAKE_ALEMBIC_LOG': str(log),
        'FAKE_RC_current': str(rc_current),
        'FAKE_RC_history': str(rc_history),
        'DATABASE_URI': _FAKE_DSN,
        'POSTGRES_PASS': 'dummy-not-a-secret',
        'INSTANCE_WRITE_SECRET': secret,
    }
    completed = subprocess.run([sh, '-c', script], env=env, cwd=tmp_path, capture_output=True, timeout=30, check=False)
    return completed, log.read_text().splitlines() if log.exists() else []


def _section(sub: str) -> str:
    return f'== alembic {sub} ==\nrev-for-{sub} (head)\nurl=<redacted>\npw=<redacted>\nkey=<redacted>\n'


def test_the_migrate_script_prints_both_sections_redacted_stdout_and_stderr(tmp_path: Path):
    """End to end: the DSN on stdout and the password and secret on stderr all print as <redacted>."""
    completed, calls = _run_migrate(tmp_path)
    assert calls == ['current', 'history']
    assert completed.returncode == 0, completed
    assert completed.stdout.decode() == _section('current') + _section('history')
    assert completed.stderr == b''


@pytest.mark.parametrize('secret', list(_FAKE_VALUES.values()), ids=list(_FAKE_VALUES))
def test_no_fake_value_survives_the_migrate_script(tmp_path: Path, secret: str):
    completed, _ = _run_migrate(tmp_path, rc_history=3, secret=secret)
    printed = completed.stdout.decode('utf-8', errors='replace') + completed.stderr.decode('utf-8', errors='replace')
    leaked = [value for value in (secret.strip(), 'dummy-not-a-secret', _FAKE_DSN) if value in printed]
    assert leaked == [], f'the script printed fake values: {leaked}'
    assert 'key=<redacted>' in printed and 'url=<redacted>' in printed, printed
    assert completed.returncode == 3


def test_a_failing_history_is_printed_redacted_and_its_status_is_kept(tmp_path: Path):
    completed, calls = _run_migrate(tmp_path, rc_history=3)
    assert calls == ['current', 'history']
    assert completed.returncode == 3, completed
    assert completed.stdout.decode() == _section('current') + _section('history')


@pytest.mark.parametrize(('rc_current', 'rc_history'), [(2, 0), (2, 3)], ids=['history would pass', 'both fail'])
def test_the_first_non_zero_status_is_the_scripts_and_history_does_not_run(tmp_path: Path, rc_current, rc_history):
    completed, calls = _run_migrate(tmp_path, rc_current=rc_current, rc_history=rc_history)
    assert completed.returncode == rc_current, completed
    assert calls == ['current']
    assert completed.stdout.decode() == _section('current')
