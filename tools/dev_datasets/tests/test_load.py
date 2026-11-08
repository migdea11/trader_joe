"""LOAD --seed DIR (validator, tj-7zjov1.297 P56v; built by tj-7zjov1.296 in request.run_load and dev_seed.load_seed).

THE DESIGN: decision tj-7zjov1.291 D1, D3 and D7. load puts a seed into a database whose eighteen tables are ALL EMPTY,
behind the same rail as every writing mode (the store's label dev or test, and the database's cluster_name), in ONE
transaction: lock timeout, LOCK the eighteen, the session settings, then, writing nothing until the last step: the seed's
revision = the database's alembic_version = the checkout head (else exit 3 naming the three, never a value); every
table's column fingerprint equal to the manifest's (else exit 3 naming the tables); the live schema's serial columns
exactly the one setval resets; every table empty (else exit 1 naming them); every file's size and sha256 (exit 1); then
the COPYs, parents first, the bytes the server reads hashed again; the setval; the recount against the manifest; COMMIT.
It never TRUNCATEs. --dry-run reads only the files: no rail, no database. DIR is a stamp directory, or a revision
directory for its newest COMPLETE stamp (resolve_seed); a symbolic link is refused.
THE STACK RULE (the 2026-11-04 owner ruling on tj-7zjov1.291, P58 tj-7zjov1.327, pinned here by P58v tj-7zjov1.328): the
manifest's stack must be 'dev' or 'test', and a load refuses (exit 3, naming both, before any lock) a seed whose stack
differs from the target's label; the dry run prints the seed's stack.

The seeds here are real files, written by a dev reset against doubles.FakeDb (so they are exactly what the tool writes)
under tmp_path; the target database is a second FakeDb with empty tables. Postgres itself is
tests/system/test_dev_seed_reset_pg.py.
"""

import json
import os
from pathlib import Path

import pytest

from tools.dev_datasets import dev_seed
from tools.dev_datasets import request as tool
from tools.dev_datasets.tests.doubles import (
    ALEMBIC_TEXT,
    COLUMNS_TEXT,
    DB_PASSWORD,
    EIGHTEEN_TABLES,
    GOOD_ENV,
    SEED_BAR_IDS,
    SEED_ROWS,
    SERIAL_TEXT,
    SESSION_TEXTS,
    SETVAL_TEXT,
    STACK_TEXT,
    TEST_CONFIG,
    ExplodingHttp,
    FakeDb,
    assert_no_canary,
    exploding_connect,
    run_tool,
    seeded_copy_data,
    store_for,
)
from tools.dev_datasets.tests.test_preserving_reset import LOAD_ORDER, STAMP, transcript
from tools.dev_datasets.tests.test_reset import COUNTS, LOCK_TIMEOUT, TABLE_LOCK, TRUNCATE, counts_block


pytestmark = pytest.mark.build_infra

EMPTY = dict.fromkeys(EIGHTEEN_TABLES, 0)
RAIL_DEV = "rail: deploymentLabel is 'dev' (real Alpaca backend)"


def head() -> str:
    return dev_seed.head_revision()


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    """A complete seed: the stamp directory a dev reset wrote, at the checkout head, with SEED_ROWS rows."""
    root = tmp_path / 'seeds'
    source = FakeDb(copy_data=seeded_copy_data())
    outcome = run_tool(['reset', '--yes-reset'], http=store_for(), connect=source.connect, seed_root=root)
    assert outcome.status == 0, outcome.text
    return root / head() / STAMP


@pytest.fixture
def test_seed(tmp_path: Path) -> Path:
    """A complete seed exported from the TEST stack (manifest stack 'test'), the same SEED_ROWS rows.

    The test label's reset is the plain wipe and keeps no seed, so this is the export mode against a test stack: the
    files and manifest exactly what the tool writes there.
    """
    root = tmp_path / 'test-seeds'
    source = FakeDb(copy_data=seeded_copy_data(), stack='test')
    outcome = run_tool(['export'], http=store_for(config=TEST_CONFIG), connect=source.connect, seed_root=root)
    assert outcome.status == 0, outcome.text
    [stamp] = list((root / head()).iterdir())
    return stamp


