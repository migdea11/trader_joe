"""The migration-safety gate's scripts (epic tj-7zjov1 P03, bead tj-7zjov1.9; the gate items m4 to m6).

THE DESIGN, from the epic's migration-safety gate and its P03 row:

* m4, BACKUP BEFORE MIGRATE: run_migrations.sh dumps the database before every command that moves the schema
  (upgrade, downgrade, stamp; the default is `upgrade head`) and REFUSES TO MIGRATE IF THE DUMP FAILS -- on the dev
  stack a failing dump stops before alembic runs. A dump is written under .partial, checked non-empty, read back
  with pg_restore --list and only then renamed, private (mode 600). The read-only commands (current, history,
  heads, check) take no dump. WHERE IT LANDS (tj-7zjov1.46; the architect's CORRECTIONS B on tj-7zjov1.12):
  MIGRATE_BACKUP_DIR, else the parent of the host directory bind-mounted at the running postgres's data directory,
  plus /backups; the shell's DATA_DIR no longer decides. A mount that cannot be found (the inspect fails, no bind
  mount there, several, a relative source) with MIGRATE_BACKUP_DIR unset refuses before the dump and before alembic.
* m6, THE PRODUCTION LABEL: production's DEPLOYMENT_LABEL is empty, so before a migrate on the PRODUCTION definition
  the label data_store would run with is read and anything non-empty (or a failed read) refuses before the dump.
  The dev and test definitions carry a label by design and are not asserted.
* m5, REHEARSAL: restore_dump.sh loads a dump into the TEST stack only (trader_joe_dev, base + the fake overlay),
  attested disposable with SYSTEM_TEST_DISPOSABLE_DB=1, and refuses an unreadable dump before it drops anything;
  rehearse_migration.sh restores, migrates and reports health, ending REHEARSAL OK or exiting non-zero.

restore_dump.sh restates run_migrations.sh's TEST rule because that script cannot be sourced; the parity tests below
drive both over the same labels, well-formed and malformed, so the two cannot drift apart unnoticed (tj-7zjov1.43:
restore refuses every label classify_config_files does not call TEST).

TIER. The real scripts under bash with the stub docker of compose_double.py first on PATH; no Docker runs. The
rehearsal runs the real restore_dump.sh and run_migrations.sh it calls, against the same stub.
"""

import itertools
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from common.tests.roots import REPO_ROOT
from data.store.tests.compose_double import (
    ALEMBIC_RUN,
    BASH,
    DUMP_BYTES,
    DUMP_LINE,
    LABEL_READ,
    MOUNTS_FORMAT,
    PG_DATA,
    PG_DUMP,
    PG_RESTORE,
    PG_RESTORE_LIST,
    PSQL_DROP,
    REHEARSE_MIGRATION,
    RESTORE_DUMP,
    REVISIONS_PROBE,
    RUN_MIGRATIONS,
    Harness,
    Mount,
    Outcome,
    Stack,
    render_mounts,
)


pytestmark = pytest.mark.data_store

BASE = 'docker-compose.yaml'
OVERRIDE = 'docker-compose.override.yaml'
FAKE = 'docker-compose.fake.yaml'
WEB = 'docker-compose.web.yaml'
HOST = '/home/owner/src/trader_joe'
DEVCONTAINER = '/workspace'
DEV_PROJECT = 'trader_joe_dev'


def _label(root: str, *names: str) -> str:
    return ','.join(f'{root}/{name}' for name in names)


DEV_LABEL = _label(HOST, BASE, OVERRIDE, WEB, 'docker-compose.web.dev.yaml')
PROD_LABEL = _label(HOST, BASE, WEB)
SYSTEM_LABEL = _label(HOST, BASE, FAKE)
BOTH_LABEL = _label(HOST, BASE, OVERRIDE, FAKE)

# The three definitions, each in the project it runs in: production in the default project, dev and test in
# trader_joe_dev.
DEFINITIONS = {
    'prod': (Stack(('pg',), {'pg': PROD_LABEL}), 'default'),
    'dev': (Stack(('pg',), {'pg': DEV_LABEL}), 'dev'),
    'test': (Stack(('pg',), {'pg': SYSTEM_LABEL}), 'dev'),
}
PROJECT_NAME = {'default': 'default', 'dev': DEV_PROJECT}

PG_DUMP_FAILED = 'REFUSED: pg_dump failed, so the migration was not started (no dump, no migration).'
EMPTY_DUMP = 'REFUSED: pg_dump wrote an empty file, so the migration was not started.'
UNREADABLE_DUMP = 'REFUSED: the dump does not read back (pg_restore --list failed), so the migration was not started.'
LABEL_SET = (
    "REFUSED: the production definition would run data_store with DEPLOYMENT_LABEL set (non-empty); production's "
    'label is empty. This looks like a dev or test environment mislabelled as production, or a label leaked into '
    'the production environment. Nothing was migrated.'
)
LABEL_UNREADABLE = (
    'REFUSED: could not read DEPLOYMENT_LABEL from the data_store container, so the production label cannot be '
    'asserted empty. Nothing was migrated.'
)


@pytest.fixture(scope='module')
def checkout_digest() -> str:
    """This checkout's source digest, so `check` meets an image whose stamp matches and reaches alembic."""
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
    assert BASH, 'bash is not on PATH, so the scripts cannot be exercised'
    return Harness(tmp_path, checkout_digest)


def _on(harness: Harness, definition: str) -> None:
    stack, project = DEFINITIONS[definition]
    harness.stack(stack, project)


def _stderr_lines(outcome: Outcome) -> list[str]:
    return outcome.result.stderr.splitlines()


def _assert_refused_before_alembic(outcome: Outcome, harness: Harness, message: str) -> None:
    """Exit 1, MESSAGE as the last stderr line, no alembic one-off, and no dump left behind, partial or whole."""
    assert outcome.result.returncode == 1, outcome.explain()
    assert _stderr_lines(outcome)[-1] == message, outcome.explain()
    assert not outcome.compose(*ALEMBIC_RUN), outcome.explain()
    assert harness.backup_files() == [], (harness.backup_files(), outcome.explain())


# ================================================================================================= run_migrations.sh
# ------------------------------------------------------------------------------------------------- m4: the dump


