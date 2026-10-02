"""build_infra pins for the seed dump's host side: make seed-dump, SEED_OUT, and test_client's new mounts.

tj-irhy0a.22 V6 and V7, and item 3's exit-status rule. Design: ADR tj-4rr0la addenda 5 and 10;
decision tj-vhboky.55 S9-S11 (S11: the host writer is `python -m data.store.seeds.bundle --out`).
tj-irhy0a.25 items 2 and 4: the shared guard's generic refusal, and DATE/SEED_OUT reaching the
producer and the writer through the environment, one argument each, whatever they hold.

Docker-free, like every build_infra pin. make runs for real from an empty temporary directory, with
`docker` stubbed first on PATH and VENV_PYTHON pointed at a stub writer, so the recipe's pipeline,
its guard and its status rule are exercised -- never a stack, a database or the real writer. With no
.env beside it the recipe stops at its .env check, so even a guard weakened by a mutation reaches
nothing. What only a daemon can show (the producer in test_client over the stack's networks) is the
real stack run, recorded on the bead as owed.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from common.tests.compose_model import BASE_FILE, FAKE_FILE, TEST_CLIENT_FILE, load, merge, volume
from common.tests.test_ci_invariants import (
    _ACCEPTED_GUARDS,
    _REFUSED_GUARDS,
    MAKEFILE,
    REPO_ROOT,
    SYSTEM_GUARD,
    _compose_calls,
    _compose_projects,
    _expanded_make_variable,
    _make_recipe,
    _run_make,
    _subprocess_env,
)


pytestmark = pytest.mark.build_infra

SEED_DUMP = 'seed-dump'
# Spelled out (ADR tj-4rr0la addendum 10 (1)): the ONE producer invocation both callers use.
PRODUCER_WORDS = [
    'run',
    '--rm',
    '-T',
    '--entrypoint',
    '/code/.venv/bin/python',
    'test_client',
    '-m',
    'data.store.seeds',
]
# Item 3: base + fake overlay + test-client file, the default project (the stack system-launch started).
SEED_DUMP_FILES = [BASE_FILE.name, FAKE_FILE.name, TEST_CLIENT_FILE.name]
SEED_OUT_DEFAULT = 'output/seeds'

# test_client's mounts, every one read-only, each onto its own /code path. The last three of the
# first group are the producer's import closure as tj-irhy0a.21 recorded it (architect, 04:49 UTC
# 2026-09-30): data/store/seeds, tests/fakes and data/ingest/app are NEW; common, routers and
# data/store/migrations were already there; schemas is not imported but was mounted before.
CLIENT_MOUNTS_BEFORE = {
    ('./common', '/code/common'),
    ('./routers', '/code/routers'),
    ('./schemas', '/code/schemas'),
    ('./data/store/app', '/code/data/store/app'),
    ('./data/store/migrations', '/code/data/store/migrations'),
    ('./tests/system', '/code/tests/system'),
    ('./pytest.ini', '/code/pytest.ini'),
}
PRODUCER_CLOSURE_MOUNTS = {
    ('./data/store/seeds', '/code/data/store/seeds'),
    ('./tests/fakes', '/code/tests/fakes'),
    ('./data/ingest/app', '/code/data/ingest/app'),
}
# tj-7294qb: the repository's alembic ini, a FILE, beside the migrations mount as data_store lays them
# out, so tests/system/test_migration_with_data.py runs the alembic CLI from cwd data/store with it.
ALEMBIC_INI_MOUNTS = {('./data/store/alembic.ini', '/code/data/store/alembic.ini')}

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG"
printf '%s\\n' "$STUB_STDOUT"
echo 'producer stderr' >&2
exit "${STUB_EXIT:-0}"
"""
WRITER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$WRITER_LOG"
cat > "$WRITER_STDIN"
exit "${WRITER_EXIT:-0}"
"""
BUNDLE_LINE = '{"bundle": "trader_joe-seed/1", "revision": "0a1b2c3d4e5f"}'
# tj-irhy0a.25 item 4: the same stubs, but logging argv ONE ELEMENT PER LINE, so a value split by the
# shell, or glued to its neighbour, shows as a different list rather than the same joined string.
DOCKER_ARGV_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$STUB_LOG"
printf '%s\\n' "$STUB_STDOUT"
exit 0
"""
WRITER_ARGV_STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$WRITER_LOG"
cat > "$WRITER_STDIN"
exit 0
"""


# --- V6: test_client's mounts and the seed-dump stack ----------------------------------------------


def _client_mounts() -> list[dict]:
    return [volume(entry) for entry in load(TEST_CLIENT_FILE)['services']['test_client']['volumes']]


def test_test_client_mounts_are_exactly_the_old_set_plus_the_producers_closure_all_read_only():
    """V6 / item 1, plus tj-7294qb's ini: the new mounts, nothing else; all :ro; never the root or an env path."""
    mounts = _client_mounts()
    assert all(mount['type'] == 'bind' and mount['read_only'] for mount in mounts), mounts
    pairs = [(mount['source'], mount['target']) for mount in mounts]
    assert len(pairs) == len(set(pairs)), pairs
    assert set(pairs) == CLIENT_MOUNTS_BEFORE | PRODUCER_CLOSURE_MOUNTS | ALEMBIC_INI_MOUNTS, sorted(pairs)
    for source, _ in PRODUCER_CLOSURE_MOUNTS:
        assert (REPO_ROOT / source).is_dir(), f'the mount source {source} does not exist'
    for source, _ in ALEMBIC_INI_MOUNTS:
        assert (REPO_ROOT / source).is_file(), f'the mount source {source} is not a file'


