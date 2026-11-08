"""The seed producer's run (data/store/seeds/producer.py and __main__.py): order, refusals, the one bundle line.

WHY THIS FILE EXISTS (validator, tj-vhboky.60; ported for tj-irhy0a.21, decision tj-vhboky.55 S9).
The design's safety claims are about ORDER and about what leaves the process: the synthetic-only
refusal runs before the first POST and again before the destructive rewrite, a revision mismatch
refuses before anything is touched, and the bundle is printed last, as the ONLY stdout line, and only
when every step passed. The consumers (tj-vhboky.62, .65, the MCP's reader) see only a finished
bundle, so an order bug -- a POST into a database holding real rows, a rewrite before the second
check, a half-printed bundle -- would never reach them.

WHAT TIER THIS IS. produce() is driven with a duck-typed stack at Stack's seam (query / script /
post, rows not psql text) that answers each SQL by its shape and records every call. The END-TO-END
section drives __main__.main() through the REAL Stack with psycopg2.connect and httpx.post replaced
by adapters onto the same fake, to pin that neither the write secret nor the database password
reaches stdout, stderr or any SQL statement on any path. NOT PROVED HERE, and NOT RUN until the MCP
sitting tj-c4mosr.6: every SQL statement and the POSTs against a real fake-mode stack (tj-vhboky.65
runs the producer twice through the MCP).

THE STORED COUNTS (tj-o36zj2). Each POST reply carries data_points, the number of bars data_store
wrote for that request (routers/data_store/asset_dataset_store.py). FakeStack derives it from what
was POSTed, through the fake's own grid and scenario helpers (tests/fakes/market_data.py), never
from a hard-coded number, and answers the collision query from the same bars.

A COUNT THAT IS NOT A NON-NEGATIVE INT (supersedes tj-rptmtg's non-ASCII-digit tests, S9 (5)). The
driver returns ints, so psql's text framing is gone; what stays is the refusal semantics: in the
synthetic check a row whose count is not a non-negative int is ONE foreign row (exit 3, count-only
message); in a count query it is a StackError (exit 1) that never echoes the result.

THE INGEST RECORDS (bead tj-7zjov1.100). The seed carries five tables. produce() empties the three ingest tables
after the first synthetic check and before the first POST, then, after the collision check, hands
seed_records (default: ingest_records.seed_ingest_records, the store's own code over asyncpg) the scratch
database's async URL, the scenario's first dataset and its symbol, and runs it with asyncio.run. That function
needs a real Postgres, so EVERY test here runs produce() with a stub seeder (the autouse fixture below swaps the
default argument, so the entry point's own call gets it too); test_the_default_seeder_is_the_store_code pins
the real default. What the cast writes is proved against Postgres only: the orchestrator's two seed_dump runs
at f2b7d4a91c36 (byte-identical) and the committed seed tests/system/seeds/f2b7d4a91c36.*, which the
system suite loads.
"""

import inspect
import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg2
import pytest

from common.enums.data_stock import Granularity
from common.tests.roots import REPO_ROOT
from data.store.seeds import __main__ as entry
from data.store.seeds import producer as producer_module
from data.store.seeds.bundle import BUNDLE_TAG, MAX_BUNDLE_BYTES, parse_bundle
from data.store.seeds.dump import (
    BAR_TABLE,
    CORPORATE_ACTION_TABLE,
    ENTRY_TABLE,
    INSTANCE_TABLE,
    NORMALISE_SQL,
    REQUEST_TABLE,
    RUN_TABLE,
    SEED_TABLES,
    DumpRefused,
    render_table_sql,
)
from data.store.seeds.ingest_records import (
    EXPECTED_ATTEMPT_ROWS,
    EXPECTED_INSTANCES,
    EXPECTED_INTERRUPTED_RUNS,
    EXPECTED_LEGACY_RUNS,
    EXPECTED_LEGACY_STATES,
    EXPECTED_OBSERVATIONS,
    EXPECTED_REQUEST_STATES,
    EXPECTED_REVISED_ACTIONS,
    EXPECTED_RUN_ROWS,
    REQUEST_IDS,
    seed_ingest_records,
)
from data.store.seeds.manifest import head_revision
from data.store.seeds.producer import VERSIONS_DIR, SeedRefused, produce
from data.store.seeds.scenario import SEED_REQUESTS
from data.store.seeds.stack import Stack, StackError
from routers.common.instance_secret import INSTANCE_SECRET_HEADER
from tests.fakes.market_data import EMPTY_PREFIX, Scenario, grid_index, grid_timestamps, scenario_for


pytestmark = pytest.mark.data_store

HEAD = head_revision(VERSIONS_DIR)
# produce()'s defaults as imported, before the autouse fixture swaps the seeder.
REAL_DEFAULTS = produce.__defaults__
SYNTHETIC_ROWS = [
    ('e', 'ZZSEEDAA', 'seed-owner-a', 2),
    ('b', 'ZZSEEDAA', '', 40),
    ('b', 'GAPS_ZZSEEDGG', '', 15),
    ('q', 'ZZSEEDAA', 'seed-owner-a', 9),
    ('r', 'ZZSEEDAA', 'seed-owner-b', 18),
    ('c', 'ZZSEEDCA', '', 3),
]
ENTRY_LINES = [
    "INSERT INTO public.store_dataset_entry (id, owner) VALUES ('0a', 'seed-owner-a');",
    "INSERT INTO public.store_dataset_entry (id, owner) VALUES ('0b', 'seed-owner-b');",
]
BAR_LINES = ["INSERT INTO public.stock_market_activity (close, id) VALUES ('10.5', '1');"]
RENDERED = {
    ENTRY_TABLE: ENTRY_LINES,
    BAR_TABLE: BAR_LINES,
    INSTANCE_TABLE: [
        "INSERT INTO public.ingest_instance (instance_id, superseded_at) VALUES ('seed-instance-a', NULL);"
    ],
    REQUEST_TABLE: ["INSERT INTO public.ingest_request (id, state) VALUES ('5eed', 'PENDING');"],
    RUN_TABLE: ["INSERT INTO public.ingest_run (id, request_id) VALUES ('0r', NULL);"],
    CORPORATE_ACTION_TABLE: [
        "INSERT INTO public.corporate_action (id, vendor_action_id) VALUES ('0c', 'seed-action-a');"
    ],
}
SETVAL_7 = "SELECT pg_catalog.setval('public.stock_market_activity_id_seq', 7, true);\n"
EXPECTED_SQL = ''.join(f'{line}\n' for table in SEED_TABLES for line in RENDERED[table]) + SETVAL_7
COLUMNS = {
    ENTRY_TABLE: ['owner', 'id'],
    BAR_TABLE: ['id', 'close'],
    INSTANCE_TABLE: ['superseded_at', 'instance_id'],
    REQUEST_TABLE: ['state', 'id'],
    RUN_TABLE: ['request_id', 'id'],
    CORPORATE_ACTION_TABLE: ['vendor_action_id', 'id'],
}
# corporate_action's count is also the observation count of the cast refusal (the same statement), so it is the
# cast's EXPECTED_OBSERVATIONS, as ingest_instance's is EXPECTED_INSTANCES and ingest_run's is EXPECTED_RUN_ROWS (the
# 'run rows' check, ten attempt rows and four legacy-shaped runs).
COUNTS = {ENTRY_TABLE: 3, BAR_TABLE: 7, INSTANCE_TABLE: 2, REQUEST_TABLE: 11, RUN_TABLE: 14, CORPORATE_ACTION_TABLE: 3}
DIGESTS = {
    ENTRY_TABLE: {'id': 'i1', 'owner': 'o1'},
    BAR_TABLE: {'close': 'c0', 'id': 'i0'},
    INSTANCE_TABLE: {'instance_id': 'n0', 'superseded_at': 'n1'},
    REQUEST_TABLE: {'id': 'q0', 'state': 'q1'},
    RUN_TABLE: {'id': 'r0', 'request_id': 'r1'},
    CORPORATE_ACTION_TABLE: {'id': 'c0', 'vendor_action_id': 'c1'},
}
# The one query that asks for old four-column bar key collisions.
COLLISION_MARK = 'HAVING count(*) > 1'
# The reset of the three ingest tables and corporate_action (tj-7zjov1.31), the one script besides NORMALISE_SQL.
RESET_SQL = (
    f'DELETE FROM {RUN_TABLE}; DELETE FROM {REQUEST_TABLE}; DELETE FROM {INSTANCE_TABLE}; '
    f'DELETE FROM {CORPORATE_ACTION_TABLE};'
)
DATASET_ID = uuid.UUID('7ceff30a-af80-1760-5057-d346cc334f4d')
# The steps of the ingest-record phase, in order, between the collision check and the rewrite.
# Since tj-7zjov1.31 the ingest reset runs a second time, after the dataset lookup and right before the cast, so the
# seed holds the cast alone and not the POSTs' own request and attempt rows.
RECORD_STEPS = [
    'seed dataset lookup',
    'ingest reset',
    'seed',
    'request states',
    'attempt rows',
    'legacy run states',
    'interrupted runs',
    'run rows',
    'half-attempt rows',
    'instance count',
    'superseded count',
    'observation count',
    'revised action count',
    'synthetic check',
]
# Every async URL a stub seeder was handed by the REAL Stack (the end-to-end tier), per test.
SEEDED_URLS: list = []


class FakeUrl:
    """What FakeStack.async_url() hands the seeder: a handle back onto the fake, never a real URL."""

    def __init__(self, fake):
        self.fake = fake