@pytest.mark.parametrize('definition', DEFINITIONS)
def test_a_failing_pg_dump_refuses_and_alembic_never_runs(harness: Harness, definition: str):
    """A failed pg_dump is the refusal: no read-back, no alembic, and the half-written .partial is removed.

    The stub writes some bytes before failing, so the .partial is not empty and its removal is the script's doing.
    run_status=3 so that reaching alembic would show as 3, never as the refusal's 1.
    """
    _on(harness, definition)
    harness.write('dump', 'half a dump')
    harness.write('dump.rc', '2')
    outcome = harness.run('upgrade', 'head', run_status=3)

    _assert_refused_before_alembic(outcome, harness, PG_DUMP_FAILED)
    assert outcome.gate_calls()[-1:] == [PG_DUMP], outcome.explain()
    assert not outcome.compose(*PG_RESTORE_LIST), outcome.explain()


@pytest.mark.parametrize('definition', DEFINITIONS)
def test_an_empty_dump_refuses_and_leaves_no_partial(harness: Harness, definition: str):
    """pg_dump exits 0 with nothing written: refused before the read-back and before alembic, no file left."""
    _on(harness, definition)
    harness.write('dump', '')
    outcome = harness.run('upgrade', 'head', run_status=3)

    _assert_refused_before_alembic(outcome, harness, EMPTY_DUMP)
    assert not outcome.compose(*PG_RESTORE_LIST), outcome.explain()


@pytest.mark.parametrize('definition', DEFINITIONS)
def test_an_unreadable_dump_refuses_and_leaves_no_partial(harness: Harness, definition: str):
    """pg_restore --list rejects what pg_dump wrote: refused, no file left, alembic never runs.

    The read-back was handed the bytes pg_dump wrote, so it read the dump itself and not some other file.
    """
    _on(harness, definition)
    harness.write('list.rc', '1')
    outcome = harness.run('upgrade', 'head', run_status=3)

    _assert_refused_before_alembic(outcome, harness, UNREADABLE_DUMP)
    assert outcome.gate_calls()[-2:] == [PG_DUMP, PG_RESTORE_LIST], outcome.explain()
    assert harness.read('stdin.list.0') == DUMP_BYTES


@pytest.mark.parametrize('definition', DEFINITIONS)
def test_a_good_dump_is_kept_private_verified_and_named(harness: Harness, definition: str):
    """The dump that passes: one file, no .partial, the bytes pg_dump wrote, mode 600, named for UTC time and project.

    Run under umask 000, so the private mode is the script's own doing and not the caller's umask. The backup
    directory the script creates is private too. The dump line on stderr names the file, and alembic runs last.
    """
    stack, project = DEFINITIONS[definition]
    harness.stack(stack, project)
    outcome = harness.run('upgrade', 'head', umask=0)

    assert outcome.result.returncode == 0, outcome.explain()
    (dump,) = harness.backup_files()
    assert re.fullmatch(rf'store-\d{{8}}T\d{{6}}Z-{PROJECT_NAME[project]}-pre-migrate\.dump', dump.name), dump.name
    assert dump.read_text(encoding='utf-8') == DUMP_BYTES
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600, oct(dump.stat().st_mode)
    assert stat.S_IMODE(harness.backups.stat().st_mode) == 0o700, oct(harness.backups.stat().st_mode)
    assert harness.read('stdin.list.0') == DUMP_BYTES

    (line,) = [line for line in _stderr_lines(outcome) if DUMP_LINE.fullmatch(line)]
    assert DUMP_LINE.fullmatch(line).group(1) == str(dump), outcome.explain()
    # The dump goes to a file and its messages to stderr: stdout is still the Running line and alembic's output.
    assert outcome.result.stdout.splitlines()[1:] == ['deadbeef0001 (head)'], outcome.explain()

    compose = [c.rest for c in outcome.calls if c.is_compose]
    assert compose[-3:] == [PG_DUMP, PG_RESTORE_LIST, (*ALEMBIC_RUN, '/code/.venv/bin/alembic', 'upgrade', 'head')], (
        outcome.explain()
    )


@pytest.mark.parametrize(
    'invocation',
    [(), ('upgrade', 'head'), ('upgrade', '+1'), ('downgrade', '-1'), ('stamp', 'head')],
    ids=lambda i: ' '.join(i) or 'no-args',
)
@pytest.mark.parametrize('definition', DEFINITIONS)
def test_every_schema_moving_command_dumps_first(harness: Harness, definition: str, invocation: tuple[str, ...]):
    """Each schema-moving command dumps: upgrade (the default), downgrade and stamp, read back before alembic."""
    _on(harness, definition)
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    assert outcome.gate_calls()[-2:] == [PG_DUMP, PG_RESTORE_LIST], outcome.explain()
    assert len(harness.backup_files()) == 1, outcome.explain()
    assert outcome.alembic().rest[len(ALEMBIC_RUN) + 1 :] == (invocation or ('upgrade', 'head')), outcome.explain()
    compose = [c.rest for c in outcome.calls if c.is_compose]
    assert compose.index(PG_RESTORE_LIST) < compose.index(outcome.alembic().rest), outcome.explain()


@pytest.mark.parametrize('invocation', [('current',), ('history',), ('heads',), ('check',)], ids=' '.join)
@pytest.mark.parametrize('definition', DEFINITIONS)
def test_the_read_only_commands_take_no_dump_and_read_no_label(
    harness: Harness, definition: str, invocation: tuple[str, ...]
):
    """current, history, heads and check: no exec into postgres, no label read, no backup directory, alembic runs.

    The label is set to something production would refuse, so a label read that did happen could not pass unseen.
    """
    _on(harness, definition)
    harness.write('label_read', 'dev')
    outcome = harness.run(*invocation)

    assert outcome.result.returncode == 0, outcome.explain()
    # Only the look into the image for revision files (tj-7zjov1.45), which every command makes.
    assert outcome.gate_calls() == [REVISIONS_PROBE], outcome.explain()
    assert not [c for c in outcome.calls if c.is_compose and c.rest[:1] == ('exec',)], outcome.explain()
    assert not harness.backups.exists(), outcome.explain()
    assert outcome.alembic().rest[-len(invocation) :] == invocation, outcome.explain()
    assert not [line for line in _stderr_lines(outcome) if DUMP_LINE.fullmatch(line)], outcome.explain()