def empty_db(**kwargs) -> FakeDb:
    kwargs.setdefault('counts', dict(EMPTY))
    return FakeDb(**kwargs)


def load(db: FakeDb, seed: Path, *extra: str, store=None, environ=None):
    return run_tool(['load', '--seed', str(seed), *extra], environ, http=store or store_for(), connect=db.connect)


def rewrite_manifest(seed: Path, **changes) -> None:
    path = seed / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest.update(changes)
    path.write_text(json.dumps(manifest), encoding='utf-8')


def assert_wrote_nothing(db: FakeDb) -> None:
    assert db.loads == []
    assert TRUNCATE not in db.sql()
    assert SETVAL_TEXT not in db.sql()
    assert db.commits == 0
    assert db.sequence is None


LOAD_TRANSACTION = [
    'CONNECT',
    STACK_TEXT,
    LOCK_TIMEOUT,
    TABLE_LOCK,
    *SESSION_TEXTS,
    ALEMBIC_TEXT,
    *[COLUMNS_TEXT] * 18,
    SERIAL_TEXT,
    *COUNTS,
    *[f'COPY IN {table}' for table in LOAD_ORDER],
    SETVAL_TEXT,
    *COUNTS,
    'COMMIT',
    'CLOSE',
]


# ---- a load: one transaction, in the designed order ------------------------------------------------------------------


def test_a_load_is_exactly_the_designed_sequence_and_restores_the_seed(seed):
    db = empty_db()
    outcome = load(db, seed)
    assert outcome.status == 0, outcome.text
    assert transcript(db) == LOAD_TRANSACTION
    assert db.counts == SEED_ROWS
    assert db.sequence == (max(SEED_BAR_IDS), True)
    for table, data in db.loads:
        assert data == (seed / f'{table}.copy').read_bytes()
    assert (db.connects, db.commits, db.rollbacks, db.closes) == (1, 1, 0, 1)
    # It sleeps for no quiet-stack check: it removes nothing, and work in flight would show in its recount.
    assert outcome.sleeps == []


def test_a_load_prints_the_check_the_tables_and_the_counts_and_nothing_else(seed):
    outcome = load(empty_db(), seed)
    assert outcome.lines == [
        RAIL_DEV,
        f'load: revision {head()} matches the database and the checkout; files verified; loading',
        *(f'  {table}: loaded' for table in LOAD_ORDER),
        *counts_block('load: rows after', SEED_ROWS),
        f'load: committed from {seed}',
    ]
    assert outcome.stdout == outcome.stderr == ''
    assert_no_canary(outcome.text)


def test_a_load_never_truncates_and_never_reads_before_the_lock(seed):
    db = empty_db()
    assert load(db, seed).status == 0
    assert not any('TRUNCATE' in sql or 'DELETE' in sql.upper() for sql in db.sql())
    assert not any('TRANSACTION' in sql.upper() or 'ISOLATION' in sql.upper() for sql in db.sql())
    steps = transcript(db)
    assert steps[: steps.index(TABLE_LOCK)] == ['CONNECT', STACK_TEXT, LOCK_TIMEOUT]


# ---- the stack rule (tj-7zjov1.291 addendum 2026-11-04, owner ruling; built by tj-7zjov1.327 P58, pinned by .328 P58v) --
# A seed loads only into the stack it was exported from: manifest['stack'] must equal the label the rail resolved
# (deploymentLabel, then cluster_name). Unequal -> exit 3 naming both stacks, refused after the rail and the database
# proof and BEFORE the lock timeout, the lock or any read or write. The dev reset's in-transaction reload exports and
# reloads under one label and is unaffected (test_preserving_reset.py, unchanged).


def _target(label: str) -> tuple[FakeDb, object]:
    """An empty target of the label's stack: the store says the label and the server's cluster_name agrees."""
    return empty_db(stack=label), (store_for(config=TEST_CONFIG) if label == 'test' else store_for())


def _seed_of(label: str, dev: Path, test: Path) -> Path:
    return dev if label == 'dev' else test