async def stub_seed_records(url, dataset_id, symbol):
    """The seeder every test runs: records its call on the fake (or the real URL), writes nothing."""
    fake = getattr(url, 'fake', None)
    if fake is None:
        SEEDED_URLS.append((url, dataset_id, symbol))
        return
    fake.calls.append(('seed', 'seed records'))
    fake.seeded_with.append((dataset_id, symbol))
    if fake.seed_error is not None:
        raise fake.seed_error


@pytest.fixture(autouse=True)
def stub_seeder(monkeypatch):
    """produce()'s seeder default is the store's real code (needs Postgres); every call here gets the stub."""
    SEEDED_URLS.clear()
    monkeypatch.setattr(producer_module.produce, '__defaults__', (None, stub_seed_records))


def stored_bar_keys(path: str, body: dict[str, str]) -> list[tuple]:
    """The (asset_symbol, source, granularity, timestamp) of every bar the fake-mode stack stores for one POST.

    FakeRead's grid over the half-open [start, end), per the scenario the symbol's prefix selects.
    Only the three scenarios the seed uses are modelled; anything else is a test bug, not a count.
    """
    symbol = path.rsplit('/', 1)[-1]
    scenario = scenario_for(symbol)
    assert scenario in {Scenario.DEFAULT, Scenario.EMPTY, Scenario.GAPS}, scenario
    if scenario is Scenario.EMPTY:
        return []
    granularity = Granularity(body['granularity'])
    stamps = grid_timestamps(granularity, datetime.fromisoformat(body['start']), datetime.fromisoformat(body['end']))
    return [
        (symbol, body['source'], granularity, stamp)
        for stamp in stamps
        if scenario is not Scenario.GAPS or grid_index(granularity, stamp) % 2 == 0
    ]


def _table_of(sql: str) -> str:
    """The one seed table a per-table query names (the synthetic check, naming them all, is answered first)."""
    for table in (CORPORATE_ACTION_TABLE, INSTANCE_TABLE, REQUEST_TABLE, RUN_TABLE, BAR_TABLE, ENTRY_TABLE):
        if table in sql:
            return table
    raise AssertionError(f'no seed table in: {sql[:80]}')