# ------------------------------------------------------------------------------------------------- m4: where it lands
# tj-7zjov1.46. Every test here runs with MIGRATE_BACKUP_DIR unset unless it says otherwise; the harness's own
# backups folder (harness.backups) is then a place the dump must NOT land.

NO_DEFAULT_DIR = (
    'REFUSED: could not find the host directory mounted at the postgres data directory, so there is no default place '
    'for the dump. Set MIGRATE_BACKUP_DIR to choose a directory. Nothing was migrated.'
)
MOUNT_LOOK = ('inspect', '--format', MOUNTS_FORMAT, 'pg')
SCHEMA_MOVING = [(), ('upgrade', 'head'), ('downgrade', '-1'), ('stamp', 'head')]
SCHEMA_MOVING_IDS = ['no-args', 'upgrade head', 'downgrade -1', 'stamp head']
REPO_BACKUPS = REPO_ROOT / 'volume_data' / 'backups'


def _mount_looks(outcome: Outcome) -> list[tuple[str, ...]]:
    """Every `docker inspect` of a container's mounts the script made."""
    return [c.argv for c in outcome.calls if c.argv[:2] == ('inspect', '--format') and '.Mounts' in c.argv[2]]


def _index(outcome: Outcome, argv: tuple[str, ...]) -> int:
    return [c.argv for c in outcome.calls].index(argv)


def _compose_index(outcome: Outcome, rest: tuple[str, ...]) -> int:
    return [c.rest if c.is_compose else None for c in outcome.calls].index(rest)


def _repo_backups() -> set[str]:
    return {p.name for p in REPO_BACKUPS.iterdir()} if REPO_BACKUPS.exists() else set()


def _assert_dump_in(outcome: Outcome, folder: Path, project: str) -> Path:
    """One verified dump in FOLDER, no .partial, mode 600 in a 700 folder, and the dump line names it."""
    assert outcome.result.returncode == 0, outcome.explain()
    assert folder.is_dir(), outcome.explain()
    (dump,) = sorted(folder.iterdir())
    assert re.fullmatch(rf'store-\d{{8}}T\d{{6}}Z-{PROJECT_NAME[project]}-pre-migrate\.dump', dump.name), dump.name
    assert dump.read_text(encoding='utf-8') == DUMP_BYTES
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600, oct(dump.stat().st_mode)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700, oct(folder.stat().st_mode)
    (line,) = [line for line in _stderr_lines(outcome) if DUMP_LINE.fullmatch(line)]
    assert DUMP_LINE.fullmatch(line).group(1) == str(dump), outcome.explain()
    assert Path(DUMP_LINE.fullmatch(line).group(1)).parent == folder, outcome.explain()
    return dump


@pytest.mark.parametrize('trailing_slash', [False, True], ids=['source', 'source with a trailing slash'])
@pytest.mark.parametrize('definition', DEFINITIONS)
def test_with_no_backup_dir_the_dump_lands_beside_the_postgres_data_mount(
    harness: Harness, tmp_path: Path, definition: str, trailing_slash: bool
):
    """MIGRATE_BACKUP_DIR and DATA_DIR unset: the dump lands in the mount's parent/backups, private and verified.

    Run under umask 000, so the private modes are the script's own. The mount is looked up once, on the postgres
    container the script selected, with exactly the pinned template, and before the dump; nothing lands in the
    harness's own folder or the checkout's volume_data.
    """
    _, project = DEFINITIONS[definition]
    _on(harness, definition)
    data = tmp_path / 'host' / 'volumes' / 'trader_joe' / 'postgres'
    harness.mounts('pg', [Mount(str(data) + ('/' if trailing_slash else ''))])
    before = _repo_backups()
    outcome = harness.run('upgrade', 'head', umask=0, backup_dir_set=False)

    _assert_dump_in(outcome, data.parent / 'backups', project)
    assert _mount_looks(outcome) == [MOUNT_LOOK], outcome.explain()
    assert _index(outcome, MOUNT_LOOK) < _compose_index(outcome, PG_DUMP), outcome.explain()
    assert outcome.alembic().rest[-2:] == ('upgrade', 'head'), outcome.explain()
    assert not harness.backups.exists(), outcome.explain()
    assert not data.exists(), 'the data directory itself is never written'
    assert _repo_backups() == before


@pytest.mark.parametrize('definition', DEFINITIONS)
def test_a_found_mount_outranks_the_shell_data_dir(harness: Harness, tmp_path: Path, definition: str):
    """The shell's DATA_DIR names somewhere else: the mount decides, and nothing is written under DATA_DIR."""
    _, project = DEFINITIONS[definition]
    _on(harness, definition)
    data = tmp_path / 'db' / 'postgres'
    shell_data_dir = tmp_path / 'shell-data-dir'
    harness.mounts('pg', [Mount(str(data))])
    outcome = harness.run('upgrade', 'head', backup_dir_set=False, extra_env={'DATA_DIR': str(shell_data_dir)})

    _assert_dump_in(outcome, data.parent / 'backups', project)
    assert not shell_data_dir.exists(), outcome.explain()
    assert not harness.backups.exists(), outcome.explain()


NOT_FOUND = {
    'inspect fails': None,
    'no mounts at all': [],
    'a named volume there': [Mount('/var/lib/docker/volumes/pgdata/_data', type='volume')],
    'a bind mount elsewhere only': [Mount('/srv/pg', destination='/var/lib/postgresql')],
    'several bind mounts there': [Mount('/srv/a/postgres'), Mount('/srv/b/postgres')],
    'a relative source': [Mount('tj-7zjov1.46-relative/postgres')],
    'a dot-relative source': [Mount('./volumes/trader_joe/postgres')],
    'the root directory': [Mount('/')],
}


