"""The dev seed EXPORT (validator, tj-7zjov1.295; built by tj-7zjov1.294 in tools/dev_datasets/dev_seed.py).

THE DESIGN: decision tj-7zjov1.291 D2, D3, D5 and D7, and the owner's rulings on tj-7zjov1.292 (2026-11-03: O1 to O11 as
recommended, except O2: corporate_action is trimmed too, kept for DOUBLE the 90-day cutoff, 180 days, by as_of).

  O11  one cutoff, 90 days before the export instant, UTC. corporate_action uses twice it, derived, not a second number.
  O4   entries open-ended or ending after the cutoff and not expired, start clipped up to the cutoff's session boundary;
       bars at or after it.
  O1   ingest requests and runs of the last 90 days plus each kept dataset's latest accepted run (the stop rule's base);
       ingest_instance wiped. AS AMENDED (owner ruling on tj-7zjov1.318, option b; decision tj-7zjov1.291 addendum
       2026-11-04 01:40 UTC; built by tj-7zjov1.333, pinned here by tj-7zjov1.334): a request is kept only when it is
       ALSO in a terminal state, written as a POSITIVE list (SUCCEEDED, EMPTY, FAILED; PENDING, QUEUED, NOT_DELIVERED
       and any later state are left out). An ingest_run row and a panel_member_transition row that name an OPEN
       request are DROPPED, not nulled, whatever else kept them (the latest-accepted closure and the ledger rules
       included); panel_pass follows the transitions that stay. R2 dangles of requests trimmed by AGE are unchanged.
  O3   dataset_read, dataset_read_daily, dataset_override_log, dataset_deletion wiped.
  O6   asset-list snapshots of the window plus the diff base before it, chosen as the store's newest_snapshot chooses
       it (architect RE on tj-7zjov1.294, F2): per (source, asset_list), NON-EMPTY (row_count > 0), newest by as_of,
       received_at, id; asset_type is not a key. Their rows and events follow.
  O5   passes started in the window; transitions in the window plus, for each member before the cutoff judged as the
       store judges it (architect RE on tj-7zjov1.294, F1: members_at and latest_state, NON-DRY-RUN passes only, newest
       by as_of, pass started_at, pass_id, seq, no recorded_at), the ENTERED row that makes it a member (its newest
       ENTERED/EXITED/DRAINED row is ENTERED) AND its newest row of any kind; pre-cutoff dry-run rows are not kept; the
       passes those reference (foreign-key closure).
  D3   one REPEATABLE READ READ ONLY transaction; manifest.revision = alembic_version = the checkout head, else refused
       before any file is written, and that refusal (or an alembic_version that is not exactly one row) exits EXACTLY 3
       through the CLI (F3), every other failure 1; per table counts, sha256 and a column fingerprint.
  D5   output/dev_seed/<revision>/<stamp>/, 0700 / 0600, no-follow, never in a build context (.dockerignore). Every
       directory level is refused if it is a symbolic link or not a directory, and each level it creates is 0700 (F4,
       tj-7zjov1.305): inside the repository output/, output/dev_seed and the revision directory; for a seed root
       outside it only the root's own name and the revision directory. The keep-three prune counts only COMPLETE stamps
       (manifest.json a regular file), removes incomplete stamps older than the newest complete one and leaves a newer
       incomplete one alone (F5).
  D7   the rail: refused before any SQL unless the store says dev or test, and the database's cluster_name agrees.
  P55  export needs the database password, not the instance secret, and takes no --yes-reset; --dry-run prints per-table
       counts and size estimates and writes nothing; the newest three stamps of a revision are kept.

WHAT THIS FILE CANNOT PROVE. There is no Postgres here. The trim statements are pinned as TEXT: their predicates,
boundaries, literals and the columns they name (checked against the store's real models in a child interpreter), and
the F1/F2 mirrors are also compared with the store's OWN statements (latest_state, members_at and newest_snapshot,
captured and compiled for Postgres in that child), so the order, the dry-run filter and the non-empty filter cannot
drift from the readers they copy. Three statements (ingest_request, panel_member_transition, panel_pass) are ALSO run
as written on an in-memory SQLite database (they use nothing Postgres-only), so the terminal rule, the age rule, the
drop-not-null of rows naming an open request and the pass closure are shown on rows, not only as text; SQLite is not
Postgres, so that is evidence about the boolean structure, not about the plan. That they parse, plan and select the
right rows on a real database is NOT shown by anything in this file; the Postgres round trip is tests/system/test_dev_seed_export_pg.py (tj-7zjov1.295), and the first
real export is tj-7zjov1.297 (P57) with the owner. The transaction, the manifest, the files and the CLI are exercised
end to end against doubles.FakeDb, and the directory and prune rules on the real file system under tmp_path.
"""

import ast
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    NOW,
    SECRET,
    STACK_TEXT,
    ExplodingHttp,
    FakeDb,
    FakeStore,
    assert_no_canary,
    exploding_connect,
    run_tool,
)
from tools.dev_datasets.tests.test_reset import STORE_FOREIGN_KEYS
from tools.dev_datasets.tests.test_store_contract import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.build_infra

# ---- the design's numbers, written out ------------------------------------------------------------------------------

# NOW is Tuesday 2026-10-20 15:00 UTC. 90 days earlier is 2026-07-22 15:00 UTC; the first New York midnight at or after
# it is 2026-07-23 00:00 EDT, 04:00 UTC; 180 days earlier is 2026-04-23 15:00 UTC.
CUTOFF = datetime(2026, 7, 22, 15, 0, tzinfo=UTC)
SESSION_CUTOFF = datetime(2026, 7, 23, 4, 0, tzinfo=UTC)
CORPORATE_CUTOFF = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)
STAMP = '20261020T150000Z'
WIPED = ('ingest_instance', 'dataset_read', 'dataset_read_daily', 'dataset_override_log', 'dataset_deletion')
WHOLE = ('panel', 'panel_settings_version', 'panel_source_settings_version')
READ_ONLY_TEXT = 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'
COPY_TEXT = re.compile(r'^COPY \((?P<select>SELECT .*)\) TO STDOUT$', re.DOTALL)


def collapse(sql: str) -> str:
    return ' '.join(sql.split())


def trim(table: str) -> str:
    return collapse(dev_seed.TRIM_SQL[table])


# ---- the store's own facts, read in a child interpreter (the store lives under server/; see test_store_contract) ----

STORE_CHILD = r"""
import asyncio, importlib, json, pkgutil, sys, uuid
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy.dialects import postgresql
import data.store.app.database.models as models
for info in pkgutil.iter_modules(models.__path__):
    importlib.import_module(f'{models.__name__}.{info.name}')
from common.database.sql_alchemy_table import AppBase
from common.enums.data_stock import DataSource
from schemas.data_ingest.submit import AssetListStatus
from data.store.seeds.manifest import head_revision
from data.store.app.database.models.ingest_run import IngestRunState
from data.store.app.database.models.ingest_request import IngestRequestState, OPEN_STATES
from data.store.app.database.models.panel import PanelTransition
from data.store.app.database.crud import asset_list as asset_list_crud, panel_ledger
from data.store.app import panel_manager
metadata = AppBase.DATA_STORE_BASE.metadata

# The store's own readers, run against a session that only records what it is asked to execute: the statement each one
# builds, compiled for Postgres, is what the export's mirrors are compared with.
class Result:
    def all(self):
        return []
    def one_or_none(self):
        return None
class Recorder:
    def __init__(self):
        self.statements = []
    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return Result()
def compiled(statement):
    done = statement.compile(dialect=postgresql.dialect())
    params = {k: v if isinstance(v, (bool, int, type(None))) else str(v) for k, v in done.params.items()}
    return {'sql': ' '.join(str(done).split()), 'params': params}
async def capture():
    calls = {
        'latest_state': lambda db: panel_ledger.latest_state(db, uuid.uuid4()),
        'members_at': lambda db: panel_ledger.members_at(db, uuid.uuid4(), datetime(2026, 1, 1, tzinfo=UTC)),
        'newest_snapshot': lambda db: asset_list_crud.newest_snapshot(db, DataSource.ALPACA_API, AssetListStatus.ACTIVE),
    }
    out = {}
    for name, call in calls.items():
        db = Recorder()
        await call(db)
        [statement] = db.statements
        out[name] = compiled(statement)
    return out
print(json.dumps({
    'head': head_revision(Path(sys.argv[1])),
    'columns': {name: [c.name for c in table.columns] for name, table in metadata.tables.items()},
    'accepted': sorted(s.value for s in (IngestRunState.SUCCEEDED, IngestRunState.EMPTY)),
    'not_member': sorted(t.value for t in panel_manager._NOT_MEMBER),
    'membership': sorted(t.value for t in panel_ledger.MEMBERSHIP_TRANSITIONS),
    'transitions': sorted(t.value for t in PanelTransition),
    'request_states': sorted(s.value for s in IngestRequestState),
    'open_states': sorted(s.value for s in OPEN_STATES),
    'statements': asyncio.run(capture()),
}))
"""