def _writable_binds(model: dict) -> set[tuple[str, str]]:
    return {
        (name, mount['target'])
        for name, spec in model['services'].items()
        for mount in map(volume, spec.get('volumes') or [])
        if mount['type'] == 'bind' and not mount['read_only']
    }


def test_the_seed_dump_stack_adds_no_writable_bind_and_test_client_has_none():
    """V6 / addendum 5 and the user's option 1: the client gets no writable mount.

    The producer prints, never writes. Merged in SEED_DUMP_COMPOSE's order (base, fake overlay, test
    client), the writable binds are exactly the fake stack's own (system-launch's model), none of
    them test_client's.

    The agent stack's O1 (no service built from agent source has a writable bind) is pinned on the
    merged agent model in test_agent_stack_compose.py, which loads the same test-client file.
    """
    model = merge([load(BASE_FILE), load(FAKE_FILE), load(TEST_CLIENT_FILE)])
    without_client = merge([load(BASE_FILE), load(FAKE_FILE)])
    assert 'test_client' in model['services']
    assert _writable_binds(model) == _writable_binds(without_client), sorted(_writable_binds(model))
    assert not [bind for bind in _writable_binds(model) if bind[0] == 'test_client']


def test_seed_dump_compose_is_the_fake_stack_plus_the_test_client_in_the_default_project():
    """Item 3: base + fake overlay + test-client file; no -p, so it joins the stack system-launch started."""
    expanded = _expanded_make_variable('SEED_DUMP_COMPOSE', REPO_ROOT, _subprocess_env())
    assert _compose_calls(expanded) == [(SEED_DUMP_FILES, [])], expanded
    assert _compose_projects(expanded) == [None], expanded


# --- V7 and the recipe: the guard, the .env check, the invocation ----------------------------------


def _stubs(
    tmp_path: Path, guard: str | None, *, argv: bool = False, **values: str
) -> tuple[dict[str, str], dict[str, Path], Path]:
    """Stub docker and the writer; returns (env, the log paths, the writer stub's path)."""
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    (stubs / 'docker').write_text(DOCKER_ARGV_STUB if argv else DOCKER_STUB)
    (stubs / 'docker').chmod(0o755)
    writer = stubs / 'writer'
    writer.write_text(WRITER_ARGV_STUB if argv else WRITER_STUB)
    writer.chmod(0o755)
    logs = {'docker': tmp_path / 'docker.log', 'writer': tmp_path / 'writer.log', 'stdin': tmp_path / 'writer.stdin'}
    env = _subprocess_env(**{SYSTEM_GUARD: guard})
    env.update(
        PATH=f'{stubs}:{os.environ["PATH"]}',
        STUB_LOG=str(logs['docker']),
        WRITER_LOG=str(logs['writer']),
        WRITER_STDIN=str(logs['stdin']),
        STUB_STDOUT=BUNDLE_LINE,
        **values,
    )
    return env, logs, writer


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding='utf-8').splitlines() if path.exists() else []