def _assert_refused_for_no_mount(outcome: Outcome, harness: Harness, definition: str, *untouched: Path) -> None:
    """Refused with the message naming MIGRATE_BACKUP_DIR: exit 1, no pg_dump, no alembic, no dump anywhere."""
    _assert_refused_before_alembic(outcome, harness, NO_DEFAULT_DIR)
    assert 'MIGRATE_BACKUP_DIR' in NO_DEFAULT_DIR
    gate = [REVISIONS_PROBE, LABEL_READ] if definition == 'prod' else [REVISIONS_PROBE]
    assert outcome.gate_calls() == gate, outcome.explain()
    assert not [c for c in outcome.calls if c.is_compose and c.rest[:1] == ('exec',)], outcome.explain()
    assert _mount_looks(outcome) == [MOUNT_LOOK], outcome.explain()
    assert not [line for line in _stderr_lines(outcome) if DUMP_LINE.fullmatch(line)], outcome.explain()
    for path in untouched:
        assert not path.exists(), (path, outcome.explain())


@pytest.mark.parametrize('mounts', NOT_FOUND.values(), ids=NOT_FOUND)
@pytest.mark.parametrize('definition', DEFINITIONS)
def test_a_mount_not_found_refuses_before_the_dump(harness: Harness, tmp_path: Path, definition: str, mounts):
    """No usable mount and MIGRATE_BACKUP_DIR unset: refused, never a fall-back to DATA_DIR or the repo's volume_data.

    DATA_DIR is set in the shell, so the old default would have had a place to write; it must stay empty, as must
    the checkout's volume_data/backups. run_status=3 so that reaching alembic would show as 3, never as the 1.
    """
    _on(harness, definition)
    harness.mounts('pg', mounts)
    shell_data_dir = tmp_path / 'shell-data-dir'
    before = _repo_backups()
    outcome = harness.run(
        'upgrade', 'head', run_status=3, backup_dir_set=False, extra_env={'DATA_DIR': str(shell_data_dir)}
    )

    _assert_refused_for_no_mount(outcome, harness, definition, shell_data_dir)
    assert _repo_backups() == before


@pytest.mark.parametrize('invocation', SCHEMA_MOVING, ids=SCHEMA_MOVING_IDS)
@pytest.mark.parametrize('definition', DEFINITIONS)
def test_every_schema_moving_command_refuses_without_a_mount(harness: Harness, definition: str, invocation):
    """Each schema-moving command (upgrade, the default, downgrade and stamp) refuses alike when no mount is found."""
    _on(harness, definition)
    harness.mounts('pg', None)
    outcome = harness.run(*invocation, run_status=3, backup_dir_set=False)

    _assert_refused_for_no_mount(outcome, harness, definition)


@pytest.mark.parametrize('mounts', [None, [], [Mount('/srv/db/postgres')]], ids=['inspect fails', 'none', 'found'])
@pytest.mark.parametrize('definition', DEFINITIONS)
def test_a_set_backup_dir_wins_whatever_the_mount_says(harness: Harness, definition: str, mounts):
    """MIGRATE_BACKUP_DIR set: the dump lands there and the mount is never looked up, found, missing or failing."""
    _, project = DEFINITIONS[definition]
    _on(harness, definition)
    harness.mounts('pg', mounts)
    outcome = harness.run('upgrade', 'head')

    _assert_dump_in(outcome, harness.backups, project)
    assert _mount_looks(outcome) == [], outcome.explain()


@pytest.mark.parametrize('invocation', [('current',), ('history',), ('heads',), ('check',)], ids=' '.join)
@pytest.mark.parametrize('definition', DEFINITIONS)
def test_the_read_only_commands_need_no_mount(harness: Harness, tmp_path: Path, definition: str, invocation):
    """current, history, heads and check take no dump, so a missing mount refuses nothing and is never looked up."""
    _on(harness, definition)
    harness.mounts('pg', None)
    outcome = harness.run(*invocation, backup_dir_set=False)

    assert outcome.result.returncode == 0, outcome.explain()
    assert _mount_looks(outcome) == [], outcome.explain()
    assert outcome.gate_calls() == [REVISIONS_PROBE], outcome.explain()
    assert NO_DEFAULT_DIR not in outcome.result.stderr, outcome.explain()
    assert outcome.alembic().rest[-len(invocation) :] == invocation, outcome.explain()


def test_the_double_renders_only_bind_mounts_at_the_data_directory():
    """The double's reading of MOUNTS_FORMAT, which the tests above lean on: binds at PG_DATA only, one line each."""
    assert PG_DATA in MOUNTS_FORMAT
    mounts = [Mount('/a'), Mount('/b', type='volume'), Mount('/c', destination='/elsewhere'), Mount('/d')]
    assert render_mounts(mounts) == '/a\n/d\n'
    assert render_mounts([]) == ''


# ------------------------------------------------------------------------------------------------- m6: the label


@pytest.mark.parametrize('label', ['dev', 'test', 'Production', ' '], ids=['dev', 'test', 'any word', 'a space'])
def test_a_non_empty_label_on_the_production_definition_refuses_before_the_dump(harness: Harness, label: str):
    """Anything but empty refuses: the label read is the only gate call, so nothing was dumped or migrated."""
    _on(harness, 'prod')
    harness.write('label_read', label)
    outcome = harness.run('upgrade', 'head', run_status=3)

    _assert_refused_before_alembic(outcome, harness, LABEL_SET)
    assert outcome.gate_calls() == [REVISIONS_PROBE, LABEL_READ], outcome.explain()
    assert not harness.backups.exists(), outcome.explain()
    # The label itself is not echoed: the refusal says set (non-empty), never what it was set to.
    assert 'Production' not in outcome.result.stderr, outcome.explain()


def test_an_empty_label_on_the_production_definition_proceeds(harness: Harness):
    """The production case: the label read, then the dump and its read-back, then alembic, in that order."""
    _on(harness, 'prod')
    outcome = harness.run()

    assert outcome.result.returncode == 0, outcome.explain()
    assert outcome.gate_calls() == [REVISIONS_PROBE, LABEL_READ, PG_DUMP, PG_RESTORE_LIST], outcome.explain()
    assert outcome.only(*LABEL_READ).files == (BASE,) and outcome.only(*LABEL_READ).project_args == ()
    assert [c.rest for c in outcome.calls if c.is_compose][-1] == outcome.alembic().rest, outcome.explain()


