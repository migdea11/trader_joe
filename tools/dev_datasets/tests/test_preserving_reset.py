"""The PRESERVING DEV RESET (validator, tj-7zjov1.297 P56v; built by tj-7zjov1.296 in tools/dev_datasets/request.py).

THE DESIGN: decision tj-7zjov1.291 D1 and D3, R2 and R3, and the owner's rulings of 2026-11-03 (no dev guard; O1 to O11
as recommended, corporate actions 180 days). On the DEV label the reset is ONE transaction, at READ COMMITTED (the
builder's choice, recorded on tj-7zjov1.296 and in the README: the ACCESS EXCLUSIVE lock is taken before the first read,
so every statement sees every committed row, where a REPEATABLE READ snapshot could predate the lock):

  lock timeout; LOCK the eighteen; the RUNNING and open-request counts (refuse on either, as before); the counts before;
  the export (dev_seed.write_seed: session settings, the revision, which must be the checkout head or exit 3 before any
  file, then per table a COPY ... TO of the trimmed rows and its column read, then the manifest); every written file
  checked against the manifest (size and sha256) BEFORE the one TRUNCATE ... RESTART IDENTITY; the COPY ... FROM of each
  file, parents first, with the bytes the server reads hashed again; the setval of stock_market_activity.id (the one
  serial) to max(id); the recount against the manifest; COMMIT. Nothing else in between. Any failure after the export
  rolls the whole transaction back and leaves the export on disk; a failure inside the export removes its directory.
  After the commit, the quiet-stack wait and a recount that expects the manifest's counts (test_reset.py runs every
  quiet-stack test on both labels). On the TEST label the reset is the plain wipe: no export, no reload, every table 0.

The database is doubles.FakeDb, which streams the seed's COPY text (doubles.seeded_copy_data, SEED_ROWS rows per table),
reads back what the tool COPYs in, and models the uncommitted counts and the bar id sequence. The files are real, under
tmp_path. WHAT THIS CANNOT PROVE is that Postgres accepts these statements, in this order, inside one transaction, and
loads the files back to the same rows: tests/system/test_dev_seed_reset_pg.py does that, on a scratch database.
"""

import json
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from tools.dev_datasets import dev_seed
from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    ALEMBIC_TEXT,
    COLUMNS_TEXT,
    EIGHTEEN_TABLES,
    NOW,
    OPEN_REQUESTS_TEXT,
    QUIESCE_SECONDS,
    RUNNING_COUNT_TEXT,
    SEED_BAR_IDS,
    SEED_ROWS,
    SESSION_TEXTS,
    SETVAL_TEXT,
    STACK_TEXT,
    TABLE_COUNTS,
    TEST_CONFIG,
    FakeDb,
    assert_no_canary,
    run_tool,
    seeded_copy_data,
    store_for,
)
from tools.dev_datasets.tests.test_reset import STORE_FOREIGN_KEYS, TRUNCATE, counts_block


pytestmark = pytest.mark.build_infra

# ---- the design, written out ---------------------------------------------------------------------------------------