class FakeStack:
    """Answers produce()'s SQL by its shape, as rows; records ('query'|'script', what), ('post', path), ('seed', ..).

    bodies overrides the reply body of the POST at that index (None: a reply with no body at all);
    statuses overrides the reply status of the POST at that index; collisions overrides the collision
    count, which is otherwise computed from the bars stored; raw_collisions and raw_counts (table ->
    rows) replace the whole result of the collision query and the row-count queries; render, columns
    and digests (table -> rows) replace those results. The ingest-record phase: foreign_records are
    rows the synthetic check sees only after the seeder ran; dataset_rows, state_rows, attempt_rows,
    stray_rows, superseded_rows, revised_rows, legacy_rows and interrupted_rows replace the answers of the queries
    of that phase (the 'run rows' total is the ingest_run row count, raw_counts); seed_error is
    raised by the stub seeder.
    """

    def __init__(
        self,
        revision_rows=None,
        foreign_before=(),
        foreign_after=(),
        post_status=200,
        bodies=None,
        collisions=None,
        statuses=None,
        raw_collisions=None,
        raw_counts=None,
        render=None,
        columns=None,
        digests=None,
        foreign_records=(),
        dataset_rows=None,
        state_rows=None,
        attempt_rows=None,
        stray_rows=None,
        superseded_rows=None,
        revised_rows=None,
        legacy_rows=None,
        interrupted_rows=None,
        seed_error=None,
    ):
        self.revision_rows = [(HEAD,)] if revision_rows is None else revision_rows
        self.rows_before = SYNTHETIC_ROWS + list(foreign_before)
        self.rows_after = SYNTHETIC_ROWS + list(foreign_after)
        self.rows_after_records = self.rows_after + list(foreign_records)
        self.post_status = post_status
        self.bodies = bodies or {}
        self.collisions = collisions
        self.statuses = statuses or {}
        self.raw_collisions = raw_collisions
        self.raw_counts = raw_counts or {}
        self.render = {table: [(line,) for line in lines] for table, lines in RENDERED.items()}
        self.render.update(render or {})
        self.columns = {table: [(name,) for name in names] for table, names in COLUMNS.items()}
        self.columns.update(columns or {})
        self.digests = {table: sorted(pairs.items()) for table, pairs in DIGESTS.items()}
        self.digests.update(digests or {})
        self.dataset_rows = [(str(DATASET_ID),)] if dataset_rows is None else dataset_rows
        self.state_rows = list(EXPECTED_REQUEST_STATES.items()) if state_rows is None else state_rows
        self.attempt_rows = [(EXPECTED_ATTEMPT_ROWS,)] if attempt_rows is None else attempt_rows
        self.stray_rows = [(0,)] if stray_rows is None else stray_rows
        self.superseded_rows = [(EXPECTED_INSTANCES - 1,)] if superseded_rows is None else superseded_rows
        self.revised_rows = [(EXPECTED_REVISED_ACTIONS,)] if revised_rows is None else revised_rows
        self.legacy_rows = list(EXPECTED_LEGACY_STATES.items()) if legacy_rows is None else legacy_rows
        self.interrupted_rows = [(EXPECTED_INTERRUPTED_RUNS,)] if interrupted_rows is None else interrupted_rows
        self.seed_error = seed_error
        self.seeded_with: list[tuple] = []
        self.calls: list[tuple] = []
        self.sql: list[str] = []
        self.posted: list[tuple] = []
        self.stored: list[tuple] = []
        self.replies: list[dict] = []
        self.collision_sql: list[str] = []
        self.synthetic_sql: list[str] = []
        self.dataset_sql: list[str] = []
        self.render_sql: dict[str, str] = {}
        self.closed = False

    # -- the answers, shared by the seam methods and the driver adapter below --

    def answer(self, sql: str) -> list[tuple]:
        if 'alembic_version' in sql:
            return self.revision_rows
        if 'GROUP BY asset_symbol, owner' in sql:
            self.synthetic_sql.append(sql)
            if self.seeded_with:
                return self.rows_after_records
            posted = any(call[0] == 'post' for call in self.calls)
            return self.rows_after if posted else self.rows_before
        # Before the collision mark, which the revised-action query also carries.
        if f'{CORPORATE_ACTION_TABLE} GROUP BY source, vendor_action_id HAVING count(*) > 1' in sql:
            return self.revised_rows
        if COLLISION_MARK in sql:
            self.collision_sql.append(sql)
            if self.raw_collisions is not None:
                return self.raw_collisions
            if self.collisions is not None:
                return [(self.collisions,)]
            return [(sum(1 for rows in Counter(self.stored).values() if rows > 1),)]
        if sql.startswith(f'SELECT id::text FROM {ENTRY_TABLE} '):
            self.dataset_sql.append(sql)
            return self.dataset_rows
        # The legacy-run checks (e5d950e), before the request-state shape, which the first of them also starts with.
        if 'WHERE request_id IS NULL AND attempt_no IS NULL GROUP BY state' in sql:
            return self.legacy_rows
        if "WHERE request_id IS NULL AND error_reason = 'INTERRUPTED'" in sql:
            return self.interrupted_rows
        if sql.startswith('SELECT state::text, count(*)'):
            return self.state_rows
        if 'WHERE request_id IS NOT NULL AND attempt_no IS NOT NULL' in sql:
            return self.attempt_rows
        if 'WHERE (request_id IS NULL) <> (attempt_no IS NULL)' in sql:
            return self.stray_rows
        if 'WHERE superseded_at IS NOT NULL' in sql:
            return self.superseded_rows
        if sql.startswith('SELECT count(*) FROM'):
            table = _table_of(sql)
            return self.raw_counts.get(table, [(COUNTS[table],)])
        if 'information_schema.columns' in sql:
            return self.columns[_table_of(sql)]
        if 'quote_nullable' in sql:
            table = _table_of(sql)
            self.render_sql[table] = sql
            return self.render[table]
        if 'sha256' in sql:
            return self.digests[_table_of(sql)]
        raise AssertionError(f'unexpected SQL: {sql[:80]}')

    def reply(self, path: str, body: dict, secret_header: str) -> dict:
        index = len(self.posted)
        self.calls.append(('post', path))
        self.posted.append((path, body, secret_header))
        bars = stored_bar_keys(path, body)
        self.stored.extend(bars)
        reply = {
            'status': self.statuses.get(index, self.post_status),
            'body': json.dumps({'message': 'Data stored', 'data_points': len(bars)}),
        }
        if index in self.bodies:
            if self.bodies[index] is None:
                del reply['body']
            else:
                reply['body'] = self.bodies[index]
        self.replies.append(reply)
        return reply

    # -- Stack's seam --

    def query(self, sql, what):
        self.calls.append(('query', what))
        self.sql.append(sql)
        return [tuple(row) for row in self.answer(sql)]

    def script(self, sql, what):
        self.calls.append(('script', what))
        self.sql.append(sql)
        assert (sql, what) in {(RESET_SQL, 'ingest reset'), (NORMALISE_SQL, 'normalise')}, 'an unexpected script'

    def post(self, path, body, secret_header):
        return self.reply(path, body, secret_header)

    def async_url(self):
        return FakeUrl(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def kinds(self) -> list[str]:
        return [call[1] if call[0] in ('query', 'script') else call[0] for call in self.calls]


@pytest.fixture
def no_writes(monkeypatch, tmp_path):
    """The producer writes nothing: any file write or mkdir fails the test, and the cwd stays empty."""

    def refuse(*args, **kwargs):
        raise AssertionError('the producer wrote to disk')

    real_open = Path.open

    def read_only_open(self, mode='r', *args, **kwargs):
        if any(flag in mode for flag in 'wax+'):
            refuse()
        return real_open(self, mode, *args, **kwargs)

    for name in ('write_text', 'write_bytes', 'mkdir', 'touch'):
        monkeypatch.setattr(Path, name, refuse)
    monkeypatch.setattr(Path, 'open', read_only_open)
    monkeypatch.chdir(tmp_path)
    yield
    assert not any(tmp_path.iterdir())


# ---------------------------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------------------------


def test_a_run_returns_the_bundle_and_writes_nothing(no_writes):
    stack = FakeStack()
    result = produce(stack, date='2026-09-30')
    assert result.bundle.revision == HEAD
    assert result.row_counts == COUNTS
    assert list(result.row_counts) == list(SEED_TABLES)
    assert result.bundle.sql == EXPECTED_SQL
    manifest = json.loads(result.bundle.manifest)
    assert manifest == {
        'revision': HEAD,
        'producer': 'data.store.seeds',
        'date': '2026-09-30',
        'row_counts': COUNTS,
        'digests': DIGESTS,
    }
    assert result.bundle.manifest.endswith('}\n')


def test_the_steps_run_in_the_designed_order():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    kinds = stack.kinds()
    posts = [i for i, kind in enumerate(kinds) if kind == 'post']
    checks = [i for i, kind in enumerate(kinds) if kind == 'synthetic check']
    assert kinds[0] == 'alembic_version read'
    assert len(checks) == 3
    assert checks[0] < kinds.index('ingest reset') < posts[0], 'refusal, then the ingest reset, then the POSTs'
    # Twice: before the POSTs, and again in the record steps below, right before the cast.
    assert kinds.index('ingest reset') == checks[0] + 1 and kinds.count('ingest reset') == 2
    assert posts[-1] < checks[1] < kinds.index('normalise'), 'and again after the POSTs, before the rewrite'
    assert checks[1] < kinds.index('bar collision check') < kinds.index('normalise'), 'collisions: after, before'
    assert kinds.count('bar collision check') == 1
    collision, normalise = kinds.index('bar collision check'), kinds.index('normalise')
    assert kinds[collision + 1 : normalise] == RECORD_STEPS, 'the cast, its count refusal, a third check'
    assert kinds[normalise:] == [
        'normalise',
        *(f'{table} count' for table in SEED_TABLES),
        *(step for table in SEED_TABLES for step in (f'{table} columns', f'{table} render')),
        *(step for table in SEED_TABLES for step in (f'{table} columns', f'{table} digests')),
    ]


def test_normalise_and_the_ingest_reset_are_the_only_scripts():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert [call for call in stack.calls if call[0] == 'script'] == [
        ('script', 'ingest reset'),
        ('script', 'ingest reset'),
        ('script', 'normalise'),
    ]


def test_the_ingest_reset_empties_exactly_the_three_ingest_tables_children_first():
    """Runs, then requests, then instances, then corporate_action (tj-7zjov1.31), both times.

    Never the entry or bar table, which the scenario rebuilds itself.
    """
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    resets = [sql for sql in stack.sql if sql.startswith('DELETE')]
    assert resets == [RESET_SQL, RESET_SQL]
    (reset,) = set(resets)
    assert ENTRY_TABLE not in reset and BAR_TABLE not in reset and 'TRUNCATE' not in reset


def test_the_default_seeder_is_the_store_code():
    """The stub is this file's; the producer's own default is ingest_records.seed_ingest_records."""
    assert (None, seed_ingest_records) == REAL_DEFAULTS
    assert list(inspect.signature(producer_module.produce).parameters) == ['stack', 'date', 'seed_records']


def test_the_seeder_gets_the_stacks_async_url_and_the_first_scenario_dataset():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert stack.seeded_with == [(DATASET_ID, SEED_REQUESTS[0].symbol)]
    assert isinstance(stack.seeded_with[0][0], uuid.UUID)


def test_the_dataset_lookup_names_the_first_scenario_requests_identity():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    (sql,) = stack.dataset_sql
    first = SEED_REQUESTS[0]
    assert f"asset_symbol = '{first.symbol}'" in sql and f"owner = '{first.owner}'" in sql
    assert f"start = TIMESTAMPTZ '{first.start.isoformat()}'" in sql
    assert f'"end" = TIMESTAMPTZ \'{first.end.isoformat()}\'' in sql


@pytest.mark.parametrize('rows', [[], [('a',), ('b',)]], ids=['none', 'two'])
def test_a_first_dataset_that_is_not_one_entry_refuses_before_the_seeder(no_writes, rows):
    stack = FakeStack(dataset_rows=rows)
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == f"the scenario's first dataset is {len(rows)} entries; expected exactly one"
    assert 'seed' not in stack.kinds() and 'normalise' not in stack.kinds()


def test_a_failing_seeder_propagates_and_nothing_is_rewritten(no_writes):
    """Nothing is caught: a seed is never produced from a half-written cast (ingest_records.py, Raises)."""
    stack = FakeStack(seed_error=RuntimeError('half a cast'))
    with pytest.raises(RuntimeError, match='half a cast'):
        produce(stack)
    assert stack.kinds()[-1] == 'seed'
    assert 'normalise' not in stack.kinds() and 'request states' not in stack.kinds()


def test_each_table_is_rendered_from_its_information_schema_columns():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert stack.render_sql == {table: render_table_sql(table, names) for table, names in COLUMNS.items()}


def test_every_scenario_request_is_posted_once_with_the_header_name():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert [(path, body) for path, body, _ in stack.posted] == [(r.path, r.body()) for r in SEED_REQUESTS]
    assert {header for _, _, header in stack.posted} == {INSTANCE_SECRET_HEADER}


def test_the_sequence_line_follows_the_bar_row_count():
    stack = FakeStack(raw_counts={BAR_TABLE: [(42,)]})
    sql = produce(stack, date='2026-09-30').bundle.sql
    assert sql.endswith("SELECT pg_catalog.setval('public.stock_market_activity_id_seq', 42, true);\n")
    assert sql.count('setval(') == 1


def test_two_runs_on_the_same_answers_give_equal_bundles():
    assert produce(FakeStack(), date='2026-09-30').bundle == produce(FakeStack(), date='2026-09-30').bundle


def test_without_a_date_the_manifest_carries_todays_utc_date():
    before = datetime.now(UTC).date().isoformat()
    result = produce(FakeStack())
    after = datetime.now(UTC).date().isoformat()
    assert json.loads(result.bundle.manifest)['date'] in {before, after}


def test_produce_takes_no_output_directory():
    """S9 (3): the producer writes nothing; where the files go is the bundle writer's business."""
    with pytest.raises(TypeError):
        produce(FakeStack(), Path('out'))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------------------------


def test_a_revision_other_than_the_head_is_refused_before_any_post(no_writes):
    stack = FakeStack(revision_rows=[('8f41c2d7a3b9',)])
    with pytest.raises(SeedRefused, match='migrate first'):
        produce(stack)
    assert stack.kinds() == ['alembic_version read']


@pytest.mark.parametrize('rows', [[], [(HEAD,), (HEAD,)]], ids=['none', 'two'])
def test_alembic_version_with_other_than_one_row_is_refused(no_writes, rows):
    stack = FakeStack(revision_rows=rows)
    with pytest.raises(SeedRefused, match=rf'^alembic_version holds {len(rows)} rows; expected exactly one$'):
        produce(stack)
    assert stack.kinds() == ['alembic_version read']


@pytest.mark.parametrize('rows', [[(HEAD, 'x')], [(None,)], [(12,)], [()]], ids=['two-values', 'null', 'int', 'empty'])
def test_an_alembic_version_row_that_is_not_one_text_value_fails(rows):
    stack = FakeStack(revision_rows=rows)
    with pytest.raises(StackError, match=r'^alembic_version read: the row is not one text value$'):
        produce(stack)
    assert 'post' not in stack.kinds()


@pytest.mark.parametrize(
    'foreign',
    [
        ('e', 'AAPL', 'miguel', 3),  # a real symbol and owner
        ('e', 'ZZSEEDAA', 'miguel', 3),  # a real owner on a synthetic symbol
        ('b', 'AAPL', '', 3),  # a bar alone
        ('e', 'ZZSEEDAA', 'unassigned', 3),  # a migrated pre-owner row
        ('q', 'AAPL', 'seed-owner-a', 3),  # a request for a real symbol
        ('q', 'ZZSEEDAA', 'miguel', 3),  # a request under a real client label
        ('q', '', 'seed-owner-a', 3),  # a request naming no symbol (an asset-list request): coalesced, foreign
        ('r', 'SPY', 'seed-owner-a', 3),  # a run of a real symbol
        ('r', 'ZZSEEDAA', 'scheduler', 3),  # a run under a non-synthetic actor
    ],
)
def test_a_foreign_row_before_the_scenario_refuses_before_any_post(no_writes, foreign):
    stack = FakeStack(foreign_before=[foreign])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == '3 row(s) hold a symbol or owner outside the synthetic patterns; no seed written'
    assert 'post' not in stack.kinds()
    assert 'ingest reset' not in stack.kinds(), 'a database holding a real row is not touched, not even reset'
    assert 'normalise' not in stack.kinds()


def test_the_synthetic_check_covers_the_request_and_run_labels():
    """A request's client_label and a run's actor are the owner; a NULL request symbol reads as foreign."""
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert len(stack.synthetic_sql) == 3 and len(set(stack.synthetic_sql)) == 1
    sql = stack.synthetic_sql[0]
    assert f"SELECT 'q', coalesce(asset_symbol, ''), client_label, count(*) FROM {REQUEST_TABLE} " in sql
    assert 'GROUP BY asset_symbol, client_label' in sql
    assert f"SELECT 'r', asset_symbol, actor, count(*) FROM {RUN_TABLE} GROUP BY asset_symbol, actor" in sql
    assert INSTANCE_TABLE not in sql, 'the instance marker carries no label'


@pytest.mark.parametrize(
    'foreign',
    [('q', 'AAPL', 'seed-owner-a', 4), ('r', 'ZZSEEDAA', 'miguel', 4), ('e', 'SPY', 'seed-owner-a', 4)],
    ids=['request', 'run', 'entry'],
)
def test_a_foreign_row_the_seeder_wrote_refuses_before_the_rewrite(no_writes, foreign):
    """The third check: whatever the cast wrote is checked before anything is rewritten or rendered."""
    stack = FakeStack(foreign_records=[foreign])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == '4 row(s) hold a symbol or owner outside the synthetic patterns; no seed written'
    assert stack.kinds()[-1] == 'synthetic check' and stack.kinds().count('synthetic check') == 3
    assert 'normalise' not in stack.kinds()
    assert foreign[1] not in str(raised.value) and foreign[2] not in str(raised.value)


def test_a_foreign_row_after_the_scenario_refuses_before_the_rewrite(no_writes):
    stack = FakeStack(foreign_after=[('b', 'SPY', '', 5)])
    with pytest.raises(SeedRefused, match=r'^5 row') as raised:
        produce(stack)
    assert stack.kinds().count('post') == len(SEED_REQUESTS)
    assert 'normalise' not in stack.kinds() and f'{BAR_TABLE} render' not in stack.kinds()
    assert 'seed' not in stack.kinds(), 'the cast is never written after a foreign row'
    assert 'SPY' not in str(raised.value)


# ---------------------------------------------------------------------------------------------
# The cast refusal (bead tj-7zjov1.100): counts only, before the rewrite
# ---------------------------------------------------------------------------------------------


def _states(**changes):
    states = dict(EXPECTED_REQUEST_STATES)
    for state, count in changes.items():
        if count is None:
            del states[state]
        else:
            states[state] = count
    return list(states.items())


# The refusal names the counts per state found and expected (enum names and counts, never a value), in name order.
EXPECTED_STATES_TEXT = 'EMPTY=1, FAILED=4, NOT_DELIVERED=1, PENDING=1, QUEUED=1, SUCCEEDED=3'


def _cast_message(found: str) -> str:
    return (
        'the seeded ingest requests are not the expected cast (counts per state differ); '
        f'found {found}, expected {EXPECTED_STATES_TEXT}; no seed written'
    )


CAST_REFUSALS = [
    pytest.param(
        {'state_rows': _states(EMPTY=None)},
        _cast_message('FAILED=4, NOT_DELIVERED=1, PENDING=1, QUEUED=1, SUCCEEDED=3'),
        id='a-state-missing',
    ),
    pytest.param(
        {'state_rows': _states(FAILED=3)},
        _cast_message('EMPTY=1, FAILED=3, NOT_DELIVERED=1, PENDING=1, QUEUED=1, SUCCEEDED=3'),
        id='a-count-short',
    ),
    pytest.param(
        {'state_rows': _states(PENDING=2)},
        _cast_message('EMPTY=1, FAILED=4, NOT_DELIVERED=1, PENDING=2, QUEUED=1, SUCCEEDED=3'),
        id='a-count-over',
    ),
    pytest.param(
        {'state_rows': _states(RUNNING=1)},
        _cast_message('EMPTY=1, FAILED=4, NOT_DELIVERED=1, PENDING=1, QUEUED=1, RUNNING=1, SUCCEEDED=3'),
        id='an-extra-state',
    ),
    pytest.param({'state_rows': []}, _cast_message('none'), id='no-requests'),
    # Text counts render as the same digits: still refused (the comparison is typed); the message cannot show why.
    pytest.param(
        {'state_rows': [(s, str(n)) for s, n in _states()]}, _cast_message(EXPECTED_STATES_TEXT), id='text-counts'
    ),
    pytest.param(
        {'attempt_rows': [(9,)]}, '9 attempt rows were written; expected 10; no seed written', id='attempts-9'
    ),
    pytest.param(
        {'attempt_rows': [(11,)]}, '11 attempt rows were written; expected 10; no seed written', id='attempts-11'
    ),
    pytest.param(
        {'stray_rows': [(1,)]},
        'a run has a request id without an attempt number or the reverse; no seed written',
        id='half-attempt',
    ),
    pytest.param(
        {'raw_counts': {INSTANCE_TABLE: [(1,)]}}, '1 ingest instances are held; expected 2; no seed written', id='one'
    ),
    pytest.param(
        {'raw_counts': {INSTANCE_TABLE: [(3,)]}}, '3 ingest instances are held; expected 2; no seed written', id='three'
    ),
    pytest.param(
        {'superseded_rows': [(0,)]},
        'the instance marker did not supersede exactly the older instance; no seed written',
        id='none-superseded',
    ),
    pytest.param(
        {'superseded_rows': [(2,)]},
        'the instance marker did not supersede exactly the older instance; no seed written',
        id='both-superseded',
    ),
    # e5d950e: the four legacy-shaped runs, by state, the one swept INTERRUPTED, and the run total.
    pytest.param(
        {'legacy_rows': [('EMPTY', 1), ('FAILED', 2)]},
        'the seeded legacy runs are not the expected cast (3 found, 4 expected); counts per state: '
        'found EMPTY=1, FAILED=2, expected EMPTY=1, FAILED=2, SUCCEEDED=1; no seed written',
        id='legacy-state-missing',
    ),
    pytest.param(
        {'legacy_rows': [('EMPTY', 1), ('FAILED', 2), ('SUCCEEDED', 1), ('RUNNING', 1)]},
        'the seeded legacy runs are not the expected cast (5 found, 4 expected); counts per state: '
        'found EMPTY=1, FAILED=2, RUNNING=1, SUCCEEDED=1, expected EMPTY=1, FAILED=2, SUCCEEDED=1; no seed written',
        id='legacy-running-left',
    ),
    pytest.param(
        {'legacy_rows': []},
        'the seeded legacy runs are not the expected cast (0 found, 4 expected); counts per state: '
        'found none, expected EMPTY=1, FAILED=2, SUCCEEDED=1; no seed written',
        id='no-legacy-runs',
    ),
    pytest.param(
        {'interrupted_rows': [(0,)]},
        '0 interrupted legacy runs were written; expected 1; no seed written',
        id='none-interrupted',
    ),
    pytest.param(
        {'interrupted_rows': [(2,)]},
        '2 interrupted legacy runs were written; expected 1; no seed written',
        id='two-interrupted',
    ),
    pytest.param(
        {'raw_counts': {RUN_TABLE: [(13,)]}},
        '13 ingest_run rows were written; expected 14; no seed written',
        id='runs-13',
    ),
    pytest.param(
        {'raw_counts': {RUN_TABLE: [(15,)]}},
        '15 ingest_run rows were written; expected 14; no seed written',
        id='runs-15',
    ),
    # tj-7zjov1.31: the observations of the cast (A, B, A revised) and the one revised action.
    pytest.param(
        {'raw_counts': {CORPORATE_ACTION_TABLE: [(2,)]}},
        '2 corporate-action observations were written; expected 3; no seed written',
        id='observations-2',
    ),
    pytest.param(
        {'raw_counts': {CORPORATE_ACTION_TABLE: [(4,)]}},
        '4 corporate-action observations were written; expected 3; no seed written',
        id='observations-4',
    ),
    pytest.param(
        {'revised_rows': [(0,)]},
        '0 corporate actions hold more than one payload; expected 1; no seed written',
        id='no-revision',
    ),
    pytest.param(
        {'revised_rows': [(2,)]},
        '2 corporate actions hold more than one payload; expected 1; no seed written',
        id='two-revisions',
    ),
]


@pytest.mark.parametrize(('override', 'message'), CAST_REFUSALS)
def test_a_cast_that_differs_is_refused_before_the_rewrite(no_writes, override, message):
    stack = FakeStack(**override)
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == message
    assert 'seed' in stack.kinds()
    assert 'normalise' not in stack.kinds() and stack.kinds().count('synthetic check') == 2


@pytest.mark.parametrize(('override', 'message'), CAST_REFUSALS[:1] + CAST_REFUSALS[6:7])
def test_a_cast_refusal_exits_3_through_the_entry_point(monkeypatch, capsys, override, message):
    monkeypatch.setattr(entry, 'Stack', lambda: FakeStack(**override))
    assert entry.main([]) == entry.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.err == f'seed refused: {message}\n'
    assert captured.out == ''


@pytest.mark.parametrize(
    ('override', 'what'),
    [
        ({'attempt_rows': [('8',)]}, 'attempt rows'),
        ({'stray_rows': []}, 'half-attempt rows'),
        ({'raw_counts': {INSTANCE_TABLE: [(None,)]}}, 'instance count'),
        ({'superseded_rows': [(True,)]}, 'superseded count'),
        ({'raw_counts': {CORPORATE_ACTION_TABLE: [('3',)]}}, 'observation count'),
        ({'revised_rows': [(None,)]}, 'revised action count'),
        ({'interrupted_rows': [('1',)]}, 'interrupted runs'),
        ({'raw_counts': {RUN_TABLE: [(None,)]}}, 'run rows'),
    ],
    ids=['attempts', 'half-attempts', 'instances', 'superseded', 'observations', 'revised', 'interrupted', 'runs'],
)
def test_a_cast_count_that_is_not_one_integer_fails_without_echoing_it(no_writes, override, what):
    with pytest.raises(StackError) as raised:
        produce(FakeStack(**override))
    assert str(raised.value) == COUNT_MESSAGE.format(what=what)


def test_the_cast_queries_count_what_the_refusal_names():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert f'SELECT state::text, count(*) FROM {REQUEST_TABLE} GROUP BY state;' in stack.sql
    assert f'SELECT count(*) FROM {RUN_TABLE} WHERE request_id IS NOT NULL AND attempt_no IS NOT NULL;' in stack.sql
    assert f'SELECT count(*) FROM {RUN_TABLE} WHERE (request_id IS NULL) <> (attempt_no IS NULL);' in stack.sql
    assert f'SELECT count(*) FROM {INSTANCE_TABLE} WHERE superseded_at IS NOT NULL;' in stack.sql
    assert f'SELECT count(*) FROM {CORPORATE_ACTION_TABLE};' in stack.sql
    assert (
        f'SELECT state::text, count(*) FROM {RUN_TABLE} WHERE request_id IS NULL AND attempt_no IS NULL GROUP BY state;'
    ) in stack.sql
    assert f"SELECT count(*) FROM {RUN_TABLE} WHERE request_id IS NULL AND error_reason = 'INTERRUPTED';" in stack.sql
    assert f'SELECT count(*) FROM {RUN_TABLE};' in stack.sql
    assert (
        f'SELECT count(*) FROM (SELECT 1 FROM {CORPORATE_ACTION_TABLE} GROUP BY source, vendor_action_id '
        'HAVING count(*) > 1) AS revised;'
    ) in stack.sql


def test_the_expected_cast_is_the_cast_ingest_records_names():
    """Eleven named requests (two corporate-actions since tj-7zjov1.31); ten attempt rows; two instances.

    One per state but four FAILED and three SUCCEEDED (the bars success and the two corporate-actions requests);
    three observations (A, B, A revised), one action holding two payloads.
    """
    assert sum(EXPECTED_REQUEST_STATES.values()) == len(REQUEST_IDS) == 11
    assert len(set(REQUEST_IDS.values())) == len(REQUEST_IDS)
    assert EXPECTED_REQUEST_STATES['FAILED'] == sum(1 for name in REQUEST_IDS if name.startswith('FAILED_'))
    assert EXPECTED_REQUEST_STATES['SUCCEEDED'] == 1 + sum(1 for name in REQUEST_IDS if name.startswith('CA_'))
    assert EXPECTED_ATTEMPT_ROWS == 10 and EXPECTED_INSTANCES == 2
    assert (EXPECTED_OBSERVATIONS, EXPECTED_REVISED_ACTIONS) == (3, 1)
    # e5d950e: four legacy-shaped runs (one swept INTERRUPTED), so fourteen ingest_run rows in all.
    assert sum(EXPECTED_LEGACY_STATES.values()) == EXPECTED_LEGACY_RUNS == 4
    assert EXPECTED_LEGACY_STATES == {'EMPTY': 1, 'FAILED': 2, 'SUCCEEDED': 1} and EXPECTED_INTERRUPTED_RUNS == 1
    assert EXPECTED_RUN_ROWS == EXPECTED_ATTEMPT_ROWS + EXPECTED_LEGACY_RUNS == 14


def test_the_committed_head_seed_holds_the_cast():
    """A relationship with the seed the producer wrote at the head: the cast alone (tj-7zjov1.31).

    The ingest tables are emptied after the POSTs, so the POSTs' own runs are not in it: the attempt rows and the
    cast's four legacy-shaped runs (e5d950e).
    """
    manifest = json.loads((REPO_ROOT / 'tests' / 'system' / 'seeds' / f'{HEAD}.json').read_text(encoding='utf-8'))
    assert manifest['row_counts'][REQUEST_TABLE] == sum(EXPECTED_REQUEST_STATES.values())
    assert manifest['row_counts'][INSTANCE_TABLE] == EXPECTED_INSTANCES
    assert manifest['row_counts'][RUN_TABLE] == EXPECTED_RUN_ROWS
    assert manifest['row_counts'][CORPORATE_ACTION_TABLE] == EXPECTED_OBSERVATIONS
    sql = (REPO_ROOT / 'tests' / 'system' / 'seeds' / f'{HEAD}.sql').read_text(encoding='utf-8')
    for request_id in REQUEST_IDS.values():
        assert f"'{request_id}'" in sql, request_id


# Rows the check cannot read. Each counts as ONE foreign row, never parsed for its size, and because
# its real size is unknown the message says 'at least' (tj-irhy0a.23 N3): it never understates.
UNREADABLE_ROWS = [
    pytest.param(('e', 'ZZSEEDAA', 'seed-owner-a'), id='three-values'),
    pytest.param(('e', 'ZZSEED', 'AA', 'seed-owner-a', 2), id='five-values'),
    pytest.param(('b', None, '', 4), id='null-symbol'),
    pytest.param(('e', 'ZZSEEDAA', None, 4), id='null-owner'),
    pytest.param(('e', 7, 'seed-owner-a', 4), id='non-text-symbol'),
]


@pytest.mark.parametrize('row', UNREADABLE_ROWS)
def test_a_row_the_check_cannot_read_is_one_foreign_row(no_writes, row):
    stack = FakeStack(foreign_before=[row])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == ONE_FOREIGN_ROW
    assert 'post' not in stack.kinds()


def test_readable_and_unreadable_foreign_rows_add_up_to_a_lower_bound(no_writes):
    """3 readable foreign rows plus one unreadable group: 'at least 4', never '3' or a guessed size."""
    stack = FakeStack(foreign_before=[('e', 'AAPL', 'miguel', 3), ('b', None, '', 40)])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == AT_LEAST.format(n=4)


def test_readable_foreign_rows_alone_are_an_exact_count(no_writes):
    """'at least' only when an unreadable group was counted: readable counts are exact."""
    stack = FakeStack(foreign_before=[('e', 'AAPL', 'miguel', 3), ('b', 'SPY', '', 2)])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == '5 row(s) hold a symbol or owner outside the synthetic patterns; no seed written'


NOT_A_COUNT = [
    pytest.param('17', id='text'),
    pytest.param(chr(0x00B9) + chr(0x2077), id='superscript-17'),
    pytest.param(chr(0x0661) + chr(0x0667), id='arabic-indic-17'),
    pytest.param(chr(0xFF11) + chr(0xFF17), id='fullwidth-17'),
    pytest.param(17.0, id='float'),
    pytest.param(Decimal(17), id='decimal'),
    pytest.param(True, id='bool'),
    pytest.param(-1, id='negative'),
    pytest.param(None, id='null'),
]
COUNT_ROWS = [
    pytest.param(('e', 'AAPL', 'miguel'), id='real-entry'),
    pytest.param(('e', 'ZZSEEDAA', 'seed-owner-a'), id='synthetic-entry'),
    pytest.param(('b', 'ZZSEEDAA', ''), id='synthetic-bar'),
]
AT_LEAST = 'at least {n} row(s) hold a symbol or owner outside the synthetic patterns; no seed written'
ONE_FOREIGN_ROW = AT_LEAST.format(n=1)


@pytest.mark.parametrize('count', NOT_A_COUNT)
@pytest.mark.parametrize('row', COUNT_ROWS)
def test_a_count_that_is_not_a_non_negative_int_before_the_scenario_is_one_foreign_row(no_writes, row, count):
    stack = FakeStack(foreign_before=[(*row, count)])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == ONE_FOREIGN_ROW
    assert 'post' not in stack.kinds()
    assert 'normalise' not in stack.kinds()


@pytest.mark.parametrize('count', NOT_A_COUNT)
@pytest.mark.parametrize('row', COUNT_ROWS)
def test_a_count_that_is_not_a_non_negative_int_after_the_scenario_is_one_foreign_row(no_writes, row, count):
    stack = FakeStack(foreign_after=[(*row, count)])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == ONE_FOREIGN_ROW
    assert stack.kinds().count('post') == len(SEED_REQUESTS)
    assert 'bar collision check' not in stack.kinds()
    assert 'normalise' not in stack.kinds()


@pytest.mark.parametrize('count', NOT_A_COUNT)
def test_a_count_that_is_not_a_non_negative_int_exits_3_through_the_entry_point(monkeypatch, capsys, count):
    monkeypatch.setattr(entry, 'Stack', lambda: FakeStack(foreign_before=[('e', 'AAPL', 'miguel', count)]))
    assert entry.main([]) == entry.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.err == f'seed refused: {ONE_FOREIGN_ROW}\n'
    assert captured.out == ''


def test_a_post_that_is_not_200_refuses_and_stops(no_writes):
    stack = FakeStack(post_status=409)
    with pytest.raises(SeedRefused, match='answered 409'):
        produce(stack)
    assert stack.kinds().count('post') == 1
    assert 'normalise' not in stack.kinds()


def test_a_stack_failure_propagates_and_prints_nothing(monkeypatch, capsys):
    class Failing(FakeStack):
        def query(self, sql, what):
            if what.endswith('render'):
                raise StackError(f'{what} failed (SQLSTATE 42P01)')
            return super().query(sql, what)

    monkeypatch.setattr(entry, 'Stack', Failing)
    assert entry.main([]) == entry.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.err == f'seed failed: {ENTRY_TABLE} render failed (SQLSTATE 42P01)\n'
    assert captured.out == ''


@pytest.mark.parametrize('table', SEED_TABLES)
def test_a_backslash_line_in_the_render_fails_and_prints_nothing(monkeypatch, capsys, table):
    render = {table: [("INSERT INTO t (a) VALUES (E'x\n\\connect other');",)]}
    stack = FakeStack(render=render)
    with pytest.raises(DumpRefused):
        produce(stack)
    monkeypatch.setattr(entry, 'Stack', lambda: FakeStack(render=render))
    assert entry.main([]) == entry.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.err == 'seed failed: 1 line(s) starting with a backslash in the dump\n'
    assert captured.out == ''


# ---------------------------------------------------------------------------------------------
# Query results of the wrong shape (the driver's rows replace psql text)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('override', 'message'),
    [
        ({'columns': {ENTRY_TABLE: []}}, f'{ENTRY_TABLE} columns: the query did not return column names'),
        ({'columns': {BAR_TABLE: [('id',), (None,)]}}, f'{BAR_TABLE} columns: the query did not return column names'),
        ({'render': {ENTRY_TABLE: [(None,)]}}, f'{ENTRY_TABLE} render: a row is not one text value'),
        ({'render': {BAR_TABLE: [('a', 'b')]}}, f'{BAR_TABLE} render: a row is not one text value'),
        ({'digests': {ENTRY_TABLE: [('id',)]}}, f'{ENTRY_TABLE} digests: a row is not a (column, digest) pair'),
        ({'digests': {BAR_TABLE: [('id', None)]}}, f'{BAR_TABLE} digests: a row is not a (column, digest) pair'),
    ],
    ids=['no-columns', 'null-column', 'null-line', 'two-values', 'short-digest', 'null-digest'],
)
def test_a_result_of_the_wrong_shape_fails_without_echoing_it(no_writes, override, message):
    with pytest.raises(StackError) as raised:
        produce(FakeStack(**override))
    assert str(raised.value) == message