def _cross_line(seed_stack: str, label: str) -> str:
    return (
        f"refused: this seed was exported from the '{seed_stack}' stack and the target is the '{label}' stack; "
        'nothing was written'
    )


@pytest.mark.parametrize('label', ['dev', 'test'])
def test_a_seed_loads_into_a_target_of_the_stack_it_was_exported_from(seed, test_seed, label):
    db, store = _target(label)
    source = _seed_of(label, seed, test_seed)
    assert _manifest(source)['stack'] == label
    outcome = load(db, source, store=store)
    assert outcome.status == 0, outcome.text
    assert outcome.lines[0] == (RAIL_DEV if label == 'dev' else "rail: deploymentLabel is 'test' (fake broker backend)")
    assert outcome.lines[-1] == f'load: committed from {source}'
    assert transcript(db) == LOAD_TRANSACTION
    assert db.counts == SEED_ROWS == {table: entry['rows'] for table, entry in _manifest(source)['tables'].items()}


@pytest.mark.parametrize(
    ('seed_stack', 'label'), [('dev', 'test'), ('test', 'dev')], ids=['dev-into-test', 'test-into-dev']
)
def test_a_seed_from_the_other_stack_refuses_with_exit_3_naming_both_before_any_lock(
    seed, test_seed, seed_stack, label
):
    db, store = _target(label)
    source = _seed_of(seed_stack, seed, test_seed)
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    outcome = load(db, source, store=store)
    assert outcome.status == 3 == tool.EXIT_REFUSED
    assert outcome.lines == [
        RAIL_DEV if label == 'dev' else "rail: deploymentLabel is 'test' (fake broker backend)",
        _cross_line(seed_stack, label),
    ]
    # After the database proof (cluster_name) and before the lock timeout, the lock or any read: nothing but the
    # proof ran, then the rollback and the close.
    assert transcript(db) == ['CONNECT', STACK_TEXT, 'ROLLBACK', 'CLOSE']
    assert LOCK_TIMEOUT not in db.sql() and TABLE_LOCK not in db.sql()
    assert_wrote_nothing(db)
    assert db.counts == EMPTY
    assert (db.connects, db.rollbacks, db.closes) == (1, 1, 1)
    assert {path.name: path.read_bytes() for path in source.iterdir()} == before  # the seed itself untouched


def test_the_check_reads_the_manifests_stack_and_not_where_the_files_came_from(seed):
    # The same dev-exported files, the manifest's stack edited: the manifest field is what is compared with the label.
    rewrite_manifest(seed, stack='test')
    db = empty_db()
    outcome = load(db, seed)
    assert outcome.status == 3
    assert outcome.lines[-1] == _cross_line('test', 'dev')
    assert_wrote_nothing(db)
    db, store = _target('test')
    assert load(db, seed, store=store).status == 0
    assert db.counts == SEED_ROWS


def test_the_readme_states_the_stack_rule_and_its_message():
    text = ' '.join((Path(tool.__file__).with_name('README.md')).read_text(encoding='utf-8').split())
    assert (
        "A load refuses, exits **3** and writes nothing when that differs from the target's `deploymentLabel`: "
        "`refused: this seed was exported from the 'A' stack and the target is the 'B' stack; nothing was written`"
    ) in text
    assert 'The check runs after the rail and the database proof and before any lock or write.' in text
    # The README's message is the tool's, with A and B for the stacks.
    assert _cross_line('A', 'B') in text


def test_a_revision_directory_loads_its_newest_complete_stamp(seed):
    revision_dir = seed.parent
    older = revision_dir / '20261001T000000Z'
    older.mkdir()
    (older / 'manifest.json').write_text('{}', encoding='utf-8')
    (revision_dir / '20261231T000000Z').mkdir()  # newer, but no manifest: an interrupted export, never a seed
    db = empty_db()
    outcome = load(db, revision_dir)
    assert outcome.status == 0, outcome.text
    assert outcome.lines[-1] == f'load: committed from {seed}'


# ---- D3: the stale-seed rule (exit 3, nothing written) -------------------------------------------------------------