@pytest.fixture(scope='module')
def store_facts() -> dict:
    # panel_manager imports the generated protobuf modules, so the child needs gen/proto/python as pytest.ini gives it.
    paths = [str(SERVER_ROOT), str(REPO_ROOT), str(REPO_ROOT / 'gen' / 'proto' / 'python')]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(paths)}
    versions = SERVER_ROOT / 'data' / 'store' / 'migrations' / 'versions'
    done = subprocess.run(  # nosec B603 -- this interpreter, a fixed script, no shell
        [sys.executable, '-c', STORE_CHILD, str(versions)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


# ---- the cutoff (O11, O2 ruling) --------------------------------------------------------------------------------------


def test_the_cutoff_is_ninety_days_and_corporate_actions_keep_exactly_twice_it():
    assert dev_seed.CUTOFF_DAYS == 90
    assert dev_seed.CORPORATE_ACTION_DAYS == 180
    assert dev_seed.CORPORATE_ACTION_DAYS == 2 * dev_seed.CUTOFF_DAYS


def test_the_corporate_action_horizon_is_derived_from_the_cutoff_not_a_second_literal():
    # The ruling is "double the cutoff": changing CUTOFF_DAYS must move it. Read from the source, because the value is
    # bound at import and an equal literal (180) would pass the arithmetic test above.
    tree = ast.parse(Path(dev_seed.__file__).read_text(encoding='utf-8'))
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == 'CORPORATE_ACTION_DAYS'
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    assert len(assigned) == 1
    value = assigned[0]
    assert isinstance(value, ast.BinOp) and isinstance(value.op, ast.Mult), ast.dump(value)
    operands = {type(value.left), type(value.right)}
    assert operands == {ast.Constant, ast.Name}, ast.dump(value)
    constant = value.left if isinstance(value.left, ast.Constant) else value.right
    name = value.right if isinstance(value.right, ast.Name) else value.left
    assert constant.value == 2 and name.id == 'CUTOFF_DAYS'


def test_the_cutoffs_of_one_export_all_come_from_its_instant():
    cutoffs = dev_seed.cutoffs_for(NOW)
    assert cutoffs.now == NOW
    assert cutoffs.cutoff == CUTOFF
    assert cutoffs.session_cutoff == SESSION_CUTOFF
    assert cutoffs.corporate_cutoff == CORPORATE_CUTOFF
    assert cutoffs.parameters() == {
        'now': NOW,
        'cutoff': CUTOFF,
        'session_cutoff': SESSION_CUTOFF,
        'corporate_cutoff': CORPORATE_CUTOFF,
    }


def test_a_naive_instant_is_refused():
    with pytest.raises(dev_seed.DevSeedError):
        dev_seed.cutoffs_for(datetime(2026, 10, 20, 15, 0))


@pytest.mark.parametrize(
    ('instant', 'expected'),
    [
        # Already a New York midnight (EDT): itself.
        (datetime(2026, 7, 23, 4, 0, tzinfo=UTC), datetime(2026, 7, 23, 4, 0, tzinfo=UTC)),
        # One second after: the next one, never a part-session.
        (datetime(2026, 7, 23, 4, 0, 1, tzinfo=UTC), datetime(2026, 7, 24, 4, 0, tzinfo=UTC)),
        # UTC midnight is the previous New York evening: that same New York date's next midnight.
        (datetime(2026, 1, 15, 0, 0, tzinfo=UTC), datetime(2026, 1, 15, 5, 0, tzinfo=UTC)),
        # Across the autumn change (2026-11-01): EDT evening to an EST midnight.
        (datetime(2026, 11, 1, 2, 0, tzinfo=UTC), datetime(2026, 11, 1, 4, 0, tzinfo=UTC)),
        (datetime(2026, 11, 1, 4, 30, tzinfo=UTC), datetime(2026, 11, 2, 5, 0, tzinfo=UTC)),
    ],
)
def test_the_session_boundary_is_the_first_new_york_midnight_at_or_after(instant, expected):
    assert dev_seed.session_boundary(instant) == expected


# ---- the eighteen and the revision head ------------------------------------------------------------------------------


def test_the_seed_tables_are_exactly_the_eighteen_the_reset_empties():
    assert len(dev_seed.SEED_TABLES) == len(set(dev_seed.SEED_TABLES)) == 18
    assert set(dev_seed.SEED_TABLES) == set(tool.RESET_TABLES) == set(EIGHTEEN_TABLES)
    assert 'alembic_version' not in dev_seed.SEED_TABLES
    # One trim statement and one estimate per table, no more.
    assert set(dev_seed.TRIM_SQL) == set(dev_seed.SEED_TABLES)
    assert set(dev_seed.ESTIMATE_SQL) == set(dev_seed.SEED_TABLES)


def test_the_load_order_puts_every_parent_before_its_child():
    position = {table: index for index, table in enumerate(dev_seed.SEED_TABLES)}
    for child, parent, _ in STORE_FOREIGN_KEYS:
        assert position[parent] < position[child], (child, parent)


def test_the_head_revision_mirror_answers_what_the_store_seed_manifest_answers(store_facts):
    assert dev_seed.VERSIONS_DIR == SERVER_ROOT / 'data' / 'store' / 'migrations' / 'versions'
    assert dev_seed.head_revision() == store_facts['head']


def _revision_file(directory: Path, revision: str, down) -> None:
    (directory / f'{revision}_x.py').write_text(
        f'revision: str = {revision!r}\ndown_revision = {down!r}\n', encoding='utf-8'
    )


def test_the_head_revision_refuses_two_heads_and_none(tmp_path):
    with pytest.raises(dev_seed.DevSeedError, match='found 0'):
        dev_seed.head_revision(tmp_path)
    _revision_file(tmp_path, 'aaa', None)
    _revision_file(tmp_path, 'bbb', 'aaa')
    assert dev_seed.head_revision(tmp_path) == 'bbb'
    _revision_file(tmp_path, 'ccc', 'aaa')
    with pytest.raises(dev_seed.DevSeedError, match='found 2'):
        dev_seed.head_revision(tmp_path)
    _revision_file(tmp_path, 'ddd', ('bbb', 'ccc'))
    assert dev_seed.head_revision(tmp_path) == 'ddd'


# ---- the trim statements, as text (see the module docstring: no Postgres proves them here) ---------------------------


@pytest.mark.parametrize('table', EIGHTEEN_TABLES)
def test_each_trim_statement_is_one_plain_select_of_its_own_table(table):
    text = trim(table)
    assert text.startswith('SELECT ')
    assert re.search(rf'\bFROM {table} [a-z]\b', text), text
    assert ';' not in text
    assert re.search(r'ORDER BY [a-z]\.(id|instance_id)$', text), text
    for word in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'LOCK', 'FOR SHARE', 'ALTER', 'DROP', 'COPY'):
        assert word not in text.upper().replace('JSONB_POPULATE_RECORD', ''), (table, word)


def test_the_only_placeholders_are_the_four_cutoffs():
    named = set()
    for sql in dev_seed.TRIM_SQL.values():
        named |= set(re.findall(r'%\((\w+)\)s', sql))
        # Nothing else may carry a percent sign: mogrify would read it as a placeholder.
        assert sql.count('%') == len(re.findall(r'%\(\w+\)s', sql)), sql
    assert named == set(dev_seed.cutoffs_for(NOW).parameters())


def test_the_estimate_counts_and_sizes_exactly_the_rows_the_trim_keeps():
    for table, sql in dev_seed.TRIM_SQL.items():
        assert dev_seed.ESTIMATE_SQL[table] == f'SELECT count(*), coalesce(sum(pg_column_size(k)), 0) FROM ({sql}) k'


@pytest.mark.parametrize('table', WIPED)
def test_the_instance_usage_and_audit_tables_are_exported_empty(table):
    # O1 (ingest_instance: the live ingest re-registers) and O3 (usage and audit history, wiped as ruled).
    assert re.fullmatch(
        rf'SELECT [a-z]\.\* FROM {table} [a-z] WHERE false ORDER BY [a-z]\.(id|instance_id)', trim(table)
    )


@pytest.mark.parametrize('table', WHOLE)
def test_the_panel_and_settings_tables_are_exported_whole(table):
    # The builder's judgement (dev_seed docstring), required by the foreign keys: a kept pass names its versions.
    assert re.fullmatch(rf'SELECT [a-z]\.\* FROM {table} [a-z] ORDER BY [a-z]\.id', trim(table))


def test_corporate_actions_are_kept_by_as_of_from_twice_the_cutoff_inclusive():
    assert trim('corporate_action') == (
        'SELECT c.* FROM corporate_action c WHERE c.as_of >= %(corporate_cutoff)s ORDER BY c.id'
    )


# The amended O1 (tj-7zjov1.291 addendum 2026-11-04), written out from the design: the three terminal labels as a
# POSITIVE list, and the three open ones that a referring row must not name.
TERMINAL_STATES = ('SUCCEEDED', 'EMPTY', 'FAILED')
OPEN_REQUEST_STATES = ('PENDING', 'QUEUED', 'NOT_DELIVERED')
TERMINAL = "('SUCCEEDED', 'EMPTY', 'FAILED')"


def names_open(alias: str) -> str:
    """The design's drop: the row's request_id names a request (alias o) that is in an open state."""
    return (
        f'EXISTS (SELECT 1 FROM ingest_request o WHERE o.id = {alias}.request_id '
        "AND o.state IN ('PENDING', 'QUEUED', 'NOT_DELIVERED'))"
    )


def _state_list(text: str, prefix: str) -> list[str]:
    match = re.search(re.escape(prefix) + r" IN \(('[A-Z_]+'(?:, '[A-Z_]+')*)\)", text)
    assert match, (prefix, text)
    return re.findall(r"'([A-Z_]+)'", match.group(1))


def test_ingest_requests_are_kept_from_the_cutoff_inclusive_and_only_in_a_terminal_state():
    # Both conditions, joined by AND: a terminal request created before the cutoff is still trimmed by age.
    assert trim('ingest_request') == (
        f'SELECT r.* FROM ingest_request r WHERE r.created_at >= %(cutoff)s AND r.state IN {TERMINAL} ORDER BY r.id'
    )


def test_the_kept_request_states_are_exactly_the_stores_terminal_ones_written_as_a_positive_list(store_facts):
    # The store's own enum and OPEN_STATES (server/data/store/app/database/models/ingest_request.py), read in the child:
    # the terminal set is every state the store has that is not open, and the trim names exactly it.
    states, open_states = set(store_facts['request_states']), set(store_facts['open_states'])
    assert open_states == set(OPEN_REQUEST_STATES)
    assert states - open_states == set(TERMINAL_STATES)
    text = trim('ingest_request')
    assert set(_state_list(text, 'r.state')) == set(TERMINAL_STATES)
    # A positive list: no NOT IN, no negated open list, and no open label anywhere in the statement, so a state added
    # to the enum later is left out of the seed until someone decides otherwise.
    assert 'NOT ' not in text
    assert '<>' not in text and '!=' not in text
    for label in OPEN_REQUEST_STATES:
        assert label not in text, label
    assert dev_seed._TERMINAL_STATES == TERMINAL


@pytest.mark.parametrize(
    ('table', 'alias'), [('ingest_run', 'r'), ('panel_member_transition', 't'), ('panel_pass', 't')]
)
def test_every_drop_names_exactly_the_stores_open_states(store_facts, table, alias):
    # The drop is the store's OPEN_STATES, all three, and it is negated as a whole (AND NOT EXISTS).
    text = trim(table)
    assert text.count(f'AND NOT {names_open(alias)}') == 1, text
    assert set(_state_list(text, 'o.state')) == set(store_facts['open_states'])
    # The open request is joined by its id to the referring row's request_id, never another column.
    assert re.findall(r'o\.id = ([a-z])\.([a-z_]+)', text) == [(alias, 'request_id')]


KEPT_ENTRY = (
    """(e."end" = '1970-01-01 00:00:00+00'::timestamptz OR e."end" > %(session_cutoff)s) """
    'AND (e.expiry IS NULL OR e.expiry > %(now)s)'
)


def test_an_entry_is_kept_when_open_ended_or_ending_after_the_cutoff_and_not_expired_and_its_start_is_clipped():
    # O4. The open-ended end is stored as the epoch (common.database.sql_alchemy_nullable_datetime).
    assert trim('store_dataset_entry') == (
        """SELECT (jsonb_populate_record(e, jsonb_build_object('start', GREATEST(e."start", %(session_cutoff)s)))).* """
        f'FROM store_dataset_entry e WHERE {KEPT_ENTRY} ORDER BY e.id'
    )


def test_bars_are_those_of_a_kept_entry_from_the_session_cutoff_inclusive():
    assert trim('stock_market_activity') == (
        'SELECT m.* FROM stock_market_activity m WHERE m."timestamp" >= %(session_cutoff)s '
        f'AND m.dataset_id IN (SELECT e.id FROM store_dataset_entry e WHERE {KEPT_ENTRY}) ORDER BY m.id'
    )


def test_runs_are_the_window_plus_each_kept_datasets_latest_accepted_run(store_facts):
    # O1. Accepted, as the store's last_accepted_vendor_asset_id reads it (crud/ingest_run.py): SUCCEEDED or EMPTY,
    # a vendor asset id and a granularity, newest by finished_at (NULLS LAST) then id.
    # Amended (tj-7zjov1.333): then every row naming an OPEN request is dropped, the latest-accepted closure included,
    # so the window and the closure are parenthesised and the drop is the outer AND.
    assert store_facts['accepted'] == ['EMPTY', 'SUCCEEDED']
    assert trim('ingest_run') == (
        'SELECT r.* FROM ingest_run r WHERE (r.started_at >= %(cutoff)s OR r.id IN ('
        'SELECT DISTINCT ON (a.dataset_id) a.id FROM ingest_run a '
        f'WHERE a.dataset_id IN (SELECT e.id FROM store_dataset_entry e WHERE {KEPT_ENTRY}) '
        'AND a.vendor_asset_id IS NOT NULL AND a.granularity IS NOT NULL '
        "AND a.state IN ('SUCCEEDED', 'EMPTY') "
        'ORDER BY a.dataset_id, a.finished_at DESC NULLS LAST, a.id DESC)) '
        f'AND NOT {names_open("r")} ORDER BY r.id'
    )


def test_a_run_naming_an_open_request_is_dropped_whole_never_nulled():
    # Dropped, not nulled: the statement selects the stored row as it is (r.*), with no expression over request_id.
    text = trim('ingest_run')
    assert text.startswith('SELECT r.* FROM ingest_run r WHERE ')
    assert 'NULL AS' not in text.upper() and 'CASE' not in text.upper() and 'NULLIF' not in text.upper()


# O6 as ruled (F2): the base is newest_snapshot's answer per (source, asset_list): non-empty only, newest by as_of, then
# received_at, then id. asset_type is NOT a key.
KEPT_SNAPSHOT = (
    's.as_of >= %(cutoff)s OR s.id IN ('
    'SELECT DISTINCT ON (b.source, b.asset_list) b.id FROM asset_list_snapshot b '
    'WHERE b.as_of < %(cutoff)s AND b.row_count > 0 '
    'ORDER BY b.source, b.asset_list, b.as_of DESC, b.received_at DESC, b.id DESC)'
)


def test_snapshots_are_the_window_plus_the_newest_non_empty_one_before_it_per_source_and_list():
    assert trim('asset_list_snapshot') == f'SELECT s.* FROM asset_list_snapshot s WHERE {KEPT_SNAPSHOT} ORDER BY s.id'
    assert 'asset_type' not in trim('asset_list_snapshot')


def _order_terms(clause: str) -> list[str]:
    return [term.strip() for term in clause.split(',')]


def test_the_diff_base_mirrors_the_stores_newest_snapshot(store_facts):
    # newest_snapshot itself, as the store builds it (captured in the child, compiled for Postgres): the filter, the
    # key and the order the export's DISTINCT ON must copy. A change to the store's reader turns this red.
    store = store_facts['statements']['newest_snapshot']
    where, order = re.fullmatch(
        r'SELECT .* FROM asset_list_snapshot WHERE (.*) ORDER BY (.*) LIMIT .*', store['sql']
    ).groups()
    assert where == (
        'asset_list_snapshot.source = %(source_1)s AND asset_list_snapshot.asset_list = %(asset_list_1)s '
        'AND asset_list_snapshot.row_count > %(row_count_1)s'
    )
    assert store['params']['row_count_1'] == 0
    store_order = [term.replace('asset_list_snapshot.', 'b.') for term in _order_terms(order)]
    assert store_order == ['b.as_of DESC', 'b.received_at DESC', 'b.id DESC']
    distinct, mirror_where, mirror_order = re.fullmatch(
        r'.* s\.id IN \(SELECT DISTINCT ON \((.*?)\) b\.id FROM asset_list_snapshot b WHERE (.*?) ORDER BY (.*?)\)',
        collapse(dev_seed._SNAPSHOT_KEPT),
    ).groups()
    keys = _order_terms(distinct)
    assert keys == ['b.source', 'b.asset_list']  # the store's two equality keys, nothing more
    assert _order_terms(mirror_order) == keys + store_order
    assert mirror_where == 'b.as_of < %(cutoff)s AND b.row_count > 0'


@pytest.mark.parametrize(('table', 'alias'), [('asset_list_row', 'w'), ('asset_list_event', 'v')])
def test_asset_list_rows_and_events_follow_their_snapshot(table, alias):
    assert trim(table) == (
        f'SELECT {alias}.* FROM {table} {alias} WHERE {alias}.snapshot_id IN '
        f'(SELECT s.id FROM asset_list_snapshot s WHERE {KEPT_SNAPSHOT}) ORDER BY {alias}.id'
    )


# O5 as ruled (F1), written out. RANKED is the store's _ranked over the rows before the cutoff: NON-DRY-RUN passes only
# (the join to panel_pass), newest first by as_of, the pass's started_at, pass_id, seq; recorded_at is not in the order.
RANKED = (
    'SELECT c.id, c.panel_id, c.vendor_asset_id, c.transition, row_number() OVER ('
    'PARTITION BY c.panel_id, c.vendor_asset_id '
    'ORDER BY c.as_of DESC, q.started_at DESC, c.pass_id DESC, c.seq DESC) AS rnk '
    'FROM panel_member_transition c JOIN panel_pass q ON q.id = c.pass_id '
    'WHERE q.dry_run = false AND c.as_of < %(cutoff)s'
)
# members_at's rule: a member when its newest ENTERED/EXITED/DRAINED row is ENTERED. That ENTERED row, per member.
ENTERED_MEMBERS = (
    f"SELECT m.id, m.panel_id, m.vendor_asset_id FROM ({RANKED} AND c.transition IN ('ENTERED', 'EXITED', 'DRAINED')) m "
    "WHERE m.rnk = 1 AND m.transition = 'ENTERED'"
)
# Kept: the window, a window pass's rows, each member's ENTERED row, and the same member's newest row of any kind
# (latest_state's answer: the tied CREATED, a DEFERRED or a DRAINING). Two rows, not one.
KEPT_BY_LEDGER = (
    't.as_of >= %(cutoff)s '
    'OR t.pass_id IN (SELECT q.id FROM panel_pass q WHERE q.started_at >= %(cutoff)s) '
    f'OR t.id IN (SELECT e.id FROM ({ENTERED_MEMBERS}) e) '
    f'OR t.id IN (SELECT a.id FROM ({RANKED}) a WHERE a.rnk = 1 AND EXISTS ('
    f'SELECT 1 FROM ({ENTERED_MEMBERS}) n WHERE n.panel_id = a.panel_id AND n.vendor_asset_id = a.vendor_asset_id))'
)
# Amended (tj-7zjov1.333): whatever kept it, a row naming an OPEN request is dropped: the outer AND.
KEPT_TRANSITION = f'({KEPT_BY_LEDGER}) AND NOT {names_open("t")}'


def test_transitions_keep_the_window_and_each_members_entered_row_and_its_newest_row(store_facts):
    # The deciding set is the store's own MEMBERSHIP_TRANSITIONS (members_at), not is_member_state's complement.
    assert store_facts['membership'] == ['DRAINED', 'ENTERED', 'EXITED']
    assert {'ENTERED', 'EXITED', 'DRAINED', 'CREATED', 'DEFERRED', 'DRAINING'} <= set(store_facts['transitions'])
    assert trim('panel_member_transition') == (
        f'SELECT t.* FROM panel_member_transition t WHERE {KEPT_TRANSITION} ORDER BY t.id'
    )
    text = trim('panel_member_transition')
    assert 'recorded_at' not in text
    # The dry-run filter sits in every ranking (two in each ENTERED_MEMBERS, one in the any-kind ranking).
    assert text.count('q.dry_run = false') == 3
    assert text.count('c.as_of < %(cutoff)s') == 3


def _window(sql: str) -> tuple[list[str], list[str]]:
    partition, order = re.search(r'row_number\(\) OVER \(PARTITION BY (.*?) ORDER BY (.*?)\)', sql).groups()
    return _order_terms(partition), _order_terms(order)


def _as_mirror(term: str) -> str:
    return term.replace('panel_member_transition.', 'c.').replace('panel_pass.', 'q.')


@pytest.mark.parametrize('reader', ['latest_state', 'members_at'])
def test_the_member_ranking_mirrors_the_stores_own_ranking(store_facts, reader):
    # The store's _ranked as latest_state and members_at build it (captured in the child, compiled for Postgres).
    store = store_facts['statements'][reader]['sql']
    store_partition, store_order = _window(store)
    mirror_partition, mirror_order = _window(collapse(dev_seed._RANKED))
    assert [_as_mirror(term) for term in store_order] == mirror_order
    assert mirror_order == ['c.as_of DESC', 'q.started_at DESC', 'c.pass_id DESC', 'c.seq DESC']
    # The store partitions by vendor asset within ONE panel (a WHERE on panel_id); the export ranks every panel at once.
    assert [_as_mirror(term) for term in store_partition] == ['c.vendor_asset_id']
    assert 'panel_member_transition.panel_id = %(panel_id_1)s' in store
    assert mirror_partition == ['c.panel_id', 'c.vendor_asset_id']
    # Dry-run passes are ignored by both, through the same join.
    assert 'JOIN panel_pass ON panel_pass.id = panel_member_transition.pass_id' in store
    assert 'panel_pass.dry_run IS false' in store
    assert 'JOIN panel_pass q ON q.id = c.pass_id WHERE q.dry_run = false' in collapse(dev_seed._RANKED)


def test_passes_are_the_window_plus_every_pass_a_kept_transition_names():
    # O5's foreign-key closure: the same transition predicate, so no kept transition can name a pass left out.
    assert trim('panel_pass') == (
        'SELECT p.* FROM panel_pass p WHERE p.started_at >= %(cutoff)s OR p.id IN '
        f'(SELECT t.pass_id FROM panel_member_transition t WHERE {KEPT_TRANSITION}) ORDER BY p.id'
    )


# ---- three statements run on rows (SQLite, in memory; see the module docstring) ---------------------------------------
#
# Only the columns the statements read, plus symbol for reading the cases. Instants are stored as UTC ISO text, which
# orders as the instants do, and bound the same way.

SQLITE_SCHEMA = (
    'CREATE TABLE ingest_request (id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL)',
    'CREATE TABLE panel_pass (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, dry_run BOOLEAN NOT NULL)',
    'CREATE TABLE panel_member_transition (id TEXT PRIMARY KEY, pass_id TEXT NOT NULL, panel_id TEXT NOT NULL, '
    'vendor_asset_id TEXT NOT NULL, symbol TEXT NOT NULL, transition TEXT NOT NULL, request_id TEXT, '
    'as_of TEXT NOT NULL, seq INTEGER NOT NULL)',
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


@dataclass
class Ledger:
    """A closed world: every request a row names exists, so a dangle can only come from the trim."""

    requests: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    passes: dict[str, datetime] = field(default_factory=dict)
    transitions: dict[str, tuple] = field(default_factory=dict)

    def run(self, table: str) -> list[tuple]:
        db = sqlite3.connect(':memory:')
        try:
            for statement in SQLITE_SCHEMA:
                db.execute(statement)
            db.executemany(
                'INSERT INTO ingest_request VALUES (?, ?, ?)',
                [(rid, state, _iso(at)) for rid, (state, at) in self.requests.items()],
            )
            db.executemany(
                'INSERT INTO panel_pass VALUES (?, ?, 0)', [(pid, _iso(at)) for pid, at in self.passes.items()]
            )
            db.executemany(
                'INSERT INTO panel_member_transition VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (tid, pass_id, 'panel-1', f'va-{member}', member, kind, request, _iso(at), seq)
                    for tid, (pass_id, member, kind, request, at, seq) in self.transitions.items()
                ],
            )
            sql = re.sub(r'%\((\w+)\)s', r':\1', dev_seed.TRIM_SQL[table])
            parameters = {name: _iso(value) for name, value in dev_seed.cutoffs_for(NOW).parameters().items()}
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()


D = timedelta(days=1)


def _ledger() -> Ledger:
    ledger = Ledger()
    # Requests: the three terminal states in the window (one exactly at the cutoff), the three open ones in the window,
    # a state the enum may gain later, a terminal one a second before the cutoff (the age rule) and an open one before.
    ledger.requests = {
        'q-succeeded': ('SUCCEEDED', CUTOFF + D),
        'q-empty-at-cutoff': ('EMPTY', CUTOFF),
        'q-failed': ('FAILED', CUTOFF + 2 * D),
        'q-pending': ('PENDING', CUTOFF + D),
        'q-queued': ('QUEUED', CUTOFF + D),
        'q-not-delivered': ('NOT_DELIVERED', CUTOFF + D),
        'q-later-state': ('ARCHIVED', CUTOFF + D),
        'q-succeeded-old': ('SUCCEEDED', CUTOFF - timedelta(seconds=1)),
        'q-not-delivered-old': ('NOT_DELIVERED', CUTOFF - 3 * D),
    }
    ledger.passes = {
        'p-old': CUTOFF - 20 * D,
        'p-old-created': CUTOFF - 19 * D,  # holds only m2's CREATED (a request trimmed by age): kept by the closure
        'p-open-created': CUTOFF - 18 * D,  # holds only m1's CREATED (an open request): must go with it
        'p-window': CUTOFF + D,
    }
    ledger.transitions = {
        # m1: a member before the cutoff whose newest row is a CREATED naming an open request.
        't-m1-entered': ('p-old', 'm1', 'ENTERED', None, CUTOFF - 20 * D, 1),
        't-m1-created': ('p-open-created', 'm1', 'CREATED', 'q-queued', CUTOFF - 18 * D, 1),
        # m2: the same shape, its CREATED naming a terminal request trimmed by AGE (R2: kept, and it may dangle).
        't-m2-entered': ('p-old', 'm2', 'ENTERED', None, CUTOFF - 20 * D, 2),
        't-m2-created': ('p-old-created', 'm2', 'CREATED', 'q-succeeded-old', CUTOFF - 19 * D, 1),
        # m3, m4: in the window; m3's CREATED names an open request, m4's a terminal one.
        't-m3-entered': ('p-window', 'm3', 'ENTERED', None, CUTOFF + D, 1),
        't-m3-created': ('p-window', 'm3', 'CREATED', 'q-pending', CUTOFF + D, 2),
        't-m4-entered': ('p-window', 'm4', 'ENTERED', None, CUTOFF + D, 3),
        't-m4-created': ('p-window', 'm4', 'CREATED', 'q-succeeded', CUTOFF + D, 4),
        # m5: ENTERED and CREATED tied in one pass before the cutoff, the CREATED naming an open request made before it.
        't-m5-entered': ('p-old', 'm5', 'ENTERED', None, CUTOFF - 20 * D, 3),
        't-m5-created': ('p-old', 'm5', 'CREATED', 'q-not-delivered-old', CUTOFF - 20 * D, 4),
    }
    return ledger


def test_on_rows_only_terminal_requests_from_the_cutoff_inclusive_are_kept():
    kept = {row[0]: row for row in _ledger().run('ingest_request')}
    assert set(kept) == {'q-succeeded', 'q-empty-at-cutoff', 'q-failed'}
    # Dropped: every open state (PENDING, QUEUED, NOT_DELIVERED), a state outside the list, and by AGE a terminal
    # request a second before the cutoff and an open one before it.
    assert not {'q-pending', 'q-queued', 'q-not-delivered', 'q-later-state', 'q-succeeded-old'} & set(kept)
    assert 'q-not-delivered-old' not in kept


def test_on_rows_a_transition_naming_an_open_request_is_dropped_whole_and_the_rest_are_kept_as_stored():
    ledger = _ledger()
    rows = ledger.run('panel_member_transition')
    kept = {row[0] for row in rows}
    assert kept == {
        't-m1-entered',
        't-m2-entered',
        't-m2-created',
        't-m3-entered',
        't-m4-entered',
        't-m4-created',
        't-m5-entered',
    }
    # Dropped, never nulled: each kept row is exactly the stored row, its request_id included.
    for row in rows:
        pass_id, member, kind, request, at, seq = ledger.transitions[row[0]]
        assert row == (row[0], pass_id, 'panel-1', f'va-{member}', member, kind, request, _iso(at), seq)
    # Each member whose CREATED was dropped keeps its ENTERED, so its newest row is ENTERED and the next pass creates it.
    for member in ('m1', 'm3', 'm5'):
        assert {row[5] for row in rows if row[4] == member} == {'ENTERED'}


def test_on_rows_the_passes_follow_the_transitions_that_stay():
    kept = {row[0] for row in _ledger().run('panel_pass')}
    # p-open-created held only the dropped CREATED; p-old-created holds a kept one; p-window is in the window.
    assert kept == {'p-old', 'p-old-created', 'p-window'}


def test_on_rows_no_kept_row_names_an_open_request_or_a_request_the_seed_left_out_except_by_age():
    ledger = _ledger()
    seed_requests = {row[0] for row in ledger.run('ingest_request')}
    named = {row[6] for row in ledger.run('panel_member_transition') if row[6] is not None}
    assert not {rid for rid in named if ledger.requests[rid][0] in OPEN_REQUEST_STATES}
    # The only reference left dangling is the one R2 allows: a terminal request trimmed by age.
    dangling = named - seed_requests
    assert dangling == {'q-succeeded-old'}
    assert all(ledger.requests[rid][1] < CUTOFF and ledger.requests[rid][0] in TERMINAL_STATES for rid in dangling)


# Which table each alias names, per statement, written out from the statements' FROM clauses. In the two panel ledger
# statements e and n are ENTERED_MEMBERS (derived: id, panel_id, vendor_asset_id) and a and m are a RANKED derived table
# (id, panel_id, vendor_asset_id, transition, rnk); elsewhere e is store_dataset_entry.
LEDGER_ALIASES = {
    't': 'panel_member_transition',
    'q': 'panel_pass',
    'c': 'panel_member_transition',
    'e': 'entered',
    'n': 'entered',
    'a': 'ranked',
    'm': 'ranked',
}
ALIASES = {
    'store_dataset_entry': {'e': 'store_dataset_entry'},
    'stock_market_activity': {'m': 'stock_market_activity', 'e': 'store_dataset_entry'},
    'ingest_run': {'r': 'ingest_run', 'a': 'ingest_run', 'e': 'store_dataset_entry'},
    'ingest_request': {'r': 'ingest_request'},
    'ingest_instance': {'i': 'ingest_instance'},
    'dataset_read': {'d': 'dataset_read'},
    'dataset_read_daily': {'d': 'dataset_read_daily'},
    'dataset_override_log': {'d': 'dataset_override_log'},
    'dataset_deletion': {'d': 'dataset_deletion'},
    'asset_list_snapshot': {'s': 'asset_list_snapshot', 'b': 'asset_list_snapshot'},
    'asset_list_row': {'w': 'asset_list_row', 's': 'asset_list_snapshot', 'b': 'asset_list_snapshot'},
    'asset_list_event': {'v': 'asset_list_event', 's': 'asset_list_snapshot', 'b': 'asset_list_snapshot'},
    'corporate_action': {'c': 'corporate_action'},
    'panel': {'p': 'panel'},
    'panel_settings_version': {'v': 'panel_settings_version'},
    'panel_source_settings_version': {'v': 'panel_source_settings_version'},
    'panel_pass': {'p': 'panel_pass', **LEDGER_ALIASES},
    'panel_member_transition': dict(LEDGER_ALIASES),
}
# tj-7zjov1.333: the open request of the two drops is alias o, in ingest_run, panel_member_transition and (through the
# transition predicate) panel_pass. Nowhere else.
for _table in ('ingest_run', 'panel_member_transition', 'panel_pass'):
    ALIASES[_table]['o'] = 'ingest_request'
# The derived tables' columns, written out from their select lists.
DERIVED = {
    'ranked': ['id', 'panel_id', 'vendor_asset_id', 'transition', 'rnk'],
    'entered': ['id', 'panel_id', 'vendor_asset_id'],
}


def test_the_derived_tables_select_exactly_the_columns_the_alias_map_gives_them():
    assert collapse(dev_seed._RANKED).startswith(
        'SELECT c.id, c.panel_id, c.vendor_asset_id, c.transition, row_number() OVER ('
    )
    assert ') AS rnk FROM panel_member_transition c ' in collapse(dev_seed._RANKED)
    assert collapse(dev_seed._ENTERED_MEMBERS).startswith('SELECT m.id, m.panel_id, m.vendor_asset_id FROM (')


@pytest.mark.parametrize('table', EIGHTEEN_TABLES)
def test_every_column_a_trim_statement_names_exists_on_the_store_model(table, store_facts):
    # The strongest pin available without Postgres: a misspelt or renamed column turns this red.
    columns = {**store_facts['columns'], **DERIVED}
    aliases = ALIASES[table]
    references = re.findall(r'\b([a-z])\.("?)([a-z_]+)\2', trim(table))
    assert references, table
    for alias, _, column in references:
        assert alias in aliases, (table, alias)
        assert column in columns[aliases[alias]], (table, f'{alias}.{column}', aliases[alias])


@pytest.mark.parametrize('table', EIGHTEEN_TABLES)
def test_every_alias_a_trim_statement_binds_is_the_table_the_alias_map_says(table):
    # The column check above trusts the map; this checks the map against the statement's own FROM and JOIN clauses, so
    # an alias bound to another table (o naming panel_pass, say) cannot pass because that table has the column too.
    text = trim(table)
    bound = re.findall(r'\b(?:FROM|JOIN) ([a-z_]+) ([a-z])\b', text)
    assert bound, table
    for name, alias in bound:
        assert ALIASES[table].get(alias) == name, (table, alias, name)
    # The open-request drop (alias o) is in exactly the three statements the design names.
    assert (('ingest_request', 'o') in bound) == (table in ('ingest_run', 'panel_member_transition', 'panel_pass'))


# ---- the export, end to end against the double -----------------------------------------------------------------------

PANEL_COLUMNS = [('id', 'uuid', 'uuid'), ('name', 'text', 'text'), ('source', 'USER-DEFINED', 'datasource')]
PANEL_COPY = b'p-1\tzz-one\tALPACA_API\np-2\tzz two\\nlines\tALPACA_API\n'
BAR_COPY = b'1\td-1\n2\td-1\n3\td-2\n'


def export_db(**overrides) -> FakeDb:
    db = FakeDb(**overrides)
    db.copy_data.setdefault('panel', PANEL_COPY)
    db.copy_data.setdefault('stock_market_activity', BAR_COPY)
    db.columns.setdefault('panel', PANEL_COLUMNS)
    return db


def export(db: FakeDb, root: Path, **kwargs) -> dev_seed.ExportResult:
    lines: list[str] = []
    connection = db.connect()
    return dev_seed.export_seed(connection, 'dev', NOW, lines.append, root=root, **kwargs)


def test_the_export_is_one_read_only_repeatable_read_transaction_that_is_never_committed(tmp_path):
    db = export_db()
    export(db, tmp_path)
    assert db.commits == 0
    statements = db.sql()
    # The rail check's transaction is ended first, so SET TRANSACTION is the first statement of the new one.
    first_rollback = db.events.index(('rollback',))
    after = [event for event in db.events[first_rollback + 1 :] if event[0] == 'execute']
    assert collapse(after[0][1]) == READ_ONLY_TEXT == dev_seed.READ_ONLY_SQL
    assert statements.count(READ_ONLY_TEXT) == 1
    assert statements[:4] == [
        READ_ONLY_TEXT,
        "SET LOCAL TimeZone = 'UTC'",
        'SET LOCAL extra_float_digits = 1',
        ALEMBIC_TEXT,
    ]
    # Nothing but reads and transaction-local settings, and the COPYs are COPY ... TO STDOUT of the trim statements.
    for sql in statements:
        assert sql.startswith(('SELECT ', 'SET TRANSACTION ', 'SET LOCAL ')), sql
    assert [table for table, _ in db.copies] == list(dev_seed.SEED_TABLES)
    for _, sql in db.copies:
        assert COPY_TEXT.match(sql), sql
    # It ends rolled back: the last database event is a rollback.
    assert [event for event in db.events if event[0] in ('commit', 'rollback')][-1] == ('rollback',)


def test_every_trim_statement_is_bound_with_the_four_cutoffs_of_the_export_instant(tmp_path):
    db = export_db()
    export(db, tmp_path)
    assert [sql for sql, _ in db.mogrified] == [dev_seed.TRIM_SQL[t] for t in dev_seed.SEED_TABLES]
    expected = {'now': NOW, 'cutoff': CUTOFF, 'session_cutoff': SESSION_CUTOFF, 'corporate_cutoff': CORPORATE_CUTOFF}
    for _, parameters in db.mogrified:
        assert parameters == expected
    # And what COPY runs is the bound statement, no placeholder left.
    for _, sql in db.copies:
        assert '%(' not in sql


def test_the_manifest_binds_the_files_to_the_revision_the_cutoffs_and_the_schema(tmp_path):
    db = export_db()
    result = export(db, tmp_path)
    head = dev_seed.head_revision()
    assert result.directory == tmp_path / head / STAMP
    manifest = json.loads((result.directory / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['revision'] == manifest['checkout_head'] == head == result.revision
    assert manifest['format'] == 1
    assert manifest['stack'] == 'dev'
    assert manifest['cutoff_days'] == 90
    assert manifest['corporate_action_days'] == 180
    assert manifest['created_at'] == NOW.isoformat()
    assert manifest['cutoff'] == CUTOFF.isoformat()
    assert datetime.fromisoformat(manifest['session_cutoff']) == SESSION_CUTOFF
    assert manifest['corporate_action_cutoff'] == CORPORATE_CUTOFF.isoformat()
    assert manifest['load_order'] == list(dev_seed.SEED_TABLES)
    assert set(manifest['tables']) == set(EIGHTEEN_TABLES)
    for table, entry in manifest['tables'].items():
        data = (result.directory / f'{table}.copy').read_bytes()
        assert data == db.copy_data.get(table, b'')
        assert entry['file'] == f'{table}.copy'
        assert entry['bytes'] == len(data)
        assert entry['sha256'] == hashlib.sha256(data).hexdigest()
        assert entry['rows'] == data.count(b'\n')
        assert result.counts[table] == entry['rows']
    # A COPY text row is one line; an escaped newline inside a value is not a row.
    assert manifest['tables']['panel']['rows'] == 2
    assert manifest['tables']['stock_market_activity']['rows'] == 3
    assert manifest['tables']['panel_pass']['rows'] == 0


def test_the_manifest_counts_are_the_rows_written_after_the_trim_never_the_tables_own_counts(tmp_path):
    # The tables hold more rows than the trim keeps (the double answers count(*) from counts); the manifest, the result
    # and the files must all say the trimmed number, the one a reload recounts against.
    db = export_db()
    db.counts = {table: 1000 + index for index, table in enumerate(EIGHTEEN_TABLES)}
    db.copy_data['ingest_request'] = b'q-1\tSUCCEEDED\nq-2\tFAILED\n'
    db.copy_data['ingest_run'] = b'r-1\tq-1\n'
    db.copy_data['panel_member_transition'] = b't-1\tENTERED\nt-2\tCREATED\nt-3\tEXITED\n'
    result = export(db, tmp_path)
    manifest = json.loads((result.directory / 'manifest.json').read_text(encoding='utf-8'))
    for table in EIGHTEEN_TABLES:
        written = (result.directory / f'{table}.copy').read_bytes().count(b'\n')
        assert manifest['tables'][table]['rows'] == written == result.counts[table], table
        assert written != db.counts[table], table
    assert manifest['tables']['ingest_request']['rows'] == 2
    assert manifest['tables']['ingest_run']['rows'] == 1
    assert manifest['tables']['panel_member_transition']['rows'] == 3
    # No count(*) of a table is asked for at all: the counts come from what COPY streamed.
    assert not [sql for sql in db.sql() if sql.upper().startswith('SELECT COUNT(*) FROM')]


def _fingerprint(columns) -> str:
    # Written out from D3: name, type and udt name, one line each, in ordinal order.
    return hashlib.sha256(''.join(f'{n}|{d}|{u}\n' for n, d, u in columns).encode('utf-8')).hexdigest()


def test_the_column_fingerprint_is_names_and_types_in_ordinal_order(tmp_path):
    db = export_db()
    result = export(db, tmp_path)
    manifest = json.loads((result.directory / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['tables']['panel']['columns'] == ['id', 'name', 'source']
    assert manifest['tables']['panel']['fingerprint'] == _fingerprint(PANEL_COLUMNS)
    assert dev_seed.column_fingerprint(PANEL_COLUMNS) == _fingerprint(PANEL_COLUMNS)
    # Order and type both count.
    assert dev_seed.column_fingerprint(PANEL_COLUMNS[::-1]) != _fingerprint(PANEL_COLUMNS)
    retyped = [PANEL_COLUMNS[0], ('name', 'character varying', 'varchar'), PANEL_COLUMNS[2]]
    assert dev_seed.column_fingerprint(retyped) != _fingerprint(PANEL_COLUMNS)
    # Each table's columns are read from information_schema by name, for every table.
    asked = [params for sql, params in db.statements if collapse(sql) == COLUMNS_TEXT]
    assert len(asked) == 18


def test_a_table_with_no_columns_fails_the_export_and_removes_its_directory(tmp_path):
    db = export_db()
    db.columns['corporate_action'] = []
    with pytest.raises(dev_seed.DevSeedError, match='corporate_action'):
        export(db, tmp_path)
    assert list((tmp_path / dev_seed.head_revision()).iterdir()) == []


# ---- the revision (D3) ------------------------------------------------------------------------------------------------


def test_a_database_not_at_the_checkout_head_is_refused_before_any_file_is_written(tmp_path):
    db = export_db(revision='0000aaaa1111')
    with pytest.raises(dev_seed.DevSeedRefused) as raised:
        export(db, tmp_path / 'seed')
    assert issubclass(dev_seed.DevSeedRefused, dev_seed.DevSeedError)
    message = str(raised.value)
    assert '0000aaaa1111' in message and dev_seed.head_revision() in message
    assert not (tmp_path / 'seed').exists()
    assert db.copies == [] and db.mogrified == []
    assert db.commits == 0


@pytest.mark.parametrize('rows', [[], [('a',), ('b',)], [(None,)], [('',)]])
def test_an_alembic_version_that_is_not_exactly_one_revision_is_refused(tmp_path, rows):
    db = export_db(alembic_rows=rows)
    with pytest.raises(dev_seed.DevSeedRefused):
        export(db, tmp_path / 'seed')
    assert not (tmp_path / 'seed').exists()
    assert db.copies == []


def test_the_dry_run_refuses_a_revision_mismatch_too(tmp_path):
    db = export_db(revision='0000aaaa1111')
    with pytest.raises(dev_seed.DevSeedRefused):
        dev_seed.estimate_seed(db.connect(), NOW, lambda line: None)
    assert not any(sql.startswith('SELECT count(*), coalesce') for sql in db.sql())


def test_a_failure_that_is_not_a_revision_refusal_is_a_plain_seed_error(tmp_path):
    # Only the revision refusal is DevSeedRefused (exit 3); a database failure is not.
    db = export_db(copy_error=RuntimeError('boom'), copy_error_on='panel')
    with pytest.raises(dev_seed.DevSeedError) as raised:
        export(db, tmp_path)
    assert not isinstance(raised.value, dev_seed.DevSeedRefused)


# ---- the files (D5) ---------------------------------------------------------------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_directories_are_0700_and_files_0600(tmp_path):
    result = export(export_db(), tmp_path)
    assert _mode(result.directory) == 0o700
    assert _mode(result.directory.parent) == 0o700
    files = sorted(result.directory.iterdir())
    assert len(files) == 19
    for path in files:
        assert path.is_file() and not path.is_symlink()
        assert _mode(path) == 0o600, path


def test_files_are_created_exclusively_and_never_through_a_symbolic_link(tmp_path, monkeypatch):
    flags = []
    real_open = os.open

    def spying_open(path, flag, *args, **kwargs):
        flags.append((Path(path).name, flag))
        return real_open(path, flag, *args, **kwargs)

    monkeypatch.setattr(dev_seed.os, 'open', spying_open)
    export(export_db(), tmp_path)
    created = [(name, flag) for name, flag in flags if flag & os.O_CREAT]
    assert len(created) == 19
    for name, flag in created:
        assert flag & os.O_EXCL and flag & os.O_NOFOLLOW and flag & os.O_WRONLY, name


def test_create_file_refuses_an_existing_file_and_a_symlink_and_leaves_the_target_alone(tmp_path):
    existing = tmp_path / 'existing.copy'
    existing.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        dev_seed._create_file(existing)
    assert existing.read_bytes() == b'keep'
    target = tmp_path / 'target'
    target.write_bytes(b'target')
    link = tmp_path / 'link.copy'
    link.symlink_to(target)
    with pytest.raises(OSError) as raised:
        dev_seed._create_file(link)
    assert raised.value.errno in (errno.EEXIST, errno.ELOOP)
    assert target.read_bytes() == b'target'
    fresh = dev_seed._create_file(tmp_path / 'fresh.copy')
    fresh.close()
    assert _mode(tmp_path / 'fresh.copy') == 0o600


def test_every_file_and_both_directories_are_fsynced(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def recording_fsync(descriptor):
        synced.append(os.readlink(f'/proc/self/fd/{descriptor}'))
        return real_fsync(descriptor)

    monkeypatch.setattr(dev_seed.os, 'fsync', recording_fsync)
    result = export(export_db(), tmp_path)
    expected = {str(path) for path in result.directory.iterdir()} | {
        str(result.directory),
        str(result.directory.parent),
    }
    assert expected <= set(synced)


def test_a_failed_export_removes_its_own_directory_and_says_only_the_type(tmp_path):
    db = export_db(copy_error=RuntimeError('row says SECRETCANARY'), copy_error_on='panel_pass')
    with pytest.raises(dev_seed.DevSeedError) as raised:
        export(db, tmp_path)
    assert 'RuntimeError' in str(raised.value)
    assert 'SECRETCANARY' not in str(raised.value)
    revision_dir = tmp_path / dev_seed.head_revision()
    assert list(revision_dir.iterdir()) == []
    assert db.commits == 0
    assert db.events[-1] == ('rollback',)


def test_a_stamp_that_already_exists_is_refused_and_left_as_it_was(tmp_path):
    existing = tmp_path / dev_seed.head_revision() / STAMP
    existing.mkdir(parents=True)
    (existing / 'manifest.json').write_text('{}', encoding='utf-8')
    with pytest.raises(dev_seed.DevSeedError, match='already exists'):
        export(export_db(), tmp_path)
    assert (existing / 'manifest.json').read_text(encoding='utf-8') == '{}'


def test_the_newest_three_stamps_of_the_revision_are_kept_and_nothing_else_is_touched(tmp_path):
    head = dev_seed.head_revision()
    revision_dir = tmp_path / head
    for name in ('20261017T000000Z', '20261018T000000Z', '20261019T000000Z', '20200101T000000Z'):
        (revision_dir / name).mkdir(parents=True)
        (revision_dir / name / 'manifest.json').write_text('{}', encoding='utf-8')
    (revision_dir / 'notes').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'precious').write_text('x', encoding='utf-8')
    (revision_dir / '20000101T000000Z').symlink_to(outside, target_is_directory=True)
    other = tmp_path / 'otherrevision' / '20190101T000000Z'
    other.mkdir(parents=True)
    result = export(export_db(), tmp_path)
    assert dev_seed.KEEP_SEEDS == 3
    stamps = sorted(
        p.name for p in revision_dir.iterdir() if re.fullmatch(r'\d{8}T\d{6}Z', p.name) and not p.is_symlink()
    )
    assert stamps == ['20261018T000000Z', '20261019T000000Z', STAMP]
    assert set(result.pruned) == {'20261017T000000Z', '20200101T000000Z'}
    assert (revision_dir / 'notes').is_dir()
    assert (outside / 'precious').exists() and (revision_dir / '20000101T000000Z').is_symlink()
    assert other.is_dir()


def _stamp(revision_dir: Path, name: str, manifest: str | None = 'file') -> Path:
    """A stamp directory: manifest 'file' (complete), None (no manifest), 'dir' or 'link' (neither is complete)."""
    path = revision_dir / name
    path.mkdir(parents=True)
    (path / 'panel.copy').write_bytes(b'')
    if manifest == 'file':
        (path / 'manifest.json').write_text('{}', encoding='utf-8')
    elif manifest == 'dir':
        (path / 'manifest.json').mkdir()
    elif manifest == 'link':
        target = revision_dir.parent / f'{name}.target.json'
        target.write_text('{}', encoding='utf-8')
        (path / 'manifest.json').symlink_to(target)
    return path


def _stamps(revision_dir: Path) -> list[str]:
    return sorted(p.name for p in revision_dir.iterdir() if re.fullmatch(r'\d{8}T\d{6}Z', p.name))


def test_only_complete_stamps_count_toward_the_newest_three(tmp_path):
    # F5. An incomplete stamp (a SIGKILLed export: no manifest.json) between the complete ones is not one of the three.
    revision_dir = tmp_path / dev_seed.head_revision()
    for name in ('20261017T000000Z', '20261018T000000Z', '20261019T000000Z'):
        _stamp(revision_dir, name)
    _stamp(revision_dir, '20261019T120000Z', manifest=None)
    result = export(export_db(), tmp_path)
    assert _stamps(revision_dir) == ['20261018T000000Z', '20261019T000000Z', STAMP]
    assert result.pruned == ('20261019T120000Z', '20261017T000000Z')


@pytest.mark.parametrize('manifest', [None, 'dir', 'link'])
def test_an_incomplete_stamp_older_than_the_newest_complete_one_is_removed(tmp_path, manifest):
    # Complete means manifest.json is a REGULAR file: a directory or a symbolic link of that name is not a seed.
    revision_dir = tmp_path / 'rev'
    _stamp(revision_dir, '20261010T000000Z')
    _stamp(revision_dir, '20261011T000000Z', manifest=manifest)
    _stamp(revision_dir, '20261012T000000Z')
    assert dev_seed._prune(revision_dir, 3) == ('20261011T000000Z',)
    assert _stamps(revision_dir) == ['20261010T000000Z', '20261012T000000Z']
    # The link's target, outside the stamp, is not touched.
    assert manifest != 'link' or (tmp_path / '20261011T000000Z.target.json').is_file()


def test_an_incomplete_stamp_newer_than_the_newest_complete_one_is_left_alone(tmp_path):
    # It may be an export still running.
    revision_dir = tmp_path / dev_seed.head_revision()
    _stamp(revision_dir, '20261021T000000Z', manifest=None)
    result = export(export_db(), tmp_path)
    assert _stamps(revision_dir) == [STAMP, '20261021T000000Z']
    assert result.pruned == ()
    revision_dir = tmp_path / 'only-incomplete'
    _stamp(revision_dir, '20261001T000000Z', manifest=None)
    _stamp(revision_dir, '20261002T000000Z', manifest=None)
    assert dev_seed._prune(revision_dir, 3) == ()
    assert _stamps(revision_dir) == ['20261001T000000Z', '20261002T000000Z']


def test_complete_stamps_beyond_the_newest_three_go_with_the_older_incomplete_ones(tmp_path):
    revision_dir = tmp_path / 'rev'
    for day in range(1, 6):
        _stamp(revision_dir, f'2026100{day}T000000Z')
    _stamp(revision_dir, '20261001T120000Z', manifest=None)
    _stamp(revision_dir, '20261004T120000Z', manifest=None)
    _stamp(revision_dir, '20261009T000000Z', manifest=None)
    removed = dev_seed._prune(revision_dir, 3)
    assert removed == ('20261004T120000Z', '20261002T000000Z', '20261001T120000Z', '20261001T000000Z')
    assert _stamps(revision_dir) == ['20261003T000000Z', '20261004T000000Z', '20261005T000000Z', '20261009T000000Z']


# ---- every directory level (F4, tj-7zjov1.305) -------------------------------------------------------------------


def test_a_seed_root_outside_the_repository_is_created_0700_with_its_revision_directory(tmp_path):
    root = tmp_path / 'seed'
    result = export(export_db(), root)
    assert _mode(root) == 0o700
    assert _mode(root / dev_seed.head_revision()) == 0o700
    assert _mode(result.directory) == 0o700


def test_inside_the_repository_every_level_from_output_down_is_checked(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(dev_seed, 'REPO_ROOT', repo)
    root = repo / 'output' / 'dev_seed'
    assert dev_seed._seed_levels(root, 'rev') == [repo / 'output', root, root / 'rev']
    # Outside the repository: the root's own name and the revision directory, never the root's parents.
    elsewhere = tmp_path / 'a' / 'b' / 'seed'
    assert dev_seed._seed_levels(elsewhere, 'rev') == [elsewhere, elsewhere / 'rev']
    result = export(export_db(), root)
    for level in (repo / 'output', root, root / dev_seed.head_revision(), result.directory):
        assert level.is_dir() and not level.is_symlink()
        assert _mode(level) == 0o700, level


def _planted(tmp_path: Path) -> Path:
    target = tmp_path / 'elsewhere'
    target.mkdir()
    (target / 'precious').write_text('x', encoding='utf-8')
    return target


@pytest.mark.parametrize('level', ['output', 'output/dev_seed', 'output/dev_seed/REVISION'])
def test_a_symbolic_link_at_any_level_inside_the_repository_is_refused(tmp_path, monkeypatch, level):
    repo = tmp_path / 'repo'
    repo.mkdir()
    monkeypatch.setattr(dev_seed, 'REPO_ROOT', repo)
    link = repo / level.replace('REVISION', dev_seed.head_revision())
    link.parent.mkdir(parents=True, exist_ok=True)
    target = _planted(tmp_path)
    link.symlink_to(target, target_is_directory=True)
    db = export_db()
    with pytest.raises(dev_seed.DevSeedError, match='symbolic link'):
        export(db, repo / 'output' / 'dev_seed')
    assert sorted(p.name for p in target.iterdir()) == ['precious']
    assert db.copies == []


@pytest.mark.parametrize('level', ['root', 'revision'])
def test_a_symbolic_link_at_the_seed_root_or_its_revision_directory_is_refused(tmp_path, level):
    root = tmp_path / 'seed'
    target = _planted(tmp_path)
    if level == 'root':
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir()
        (root / dev_seed.head_revision()).symlink_to(target, target_is_directory=True)
    with pytest.raises(dev_seed.DevSeedError, match='symbolic link'):
        export(export_db(), root)
    assert sorted(p.name for p in target.iterdir()) == ['precious']


@pytest.mark.parametrize('level', ['root', 'revision'])
def test_a_level_that_is_not_a_directory_is_refused(tmp_path, level):
    root = tmp_path / 'seed'
    if level == 'root':
        root.write_text('a file', encoding='utf-8')
    else:
        root.mkdir()
        (root / dev_seed.head_revision()).write_text('a file', encoding='utf-8')
    with pytest.raises(dev_seed.DevSeedError, match='not a directory'):
        export(export_db(), root)


def test_a_symbolic_link_above_a_seed_root_outside_the_repository_is_followed(tmp_path):
    # As built and as dispatched: outside the repository only the root's own name and the revision directory are
    # checked; a link among the root's parents is the operator's choice of location.
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'via').symlink_to(real, target_is_directory=True)
    result = export(export_db(), tmp_path / 'via' / 'seed')
    assert (real / 'seed' / dev_seed.head_revision() / STAMP / 'manifest.json').is_file()
    assert result.directory == tmp_path / 'via' / 'seed' / dev_seed.head_revision() / STAMP


def test_the_seed_root_is_output_dev_seed_and_output_is_kept_out_of_the_build_context():
    assert dev_seed.SEED_ROOT == REPO_ROOT / 'output' / 'dev_seed'
    lines = [line.strip() for line in (REPO_ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()]
    assert 'output/' in lines


# ---- the CLI: the rail, the credentials, the flags, the dry run ------------------------------------------------------


def run_export(tmp_path, *extra, environ=None, http=None, db=None, connect=None):
    db = db if db is not None else export_db()
    outcome = run_tool(
        ['export', *extra],
        environ,
        http=http if http is not None else FakeStore(),
        connect=connect if connect is not None else db.connect,
        seed_root=tmp_path / 'seed',
    )
    return outcome, db


def test_export_writes_a_seed_with_exit_0_and_no_credential_in_the_output(tmp_path):
    outcome, db = run_export(tmp_path)
    assert outcome.status == 0, outcome.text
    seeds = list((tmp_path / 'seed' / dev_seed.head_revision()).iterdir())
    assert [p.name for p in seeds] == [STAMP]
    assert (seeds[0] / 'manifest.json').is_file()
    assert db.commits == 0
    assert db.closes == 1
    assert_no_canary(outcome.text)


def test_export_needs_the_database_password_and_stops_before_any_call(tmp_path):
    environ = {key: value for key, value in GOOD_ENV.items() if key != tool.DB_PASSWORD_VAR}
    outcome, _ = run_export(tmp_path, environ=environ, http=ExplodingHttp(), connect=exploding_connect)
    assert outcome.status == 1
    assert tool.DB_PASSWORD_VAR in outcome.text
    assert not (tmp_path / 'seed').exists()


def test_export_does_not_need_the_instance_secret(tmp_path):
    environ = {key: value for key, value in GOOD_ENV.items() if value != SECRET}
    assert tool.DB_PASSWORD_VAR in environ and DB_PASSWORD in environ.values()
    outcome, _ = run_export(tmp_path, environ=environ)
    assert outcome.status == 0, outcome.text


@pytest.mark.parametrize('flag', [['--yes-reset'], ['--row', 'r1']])
def test_export_with_yes_reset_or_row_is_a_usage_error(tmp_path, flag):
    outcome, _ = run_export(tmp_path, *flag, http=ExplodingHttp(), connect=exploding_connect)
    assert outcome.status == 2
    assert not (tmp_path / 'seed').exists()


def test_the_flag_form_is_the_same_mode(tmp_path):
    outcome = run_tool(['--export'], http=FakeStore(), connect=export_db().connect, seed_root=tmp_path / 'seed')
    assert outcome.status == 0, outcome.text
    assert (tmp_path / 'seed').is_dir()


@pytest.mark.parametrize('label', ['', 'prod', 'Dev'])
def test_the_rail_refuses_a_stack_that_is_not_dev_or_test_before_any_sql(tmp_path, label):
    store = FakeStore(config=tool.Response(200, {'deploymentLabel': label}))
    outcome, _ = run_export(tmp_path, http=store, connect=exploding_connect)
    assert outcome.status == 1
    assert 'refused' in outcome.text
    assert not (tmp_path / 'seed').exists()
    outcome, _ = run_export(tmp_path, '--dry-run', http=store, connect=exploding_connect)
    assert outcome.status == 1


def test_the_database_rail_refuses_a_database_that_is_not_the_stores_before_the_export_starts(tmp_path):
    db = export_db(stack='test')
    outcome, _ = run_export(tmp_path, db=db)
    assert outcome.status == 1
    assert db.sql() == [STACK_TEXT]
    assert db.copies == []
    assert not (tmp_path / 'seed').exists()


def test_a_test_stack_exports_under_its_own_label(tmp_path):
    db = export_db(stack='test')
    outcome, _ = run_export(tmp_path, db=db, http=FakeStore(config=tool.Response(200, {'deploymentLabel': 'test'})))
    assert outcome.status == 0, outcome.text
    [stamp] = list((tmp_path / 'seed' / dev_seed.head_revision()).iterdir())
    assert json.loads((stamp / 'manifest.json').read_text(encoding='utf-8'))['stack'] == 'test'


def test_a_revision_mismatch_through_the_cli_exits_3_and_writes_nothing(tmp_path):
    # D3 and the architect's F3 ruling: exit 3, like the seed producer's EXIT_REFUSED.
    assert tool.EXIT_REFUSED == 3
    outcome, db = run_export(tmp_path, db=export_db(revision='0000aaaa1111'))
    assert outcome.status == 3
    assert '0000aaaa1111' in outcome.text
    assert not (tmp_path / 'seed').exists()
    assert db.copies == []


def test_a_revision_mismatch_in_a_dry_run_exits_3(tmp_path):
    db = export_db(revision='0000aaaa1111')
    outcome, _ = run_export(tmp_path, '--dry-run', db=db)
    assert outcome.status == 3
    assert '0000aaaa1111' in outcome.text
    assert not any(sql.startswith('SELECT count(*), coalesce') for sql in db.sql())


@pytest.mark.parametrize('rows', [[], [('a',), ('b',)]])
def test_an_alembic_version_that_is_not_one_row_exits_3_through_the_cli(tmp_path, rows):
    outcome, db = run_export(tmp_path, db=export_db(alembic_rows=rows))
    assert outcome.status == 3
    assert 'migrate first' in outcome.text
    assert db.copies == []


def test_the_cli_revision_refusal_is_a_dev_datasets_error():
    assert issubclass(tool.RevisionRefused, tool.DevDatasetsError)


def test_an_export_failure_that_is_not_a_revision_refusal_exits_1(tmp_path):
    db = export_db(copy_error=RuntimeError('row says SECRETCANARY'), copy_error_on='panel_pass')
    outcome, _ = run_export(tmp_path, db=db)
    assert outcome.status == 1
    assert 'RuntimeError' in outcome.text
    assert 'SECRETCANARY' not in outcome.text


def test_a_symlinked_seed_root_through_the_cli_exits_1(tmp_path):
    target = _planted(tmp_path)
    (tmp_path / 'seed').symlink_to(target, target_is_directory=True)
    outcome, db = run_export(tmp_path)
    assert outcome.status == 1
    assert 'symbolic link' in outcome.text
    assert sorted(p.name for p in target.iterdir()) == ['precious']
    assert db.copies == []


def test_the_dry_run_prints_per_table_counts_and_heap_estimates_and_writes_nothing(tmp_path):
    db = export_db()
    db.estimates = {table: (index + 1, (index + 1) * 1000 + 7) for index, table in enumerate(EIGHTEEN_TABLES)}
    outcome, _ = run_export(tmp_path, '--dry-run', db=db)
    assert outcome.status == 0, outcome.text
    for table, (rows, size) in db.estimates.items():
        assert f'  {table}: rows {rows} approx heap bytes {size}' in outcome.lines
    assert f'export dry run: {sum(r for r, _ in db.estimates.values())} rows in all' in outcome.lines
    assert not (tmp_path / 'seed').exists()
    assert db.copies == [] and db.mogrified == []
    assert db.commits == 0
    estimates = [sql for sql, _ in db.statements if sql in dev_seed.ESTIMATE_SQL.values()]
    assert estimates == [dev_seed.ESTIMATE_SQL[t] for t in dev_seed.SEED_TABLES]
    statements = db.sql()
    assert statements[1] == READ_ONLY_TEXT  # after the rail's identity query
    for sql in statements:
        assert sql.startswith(('SELECT ', 'SET TRANSACTION ', 'SET LOCAL ')), sql


def test_the_dry_run_without_a_database_password_stops_after_the_rail(tmp_path):
    environ = {key: value for key, value in GOOD_ENV.items() if key != tool.DB_PASSWORD_VAR}
    outcome, _ = run_export(tmp_path, '--dry-run', environ=environ, connect=exploding_connect)
    assert outcome.status == 0
    assert 'export counts unavailable: the database password is not set' in outcome.lines
    assert not (tmp_path / 'seed').exists()