# ---------------------------------------------------------------------------------------------
# The stored-count and collision refusals (tj-o36zj2; decision tj-vhboky.55 addendum S3)
# ---------------------------------------------------------------------------------------------


EMPTY_INDEX = next(i for i, request in enumerate(SEED_REQUESTS) if request.symbol.startswith(EMPTY_PREFIX))
NON_EMPTY_INDICES = [i for i in range(len(SEED_REQUESTS)) if i != EMPTY_INDEX]


def _assert_refused_at(stack: FakeStack, index: int) -> None:
    """Refused on the POST at index: no later POST, no collision check, no rewrite."""
    assert stack.kinds().count('post') == index + 1
    assert 'bar collision check' not in stack.kinds()
    assert 'normalise' not in stack.kinds()


def test_the_fake_mode_scenario_stores_bars_everywhere_but_empty_and_collides():
    """The model the other tests stand on: the happy path is the scenario as the fake stores it."""
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    counts = [json.loads(reply['body'])['data_points'] for reply in stack.replies]
    assert counts[EMPTY_INDEX] == 0
    assert all(counts[i] > 0 for i in NON_EMPTY_INDICES)
    assert stack.answer(stack.collision_sql[0])[0][0] > 0


@pytest.mark.parametrize(
    'body',
    [
        pytest.param('{"message": "Data stored"}', id='missing'),
        pytest.param('{"data_points": "12"}', id='string'),
        pytest.param('{"data_points": 12.0}', id='float'),
        pytest.param('{"data_points": true}', id='bool'),
        pytest.param('{"data_points": false}', id='bool-false'),
        pytest.param('{"data_points": null}', id='null'),
        pytest.param('{"data_points": -1}', id='negative'),
        pytest.param('Internal Server Error', id='not-json'),
        pytest.param('', id='empty'),
        pytest.param('[12]', id='json-list'),
        pytest.param('"12"', id='json-string'),
        pytest.param('12', id='json-number'),
        pytest.param(None, id='no-body'),
    ],
)
@pytest.mark.parametrize('index', [0, EMPTY_INDEX])
def test_a_reply_without_a_valid_data_points_count_refuses_and_stops(no_writes, body, index):
    stack = FakeStack(bodies={index: body})
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    path = re.escape(SEED_REQUESTS[index].path)
    assert re.fullmatch(rf'request {index} \({path}\): the reply carries no valid data_points count', str(raised.value))
    _assert_refused_at(stack, index)