def _stale_line(seed_revision: str, database_revision: str) -> str:
    return (
        f'refused: the seed is at revision {seed_revision}, the database at {database_revision} and the checkout head '
        f'is {head()}; a seed loads only when all three are the same. Migrate the dev database to the head, then reset '
        '(it exports a seed of the current revision). Nothing was changed'
    )


@pytest.mark.parametrize(
    ('seed_revision', 'database_revision'),
    [('aaaaaaaaaaaa', None), (None, 'bbbbbbbbbbbb'), ('cccccccccccc', 'cccccccccccc')],
    ids=['seed-stale', 'database-stale', 'both-equal-but-not-the-head'],
)
def test_a_load_refuses_unless_seed_database_and_checkout_revisions_all_match(seed, seed_revision, database_revision):
    if seed_revision is not None:
        rewrite_manifest(seed, revision=seed_revision)
    db = empty_db(revision=database_revision or head())
    outcome = load(db, seed)
    assert outcome.status == 3
    assert outcome.lines[-1] == _stale_line(seed_revision or head(), database_revision or head())
    assert_wrote_nothing(db)
    assert (db.rollbacks, db.closes) == (1, 1)
    # Refused at the revision read: no column, serial or count read after it.
    assert transcript(db)[-3:] == [ALEMBIC_TEXT, 'ROLLBACK', 'CLOSE']


def test_an_alembic_version_that_is_not_one_revision_refuses_the_load_with_exit_3(seed):
    db = empty_db(alembic_rows=[])
    outcome = load(db, seed)
    assert outcome.status == 3
    assert outcome.lines[-1] == 'alembic_version holds 0 row(s), not exactly one revision: migrate first'
    assert_wrote_nothing(db)


def test_a_column_layout_that_moved_refuses_with_exit_3_naming_the_tables(seed):
    moved = [('id', 'uuid', 'uuid'), ('added', 'text', 'text')]
    db = empty_db(columns={'panel': moved, 'corporate_action': [('id', 'text', 'text')]})
    outcome = load(db, seed)
    assert outcome.status == 3
    assert outcome.lines[-1] == (
        "refused: the columns of corporate_action, panel differ from the seed's (name, type or order); the schema moved "
        'without the revision. Nothing was changed'
    )
    assert_wrote_nothing(db)
    assert SERIAL_TEXT not in db.sql()
    assert COUNTS[0] not in db.sql()


def test_a_column_order_change_alone_is_a_layout_change(seed):
    # The fingerprint is ordinal: the same columns in another order refuse too.
    columns = [('id', 'uuid', 'uuid'), ('name', 'text', 'text')]
    first = empty_db(columns={'panel': columns})
    source = FakeDb(copy_data=seeded_copy_data(), columns={'panel': columns})
    root = seed.parent.parent.parent / 'reordered'
    assert run_tool(['reset', '--yes-reset'], http=store_for(), connect=source.connect, seed_root=root).status == 0
    reordered = root / head() / STAMP
    assert load(first, reordered).status == 0
    second = empty_db(columns={'panel': list(reversed(columns))})
    outcome = load(second, reordered)
    assert outcome.status == 3
    assert 'the columns of panel differ' in outcome.lines[-1]


@pytest.mark.parametrize(
    'found',
    [[], [('stock_market_activity', 'id'), ('panel_pass', 'seq')], [('panel', 'id')]],
    ids=['none', 'one-more', 'another'],
)
def test_a_schema_with_other_serial_columns_than_the_setval_resets_refuses_before_any_write(seed, found):
    db = empty_db(serial_columns=found)
    outcome = load(db, seed)
    assert outcome.status == 1
    difference = sorted(set(found) ^ {('stock_market_activity', 'id')})
    assert outcome.lines[-1] == (
        'refused: the schema has serial or identity columns this loader does not reset '
        f'({difference}); update SETVAL_SQL. Nothing was changed'
    )
    assert db.serial_queries == [{'tables': list(LOAD_ORDER)}]
    assert_wrote_nothing(db)


# ---- empty tables only, and every file verified before the first write ------------------------------------------------