def _seed_dump(
    tmp_path: Path, *arguments: str, guard: str | None = '1', env_file: bool = True, argv: bool = False, **values: str
):
    """Run make seed-dump from an empty directory; returns (result, logs)."""
    work = tmp_path / 'work'
    work.mkdir()
    if env_file:
        (work / '.env').write_text('')
    env, logs, writer = _stubs(tmp_path, guard, argv=argv, **values)
    result = _run_make(work, SEED_DUMP, f'VENV_PYTHON={writer}', *arguments, env=env)
    return result, logs


def _recipe_status(result: subprocess.CompletedProcess) -> int:
    """The recipe's own exit status: make itself exits 2 on any failure and names the status in 'Error N'."""
    if result.returncode == 0:
        return 0
    found = re.findall(rf'\[[^\]]*{re.escape(SEED_DUMP)}\] Error (\d+)', result.stderr)
    assert found, f'make failed without naming the recipe status:\n{result.stderr}'
    return int(found[-1])


def test_seed_dump_opens_with_the_shared_disposable_database_guard():
    """V7: the SAME guard as test-system and system-launch, first, before anything reaches docker."""
    recipe = _make_recipe(SEED_DUMP)
    assert recipe[0] == '$(SYSTEM_TEST_DISPOSABLE_GUARD)', recipe
    assert _make_recipe('test-system')[0] == recipe[0]


def test_make_n_prints_the_guard_before_the_compose_run(tmp_path: Path):
    """V7, as make itself expands it (make -n): the guard's check comes before the producer's compose run."""
    env = _subprocess_env(**{SYSTEM_GUARD: None})
    result = _run_make(tmp_path, '-n', SEED_DUMP, env=env)
    assert result.returncode == 0, result.stderr
    text = re.sub(r'\\\n\t?', ' ', result.stdout)
    guard = text.find('if [ "" != "1" ]')
    run = text.find('compose -f')
    assert guard != -1 and run != -1 and guard < run, text


@pytest.mark.parametrize(('arguments', 'environment'), list(_REFUSED_GUARDS.values()), ids=list(_REFUSED_GUARDS))
def test_seed_dump_refuses_without_the_attestation_and_runs_neither_docker_nor_the_writer(
    tmp_path: Path, arguments: list[str], environment: str | None
):
    """V7 / DONE WHEN: the refusal without SYSTEM_TEST_DISPOSABLE_DB=1 -- non-zero, the reason, nothing run."""
    result, logs = _seed_dump(tmp_path, *arguments, guard=environment)
    assert result.returncode != 0, result.stdout
    assert f'make {SEED_DUMP} REFUSED' in result.stderr and f'{SYSTEM_GUARD}=1' in result.stderr, result.stderr
    assert _lines(logs['docker']) == [] and _lines(logs['writer']) == []


def test_seed_dump_without_a_dot_env_stops_before_docker(tmp_path: Path):
    result, logs = _seed_dump(tmp_path, env_file=False)
    assert result.returncode != 0 and 'no .env' in result.stderr, result.stderr
    assert _lines(logs['docker']) == [] and _lines(logs['writer']) == []


@pytest.mark.parametrize(('arguments', 'environment'), list(_ACCEPTED_GUARDS.values()), ids=list(_ACCEPTED_GUARDS))
def test_seed_dump_runs_the_one_invocation_and_pipes_it_to_the_host_writer(
    tmp_path: Path, arguments: list[str], environment: str | None
):
    """Item 3: the producer by the one spelling, its stdout piped to `python -m data.store.seeds.bundle --out output/seeds`."""
    result, logs = _seed_dump(tmp_path, *arguments, guard=environment)
    assert result.returncode == 0, f'{result.stdout}{result.stderr}'
    [call] = _lines(logs['docker'])
    [(files, rest)] = _compose_calls(f'docker {call}')
    assert files == SEED_DUMP_FILES and rest == PRODUCER_WORDS, call
    assert '-p' not in call.split() and 'exec' not in call.split(), call
    assert _lines(logs['writer']) == [f'-m data.store.seeds.bundle --out {SEED_OUT_DEFAULT}']
    assert logs['stdin'].read_text(encoding='utf-8') == BUNDLE_LINE + '\n', 'the writer did not get the bundle'