@pytest.mark.parametrize('index', NON_EMPTY_INDICES)
def test_a_symbol_without_the_empty_prefix_that_stored_no_bars_refuses(no_writes, index):
    """The 200 a stack not in fake mode gives: ingest returned no batch, so data_store stored nothing."""
    stack = FakeStack(bodies={index: '{"message": "Data stored", "data_points": 0}'})
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    path = re.escape(SEED_REQUESTS[index].path)
    assert re.fullmatch(rf'request {index} \({path}\): stored 0 bars; .*', str(raised.value))
    _assert_refused_at(stack, index)


@pytest.mark.parametrize('stored', [1, 31])
def test_an_empty_symbol_that_stored_bars_refuses(no_writes, stored):
    """A fake that ignored the EMPTY_ prefix: the stack is not the one the scenario assumes."""
    stack = FakeStack(bodies={EMPTY_INDEX: json.dumps({'data_points': stored})})
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    path = re.escape(SEED_REQUESTS[EMPTY_INDEX].path)
    assert re.fullmatch(
        rf'request {EMPTY_INDEX} \({path}\): an {EMPTY_PREFIX} symbol stored {stored} bar\(s\); .*', str(raised.value)
    )
    _assert_refused_at(stack, EMPTY_INDEX)


def test_a_count_refusal_names_no_value_from_the_reply():
    """Counts, indices and paths only: whatever else the reply carries stays out of the message."""
    body = json.dumps({'message': 'AAPL owner miguel', 'data_points': 'AAPL'})
    with pytest.raises(SeedRefused) as raised:
        produce(FakeStack(bodies={0: body}))
    for value in ('AAPL', 'miguel', 'Data stored'):
        assert value not in str(raised.value)