@pytest.mark.parametrize(
    'populated',
    [{'panel': 6}, {'stock_market_activity': 1, 'panel_member_transition': 2}, {'ingest_instance': 1}],
    ids=['panel', 'two', 'a-wiped-table'],
)
def test_a_load_into_tables_that_hold_rows_refuses_with_exit_1_naming_them(seed, populated):
    db = empty_db(counts={**EMPTY, **populated})
    outcome = load(db, seed)
    assert outcome.status == 1
    detail = ', '.join(f'{table} {rows}' for table, rows in populated.items())
    assert outcome.lines[-1] == (
        f'refused: a load fills empty tables only and these hold rows ({detail}). Nothing was changed'
    )
    assert_wrote_nothing(db)
    assert db.counts == {**EMPTY, **populated}


def test_a_file_whose_size_differs_from_the_manifest_refuses_before_any_write(seed):
    path = seed / 'panel_pass.copy'
    size = path.stat().st_size
    with open(path, 'ab') as handle:
        handle.write(b'x\n')
    db = empty_db()
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1] == f'panel_pass.copy has {size + 2} bytes and the manifest says {size}: refused'
    assert_wrote_nothing(db)
    assert db.rollbacks == 1


def test_a_file_whose_bytes_differ_from_the_manifest_refuses_before_any_write(seed):
    path = seed / 'store_dataset_entry.copy'
    data = bytearray(path.read_bytes())
    data[-2] ^= 0x01
    path.write_bytes(bytes(data))
    db = empty_db()
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'store_dataset_entry.copy does not match the manifest sha256: refused'
    assert_wrote_nothing(db)


@pytest.mark.parametrize('how', ['missing', 'symlink'])
def test_a_file_that_is_missing_or_a_symbolic_link_refuses_before_any_write(seed, how):
    path = seed / 'corporate_action.copy'
    kept = seed.parent / 'kept.copy'
    path.rename(kept)
    if how == 'symlink':
        os.symlink(kept, path)
    db = empty_db()
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'corporate_action.copy cannot be opened (missing, or a symbolic link)'
    assert_wrote_nothing(db)


def test_a_file_changed_after_its_check_is_caught_during_the_copy_and_rolls_back(seed):
    def edit(table: str) -> None:
        if table == 'asset_list_event':
            path = seed / 'asset_list_event.copy'
            data = bytearray(path.read_bytes())
            data[0] ^= 0x01
            path.write_bytes(bytes(data))

    db = empty_db(before_load=edit)
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'asset_list_event.copy changed while it was loaded: refused'
    assert [table for table, _ in db.loads][-1] == 'asset_list_event'
    assert (db.commits, db.rollbacks) == (0, 1)
    assert db.counts == EMPTY
    assert db.sequence is None


def test_a_load_whose_recount_differs_from_the_manifest_rolls_back(seed):
    db = empty_db(load_shortfall={'panel': 1})
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the load did not restore the manifest counts (panel 5 expected 6); rolled back'
    assert (db.commits, db.rollbacks) == (0, 1)
    assert db.counts == EMPTY


@pytest.mark.parametrize('step', ['setval', 'copy'])
def test_a_driver_failure_during_the_load_rolls_back_and_names_the_type_only(seed, step):
    error = RuntimeError(f'password={DB_PASSWORD} DETAILTEXT')
    if step == 'setval':
        db = empty_db(execute_error=error, fail_on='setval')
    else:
        db = empty_db(load_error=error, load_error_on='panel_pass')
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1] == 'the load transaction failed and was rolled back (RuntimeError)'
    assert 'DETAILTEXT' not in outcome.text
    assert_no_canary(outcome.text)
    assert db.counts == EMPTY
    assert db.commits == 0


# ---- the rails, the credentials and the usage -------------------------------------------------------------------------