def test_date_is_forwarded_and_seed_out_overrides_the_directory(tmp_path: Path):
    result, logs = _seed_dump(tmp_path, 'DATE=2026-01-02', 'SEED_OUT=elsewhere/seeds')
    assert result.returncode == 0, result.stderr
    [call] = _lines(logs['docker'])
    [(_, rest)] = _compose_calls(f'docker {call}')
    assert rest == [*PRODUCER_WORDS, '--date', '2026-01-02'], call
    assert _lines(logs['writer']) == ['-m data.store.seeds.bundle --out elsewhere/seeds']


# --- tj-irhy0a.25 item 4: DATE and SEED_OUT reach their argv intact, through the environment --------


def _producer_tail(logs: dict[str, Path]) -> list[str]:
    """The producer's own arguments: what follows the one invocation (PRODUCER_WORDS) in docker's argv."""
    argv = _lines(logs['docker'])
    assert argv[:1] == ['compose'], argv
    end = len(argv) - argv[::-1].index(PRODUCER_WORDS[-1])
    assert argv[end - len(PRODUCER_WORDS) : end] == PRODUCER_WORDS, argv
    return argv[end:]


# (DATE, SEED_OUT). Before tj-irhy0a.25 both were spliced into the recipe inside single quotes, so a
# quote closed the quoting: the producer got the pipe and the writer's argv as its --date.
_AWKWARD_VALUES = {
    'single-quote': ("2026-01-0'2", "elsewhere/it's"),
    'quote-and-spaces': ("2026-01-0'2 and more", "out dir/it's seeds"),
    'shell-metacharacters': ('2026-01-02"; exit 9; "', 'a"b;c|d `true` & e'),
}


@pytest.mark.parametrize(('date', 'seed_out'), list(_AWKWARD_VALUES.values()), ids=list(_AWKWARD_VALUES))
def test_date_and_seed_out_each_reach_their_command_as_one_argument(tmp_path: Path, date: str, seed_out: str):
    """Item 4: whatever DATE and SEED_OUT hold, the producer's --date and the writer's --out are exactly them.

    The producer re-validates the date and refuses this one; reaching it intact is the recipe's job.
    """
    result, logs = _seed_dump(tmp_path, f'DATE={date}', f'SEED_OUT={seed_out}', argv=True)
    assert result.returncode == 0, f'{result.stdout}{result.stderr}'
    assert _producer_tail(logs) == ['--date', date]
    assert _lines(logs['writer']) == ['-m', 'data.store.seeds.bundle', '--out', seed_out]
    assert logs['stdin'].read_text(encoding='utf-8') == BUNDLE_LINE + '\n', 'the writer did not get the bundle'


def test_without_date_there_is_no_date_and_the_callers_environment_cannot_supply_the_values(tmp_path: Path):
    """Item 4: no DATE -> no --date at all; SEED_DUMP_DATE/SEED_DUMP_OUT are the recipe's, not the caller's."""
    result, logs = _seed_dump(tmp_path, argv=True, SEED_DUMP_DATE='1999-01-01', SEED_DUMP_OUT='/elsewhere/seeds')
    assert result.returncode == 0, f'{result.stdout}{result.stderr}'
    assert _producer_tail(logs) == []
    assert _lines(logs['writer']) == ['-m', 'data.store.seeds.bundle', '--out', SEED_OUT_DEFAULT]


def test_date_and_seed_out_win_over_the_callers_environment(tmp_path: Path):
    result, logs = _seed_dump(
        tmp_path,
        'DATE=2026-01-02',
        'SEED_OUT=elsewhere/seeds',
        argv=True,
        SEED_DUMP_DATE='1999-01-01',
        SEED_DUMP_OUT='/elsewhere/seeds',
    )
    assert result.returncode == 0, f'{result.stdout}{result.stderr}'
    assert _producer_tail(logs) == ['--date', '2026-01-02']
    assert _lines(logs['writer']) == ['-m', 'data.store.seeds.bundle', '--out', 'elsewhere/seeds']


# --- tj-irhy0a.25 item 2: the shared guard's refusal, generic, naming the target that refused ------

GUARDED_TARGETS = ('test-system', 'system-launch', SEED_DUMP)