def test_no_bar_collision_refuses_before_the_rewrite(no_writes):
    stack = FakeStack(collisions=0)
    with pytest.raises(SeedRefused, match=r'^no bar shares an \(asset_symbol, source, granularity, timestamp\) key'):
        produce(stack)
    assert stack.kinds().count('post') == len(SEED_REQUESTS)
    assert stack.kinds()[-1] == 'bar collision check'
    assert 'normalise' not in stack.kinds()


def test_the_collision_query_groups_the_bar_table_by_the_old_four_column_key():
    """The key is the property (E4): a fifth column in the GROUP BY would count no collision at all."""
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    (sql,) = stack.collision_sql
    assert f'FROM {BAR_TABLE} GROUP BY asset_symbol, source, granularity, timestamp HAVING count(*) > 1)' in sql
    assert sql.startswith('SELECT count(*) FROM (SELECT 1 FROM ')


@pytest.mark.parametrize(
    ('stack', 'reason'),
    [
        (lambda: FakeStack(bodies={0: '{}'}), 'request 0 '),
        (lambda: FakeStack(bodies={EMPTY_INDEX: '{"data_points": 2}'}), f'request {EMPTY_INDEX} '),
        (lambda: FakeStack(collisions=0), 'no bar shares'),
        (lambda: FakeStack(revision_rows=[]), 'alembic_version holds 0 rows'),
        (lambda: FakeStack(revision_rows=[('8f41c2d7a3b9',)]), 'the database is at revision'),
        (lambda: FakeStack(foreign_after=[('b', 'SPY', '', 5)]), '5 row(s)'),
    ],
    ids=['no-count', 'empty-stored', 'no-collision', 'no-revision', 'old-revision', 'foreign-after'],
)
def test_every_refusal_exits_3_through_the_entry_point_with_nothing_on_stdout(monkeypatch, capsys, stack, reason):
    monkeypatch.setattr(entry, 'Stack', stack)
    assert entry.main([]) == 3
    captured = capsys.readouterr()
    assert captured.err.startswith(f'seed refused: {reason}')
    assert captured.out == ''


# ---------------------------------------------------------------------------------------------
# Count queries (tj-h8gg2m; decision tj-vhboky.55 addendum S3: counts, indices and paths only)
# ---------------------------------------------------------------------------------------------


# Results a count query must not be read from. A leaked value is what the message must not carry,
# so each case is distinctive enough to find in the message if it were echoed.
BAD_COUNT_ROWS = [
    pytest.param([('AAPL owner miguel',)], id='text'),
    pytest.param([('17',)], id='digit-text'),
    pytest.param([(chr(0x0661) + chr(0x0667),)], id='non-ascii-digits'),
    pytest.param([(-17,)], id='negative'),
    pytest.param([], id='no-row'),
    pytest.param([()], id='empty-row'),
    pytest.param([(17,), (23,)], id='two-rows'),
    pytest.param([(17, 23)], id='two-values'),
    pytest.param([(17.0,)], id='float'),
    pytest.param([(Decimal(17),)], id='decimal'),
    pytest.param([(True,)], id='bool'),
    pytest.param([(None,)], id='null'),
]
COUNT_MESSAGE = '{what}: the query did not return a single non-negative integer'