@pytest.mark.parametrize('label', ['', 'prod', 'Test'])
def test_the_rail_refuses_a_load_before_any_sql_or_file(tmp_path, label):
    db = empty_db()
    store = store_for(config=tool.Response(200, {'deploymentLabel': label}))
    # The seed path does not even exist: the rail is the first thing, before the seed is read.
    outcome = load(db, tmp_path / 'nowhere', store=store)
    assert outcome.status == 1
    assert 'refused: this is not a dev or test stack' in outcome.lines[-1]
    assert db.events == []


def test_the_database_rail_refuses_a_wrong_database_before_the_lock(seed):
    db = empty_db(stack='test')  # the store says dev, the server's cluster_name says test
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1].startswith('refused: the database is not this dev or test stack')
    assert db.sql() == [STACK_TEXT]
    assert_wrote_nothing(db)


def test_a_load_needs_the_database_password_and_not_the_instance_secret(seed):
    http = ExplodingHttp()
    outcome = run_tool(
        ['load', '--seed', str(seed)], {**GOOD_ENV, 'POSTGRES_PASS': ''}, http=http, connect=exploding_connect
    )
    assert outcome.status == 1
    assert outcome.lines == ['POSTGRES_PASS is empty']
    assert http.calls == []
    db = empty_db()
    outcome = load(db, seed, environ={**GOOD_ENV, 'INSTANCE_WRITE_SECRET': ''})
    assert outcome.status == 0, outcome.text


@pytest.mark.parametrize(
    ('argv', 'message'),
    [
        (['load'], 'load needs --seed DIR (a stamp directory, or a revision directory for its newest complete stamp)'),
        (
            ['--load'],
            'load needs --seed DIR (a stamp directory, or a revision directory for its newest complete stamp)',
        ),
        (['reset', '--seed', 'x'], '--seed only means something for load'),
        (['export', '--seed', 'x'], '--seed only means something for load'),
        (['load', '--seed', 'x', '--row', 'r1'], '--row does not limit a load'),
        (['load', '--seed', 'x', '--yes-reset'], '--yes-reset only means something for seed and reset'),
        (['load', '--seed', 'x', '--no-reset'], '--no-reset only means something for seed'),
    ],
)
def test_load_usage_errors_exit_2_before_anything(argv, message):
    http = ExplodingHttp()
    outcome = run_tool(argv, http=http, connect=exploding_connect)
    assert outcome.status == 2
    assert outcome.lines == [message]
    assert http.calls == []


# ---- --dry-run: the files only ----------------------------------------------------------------------------------------


def test_a_dry_run_verifies_the_files_and_prints_the_counts_with_no_rail_and_no_database(seed):
    http = ExplodingHttp()
    outcome = run_tool(['load', '--seed', str(seed), '--dry-run'], http=http, connect=exploding_connect)
    assert outcome.status == 0, outcome.text
    assert outcome.lines == [
        f'load dry run: nothing is written; seed {STAMP} files verified',
        f'  seed revision {head()}, checkout head {head()}',
        '  seed stack dev',
        *(f'  {table}: {SEED_ROWS[table]}' for table in EIGHTEEN_TABLES),
    ]
    assert http.calls == []


def test_a_dry_run_prints_the_seeds_own_stack_and_cannot_say_whether_a_load_would_be_refused(test_seed):
    # No rail in a dry run, so it names the seed's stack and passes no judgement on a target it never reads.
    http = ExplodingHttp()
    outcome = run_tool(['load', '--seed', str(test_seed), '--dry-run'], http=http, connect=exploding_connect)
    assert outcome.status == 0, outcome.text
    assert outcome.lines[:3] == [
        f'load dry run: nothing is written; seed {test_seed.name} files verified',
        f'  seed revision {head()}, checkout head {head()}',
        '  seed stack test',
    ]
    assert not any('refused' in line for line in outcome.lines)
    assert http.calls == []


def test_a_dry_run_of_a_manifest_with_no_known_stack_refuses_with_exit_1(seed):
    rewrite_manifest(seed, stack='prod')
    outcome = run_tool(['load', '--seed', str(seed), '--dry-run'], http=ExplodingHttp(), connect=exploding_connect)
    assert outcome.status == 1
    assert outcome.lines == ["manifest.json has no stack, or one that is not 'dev' or 'test'"]