def test_a_failed_label_read_refuses_before_the_dump(harness: Harness):
    """The one-off that reads the label fails: the label cannot be asserted empty, so the migrate is refused."""
    _on(harness, 'prod')
    harness.write('label_read.rc', '1')
    outcome = harness.run('upgrade', 'head', run_status=3)

    _assert_refused_before_alembic(outcome, harness, LABEL_UNREADABLE)
    assert outcome.gate_calls() == [REVISIONS_PROBE, LABEL_READ], outcome.explain()


@pytest.mark.parametrize(
    ('stack', 'project'),
    [
        (DEFINITIONS['dev'][0], 'dev'),
        (DEFINITIONS['test'][0], 'dev'),
        (DEFINITIONS['dev'][0], 'default'),
        (DEFINITIONS['test'][0], 'default'),
    ],
    ids=['dev', 'test', 'dev label in the default project', 'test label in the default project'],
)
def test_the_dev_and_test_definitions_read_no_label(harness: Harness, stack: Stack, project: str):
    """Dev and test carry a label by design: not asserted, whatever it is; the dump still happens."""
    harness.stack(stack, project)
    harness.write('label_read', 'dev')
    outcome = harness.run()

    assert outcome.result.returncode == 0, outcome.explain()
    assert outcome.gate_calls() == [REVISIONS_PROBE, PG_DUMP, PG_RESTORE_LIST], outcome.explain()
    # The one --entrypoint one-off is the look into the image for revision files, never the label read.
    assert [c.rest for c in outcome.calls if '--entrypoint' in c.argv] == [REVISIONS_PROBE], outcome.explain()


@pytest.mark.parametrize(
    'stack',
    [
        Stack(('pg',), {'pg': ''}),
        Stack(('pg',), {'pg': None}),
        Stack(('pg',), {'pg': BOTH_LABEL}),
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': PROD_LABEL}, ('ds',)),
    ],
    ids=['no label', 'inspect fails', 'both files', 'mixed stack'],
)
def test_an_unclear_stack_in_the_default_project_is_label_checked_like_production(harness: Harness, stack: Stack):
    """THE BEHAVIOUR CHANGE: an unclear stack in the default project is now label-checked, and can be refused.

    An unclear stack there runs the production definition, so it also gets the label check and is refused on a
    non-empty label, where before P03 it always migrated. Pinned so that the change is a decision on record rather than a side effect; see the verdict on tj-7zjov1.9.
    """
    harness.stack(stack, 'default')
    harness.write('label_read', 'dev')
    outcome = harness.run('upgrade', 'head', run_status=3)

    _assert_refused_before_alembic(outcome, harness, LABEL_SET)
    assert outcome.gate_calls() == [REVISIONS_PROBE, LABEL_READ], outcome.explain()
    assert _stderr_lines(outcome)[0].startswith('run_migrations.sh: using the PRODUCTION definition'), outcome.explain()


# ================================================================================================= restore_dump.sh

RESTORE_PROBE = ('compose', '-p', DEV_PROJECT, '-f', BASE, 'ps', '-q', 'postgres')
RESTORE_HEAD = ('compose', '-p', DEV_PROJECT, '-f', BASE, '-f', FAKE)
INSPECT_PG = (
    'inspect', '--format',
    '{{ with .Config.Labels }}{{ index . "com.docker.compose.project.config_files" }}{{ end }}', 'pg',
)  # fmt: skip
NOT_THE_TEST_STACK = (
    f'REFUSED: the running postgres in {DEV_PROJECT} is not the test stack (its compose label must name '
    'docker-compose.fake.yaml and not docker-compose.override.yaml). Start it with make system-launch.'
)
NOT_ONE_POSTGRES = (
    f'REFUSED: expected exactly one running postgres in the {DEV_PROJECT} project (make system-launch starts it).'
)
NOT_ATTESTED = (
    "REFUSED: this replaces the target database's contents. Set SYSTEM_TEST_DISPOSABLE_DB=1 to attest that the "
    'trader_joe_dev test stack holds nothing you need.'
)
DUMP_CONTENT = 'PGDMP a production dump copied here\n'


@pytest.fixture
def dump_file(tmp_path: Path) -> Path:
    path = tmp_path / 'prod.dump'
    path.write_text(DUMP_CONTENT, encoding='utf-8')
    return path


def _restore(harness: Harness, *args: str, disposable: str | None = '1') -> Outcome:
    env = {} if disposable is None else {'SYSTEM_TEST_DISPOSABLE_DB': disposable}
    return harness.run(*args, script=RESTORE_DUMP, extra_env=env)


def _test_stack(harness: Harness) -> None:
    harness.stack(Stack(('pg',), {'pg': SYSTEM_LABEL}), 'dev')


@pytest.mark.parametrize('disposable', [None, '', '0', 'true', 'yes', ' 1', '1 '], ids=repr)
def test_restore_is_refused_without_the_disposable_attestation(harness: Harness, dump_file: Path, disposable):
    """Only the literal 1 attests; anything else refuses before a single docker call, on a valid test stack."""
    _test_stack(harness)
    outcome = _restore(harness, str(dump_file), disposable=disposable)

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == NOT_ATTESTED + '\n', outcome.explain()
    assert outcome.calls == [], outcome.explain()


@pytest.mark.parametrize('case', ['missing', 'empty', 'a directory'])
def test_restore_is_refused_without_a_non_empty_file(harness: Harness, tmp_path: Path, case: str):
    """A missing, empty or non-regular dump path refuses before any docker call."""
    _test_stack(harness)
    path = tmp_path / 'the.dump'
    if case == 'empty':
        path.write_text('', encoding='utf-8')
    elif case == 'a directory':
        path.mkdir()
    outcome = _restore(harness, str(path))

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == f'REFUSED: {path} is not a non-empty file.\n', outcome.explain()
    assert outcome.calls == [], outcome.explain()


@pytest.mark.parametrize('args', [(), ('a.dump', 'b.dump')], ids=['none', 'two'])
def test_restore_takes_exactly_one_argument(harness: Harness, args: tuple[str, ...]):
    outcome = _restore(harness, *args)
    assert outcome.result.returncode == 2, outcome.explain()
    assert outcome.result.stderr == 'usage: restore_dump.sh <dump-file>\n', outcome.explain()
    assert outcome.calls == [], outcome.explain()