@pytest.mark.parametrize('rows', BAD_COUNT_ROWS)
def test_a_collision_count_that_is_not_one_integer_fails_without_echoing_it(no_writes, rows):
    stack = FakeStack(raw_collisions=rows)
    with pytest.raises(StackError) as raised:
        produce(stack)
    assert str(raised.value) == COUNT_MESSAGE.format(what='bar collision check')
    assert stack.kinds()[-1] == 'bar collision check'
    assert 'normalise' not in stack.kinds()


@pytest.mark.parametrize('table', [ENTRY_TABLE, BAR_TABLE])
@pytest.mark.parametrize('rows', BAD_COUNT_ROWS)
def test_a_row_count_that_is_not_one_integer_fails_without_echoing_it(no_writes, table, rows):
    stack = FakeStack(raw_counts={table: rows})
    with pytest.raises(StackError) as raised:
        produce(stack)
    assert str(raised.value) == COUNT_MESSAGE.format(what=f'{table} count')
    assert f'{ENTRY_TABLE} render' not in stack.kinds()


def test_a_zero_collision_count_is_a_refusal_not_a_parse_failure():
    """0 is a valid count: it reaches the collision refusal (exit 3), not the parse failure (exit 1)."""
    with pytest.raises(SeedRefused, match=r'^no bar shares'):
        produce(FakeStack(raw_collisions=[(0,)]))


@pytest.mark.parametrize(
    ('stack', 'what'),
    [
        (lambda: FakeStack(raw_collisions=[('AAPL owner miguel',)]), 'bar collision check'),
        (lambda: FakeStack(raw_counts={BAR_TABLE: [('AAPL owner miguel',)]}), f'{BAR_TABLE} count'),
    ],
)
def test_a_count_failure_exits_1_through_the_entry_point_without_the_result(monkeypatch, capsys, stack, what):
    monkeypatch.setattr(entry, 'Stack', stack)
    assert entry.main([]) == entry.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.err == f'seed failed: {COUNT_MESSAGE.format(what=what)}\n'
    assert captured.out == ''


LEAKY_BODY = json.dumps({'detail': 'AAPL owner miguel rejected', 'data_points': 0})


@pytest.mark.parametrize('status', [409, 500, 503])
@pytest.mark.parametrize('index', [0, EMPTY_INDEX, len(SEED_REQUESTS) - 1])
def test_a_non_200_message_carries_the_index_path_and_status_only(no_writes, index, status):
    stack = FakeStack(statuses={index: status}, bodies={index: LEAKY_BODY})
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == f'request {index} (POST {SEED_REQUESTS[index].path}) answered {status}'
    _assert_refused_at(stack, index)


def test_a_non_200_refusal_exits_3_through_the_entry_point_without_the_body(monkeypatch, capsys):
    last = len(SEED_REQUESTS) - 1
    monkeypatch.setattr(entry, 'Stack', lambda: FakeStack(statuses={last: 502}, bodies={last: LEAKY_BODY}))
    assert entry.main([]) == entry.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.err == f'seed refused: request {last} (POST {SEED_REQUESTS[last].path}) answered 502\n'
    assert captured.out == ''


# ---------------------------------------------------------------------------------------------
# The bundle's own refusals, raised through produce() (bundle.py; S9 (3))
# ---------------------------------------------------------------------------------------------


def test_a_bundle_over_the_cap_is_refused_and_nothing_is_printed(monkeypatch, capsys):
    render = {
        BAR_TABLE: [("INSERT INTO public.stock_market_activity (close) VALUES ('" + 'x' * MAX_BUNDLE_BYTES + "');",)]
    }
    with pytest.raises(SeedRefused, match=rf'^the bundle is over the {MAX_BUNDLE_BYTES}-byte cap$'):
        produce(FakeStack(render=render))
    monkeypatch.setattr(entry, 'Stack', lambda: FakeStack(render=render))
    assert entry.main([]) == entry.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == f'seed refused: the bundle is over the {MAX_BUNDLE_BYTES}-byte cap\n'


def test_a_revision_that_is_not_twelve_hex_is_refused(monkeypatch):
    """The revision format check (builder's call): the chain's own ids are pinned in test_seed_bundle.py."""
    monkeypatch.setattr(producer_module, 'head_revision', lambda versions_dir: 'Not_A_Hex_Id')
    with pytest.raises(SeedRefused, match=r'^the revision is not 12 lower-case hex characters$'):
        produce(FakeStack(revision_rows=[('Not_A_Hex_Id',)]))


def test_no_rows_at_all_is_refused_as_an_empty_seed():
    """Unreachable past the collision check today; an empty .sql still cannot become a bundle."""
    stack = FakeStack(
        render={table: [] for table in SEED_TABLES},
        # The instance, run and observation counts are also the cast refusal's (the same statements), so they stay.
        raw_counts={
            table: [(0,)] for table in SEED_TABLES if table not in (INSTANCE_TABLE, RUN_TABLE, CORPORATE_ACTION_TABLE)
        },
    )
    with pytest.raises(SeedRefused, match='exactly one newline'):
        produce(stack)


# ---------------------------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------------------------


def test_success_prints_exactly_one_stdout_line_the_bundle(monkeypatch, capsys):
    stack = FakeStack()
    monkeypatch.setattr(entry, 'Stack', lambda: stack)
    assert entry.main(['--date', '2026-09-30']) == 0
    captured = capsys.readouterr()
    assert captured.out.count('\n') == 1 and captured.out.endswith('\n')
    line = json.loads(captured.out)
    assert set(line) == {'bundle', 'revision', 'sql', 'manifest'} and line['bundle'] == BUNDLE_TAG
    assert parse_bundle(captured.out) == produce(FakeStack(), date='2026-09-30').bundle
    counts = ', '.join(f'{table}={COUNTS[table]}' for table in SEED_TABLES)
    assert captured.err == f'seed produced for revision {HEAD} ({counts})\n'
    assert stack.closed, 'the connection is closed on the way out'


def test_the_stack_is_built_from_the_environment_with_no_arguments(monkeypatch):
    seen = []

    def build(*args, **kwargs):
        seen.append((args, kwargs))
        return FakeStack(collisions=0)

    monkeypatch.setattr(entry, 'Stack', build)
    entry.main([])
    assert seen == [((), {})]


def test_the_date_is_handed_through(monkeypatch):
    seen = {}

    def fake_produce(stack, date=None):
        seen['date'] = date
        raise SeedRefused('stop here')

    monkeypatch.setattr(entry, 'Stack', FakeStack)
    monkeypatch.setattr(entry, 'produce', fake_produce)
    assert entry.main(['--date', '2026-09-30']) == entry.EXIT_REFUSED
    assert seen == {'date': '2026-09-30'}


# --date (tj-irhy0a.23 N7): make seed-dump forwards DATE unvalidated, so the producer checks it before
# anything connects. Each value either breaks the YYYY-MM-DD shape in ASCII digits or names no real day.
# Several are ones date.fromisoformat alone would ACCEPT (basic format, ISO week, non-ASCII digits),
# which is why the shape check exists beside it.
BAD_DATES = [
    pytest.param('2026-02-30', id='february-30'),
    pytest.param('2026-02-29', id='not-a-leap-year'),
    pytest.param('2026-13-01', id='month-13'),
    pytest.param('2026-00-10', id='month-0'),
    pytest.param('2026-09-00', id='day-0'),
    pytest.param('2026-09-31', id='september-31'),
    pytest.param('26-09-30', id='two-digit-year'),
    pytest.param('2026-9-30', id='one-digit-month'),
    pytest.param('2026/09/30', id='slashes'),
    pytest.param('20260930', id='iso-basic'),
    pytest.param('2026-W40-3', id='iso-week'),
    pytest.param('2026-09-30T00:00', id='datetime'),
    pytest.param(' 2026-09-30', id='leading-space'),
    pytest.param('2026-09-30\n', id='trailing-newline'),
    pytest.param(chr(0xFF12) + '026-09-30', id='fullwidth-digit'),
    pytest.param(chr(0x0662) + '026-09-30', id='arabic-indic-digit'),
    pytest.param('', id='empty'),
    pytest.param('DATESENTINEL', id='text'),
]
DATE_REFUSAL = 'seed failed: --date is not a real YYYY-MM-DD date\n'


@pytest.mark.parametrize('value', BAD_DATES)
def test_a_bad_date_exits_1_before_anything_connects_and_prints_nothing(monkeypatch, capsys, value):
    built = []
    monkeypatch.setattr(entry, 'Stack', lambda: built.append('stack') or FakeStack())
    assert entry.main(['--date', value]) == entry.EXIT_FAILED == 1
    captured = capsys.readouterr()
    assert built == [], 'the Stack is never built for a bad date'
    assert captured.out == ''
    assert captured.err == DATE_REFUSAL, 'names the flag, never the value'