# The load order (D1: parents first): the bar after its entry, a panel before its versions and passes, a pass before its
# transitions. Written out here, not read from dev_seed.SEED_TABLES; the first test holds the two together.
LOAD_ORDER = (
    'store_dataset_entry',
    'stock_market_activity',
    'ingest_run',
    'ingest_request',
    'ingest_instance',
    'dataset_read',
    'dataset_read_daily',
    'dataset_override_log',
    'dataset_deletion',
    'asset_list_snapshot',
    'asset_list_row',
    'asset_list_event',
    'corporate_action',
    'panel',
    'panel_settings_version',
    'panel_source_settings_version',
    'panel_pass',
    'panel_member_transition',
)
LOCK_TIMEOUT = ' '.join(tool.LOCK_TIMEOUT_SQL.split())
TABLE_LOCK = ' '.join(tool.LOCK_SQL.split())
COUNTS = [f'SELECT count(*) FROM {table}' for table in EIGHTEEN_TABLES]
STAMP = '20261020T150000Z'
CUTOFF_TEXT = (NOW - timedelta(days=90)).isoformat()
SEED_TOTAL = sum(SEED_ROWS.values())
RAIL_DEV = "rail: deploymentLabel is 'dev' (real Alpaca backend)"
ROLLED_BACK = 'the reset transaction failed and was rolled back (RuntimeError)'


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The seed root of one test: not created, so 'nothing was written' means it does not exist or is empty."""
    return tmp_path / 'seed'


def head() -> str:
    return dev_seed.head_revision()


def seeded(**kwargs) -> FakeDb:
    kwargs.setdefault('copy_data', seeded_copy_data())
    return FakeDb(**kwargs)


def dev_reset(db: FakeDb, root: Path, *argv: str):
    return run_tool(['reset', '--yes-reset', *argv], http=store_for(), connect=db.connect, seed_root=root)


def transcript(db: FakeDb) -> list[str]:
    """Every event in order: statements collapsed, the COPYs out and in by table, and the transaction boundaries."""
    names = {'mogrify': 'MOGRIFY', 'copy': 'COPY OUT', 'copy_in': 'COPY IN'}
    out = []
    for event in db.events:
        if event[0] == 'execute':
            out.append(' '.join(event[1].split()))
        elif event[0] == 'mogrify':
            out.append(f'MOGRIFY {next(t for t, s in dev_seed.TRIM_SQL.items() if s == event[1])}')
        elif event[0] in names:
            out.append(f'{names[event[0]]} {event[1]}')
        else:
            out.append(event[0].upper() if len(event) == 1 else f'{event[0].upper()} {event[1]}')
    return out


def export_part() -> list[str]:
    part = [*SESSION_TEXTS, ALEMBIC_TEXT]
    for table in LOAD_ORDER:
        part += [f'MOGRIFY {table}', f'COPY OUT {table}', COLUMNS_TEXT]
    return part


def reload_part() -> list[str]:
    return [TRUNCATE, *[f'COPY IN {table}' for table in LOAD_ORDER], SETVAL_TEXT]


DEV_TRANSACTION = [
    'CONNECT',
    STACK_TEXT,
    LOCK_TIMEOUT,
    TABLE_LOCK,
    RUNNING_COUNT_TEXT,
    OPEN_REQUESTS_TEXT,
    *COUNTS,
    *export_part(),
    *reload_part(),
    *COUNTS,
    'COMMIT',
    'CLOSE',
]
RECOUNT = [f'SLEEP {QUIESCE_SECONDS}', 'CONNECT', STACK_TEXT, LOCK_TIMEOUT, *COUNTS, 'ROLLBACK', 'CLOSE']


def stamps(root: Path) -> list[Path]:
    """The complete stamp directories under a seed root (manifest.json present)."""
    return sorted(path.parent for path in root.glob('*/*/manifest.json'))


def manifest_of(directory: Path) -> dict:
    return json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))


def assert_export_intact(root: Path) -> Path:
    """Exactly one complete stamp, at the fixed clock, whose files still verify against its manifest."""
    [directory] = stamps(root)
    assert directory == root / head() / STAMP
    manifest = dev_seed.read_manifest(directory)
    dev_seed.verify_files(directory, manifest['tables'])
    assert {table: entry['rows'] for table, entry in manifest['tables'].items()} == SEED_ROWS
    return directory


def assert_nothing_changed(db: FakeDb) -> None:
    assert db.counts == TABLE_COUNTS
    assert db.commits == 0
    assert db.sequence is None
    assert db.sleeps == []


# ---- the order: one transaction, nothing between ------------------------------------------------------------------


def test_the_load_order_is_the_seed_tables_and_every_parent_loads_before_its_child():
    assert dev_seed.SEED_TABLES == LOAD_ORDER
    assert set(LOAD_ORDER) == set(EIGHTEEN_TABLES)
    for child, parent, _ in STORE_FOREIGN_KEYS:
        assert LOAD_ORDER.index(parent) < LOAD_ORDER.index(child), (child, parent)
    # One COPY FROM per table, written out, and nothing else in it (no column list, no options, no other table).
    assert {table: f'COPY {table} FROM STDIN' for table in LOAD_ORDER} == dev_seed.LOAD_SQL
    assert list(dev_seed.LOAD_SQL) == list(LOAD_ORDER)


def test_a_dev_reset_is_exactly_the_designed_sequence_in_one_transaction(root):
    db = seeded()
    outcome = dev_reset(db, root)
    assert outcome.status == 0, outcome.text
    assert transcript(db) == [*DEV_TRANSACTION, *RECOUNT]
    assert db.autocommit is False
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (2, 1, 1, 2)
    assert db.counts == SEED_ROWS
    assert_no_canary(outcome.text)


def test_the_copies_in_follow_the_observed_load_order_with_parents_first(root):
    db = seeded()
    assert dev_reset(db, root).status == 0
    order = [table for table, _ in db.loads]
    assert order == list(LOAD_ORDER)
    for child, parent, _ in STORE_FOREIGN_KEYS:
        assert order.index(parent) < order.index(child), (child, parent)


def test_the_reset_stays_read_committed_and_takes_the_lock_before_its_first_read(root):
    # No SET TRANSACTION (the export's own REPEATABLE READ READ ONLY must never reach the write transaction) and no
    # isolation change on the connection (FakeDb has no set_session or isolation_level: a call would fail the run).
    db = seeded()
    assert dev_reset(db, root).status == 0
    reset_statements = db.per_connection()[0]
    assert not any('TRANSACTION' in sql.upper() or 'ISOLATION' in sql.upper() for sql in reset_statements)
    assert dev_seed.READ_ONLY_SQL not in db.sql()
    steps = transcript(db)
    lock = steps.index(TABLE_LOCK)
    assert steps[:lock] == ['CONNECT', STACK_TEXT, LOCK_TIMEOUT]
    assert steps.index(ALEMBIC_TEXT) > lock
    assert min(i for i, step in enumerate(steps) if step.startswith(('COPY', 'MOGRIFY'))) > lock


def test_a_dev_reset_prints_the_export_the_reload_and_the_manifest_counts(root):
    db = seeded()
    outcome = dev_reset(db, root)
    data = seeded_copy_data()
    directory = root / head() / STAMP
    assert outcome.lines == [
        RAIL_DEV,
        *counts_block('reset: rows before', TABLE_COUNTS),
        f'export: revision {head()} (the checkout head); cutoff {CUTOFF_TEXT}',
        *(f'  {table}: rows {SEED_ROWS[table]} bytes {len(data[table])}' for table in LOAD_ORDER),
        f'reset: the seed is on disk ({directory}); wiping and reloading it in this transaction',
        *(f'  {table}: loaded' for table in LOAD_ORDER),
        *counts_block('reset: rows after', SEED_ROWS),
        f'reset: waiting {QUIESCE_SECONDS}s (two usage flush intervals) to verify the stack stayed quiet',
        f'reset: verified clean (all eighteen tables as loaded, {SEED_TOTAL} rows in all)',
    ]
    assert outcome.stdout == outcome.stderr == ''


def test_the_export_is_the_seed_the_reload_read_back_byte_for_byte(root):
    db = seeded()
    assert dev_reset(db, root).status == 0
    directory = assert_export_intact(root)
    manifest = manifest_of(directory)
    assert manifest['revision'] == manifest['checkout_head'] == head()
    assert manifest['stack'] == 'dev'
    assert manifest['load_order'] == list(LOAD_ORDER)
    for table, data in db.loads:
        assert (directory / f'{table}.copy').read_bytes() == data == seeded_copy_data()[table]
    # D5: the files and the directory are private.
    assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
    assert {stat.S_IMODE(os.lstat(path).st_mode) for path in directory.iterdir()} == {0o600}


# ---- R3: the sequence -----------------------------------------------------------------------------------------------


def test_the_setval_is_the_designed_statement_for_the_one_serial():
    assert {table: ' '.join(sql.split()) for table, sql in dev_seed.SETVAL_SQL.items()} == {
        'stock_market_activity': SETVAL_TEXT
    }
    assert frozenset({('stock_market_activity', 'id')}) == dev_seed.SERIAL_COLUMNS


SERIALS_CHILD = r"""
import importlib, json, pkgutil
import sqlalchemy as sa
import data.store.app.database.models as models
for info in pkgutil.iter_modules(models.__path__):
    importlib.import_module(f'{models.__name__}.{info.name}')