@pytest.mark.parametrize(
    ('dev', 'default'),
    [
        (Stack(()), Stack(())),
        (Stack(('pg1', 'pg2'), {'pg1': SYSTEM_LABEL, 'pg2': SYSTEM_LABEL}), Stack(())),
        (Stack(()), Stack(('pg',), {'pg': SYSTEM_LABEL})),
    ],
    ids=['no postgres', 'two postgres', 'postgres only in the default project'],
)
def test_restore_is_refused_without_exactly_one_postgres_in_the_dev_project(
    harness: Harness, dump_file: Path, dev: Stack, default: Stack
):
    """Only trader_joe_dev is probed: the default (production) project is never asked, even when it holds one."""
    harness.stack(dev, 'dev', other=default)
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == NOT_ONE_POSTGRES + '\n', outcome.explain()
    assert [c.argv for c in outcome.calls] == [RESTORE_PROBE], outcome.explain()


@pytest.mark.parametrize(
    'label',
    [
        DEV_LABEL,
        _label(HOST, BASE, OVERRIDE),
        PROD_LABEL,
        _label(HOST, BASE),
        '',
        BOTH_LABEL,
        _label(HOST, BASE, FAKE + '.bak'),
        _label(HOST, BASE, 'other-' + FAKE),
    ],
    ids=['dev', 'bare up', 'prod', 'base only', 'no label', 'both files', 'fake .bak', 'other-fake'],
)
def test_restore_is_refused_unless_postgres_is_the_test_stack(harness: Harness, dump_file: Path, label: str):
    """A dev, prod or label-less postgres, or one naming both files: refused after the label read, nothing exec'd."""
    harness.stack(Stack(('pg',), {'pg': label}), 'dev')
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == NOT_THE_TEST_STACK + '\n', outcome.explain()
    assert [c.argv for c in outcome.calls] == [RESTORE_PROBE, INSPECT_PG], outcome.explain()


def test_restore_is_refused_when_the_label_cannot_be_read(harness: Harness, dump_file: Path):
    """A failed docker inspect on the label: exit 1 with a REFUSED line naming the cause, nothing exec'd after it."""
    harness.stack(Stack(('pg',), {'pg': None}), 'dev')
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode == 1, outcome.explain()
    assert re.match(
        f'REFUSED: could not read the compose config_files label of the running postgres in {DEV_PROJECT}\\b',
        _stderr_lines(outcome)[-1],
    ), outcome.explain()
    assert [c.argv for c in outcome.calls] == [RESTORE_PROBE, INSPECT_PG], outcome.explain()
    assert not outcome.compose(*PSQL_DROP) and not outcome.compose(*PG_RESTORE), outcome.explain()


def _malformed(label: str) -> str:
    return (
        f'REFUSED: the compose config_files label of the running postgres in {DEV_PROJECT} is malformed ({label}). '
        'Start it with make system-launch.'
    )


# Each names docker-compose.fake.yaml and not the override, so a rule that read file names only would accept it:
# only the well-formedness check stands between these and a dropped schema.
MALFORMED_LABELS = {
    'relative entry': FAKE,
    'relative beside absolute': f'/a/{BASE},{FAKE}',
    'leading comma': f',/x/{FAKE}',
    'trailing comma': f'/x/{FAKE},',
    'doubled comma': f'/x/{BASE},,/x/{FAKE}',
    'space after a comma': f'/x/{BASE}, /x/{FAKE}',
    'newline': f'/x/{BASE}\n/x/{FAKE}',
}


@pytest.mark.parametrize('label', list(MALFORMED_LABELS.values()), ids=list(MALFORMED_LABELS))
def test_restore_refuses_every_label_run_migrations_calls_malformed(harness: Harness, dump_file: Path, label: str):
    """classify_config_files calls the label malformed, and restore refuses it.

    Exit 1, its REFUSED line, and no read-back, no drop and no load. Both halves are asserted, so a change to either
    script fails here.
    """
    assert _classify(label).startswith('UNKNOWN: malformed label'), _classify(label)
    harness.stack(Stack(('pg',), {'pg': label}), 'dev')
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr == _malformed(label) + '\n', outcome.explain()
    assert [c.argv for c in outcome.calls] == [RESTORE_PROBE, INSPECT_PG], outcome.explain()
    assert not outcome.compose(*PG_RESTORE_LIST), outcome.explain()
    assert not outcome.compose(*PSQL_DROP), outcome.explain()
    assert not outcome.compose(*PG_RESTORE), outcome.explain()


def test_an_unreadable_dump_is_refused_before_anything_is_dropped(harness: Harness, dump_file: Path):
    """pg_restore --list rejects the file: refused, and the only exec was the read-back: no psql, no load."""
    _test_stack(harness)
    harness.write('list.rc', '1')
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode == 1, outcome.explain()
    assert outcome.result.stderr.splitlines()[-1] == (
        f'REFUSED: {dump_file} does not read back (pg_restore --list failed); the target was not touched.'
    ), outcome.explain()
    assert [c.argv for c in outcome.calls] == [RESTORE_PROBE, INSPECT_PG, (*RESTORE_HEAD, *PG_RESTORE_LIST)], (
        outcome.explain()
    )
    assert harness.read('stdin.list.0') == DUMP_CONTENT


def test_a_restore_reads_back_then_drops_then_loads_in_that_order(harness: Harness, dump_file: Path):
    """The success path: read-back, schema drop, load, each in the test stack's postgres; the load gets the dump."""
    _test_stack(harness)
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode == 0, outcome.explain()
    assert [c.argv for c in outcome.calls] == [
        RESTORE_PROBE,
        INSPECT_PG,
        (*RESTORE_HEAD, *PG_RESTORE_LIST),
        (*RESTORE_HEAD, *PSQL_DROP),
        (*RESTORE_HEAD, *PG_RESTORE),
    ], outcome.explain()
    assert harness.read('stdin.list.0') == DUMP_CONTENT
    assert harness.read('stdin.restore') == DUMP_CONTENT
    assert outcome.result.stdout == f'restore_dump.sh: restored {dump_file} into the {DEV_PROJECT} test database\n'