@pytest.mark.parametrize('value', ['2024-02-29', '2026-01-01', '2026-12-31', '0001-01-01', '9999-12-31'])
def test_a_real_date_reaches_the_producer(monkeypatch, value):
    seen = {}

    def fake_produce(stack, date=None):
        seen['date'] = date
        raise SeedRefused('stop here')

    monkeypatch.setattr(entry, 'Stack', FakeStack)
    monkeypatch.setattr(entry, 'produce', fake_produce)
    assert entry.main(['--date', value]) == entry.EXIT_REFUSED
    assert seen == {'date': value}


@pytest.mark.parametrize(
    'argv',
    [['--out', 'o'], ['--compose-project', 'p'], ['--compose-file', 'f'], ['--env-file', 'e'], ['--allow-tests-dir']],
)
def test_the_removed_options_are_refused(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        entry.main(argv)
    assert raised.value.code == 2
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize(
    ('error', 'status'),
    [(StackError('x'), 1), (DumpRefused('x'), 1), (SeedRefused('x'), 3)],
    ids=['stack', 'dump', 'refused'],
)
def test_the_entry_point_exit_status(monkeypatch, capsys, error, status):
    def fake_produce(*args, **kwargs):
        raise error

    stack = FakeStack()
    monkeypatch.setattr(entry, 'Stack', lambda: stack)
    monkeypatch.setattr(entry, 'produce', fake_produce)
    assert entry.main([]) == status
    captured = capsys.readouterr()
    assert captured.err.startswith('seed ') and captured.out == ''
    assert stack.closed


def test_the_exit_lists_in_the_docstring_match_produces_raises():
    """S9 (4): the entry point's exit-3 and exit-1 lists stay identical in content to produce()'s Raises."""
    doc = ' '.join(entry.__doc__.split())
    raises = ' '.join(produce.__doc__.split())
    for refusal in (
        'revision mismatch',
        'alembic_version that does not hold exactly one row',
        'non-synthetic row',
        'failed POST',
        'data_points does not match its symbol',
        'collision',
        'bundle over the size cap',
    ):
        assert refusal in doc and refusal in raises, refusal
    for failure in ('a count that is not one non-negative integer', 'backslash'):
        assert failure in doc and failure in raises, failure


# ---------------------------------------------------------------------------------------------
# END TO END through the real Stack: the secret and the password never leave (S9 (1))
# ---------------------------------------------------------------------------------------------


PASSWORD = 'PWSENTINEL-9f3c'
SECRET = 'SECRETSENTINEL-41ad'
STORE_URL = 'http://data-store:8000'
ENV = {
    'DATABASE_NAME': 'store-db-host',
    'DATABASE_PORT': '5432',
    'POSTGRES_USER': 'seed-user',
    'POSTGRES_PASS': PASSWORD,
    'POSTGRES_DB_NAME': 'seed-db',
    'SYSTEM_TEST_DATA_STORE_URL': STORE_URL,
    'INSTANCE_WRITE_SECRET': SECRET,
}


class _Cursor:
    def __init__(self, driver):
        self.driver = driver
        self.rows: list = []

    def execute(self, sql):
        self.driver.fake.sql.append(sql)
        if self.driver.fail_on and self.driver.fail_on in sql:
            raise self.driver.fail_with
        self.rows = [] if sql in (NORMALISE_SQL, RESET_SQL) else self.driver.fake.answer(sql)

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Connection:
    def __init__(self, driver):
        self.driver = driver
        self.autocommit = False

    def cursor(self):
        return _Cursor(self.driver)

    def close(self):
        self.driver.closed = True


class Driver:
    """psycopg2.connect onto a FakeStack's answers; fail_on (an SQL fragment) raises fail_with there."""

    def __init__(self, fake, fail_on=None, fail_with=None, connect_error=None):
        self.fake, self.fail_on, self.fail_with, self.connect_error = fake, fail_on, fail_with, connect_error
        self.kwargs: list[dict] = []
        self.closed = False

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        if self.connect_error is not None:
            raise self.connect_error
        return _Connection(self)


class _Response:
    def __init__(self, reply):
        self.status_code = reply['status']
        self.text = reply.get('body', '')


class Http:
    """httpx.post onto a FakeStack's replies; records every call."""

    def __init__(self, fake, fail_with=None):
        self.fake, self.fail_with = fake, fail_with
        self.calls: list[dict] = []

    def __call__(self, url, json, headers, timeout):
        self.calls.append({'url': url, 'json': json, 'headers': headers, 'timeout': timeout})
        if self.fail_with is not None:
            raise self.fail_with
        (header,) = headers
        return _Response(self.fake.reply(url.removeprefix(STORE_URL), json, header))


class SqlStateError(psycopg2.Error):
    pgcode = '42501'


def _run(monkeypatch, capsys, fake=None, env=None, **driver):
    fake = fake or FakeStack()
    http = Http(fake, driver.pop('http_error', None))
    database = Driver(fake, **driver)
    monkeypatch.setattr(entry, 'Stack', lambda: Stack(ENV if env is None else env, database, http))
    status = entry.main(['--date', '2026-09-30'])
    captured = capsys.readouterr()
    return status, captured, fake, database, http


def _assert_never_leaked(captured, fake, http):
    for value in (SECRET, PASSWORD):
        assert value not in captured.out, value
        assert value not in captured.err, value
        for sql in fake.sql:
            assert value not in sql, value
    for call in http.calls:
        assert call['headers'] == {INSTANCE_SECRET_HEADER: SECRET}
        rest = {key: value for key, value in call.items() if key != 'headers'}
        assert SECRET not in repr(rest) and PASSWORD not in repr(rest)


def test_end_to_end_the_bundle_is_printed_and_nothing_secret_leaves(monkeypatch, capsys):
    status, captured, fake, database, http = _run(monkeypatch, capsys)
    assert status == 0
    assert parse_bundle(captured.out).revision == HEAD
    assert len(http.calls) == len(SEED_REQUESTS)
    assert database.kwargs[0]['password'] == PASSWORD and len(database.kwargs) == 1
    assert database.closed
    _assert_never_leaked(captured, fake, http)


def test_end_to_end_the_seeder_gets_the_real_async_url_and_it_never_prints_the_password(monkeypatch, capsys):
    status, captured, fake, _, http = _run(monkeypatch, capsys)
    assert status == 0
    ((url, dataset_id, symbol),) = SEEDED_URLS
    assert (url.drivername, url.host, url.port, url.database) == (
        'postgresql+asyncpg',
        'store-db-host',
        5432,
        'seed-db',
    )
    assert (url.username, url.password) == ('seed-user', PASSWORD)
    assert (dataset_id, symbol) == (DATASET_ID, SEED_REQUESTS[0].symbol)
    assert PASSWORD not in repr(url) and PASSWORD not in str(url)
    _assert_never_leaked(captured, fake, http)


@pytest.mark.parametrize(
    ('driver', 'status', 'err'),
    [
        (
            {'connect_error': psycopg2.OperationalError(f'password authentication failed: {PASSWORD}')},
            1,
            'seed failed: alembic_version read: could not connect (OperationalError)\n',
        ),
        (
            {'fail_on': 'GROUP BY asset_symbol, owner', 'fail_with': SqlStateError(f'AAPL miguel {PASSWORD} {SECRET}')},
            1,
            'seed failed: synthetic check failed (SQLSTATE 42501)\n',
        ),
        (
            {'fail_on': 'quote_nullable', 'fail_with': SqlStateError(f'{PASSWORD} {SECRET}')},
            1,
            f'seed failed: {ENTRY_TABLE} render failed (SQLSTATE 42501)\n',
        ),
        (
            {'http_error': httpx.ConnectError(f'refused with header {SECRET}')},
            1,
            f'seed failed: POST {SEED_REQUESTS[0].path} failed (ConnectError)\n',
        ),
    ],
    ids=['connect', 'synthetic-check', 'render', 'post'],
)
def test_end_to_end_a_failure_prints_step_and_class_only(monkeypatch, capsys, driver, status, err):
    code, captured, fake, _, http = _run(monkeypatch, capsys, **driver)
    assert code == status
    assert captured.err == err
    assert captured.out == ''
    _assert_never_leaked(captured, fake, http)


def test_end_to_end_a_non_200_whose_body_echoes_the_secret_prints_the_status_only(monkeypatch, capsys):
    fake = FakeStack(statuses={0: 401}, bodies={0: json.dumps({'detail': f'bad secret {SECRET}'})})
    status, captured, fake, _, http = _run(monkeypatch, capsys, fake=fake)
    assert status == entry.EXIT_REFUSED
    assert captured.err == f'seed refused: request 0 (POST {SEED_REQUESTS[0].path}) answered 401\n'
    assert captured.out == ''
    _assert_never_leaked(captured, fake, http)


@pytest.mark.parametrize('name', sorted(ENV))
def test_end_to_end_a_missing_setting_exits_1_naming_it(monkeypatch, capsys, name):
    env = {key: value for key, value in ENV.items() if key != name}
    status, captured, fake, database, http = _run(monkeypatch, capsys, env=env)
    assert status == entry.EXIT_FAILED
    assert captured.err == f'seed failed: the environment variable {name} is missing or empty\n'
    assert captured.out == ''
    assert database.kwargs == [] and http.calls == []
    _assert_never_leaked(captured, fake, http)


def test_end_to_end_with_the_real_process_environment(monkeypatch, capsys):
    """entry.Stack is the real class here: it reads os.environ, and a gap there is exit 1, not a traceback."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    assert entry.Stack is Stack
    assert entry.main([]) == entry.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.err == 'seed failed: the environment variable DATABASE_NAME is missing or empty\n'
    assert captured.out == ''