def test_a_dry_run_needs_no_credential(seed):
    environ = {**GOOD_ENV, 'POSTGRES_PASS': '', 'INSTANCE_WRITE_SECRET': ''}
    outcome = run_tool(
        ['load', '--seed', str(seed), '--dry-run'], environ, http=ExplodingHttp(), connect=exploding_connect
    )
    assert outcome.status == 0, outcome.text


def test_a_dry_run_of_a_stale_seed_says_a_load_would_be_refused(seed):
    rewrite_manifest(seed, revision='aaaaaaaaaaaa')
    outcome = run_tool(['load', '--seed', str(seed), '--dry-run'], http=ExplodingHttp(), connect=exploding_connect)
    assert outcome.status == 0
    assert outcome.lines[1] == f'  seed revision aaaaaaaaaaaa, checkout head {head()}'
    assert outcome.lines[-1] == (
        'this seed is stale: a load would be refused (exit 3). Migrate, then reset to export a fresh one'
    )


def test_a_dry_run_of_a_tampered_seed_refuses_with_exit_1(seed):
    path = seed / 'panel.copy'
    data = bytearray(path.read_bytes())
    data[0] ^= 0x01
    path.write_bytes(bytes(data))
    outcome = run_tool(['load', '--seed', str(seed), '--dry-run'], http=ExplodingHttp(), connect=exploding_connect)
    assert outcome.status == 1
    assert outcome.lines == ['panel.copy does not match the manifest sha256: refused']


# ---- resolve_seed and read_manifest -----------------------------------------------------------------------------------


def _stamp(parent: Path, name: str, *, complete: bool = True) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    if complete:
        (path / 'manifest.json').write_text('{}', encoding='utf-8')
    return path


def test_resolve_seed_takes_a_stamp_itself_and_a_revision_directory_by_its_newest_complete_stamp(tmp_path):
    revision = tmp_path / 'rev'
    old = _stamp(revision, '20260101T000000Z')
    new = _stamp(revision, '20260301T000000Z')
    _stamp(revision, '20260401T000000Z', complete=False)  # newer, incomplete: skipped
    _stamp(revision, 'not-a-stamp')  # complete, but not named like one: skipped
    os.symlink(old, revision / '20260501T000000Z')  # a link named like a stamp: skipped
    assert dev_seed.resolve_seed(revision) == new
    assert dev_seed.resolve_seed(old) == old


def test_resolve_seed_refuses_a_link_a_file_a_missing_path_and_a_directory_with_no_complete_stamp(tmp_path):
    revision = tmp_path / 'rev'
    _stamp(revision, '20260101T000000Z', complete=False)
    link = tmp_path / 'link'
    os.symlink(revision, link)
    file = tmp_path / 'file'
    file.write_text('x', encoding='utf-8')
    cases = {
        link: f'refused: {link} is not a plain directory (a symbolic link or a file)',
        file: f'refused: {file} is not a plain directory (a symbolic link or a file)',
        tmp_path / 'missing': f'refused: {tmp_path / "missing"} does not exist',
        revision: f'refused: {revision} holds no complete seed (no manifest.json in it or in a stamp under it)',
    }
    for path, message in cases.items():
        with pytest.raises(dev_seed.DevSeedError) as caught:
            dev_seed.resolve_seed(path)
        assert str(caught.value) == message
        assert type(caught.value) is dev_seed.DevSeedError  # not a revision refusal: exit 1, not 3


def test_a_load_of_a_seed_path_that_cannot_be_resolved_exits_1_and_opens_no_transaction(tmp_path):
    db = empty_db()
    outcome = load(db, tmp_path / 'missing')
    assert outcome.status == 1
    assert outcome.lines[-1] == f'refused: {tmp_path / "missing"} does not exist'
    assert db.events == []


def _manifest(seed: Path) -> dict:
    return json.loads((seed / 'manifest.json').read_text(encoding='utf-8'))


def test_read_manifest_returns_what_the_export_wrote(seed):
    assert dev_seed.read_manifest(seed) == _manifest(seed)