def test_a_failed_drop_never_loads(harness: Harness, dump_file: Path):
    _test_stack(harness)
    harness.write('psql.rc', '1')
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode != 0, outcome.explain()
    assert not outcome.compose(*PG_RESTORE), outcome.explain()
    assert 'restored' not in outcome.result.stdout, outcome.explain()


def test_a_failed_load_exits_non_zero(harness: Harness, dump_file: Path):
    _test_stack(harness)
    harness.write('restore.rc', '1')
    outcome = _restore(harness, str(dump_file))

    assert outcome.result.returncode != 0, outcome.explain()
    assert 'restored' not in outcome.result.stdout, outcome.explain()


# ------------------------------------------------------------------------------------------------- parity


def _classify(label: str) -> str:
    """run_migrations.sh's own classify_config_files, extracted from its marked block and run on LABEL."""
    lines = RUN_MIGRATIONS.read_text(encoding='utf-8').splitlines()
    begin = [i for i, line in enumerate(lines) if line.startswith('# BEGIN classify_config_files')]
    end = [i for i, line in enumerate(lines) if line.startswith('# END classify_config_files')]
    assert len(begin) == 1 and len(end) == 1 and begin[0] < end[0]
    block = '\n'.join(lines[begin[0] : end[0] + 1])
    result = subprocess.run(
        [BASH, '-c', f'set -euo pipefail\n{block}\nclassify_config_files "$1"', 'classify', label],
        capture_output=True,
        text=True,
        check=False,
        env={'PATH': os.environ['PATH']},
    )
    assert result.returncode == 0 and result.stderr == '', result
    return result.stdout.strip()


# Every non-empty combination of the two files that decide, the base, a web file and two near misses, in two
# orders and under two roots: each well formed, so classify_config_files gives DEV, TEST, PROD or the both-files
# UNKNOWN. The malformed labels are their own table, MALFORMED_PARITY_LABELS, below.
_POOL = (BASE, OVERRIDE, FAKE, WEB, FAKE + '.bak', 'other-' + OVERRIDE)
PARITY_LABELS = sorted({
    _label(root, *(reversed(names) if backwards else names))
    for size in range(1, len(_POOL) + 1)
    for names in itertools.combinations(_POOL, size)
    for root, backwards in ((HOST, False), (DEVCONTAINER, True))
})  # fmt: skip


def test_the_parity_table_covers_every_verdict():
    """Not vacuous: the table holds TEST labels, and DEV, PROD and both-files labels that restore must refuse."""
    verdicts = {_classify(label).split(':')[0] for label in PARITY_LABELS}
    assert verdicts == {'DEV', 'TEST', 'PROD', 'UNKNOWN'}, verdicts
    assert len(PARITY_LABELS) == 126


@pytest.mark.parametrize('label', PARITY_LABELS)
def test_restore_accepts_exactly_the_labels_run_migrations_calls_test(harness: Harness, dump_file: Path, label: str):
    """restore_dump.sh's restated rule agrees with classify_config_files on every well-formed label.

    The read-back is made to fail, so an accepted label shows as reaching it (and nothing is ever dropped); a
    refused one stops at the label refusal.
    """
    harness.stack(Stack(('pg',), {'pg': label}), 'dev')
    harness.write('list.rc', '1')
    outcome = _restore(harness, str(dump_file))

    accepted = outcome.compose(*PG_RESTORE_LIST) != []
    assert outcome.result.returncode == 1, outcome.explain()
    assert accepted == (_classify(label) == 'TEST'), (label, _classify(label), outcome.explain())
    assert accepted == (NOT_THE_TEST_STACK not in outcome.result.stderr), outcome.explain()


# Every TEST label of the well-formed table, broken each way classify_config_files calls malformed: a relative entry
# (the first, or the fake file's, made relative), a space after a comma, a leading, trailing or doubled comma, and a
# newline in place of a comma. Each would be TEST if it were well formed, so this is where a narrower restatement in
# restore_dump.sh would accept what run_migrations.sh refuses.
def _malformations(label: str) -> list[str]:
    entries = label.split(',')
    fake = next(i for i, entry in enumerate(entries) if entry.endswith('/' + FAKE))
    broken = [
        ','.join([entries[0].lstrip('/'), *entries[1:]]),
        ','.join([*entries[:fake], FAKE, *entries[fake + 1 :]]),
        ',' + label,
        label + ',',
        label.replace(',', ',,', 1),
        label.replace(',', '\n', 1),
    ]
    if len(entries) > 1:
        broken.append(label.replace(',', ', ', 1))
    return [b for b in broken if b != label]


MALFORMED_PARITY_LABELS = sorted(
    {broken for label in PARITY_LABELS if _classify(label) == 'TEST' for broken in _malformations(label)}
    | set(MALFORMED_LABELS.values())
)


def test_the_malformed_parity_table_is_all_malformed_to_run_migrations():
    """Not vacuous: every label in it is one classify_config_files calls malformed, and it is not a handful."""
    verdicts = {_classify(label) for label in MALFORMED_PARITY_LABELS}
    assert all(v.startswith('UNKNOWN: malformed label') for v in verdicts), verdicts
    assert len(MALFORMED_PARITY_LABELS) > 100, len(MALFORMED_PARITY_LABELS)


@pytest.mark.parametrize('label', MALFORMED_PARITY_LABELS, ids=repr)
def test_restore_refuses_exactly_the_labels_run_migrations_calls_malformed(
    harness: Harness, dump_file: Path, label: str
):
    """The parity over malformed labels: restore accepts one iff classify_config_files calls it TEST.

    For this table that is never: each is refused with the malformed line, before the read-back.
    """
    harness.stack(Stack(('pg',), {'pg': label}), 'dev')
    harness.write('list.rc', '1')
    outcome = _restore(harness, str(dump_file))

    accepted = outcome.compose(*PG_RESTORE_LIST) != []
    assert outcome.result.returncode == 1, outcome.explain()
    assert accepted == (_classify(label) == 'TEST'), (label, _classify(label), outcome.explain())
    assert outcome.result.stderr == _malformed(label) + '\n', outcome.explain()


# ================================================================================================= rehearse_migration.sh