from common.database.sql_alchemy_table import AppBase
found = []
for table in AppBase.DATA_STORE_BASE.metadata.tables.values():
    column = table.autoincrement_column
    if column is not None:
        found.append([table.name, column.name])
    found += [[table.name, c.name] for c in table.columns if c.identity is not None and c is not column]
print(json.dumps(sorted(found)))
"""


def test_the_one_serial_the_setval_resets_is_the_only_serial_the_store_models_declare():
    # The dev reset trusts SERIAL_COLUMNS without asking the live schema (load asks, through SERIAL_COLUMNS_SQL; the
    # reset does not: reported on tj-7zjov1.297). So the models are read here: a new serial or identity column on any
    # of the eighteen turns this red before a reset leaves its sequence at 1 under explicit-id rows.
    import subprocess  # nosec B404 -- this interpreter, a fixed script
    import sys

    from tools.dev_datasets.tests.test_store_contract import REPO_ROOT, SERVER_ROOT

    env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(SERVER_ROOT), str(REPO_ROOT)])}
    done = subprocess.run(  # nosec B603 -- this interpreter, a fixed script, no shell
        [sys.executable, '-c', SERIALS_CHILD],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    found = {tuple(pair) for pair in json.loads(done.stdout.strip().splitlines()[-1])}
    assert found == set(dev_seed.SERIAL_COLUMNS) == {('stock_market_activity', 'id')}


def test_after_a_dev_reset_the_bar_sequence_is_at_max_id_so_the_next_insert_gets_max_plus_one(root):
    db = seeded()
    assert dev_reset(db, root).status == 0
    # setval(seq, max(id), true): the next nextval is max(id) + 1, whatever order or count the ids came in.
    assert db.sequence == (max(SEED_BAR_IDS), True)
    assert db.setvals == [(4100, True)]
    steps = transcript(db)
    assert steps.index(SETVAL_TEXT) == steps.index(f'COPY IN {LOAD_ORDER[-1]}') + 1
    assert steps.count(SETVAL_TEXT) == 1


def test_an_empty_bar_seed_leaves_the_sequence_at_one_not_yet_called(root):
    data = seeded_copy_data()
    data['stock_market_activity'] = b''
    db = seeded(copy_data=data)
    assert dev_reset(db, root).status == 0
    assert db.sequence == (1, False)
    assert db.counts['stock_market_activity'] == 0


# ---- D3: the revision ---------------------------------------------------------------------------------------------


def test_a_database_not_at_the_checkout_head_refuses_with_exit_3_before_any_file_or_write(root):
    db = seeded(revision='0000deadbeef')
    outcome = dev_reset(db, root)
    assert outcome.status == 3
    assert outcome.lines[-1] == (
        f'refused: the database is at revision 0000deadbeef and the checkout head is {head()}; '
        'migrate the dev database to the head first. Nothing was written'
    )
    assert TRUNCATE not in db.sql()
    assert db.copies == db.loads == db.mogrified == []
    assert not any(root.rglob('*'))
    assert (db.commits, db.rollbacks) == (0, 1)
    assert_nothing_changed(db)


@pytest.mark.parametrize('rows', [[], [('a',), ('b',)], [(None,)]], ids=['none', 'two', 'null'])
def test_an_alembic_version_that_is_not_one_revision_refuses_with_exit_3(root, rows):
    db = seeded(alembic_rows=rows)
    outcome = dev_reset(db, root)
    assert outcome.status == 3
    assert outcome.lines[-1] == (f'alembic_version holds {len(rows)} row(s), not exactly one revision: migrate first')
    assert TRUNCATE not in db.sql()
    assert not any(root.rglob('*'))
    assert_nothing_changed(db)


def test_the_test_label_reads_no_revision_and_writes_no_seed(root):
    # The plain wipe on test runs whatever the revision: it reloads nothing, so it is keyed to nothing.
    db = seeded(stack='test', revision='0000deadbeef')
    outcome = run_tool(['reset', '--yes-reset'], http=store_for(config=TEST_CONFIG), connect=db.connect, seed_root=root)
    assert outcome.status == 0, outcome.text
    assert ALEMBIC_TEXT not in db.sql()
    assert SETVAL_TEXT not in db.sql()
    assert db.copies == db.loads == db.mogrified == []
    assert not any(root.rglob('*'))
    assert db.counts == dict.fromkeys(EIGHTEEN_TABLES, 0)
    assert outcome.lines[-1] == 'reset: verified clean (all eighteen tables 0)'


# ---- the files are checked before the TRUNCATE ----------------------------------------------------------------------


def _run_reset_tampering(db: FakeDb, root: Path, tamper) -> tuple[list[str], Exception | None]:
    """run_reset itself, with the Reporter as the hook.

    `tamper` runs once every file is written, before the manifest check (the line for the last table is the export's
    last before it).
    """
    lines: list[str] = []
    last = f'  {LOAD_ORDER[-1]}: rows '

    def line(text: str) -> None:
        lines.append(text)
        if text.startswith(last):
            [directory] = [path for path in root.glob('*/*') if path.is_dir()]
            tamper(directory)

    try:
        tool.run_reset(db.connect, tool.Reporter(line), db.sleep, label='dev', moment=NOW, seed_root=root)
    except tool.DevDatasetsError as exc:
        return lines, exc
    return lines, None


def test_a_file_whose_size_changed_after_it_was_written_refuses_before_the_truncate(root):
    db = seeded()

    def grow(directory: Path) -> None:
        with open(directory / 'panel_pass.copy', 'ab') as handle:
            handle.write(b'extra\trow\n')

    _, error = _run_reset_tampering(db, root, grow)
    size = len(seeded_copy_data()['panel_pass'])
    assert type(error) is tool.DevDatasetsError
    assert str(error) == f'panel_pass.copy has {size + 10} bytes and the manifest says {size}: refused'
    assert TRUNCATE not in db.sql()
    assert db.loads == []
    assert (db.commits, db.rollbacks) == (0, 1)
    assert_nothing_changed(db)


def test_a_file_whose_bytes_changed_after_it_was_written_refuses_before_the_truncate(root):
    db = seeded()

    def flip(directory: Path) -> None:
        path = directory / 'asset_list_row.copy'
        data = bytearray(path.read_bytes())
        data[0] ^= 0x01
        path.write_bytes(bytes(data))

    _, error = _run_reset_tampering(db, root, flip)
    assert str(error) == 'asset_list_row.copy does not match the manifest sha256: refused'
    assert TRUNCATE not in db.sql()
    assert db.loads == []
    assert_nothing_changed(db)


def test_a_file_changed_between_its_check_and_its_copy_is_caught_by_the_hash_of_what_the_server_read(root):
    # reload_seed hashes the bytes the server consumes: a file edited after verify_files is refused, and everything,
    # the TRUNCATE included, rolls back.
    seen = []

    def edit_panel(table: str) -> None:
        if table == 'panel':
            [path] = root.glob('*/*/panel.copy')
            data = bytearray(path.read_bytes())
            data[0] ^= 0x01
            path.write_bytes(bytes(data))
            seen.append(table)

    db = seeded(before_load=edit_panel)
    outcome = dev_reset(db, root)
    assert seen == ['panel']
    assert outcome.status == 1
    assert outcome.lines[-1] == 'panel.copy changed while it was loaded: refused'
    assert TRUNCATE in db.sql()
    assert SETVAL_TEXT not in db.sql()
    assert [table for table, _ in db.loads] == list(LOAD_ORDER[: LOAD_ORDER.index('panel') + 1])
    assert (db.commits, db.rollbacks) == (0, 1)
    assert_nothing_changed(db)


# ---- the recount against the manifest, before the commit ------------------------------------------------------------


@pytest.mark.parametrize('table', ['stock_market_activity', 'panel_pass', 'asset_list_row'])
def test_a_reload_that_restores_fewer_rows_than_the_manifest_rolls_back_with_the_export_on_disk(root, table):
    db = seeded(load_shortfall={table: 1})
    outcome = dev_reset(db, root)
    assert outcome.status == 1
    assert outcome.lines[-1] == (
        f'the reload did not restore the manifest counts ({table} {SEED_ROWS[table] - 1} expected {SEED_ROWS[table]}); '
        'rolled back, the export is on disk'
    )
    assert transcript(db)[-1] == 'CLOSE'
    assert 'COMMIT' not in transcript(db)
    assert 'reset: rows after:' not in outcome.lines
    assert_nothing_changed(db)
    assert_export_intact(root)


# ---- a failure at any step after the export rolls back and leaves the export -----------------------------------------


def _arm_after_reload(db: FakeDb, fail_on: str) -> None:
    """Fail the first statement containing `fail_on` that runs after the last COPY IN (the recount)."""

    def arm(table: str) -> None:
        if table == LOAD_ORDER[-1]:
            db.execute_error, db.fail_on = RuntimeError('boom DETAILTEXT'), fail_on

    db.before_load = arm


def _boom() -> RuntimeError:
    return RuntimeError('boom DETAILTEXT')


FAILURES = {
    'truncate': {'execute_error': _boom(), 'fail_on': 'TRUNCATE'},
    'first-copy-in': {'load_error': _boom(), 'load_error_on': LOAD_ORDER[0]},
    'last-copy-in': {'load_error': _boom(), 'load_error_on': LOAD_ORDER[-1]},
    'setval': {'execute_error': _boom(), 'fail_on': 'setval'},
    'recount': {},
}


@pytest.mark.parametrize('step', list(FAILURES))
def test_a_failure_at_any_step_after_the_export_rolls_everything_back_and_keeps_the_export(root, step):
    db = seeded(**FAILURES[step])
    if step == 'recount':
        _arm_after_reload(db, 'count(*) FROM panel_member_transition')
    outcome = dev_reset(db, root)
    assert outcome.status == 1
    assert outcome.lines[-1] == ROLLED_BACK
    assert 'DETAILTEXT' not in outcome.text
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 0, 1, 1)
    assert_nothing_changed(db)
    assert_export_intact(root)


def test_a_failing_commit_keeps_the_export_and_changes_nothing(root):
    db = seeded()

    def refuse():
        raise RuntimeError('could not serialize')

    db.commit = refuse
    outcome = dev_reset(db, root)
    assert outcome.status == 1
    assert outcome.lines[-1] == ROLLED_BACK
    assert db.rollbacks == 1
    assert db.counts == TABLE_COUNTS
    assert db.sleeps == []
    assert_export_intact(root)


@pytest.mark.parametrize('table', ['store_dataset_entry', 'panel', 'panel_member_transition'])
def test_a_failure_inside_the_export_removes_its_directory_and_never_truncates(root, table):
    db = seeded(copy_error=RuntimeError('boom'), copy_error_on=table)
    outcome = dev_reset(db, root)
    assert outcome.status == 1
    assert outcome.lines[-1] == ROLLED_BACK
    assert TRUNCATE not in db.sql()
    assert db.loads == []
    assert stamps(root) == []
    assert list((root / head()).iterdir()) == []
    assert_nothing_changed(db)


def test_a_stamp_that_exists_refuses_before_any_write(root):
    # The fixed clock's stamp is already there: the export refuses (exit 1) and the TRUNCATE never runs.
    (root / head() / STAMP).mkdir(parents=True)
    db = seeded()
    outcome = dev_reset(db, root)
    assert outcome.status == 1
    assert outcome.lines[-1] == f'{STAMP} already exists under {root / head()}: wait a second and rerun'
    assert TRUNCATE not in db.sql()
    assert_nothing_changed(db)


def test_an_in_flight_refusal_writes_no_seed(root):
    db = seeded(running=1)
    outcome = dev_reset(db, root)
    assert outcome.status == 1
    assert outcome.lines[-1].startswith('refused: 1 ingest run(s) are RUNNING')
    assert not any(root.rglob('*'))
    assert db.mogrified == db.copies == []


# ---- the prune, the seed mode ----------------------------------------------------------------------------------------


def test_the_reset_keeps_the_newest_three_complete_stamps_of_the_revision(root):
    revision_dir = root / head()
    revision_dir.mkdir(parents=True)
    older = ['20261001T000000Z', '20261002T000000Z', '20261003T000000Z']
    for name in older:
        (revision_dir / name).mkdir(mode=0o700)
        (revision_dir / name / 'manifest.json').write_text('{}', encoding='utf-8')
    db = seeded()
    outcome = dev_reset(db, root)
    assert outcome.status == 0, outcome.text
    assert sorted(path.name for path in revision_dir.iterdir()) == [*older[1:], STAMP]
    assert f'export: kept the newest 3; removed {older[0]}' in outcome.lines


def test_seed_on_dev_runs_the_preserving_reset_before_its_first_post(root):
    db, store = seeded(), store_for()
    outcome = run_tool(['seed', '--yes-reset'], http=store, connect=db.connect, seed_root=root)
    assert outcome.status == 0, outcome.text
    assert transcript(db)[: len(DEV_TRANSACTION)] == DEV_TRANSACTION
    assert_export_intact(root)
    assert len(store.posts()) == 14


def test_seed_on_test_runs_the_plain_wipe(root):
    db = seeded(stack='test')
    store = store_for(config=TEST_CONFIG)
    outcome = run_tool(['seed', '--yes-reset'], http=store, connect=db.connect, seed_root=root)
    assert outcome.status == 0, outcome.text
    assert not any(root.rglob('*'))
    assert db.loads == []
    assert TRUNCATE in db.sql()


def test_run_reset_without_a_label_is_the_plain_wipe(root):
    # The keyword's default is not dev: only a caller that passes the rail's DEV_LABEL gets the preserving reset.
    db = seeded()
    tool.run_reset(db.connect, tool.Reporter(lambda _: None), db.sleep, seed_root=root)
    assert db.loads == []
    assert not any(root.rglob('*'))
    assert db.counts == dict.fromkeys(EIGHTEEN_TABLES, 0)