def test_read_manifest_accepts_a_dev_and_a_test_stack(seed, test_seed):
    assert dev_seed.SEED_STACKS == ('dev', 'test')
    assert dev_seed.read_manifest(seed)['stack'] == 'dev'
    assert dev_seed.read_manifest(test_seed)['stack'] == 'test'


NO_STACK = "manifest.json has no stack, or one that is not 'dev' or 'test'"


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda m: 'not json', 'manifest.json is not valid JSON'),
        (lambda m: [], 'manifest.json is not a format 1 seed manifest'),
        (lambda m: {**m, 'format': 2}, 'manifest.json is not a format 1 seed manifest'),
        (lambda m: {**m, 'revision': ''}, 'manifest.json has no revision'),
        (lambda m: {k: v for k, v in m.items() if k != 'revision'}, 'manifest.json has no revision'),
        (lambda m: {k: v for k, v in m.items() if k != 'stack'}, NO_STACK),
        (lambda m: {**m, 'stack': None}, NO_STACK),
        (lambda m: {**m, 'stack': ''}, NO_STACK),
        (lambda m: {**m, 'stack': 'prod'}, NO_STACK),
        (lambda m: {**m, 'stack': 'Dev'}, NO_STACK),
        (lambda m: {**m, 'stack': ['dev']}, NO_STACK),
        (
            lambda m: {**m, 'tables': {k: v for k, v in m['tables'].items() if k != 'panel'}},
            'manifest.json does not list exactly the 18 seed tables',
        ),
        (
            lambda m: {**m, 'tables': {**m['tables'], 'extra': m['tables']['panel']}},
            'manifest.json does not list exactly the 18 seed tables',
        ),
        (
            lambda m: {**m, 'tables': {**m['tables'], 'panel': {**m['tables']['panel'], 'rows': -1}}},
            'manifest.json: the entry for panel is malformed',
        ),
        (
            lambda m: {**m, 'tables': {**m['tables'], 'panel': {**m['tables']['panel'], 'bytes': '6'}}},
            'manifest.json: the entry for panel is malformed',
        ),
        (
            lambda m: {**m, 'tables': {**m['tables'], 'panel': {**m['tables']['panel'], 'sha256': 'ABC'}}},
            'manifest.json: the entry for panel is malformed',
        ),
        (
            lambda m: {**m, 'tables': {**m['tables'], 'panel': {**m['tables']['panel'], 'fingerprint': None}}},
            'manifest.json: the entry for panel is malformed',
        ),
    ],
    ids=[
        'not-json',
        'a-list',
        'format-2',
        'blank-revision',
        'no-revision',
        'no-stack',
        'null-stack',
        'blank-stack',
        'prod-stack',
        'capitalised-stack',
        'list-stack',
        'a-table-missing',
        'a-table-extra',
        'negative-rows',
        'text-bytes',
        'bad-sha',
        'no-fingerprint',
    ],
)
def test_read_manifest_refuses_a_manifest_a_load_cannot_trust(seed, change, message):
    changed = change(_manifest(seed))
    text = changed if isinstance(changed, str) else json.dumps(changed)
    (seed / 'manifest.json').write_text(text, encoding='utf-8')
    with pytest.raises(dev_seed.DevSeedError) as caught:
        dev_seed.read_manifest(seed)
    assert str(caught.value) == message
    db = empty_db()
    outcome = load(db, seed)
    assert outcome.status == 1
    assert outcome.lines[-1] == message
    assert db.events == []


def test_read_manifest_refuses_one_over_the_size_limit_and_a_symbolic_link(seed, tmp_path):
    path = seed / 'manifest.json'
    path.write_bytes(b' ' * (dev_seed.MANIFEST_LIMIT + 1))
    with pytest.raises(dev_seed.DevSeedError, match='larger than 1048576 bytes'):
        dev_seed.read_manifest(seed)
    real = tmp_path / 'real.json'
    path.rename(real)
    os.symlink(real, path)
    with pytest.raises(dev_seed.DevSeedError) as caught:
        dev_seed.read_manifest(seed)
    assert str(caught.value) == 'manifest.json cannot be opened (missing, or a symbolic link)'