def _rehearsal_stack(harness: Harness, data_store: bool = True, health: str | None = 'healthy') -> None:
    labels = {'pg': SYSTEM_LABEL, **({'ds': SYSTEM_LABEL} if data_store else {})}
    harness.stack(Stack(('pg',), labels, ('ds',) if data_store else ()), 'dev')
    if health is not None:
        harness.write('health.ds', health)


def _rehearse(harness: Harness, dump: Path, alembic_stdout: str = 'deadbeef0001 (head)', disposable: str = '1'):
    return harness.run(
        str(dump),
        script=REHEARSE_MIGRATION,
        alembic_stdout=alembic_stdout,
        extra_env={'SYSTEM_TEST_DISPOSABLE_DB': disposable},
    )


def test_a_rehearsal_restores_migrates_and_reports_health(harness: Harness, dump_file: Path):
    """The whole flow on the test stack: restore, a migrate that takes its own dump, current at head, health OK."""
    _rehearsal_stack(harness)
    outcome = _rehearse(harness, dump_file)

    assert outcome.result.returncode == 0, outcome.explain()
    stdout = outcome.result.stdout.splitlines()
    assert stdout[-1] == 'REHEARSAL OK', outcome.explain()
    for line in (
        f'REHEARSAL: restored {dump_file}',
        'REHEARSAL: migrated to head',
        'REHEARSAL: deadbeef0001 (head)',
        'REHEARSAL: database is at head',
        'REHEARSAL: data_store health: healthy',
    ):
        assert line in stdout, (line, outcome.explain())

    # Restore first, then the migrate (the look into the image, dump, read-back, upgrade), then current (which looks
    # too); all in the test stack.
    exec_and_run = [c.rest for c in outcome.calls if c.is_compose and c.rest[:1] in (('exec',), ('run',))]
    assert exec_and_run == [
        PG_RESTORE_LIST, PSQL_DROP, PG_RESTORE,
        REVISIONS_PROBE, PG_DUMP, PG_RESTORE_LIST, (*ALEMBIC_RUN, '/code/.venv/bin/alembic', 'upgrade', 'head'),
        REVISIONS_PROBE, (*ALEMBIC_RUN, '/code/.venv/bin/alembic', 'current'),
    ], outcome.explain()  # fmt: skip
    # Only run_migrations.sh's default-project postgres probe (twice: the migrate and current) names no project.
    no_project = [c.rest for c in outcome.calls if c.is_compose and c.project_args != ('-p', DEV_PROJECT)]
    assert no_project == [('ps', '-q', 'postgres')] * 2, outcome.explain()
    assert harness.read('stdin.restore') == DUMP_CONTENT
    assert len(harness.backup_files()) == 1, outcome.explain()
    assert outcome.calls[-1].argv == ('inspect', '--format', '{{ with .State.Health }}{{ .Status }}{{ end }}', 'ds')


def test_a_rehearsal_not_at_head_fails_without_ok(harness: Harness, dump_file: Path):
    """Not at head after the migrate (no (head) from current): non-zero, REHEARSAL FAILED, never REHEARSAL OK."""
    _rehearsal_stack(harness)
    outcome = _rehearse(harness, dump_file, alembic_stdout='deadbeef0001')

    assert outcome.result.returncode != 0, outcome.explain()
    assert 'REHEARSAL OK' not in outcome.result.stdout, outcome.explain()
    assert outcome.result.stderr.splitlines()[-1] == (
        'REHEARSAL FAILED: the database is not at head after the migration.'
    ), outcome.explain()


def test_a_rehearsal_with_an_unhealthy_data_store_fails_without_ok(harness: Harness, dump_file: Path):
    _rehearsal_stack(harness, health='unhealthy')
    outcome = _rehearse(harness, dump_file)

    assert outcome.result.returncode != 0, outcome.explain()
    assert 'REHEARSAL OK' not in outcome.result.stdout, outcome.explain()
    assert 'REHEARSAL: data_store health: unhealthy' in outcome.result.stdout.splitlines(), outcome.explain()
    assert outcome.result.stderr.splitlines()[-1] == 'REHEARSAL FAILED: data_store reports unhealthy.'


def test_a_rehearsal_with_a_stopped_data_store_says_health_was_not_checked(harness: Harness, dump_file: Path):
    """No data_store running: OK, with the line that says its health was not checked, and no health inspect."""
    _rehearsal_stack(harness, data_store=False)
    outcome = _rehearse(harness, dump_file)

    assert outcome.result.returncode == 0, outcome.explain()
    stdout = outcome.result.stdout.splitlines()
    assert 'REHEARSAL: data_store container not running; its health was not checked' in stdout, outcome.explain()
    assert stdout[-1] == 'REHEARSAL OK', outcome.explain()
    assert not [c for c in outcome.calls if 'State.Health' in ' '.join(c.argv)], outcome.explain()


def test_a_rehearsal_with_no_healthcheck_reports_none(harness: Harness, dump_file: Path):
    _rehearsal_stack(harness, health=None)
    outcome = _rehearse(harness, dump_file)

    assert outcome.result.returncode == 0, outcome.explain()
    assert 'REHEARSAL: data_store health: none' in outcome.result.stdout.splitlines(), outcome.explain()


def test_a_refused_restore_stops_the_rehearsal_before_any_migrate(harness: Harness, dump_file: Path):
    """The restore's refusal ends the run: no dump, no alembic, no OK."""
    _rehearsal_stack(harness)
    outcome = _rehearse(harness, dump_file, disposable='0')

    assert outcome.result.returncode != 0, outcome.explain()
    assert outcome.calls == [], outcome.explain()
    assert 'REHEARSAL' not in outcome.result.stdout, outcome.explain()


def test_a_failed_migrate_stops_the_rehearsal(harness: Harness, dump_file: Path):
    """A failing alembic upgrade inside the rehearsal: non-zero, never at head, never OK."""
    _rehearsal_stack(harness)
    outcome = harness.run(
        str(dump_file), script=REHEARSE_MIGRATION, run_status=1, extra_env={'SYSTEM_TEST_DISPOSABLE_DB': '1'}
    )

    assert outcome.result.returncode != 0, outcome.explain()
    assert 'REHEARSAL: migrated to head' not in outcome.result.stdout, outcome.explain()
    assert 'REHEARSAL OK' not in outcome.result.stdout, outcome.explain()