@pytest.mark.parametrize('target', GUARDED_TARGETS)
def test_the_shared_guards_refusal_names_the_calling_target_and_every_user(tmp_path: Path, target: str):
    """Item 2: the four phrases the guard's tests pin, the refusing target's own name, and one line per user."""
    assert _make_recipe(target)[0] == '$(SYSTEM_TEST_DISPOSABLE_GUARD)', _make_recipe(target)
    result = _run_make(tmp_path, target, env=_subprocess_env(**{SYSTEM_GUARD: None}))
    assert result.returncode != 0, result.stdout
    for phrase in ('REFUSED', 'WRITES to the database it is pointed at', 'production deployment', f'{SYSTEM_GUARD}=1'):
        assert phrase in result.stderr, f'the refusal does not say {phrase!r}:\n{result.stderr}'
    assert re.findall(r'make (\S+) REFUSED', result.stderr) == [target], result.stderr
    assert f'make {target} {SYSTEM_GUARD}=1' in result.stderr, result.stderr
    for user in GUARDED_TARGETS:
        assert re.search(rf'^\s+{re.escape(user)}:\s+\S', result.stderr, re.MULTILINE), f'no line for {user}'


# (producer status, writer status, the recipe's status). Item 3: the producer's when non-zero, else
# the writer's -- never pipefail's rightmost failure (the writer's 'no bundle line' after a failed producer).
_STATUS_CASES = {
    'both-ok': ('0', '0', 0),
    'writer-refuses': ('0', '3', 3),
    'writer-fails': ('0', '1', 1),
    'producer-refuses-writer-ok': ('3', '0', 3),
    'producer-refuses-writer-refuses': ('3', '3', 3),
    'producer-fails-writer-refuses': ('1', '3', 1),
    'producer-fails-writer-ok': ('1', '0', 1),
    'producer-refuses-writer-fails': ('3', '1', 3),
}


@pytest.mark.parametrize(('producer', 'writer', 'expected'), list(_STATUS_CASES.values()), ids=list(_STATUS_CASES))
def test_the_status_is_the_producers_when_non_zero_else_the_writers(
    tmp_path: Path, producer: str, writer: str, expected: int
):
    result, logs = _seed_dump(tmp_path, STUB_EXIT=producer, WRITER_EXIT=writer)
    assert _recipe_status(result) == expected, f'{result.stdout}{result.stderr}'
    assert len(_lines(logs['docker'])) == 1 and len(_lines(logs['writer'])) == 1


def test_seed_dump_takes_only_the_venv_marker_and_is_phony():
    """Design call (d): $(VENV_MARKER), so a fresh checkout syncs the host writer's venv before the guard."""
    text = MAKEFILE.read_text(encoding='utf-8')
    header = next(line for line in text.splitlines() if line.startswith(f'{SEED_DUMP}:'))
    assert header.split(':', 1)[1].split('##')[0].split() == ['$(VENV_MARKER)'], header
    phony = [line for line in text.splitlines() if line.startswith('.PHONY:')]
    assert any(SEED_DUMP in line.split(':', 1)[1].split() for line in phony)


# --- SEED_OUT: in the repository, git-ignored, never tests/ ----------------------------------------


def test_seed_out_defaults_to_a_git_ignored_directory_outside_tests():
    """Item 3: SEED_OUT defaults inside the repository, never under tests/, and .gitignore gains it."""
    default = _expanded_make_variable('SEED_OUT', REPO_ROOT, _subprocess_env(SEED_OUT=None))
    assert default == SEED_OUT_DEFAULT
    assert not default.startswith('tests/') and not os.path.isabs(default)
    lines = (REPO_ROOT / '.gitignore').read_text(encoding='utf-8').splitlines()
    assert '/output/' in lines
    probe = f'{default}/0a1b2c3d4e5f.sql'
    ignored = subprocess.run(
        ['git', 'check-ignore', '--no-index', '-q', probe], cwd=REPO_ROOT, capture_output=True, check=False
    )
    assert ignored.returncode == 0, f'{probe} is not git-ignored'
    kept = subprocess.run(
        ['git', 'check-ignore', '--no-index', '-q', 'data/store/output/x.sql'],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert kept.returncode == 1, 'the ignore is anchored at the root: an output/ elsewhere is not swept up'
