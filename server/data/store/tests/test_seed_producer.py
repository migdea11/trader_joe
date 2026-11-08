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

THE STORE'S OWN LOOPS (bead tj-7zjov1.166). The live store's instance watcher can rewrite the cast between the checks
and the dump. FakeStack models it: a drift is a write landing just before a named statement of a named attempt, and a
write made while the dump snapshot is open is held until it closes (REPEATABLE READ). The section of that name pins
the per-request and per-instance refusals, the checks and the dump sharing one snapshot, the whole-cast retry and the
rollback. Whether the snapshot really isolates on Postgres is proved on the stack only (the orchestrator's repeated
producer runs right after a fresh test_up, byte-compared with the committed seed).
"""

import copy
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
from data.store.app.daily_refresh import SCHEDULER_ACTOR
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
    SNAPSHOT_EVENT_TABLE,
    SNAPSHOT_ROW_TABLE,
    SNAPSHOT_TABLE,
    DumpRefused,
    render_table_sql,
)
from data.store.seeds.ingest_records import (
    _AL_ASSETS,
    EXPECTED_ASSET_LIST_REQUESTS,
    EXPECTED_ATTEMPT_ROWS,
    EXPECTED_EMPTY_SNAPSHOTS,
    EXPECTED_EVENTS,
    EXPECTED_INSTANCE_SUPERSEDED,
    EXPECTED_INSTANCES,
    EXPECTED_INTERRUPTED_RUNS,
    EXPECTED_LEGACY_RUNS,
    EXPECTED_LEGACY_STATES,
    EXPECTED_OBSERVATIONS,
    EXPECTED_REQUEST_STATES,
    EXPECTED_REQUESTS,
    EXPECTED_REVISED_ACTIONS,
    EXPECTED_RUN_ROWS,
    EXPECTED_SNAPSHOT_ROWS,
    EXPECTED_SNAPSHOTS,
    INSTANCE_A,
    INSTANCE_B,
    INSTANCES,
    REQUEST_IDS,
    seed_ingest_records,
)
from data.store.seeds.manifest import head_revision
from data.store.seeds.producer import SNAPSHOT_BEGIN, SNAPSHOT_END, VERSIONS_DIR, CastDrifted, SeedRefused, produce
from data.store.seeds.scenario import SEED_REQUESTS
from data.store.seeds.stack import Stack, StackError
from routers.common.instance_secret import INSTANCE_SECRET_HEADER
from tests.fakes.market_data import (
    EMPTY_PREFIX,
    FAKE_DELISTED_SYMBOLS,
    FAKE_LISTED_SYMBOLS,
    Scenario,
    grid_index,
    grid_timestamps,
    scenario_for,
)


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
    # tj-7zjov1.30: asset-list requests and attempt rows judged by owner alone ('l'), the snapshot rows' symbols ('a'),
    # the events' symbols ('v') and a flip's previous symbol ('p').
    ('l', '', 'seed-owner-a', 10),
    ('a', 'ZZSEEDXA', '', 4),
    ('v', 'ZZSEEDXB', '', 1),
    ('p', 'ZZSEEDXB', '', 1),
]
# tj-7zjov1.195: the symbols the fake broker's asset lists serve, which the store's boot-time P47 snapshot files.
FAKE_SNAPSHOT_SYMBOLS = (*FAKE_LISTED_SYMBOLS, *FAKE_DELISTED_SYMBOLS)
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
    SNAPSHOT_TABLE: ["INSERT INTO public.asset_list_snapshot (asset_list, id) VALUES ('active', '0s');"],
    SNAPSHOT_ROW_TABLE: ["INSERT INTO public.asset_list_row (id, symbol) VALUES ('0w', 'ZZSEEDXA');"],
    SNAPSHOT_EVENT_TABLE: ["INSERT INTO public.asset_list_event (id, kind) VALUES ('0v', 'appeared');"],
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
    SNAPSHOT_TABLE: ['id', 'asset_list'],
    SNAPSHOT_ROW_TABLE: ['symbol', 'id'],
    SNAPSHOT_EVENT_TABLE: ['kind', 'id'],
}
# corporate_action's count is also the observation count of the cast refusal (the same statement), so it is the
# cast's EXPECTED_OBSERVATIONS, as ingest_instance's is EXPECTED_INSTANCES and ingest_run's is EXPECTED_RUN_ROWS (the
# 'run rows' check, fifteen attempt rows and four legacy-shaped runs since tj-7zjov1.30), and asset_list_row's is the
# 'snapshot rows' check's EXPECTED_SNAPSHOT_ROWS.
COUNTS = {
    ENTRY_TABLE: 3,
    BAR_TABLE: 7,
    INSTANCE_TABLE: 2,
    REQUEST_TABLE: 16,
    RUN_TABLE: 19,
    CORPORATE_ACTION_TABLE: 3,
    SNAPSHOT_TABLE: 5,
    SNAPSHOT_ROW_TABLE: 9,
    SNAPSHOT_EVENT_TABLE: 5,
}
DIGESTS = {
    ENTRY_TABLE: {'id': 'i1', 'owner': 'o1'},
    BAR_TABLE: {'close': 'c0', 'id': 'i0'},
    INSTANCE_TABLE: {'instance_id': 'n0', 'superseded_at': 'n1'},
    REQUEST_TABLE: {'id': 'q0', 'state': 'q1'},
    RUN_TABLE: {'id': 'r0', 'request_id': 'r1'},
    CORPORATE_ACTION_TABLE: {'id': 'c0', 'vendor_action_id': 'c1'},
    SNAPSHOT_TABLE: {'asset_list': 's1', 'id': 's0'},
    SNAPSHOT_ROW_TABLE: {'id': 'w0', 'symbol': 'w1'},
    SNAPSHOT_EVENT_TABLE: {'id': 'v0', 'kind': 'v1'},
}
# The one query that asks for old four-column bar key collisions.
COLLISION_MARK = 'HAVING count(*) > 1'
# The reset of the three ingest tables and corporate_action (tj-7zjov1.31), the one script besides NORMALISE_SQL.
RESET_SQL = (
    f'DELETE FROM {RUN_TABLE}; DELETE FROM {REQUEST_TABLE}; DELETE FROM {INSTANCE_TABLE}; '
    f'DELETE FROM {CORPORATE_ACTION_TABLE}; DELETE FROM {SNAPSHOT_EVENT_TABLE}; DELETE FROM {SNAPSHOT_ROW_TABLE}; '
    f'DELETE FROM {SNAPSHOT_TABLE};'
)
DATASET_ID = uuid.UUID('7ceff30a-af80-1760-5057-d346cc334f4d')
# The cast refusal's queries, in order (_refuse_wrong_ingest_records): the counts, then since tj-7zjov1.166 every cast
# request by its id and every instance by its id. They run twice per attempt: after the cast, and again in the snapshot.
CAST_CHECKS = [
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
    # tj-7zjov1.30: the asset-list cast.
    'asset-list requests',
    'asset-list snapshots',
    'empty snapshots',
    'snapshot rows',
    'snapshots whose row_count differs from their rows',
    'asset-list events',
    # tj-7zjov1.166: each cast request and each instance, by id.
    'request rows',
    'instance rows',
]
# The steps of the ingest-record phase, in order, between the collision check and the rewrite.
# Since tj-7zjov1.31 the ingest reset runs a second time, after the dataset lookup and right before the cast, so the
# seed holds the cast alone and not the POSTs' own request and attempt rows.
RECORD_STEPS = ['seed dataset lookup', 'ingest reset', 'seed', *CAST_CHECKS, 'synthetic check']
# tj-7zjov1.166: after the normalise, ONE snapshot holds the repeated refusals, the counts, the render and the digests.
DUMP_STEPS = [
    'dump snapshot begin',
    'synthetic check',
    *CAST_CHECKS,
    *(f'{table} count' for table in SEED_TABLES),
    *(step for table in SEED_TABLES for step in (f'{table} columns', f'{table} render')),
    *(step for table in SEED_TABLES for step in (f'{table} columns', f'{table} digests')),
    'dump snapshot end',
]
# The cast as the fake's per-request and per-instance queries answer it by default: EXPECTED_REQUESTS' own rows.
REQUEST_ROWS = [(request_id, state, instance) for request_id, (state, instance) in EXPECTED_REQUESTS.items()]
INSTANCE_ROWS = list(EXPECTED_INSTANCE_SUPERSEDED.items())
# What a drift may rewrite in the fake: the rows the cast writes (and the reset plus the next cast write again).
# tj-7zjov1.195: the synthetic check's rows after the cast and the asset-list counts too, since the reset empties the
# rows a P47 snapshot writes mid-cast.
CAST_ATTRS = (
    'state_rows',
    'request_rows',
    'instance_rows',
    'superseded_rows',
    'raw_counts',
    'render',
    'digests',
    'rows_after_records',
    'asset_request_rows',
    'snapshot_list_rows',
)
# Merged into, not replaced, by FakeStack.write.
MERGED_ATTRS = ('raw_counts', 'render', 'digests')
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
    fake.reseed()
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
    for table in (
        SNAPSHOT_EVENT_TABLE,
        SNAPSHOT_ROW_TABLE,
        SNAPSHOT_TABLE,
        CORPORATE_ACTION_TABLE,
        INSTANCE_TABLE,
        REQUEST_TABLE,
        RUN_TABLE,
        BAR_TABLE,
        ENTRY_TABLE,
    ):
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

    THE STORE'S OWN LOOPS AND THE SNAPSHOT (tj-7zjov1.166). request_rows and instance_rows answer the per-request
    and per-instance queries (default: EXPECTED_REQUESTS and EXPECTED_INSTANCE_SUPERSEDED, as rows). The cast attributes
    (CAST_ATTRS, after the overrides) are what the seeder writes: every stub seeder call restores them, as the reset and
    the cast do on a real stack. drifts maps (attempt, step) to a writer(fake) that lands ONCE, just before the first
    statement named step of that attempt (attempt: the number of seeder calls so far), as the instance watcher's probe
    lands between two of the producer's statements; a writer changes rows through write(). The fake models REPEATABLE
    READ: a write while the snapshot is open (between SNAPSHOT_BEGIN and SNAPSHOT_END) is held and lands at the END,
    so every read inside the snapshot sees the rows as they were when it opened. end_error is raised by the
    SNAPSHOT_END script, after the snapshot is closed.
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
        asset_request_rows=None,
        snapshot_list_rows=None,
        empty_snapshot_rows=None,
        snapshot_stray_rows=None,
        event_rows=None,
        request_rows=None,
        instance_rows=None,
        drifts=None,
        end_error=None,
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
        # tj-7zjov1.30: the asset-list cast's answers (the snapshot rows check is asset_list_row's count, raw_counts).
        self.asset_request_rows = (
            list(EXPECTED_ASSET_LIST_REQUESTS.items()) if asset_request_rows is None else asset_request_rows
        )
        self.snapshot_list_rows = list(EXPECTED_SNAPSHOTS.items()) if snapshot_list_rows is None else snapshot_list_rows
        self.empty_snapshot_rows = [(EXPECTED_EMPTY_SNAPSHOTS,)] if empty_snapshot_rows is None else empty_snapshot_rows
        self.snapshot_stray_rows = [(0,)] if snapshot_stray_rows is None else snapshot_stray_rows
        self.event_rows = list(EXPECTED_EVENTS.items()) if event_rows is None else event_rows
        # tj-7zjov1.166: the per-request and per-instance answers, the cast the seeder writes, the drifts, the snapshot.
        self.request_rows = list(REQUEST_ROWS) if request_rows is None else request_rows
        self.instance_rows = list(INSTANCE_ROWS) if instance_rows is None else instance_rows
        self.cast = copy.deepcopy({name: getattr(self, name) for name in CAST_ATTRS})
        self.drifts = dict(drifts or {})
        self.end_error = end_error
        self.attempts = 0
        self.snapshot_open = False
        self.held: list[dict] = []
        self.snapshot_reads: list[str] = []
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
        # tj-7zjov1.166: every cast request and every instance, by id.
        if sql == f'SELECT id::text, state::text, instance_id FROM {REQUEST_TABLE};':
            return self.request_rows
        if sql == f'SELECT instance_id, superseded_at IS NOT NULL FROM {INSTANCE_TABLE};':
            return self.instance_rows
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
        # The asset-list cast (tj-7zjov1.30), before the generic count shape two of them also start with.
        if f"FROM {REQUEST_TABLE} WHERE kind = 'ASSET_LIST' GROUP BY asset_list_status" in sql:
            return self.asset_request_rows
        if sql == f'SELECT asset_list::text, count(*) FROM {SNAPSHOT_TABLE} GROUP BY asset_list;':
            return self.snapshot_list_rows
        if sql == f'SELECT count(*) FROM {SNAPSHOT_TABLE} WHERE row_count = 0;':
            return self.empty_snapshot_rows
        if f'FROM {SNAPSHOT_TABLE} s WHERE s.row_count <> (' in sql:
            return self.snapshot_stray_rows
        if sql == f'SELECT kind::text, count(*) FROM {SNAPSHOT_EVENT_TABLE} GROUP BY kind;':
            return self.event_rows
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
        self._land(what)
        self.calls.append(('query', what))
        self.sql.append(sql)
        if self.snapshot_open:
            self.snapshot_reads.append(what)
        return [tuple(row) for row in self.answer(sql)]

    def script(self, sql, what):
        self._land(what)
        self.calls.append(('script', what))
        self.sql.append(sql)
        assert (sql, what) in {
            (RESET_SQL, 'ingest reset'),
            (NORMALISE_SQL, 'normalise'),
            (SNAPSHOT_BEGIN, 'dump snapshot begin'),
            (SNAPSHOT_END, 'dump snapshot end'),
        }, 'an unexpected script'
        if sql == SNAPSHOT_BEGIN:
            assert not self.snapshot_open, 'a snapshot opened inside another'
            self.snapshot_open = True
        elif sql == SNAPSHOT_END:
            # The writes the loops committed while the snapshot was open become visible once it closes.
            self.snapshot_open = False
            held, self.held = self.held, []
            for changes in held:
                self._apply(changes)
            if self.end_error is not None:
                raise self.end_error

    # -- the store's own loops: writes that land between two of the producer's statements --

    def _land(self, what):
        writer = self.drifts.pop((self.attempts, what), None)
        if writer is not None:
            writer(self)

    def write(self, **changes):
        """A write by the store's own loops: visible at once, or at the snapshot's end when one is open."""
        if self.snapshot_open:
            self.held.append(copy.deepcopy(changes))
        else:
            self._apply(changes)

    def _apply(self, changes):
        for name, value in changes.items():
            if name in MERGED_ATTRS:
                getattr(self, name).update(copy.deepcopy(value))
            else:
                setattr(self, name, copy.deepcopy(value))

    def reseed(self):
        """The reset and the cast write the cast again: whatever the loops changed in it is gone."""
        self.attempts += 1
        for name, value in copy.deepcopy(self.cast).items():
            setattr(self, name, value)

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
    # Four since tj-7zjov1.166: the fourth runs again inside the dump snapshot.
    assert len(checks) == 4
    assert checks[0] < kinds.index('ingest reset') < posts[0], 'refusal, then the ingest reset, then the POSTs'
    # Twice: before the POSTs, and again in the record steps below, right before the cast.
    assert kinds.index('ingest reset') == checks[0] + 1 and kinds.count('ingest reset') == 2
    assert posts[-1] < checks[1] < kinds.index('normalise'), 'and again after the POSTs, before the rewrite'
    assert checks[1] < kinds.index('bar collision check') < kinds.index('normalise'), 'collisions: after, before'
    assert kinds.count('bar collision check') == 1
    collision, normalise = kinds.index('bar collision check'), kinds.index('normalise')
    assert kinds[collision + 1 : normalise] == RECORD_STEPS, 'the cast, its count refusal, a third check'
    # tj-7zjov1.166: the refusals run AGAIN in the snapshot, with the counts, the render and the digests, and the
    # snapshot closes only after the last digest: everything the seed is made of is read inside it.
    assert kinds[normalise:] == ['normalise', *DUMP_STEPS]


def test_normalise_the_ingest_reset_and_the_snapshot_are_the_only_scripts():
    """tj-7zjov1.166 adds the snapshot's begin and end: one of each, after the normalise, the end last of all."""
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert [call for call in stack.calls if call[0] == 'script'] == [
        ('script', 'ingest reset'),
        ('script', 'ingest reset'),
        ('script', 'normalise'),
        ('script', 'dump snapshot begin'),
        ('script', 'dump snapshot end'),
    ]
    assert stack.calls[-1] == ('script', 'dump snapshot end')


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
    assert len(stack.synthetic_sql) == 4 and len(set(stack.synthetic_sql)) == 1
    sql = stack.synthetic_sql[0]
    assert f"SELECT 'q', coalesce(asset_symbol, ''), client_label, count(*) FROM {REQUEST_TABLE} " in sql
    # Since tj-7zjov1.30 an ASSET_LIST request and a run with no symbol (its attempt row) are judged by owner alone
    # ('l'); every other request and run is still judged by symbol and owner, a NULL request symbol still foreign.
    assert "WHERE kind <> 'ASSET_LIST' GROUP BY asset_symbol, client_label" in sql
    assert f"SELECT 'l', '', client_label, count(*) FROM {REQUEST_TABLE} WHERE kind = 'ASSET_LIST'" in sql
    assert (
        f"SELECT 'r', asset_symbol, actor, count(*) FROM {RUN_TABLE} WHERE asset_symbol IS NOT NULL "
        'GROUP BY asset_symbol, actor'
    ) in sql
    assert f"SELECT 'l', '', actor, count(*) FROM {RUN_TABLE} WHERE asset_symbol IS NULL GROUP BY actor" in sql
    # The asset-list tables carry the vendor's symbols and no owner: rows, events and a flip's previous symbol.
    assert f"SELECT 'a', symbol, '', count(*) FROM {SNAPSHOT_ROW_TABLE} GROUP BY symbol" in sql
    assert f"SELECT 'v', symbol, '', count(*) FROM {SNAPSHOT_EVENT_TABLE} GROUP BY symbol" in sql
    assert (
        f"SELECT 'p', previous_symbol, '', count(*) FROM {SNAPSHOT_EVENT_TABLE} WHERE previous_symbol IS NOT NULL "
        'GROUP BY previous_symbol'
    ) in sql
    assert INSTANCE_TABLE not in sql, 'the instance marker carries no label'


@pytest.mark.parametrize(
    'foreign',
    [
        ('l', '', 'miguel', 3),  # an asset-list request or attempt row under a real owner
        ('a', 'MSFT', '', 3),  # a real symbol in a snapshot row (not one the fake broker lists: tj-7zjov1.195)
        ('v', 'TSLA', '', 3),  # a real symbol on an event
        ('p', 'TWTR', '', 3),  # a real previous symbol on a flip
        ('a', 'ZZSEED', '', 3),  # the prefix alone is not the pattern
    ],
    ids=['asset-list-owner', 'row-symbol', 'event-symbol', 'flip-previous-symbol', 'bare-prefix'],
)
def test_a_foreign_asset_list_row_before_the_scenario_refuses_before_any_post(no_writes, foreign):
    stack = FakeStack(foreign_before=[foreign])
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == '3 row(s) hold a symbol or owner outside the synthetic patterns; no seed written'
    assert 'post' not in stack.kinds()
    assert foreign[1] not in str(raised.value) or not foreign[1]
    assert foreign[1] not in FAKE_SNAPSHOT_SYMBOLS, 'a symbol the fake broker lists is exempt, not foreign'


@pytest.mark.parametrize(
    'synthetic',
    [('l', '', 'seed-owner-b', 2), ('a', 'ZZSEEDXF', '', 1), ('v', 'ZZSEEDXE', '', 1), ('p', 'ZZSEEDXB', '', 1)],
    ids=['asset-list-owner', 'row-symbol', 'event-symbol', 'flip-previous-symbol'],
)
def test_a_synthetic_asset_list_row_with_no_symbol_or_no_owner_is_not_foreign(synthetic):
    """'l' has no symbol to judge (owner alone); 'a', 'v' and 'p' have no owner (symbol alone)."""
    stack = FakeStack(foreign_before=[synthetic], foreign_records=[synthetic])
    produce(stack, date='2026-09-30')


# ---------------------------------------------------------------------------------------------
# The store's own boot-time asset snapshot (bead tj-7zjov1.195): exempt by exact owner and exact symbol set
# ---------------------------------------------------------------------------------------------

# What one P47 boot writes on the fake broker: the ASSET_LIST request and its attempt row under SCHEDULER_ACTOR (two
# 'l' groups, one per table), and a snapshot row per listed or delisted symbol.
BOOT_SNAPSHOT_ROWS = [
    ('l', '', SCHEDULER_ACTOR, 2),
    ('l', '', SCHEDULER_ACTOR, 2),
    *(('a', symbol, '', 1) for symbol in FAKE_SNAPSHOT_SYMBOLS),
]
EXEMPT_ROWS = [
    pytest.param(('l', '', SCHEDULER_ACTOR, 2), id='scheduler-asset-list'),
    *(pytest.param((kind, symbol, '', 3), id=f'{kind}-{symbol}') for kind in 'avp' for symbol in FAKE_SNAPSHOT_SYMBOLS),
]


def test_the_boot_snapshot_alone_counts_no_foreign_row():
    stack = FakeStack(foreign_before=BOOT_SNAPSHOT_ROWS)
    assert producer_module._count_foreign_rows(stack) == (0, False)


@pytest.mark.parametrize('exempt', EXEMPT_ROWS)
def test_a_boot_snapshot_row_counts_zero_and_the_seed_is_made(no_writes, exempt):
    """Seen by every synthetic check: before the POSTs, after them, after the cast and in the snapshot."""
    assert producer_module._count_foreign_rows(FakeStack(foreign_before=[exempt])) == (0, False)
    stack = FakeStack(foreign_before=[exempt], foreign_after=[exempt])
    assert produce(stack, date='2026-09-30').bundle == _clean_bundle()
    assert stack.kinds().count('synthetic check') == 4


@pytest.mark.parametrize(
    'foreign',
    [
        # A scheduler BARS or corporate-actions request or run is judged by its symbol, never exempt by its owner.
        pytest.param(('q', 'AAPL', SCHEDULER_ACTOR, 3), id='scheduler-request-fake-listed-symbol'),
        pytest.param(('q', 'MSFT', SCHEDULER_ACTOR, 3), id='scheduler-request-real-symbol'),
        pytest.param(('r', 'AAPL', SCHEDULER_ACTOR, 3), id='scheduler-run-fake-listed-symbol'),
        pytest.param(('r', 'MSFT', SCHEDULER_ACTOR, 3), id='scheduler-run-real-symbol'),
        # The fake lists exempt only the asset-list tables: a corporate action or an entry naming one still counts.
        pytest.param(('c', 'AAPL', '', 3), id='corporate-action-fake-listed-symbol'),
        pytest.param(('e', 'VFV', 'seed-owner-a', 3), id='entry-fake-listed-symbol'),
        # A real symbol outside the fake lists, and near misses of a listed one: the set is matched exactly.
        pytest.param(('a', 'MSFT', '', 3), id='row-real-symbol'),
        pytest.param(('v', 'MSFT', '', 3), id='event-real-symbol'),
        pytest.param(('p', 'MSFT', '', 3), id='flip-real-symbol'),
        pytest.param(('a', 'AAPLX', '', 3), id='row-listed-prefix'),
        pytest.param(('a', 'AAP', '', 3), id='row-listed-truncated'),
        pytest.param(('a', 'aapl', '', 3), id='row-listed-lower-case'),
        # An ASSET_LIST row under any owner but exactly SCHEDULER_ACTOR.
        pytest.param(('l', '', f'{SCHEDULER_ACTOR}-x', 3), id='asset-list-owner-scheduler-prefix'),
        pytest.param(('l', '', f'x{SCHEDULER_ACTOR}', 3), id='asset-list-owner-scheduler-suffix'),
        pytest.param(('l', '', SCHEDULER_ACTOR.upper(), 3), id='asset-list-owner-scheduler-upper-case'),
        pytest.param(('l', '', 'operator', 3), id='asset-list-owner-operator'),
    ],
)
def test_beside_the_boot_snapshot_a_foreign_row_still_counts_and_refuses(no_writes, foreign):
    stack = FakeStack(foreign_before=[*BOOT_SNAPSHOT_ROWS, foreign])
    assert producer_module._count_foreign_rows(stack) == (3, False)
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == '3 row(s) hold a symbol or owner outside the synthetic patterns; no seed written'
    assert 'post' not in stack.kinds() and 'ingest reset' not in stack.kinds()


def test_the_scheduler_actor_is_the_one_the_snapshot_schedule_writes():
    """The exemption names the schedule's own label: tests above build rows from the same constant."""
    from data.store.app import asset_snapshot_schedule

    assert asset_snapshot_schedule.SCHEDULER_ACTOR is SCHEDULER_ACTOR == 'scheduler'


def boot_snapshot_residue(fake):
    """The P47 schedule firing mid-cast (an ingest restart), as rows.

    One active-list request and its attempt row under SCHEDULER_ACTOR, and one snapshot of the fake broker's listed
    symbols. Exempt from the synthetic check, it still moves the cast's counts.
    """
    listed = len(FAKE_LISTED_SYMBOLS)
    fake.write(
        rows_after_records=[
            *fake.rows_after_records,
            ('l', '', SCHEDULER_ACTOR, 1),
            ('l', '', SCHEDULER_ACTOR, 1),
            *(('a', symbol, '', 1) for symbol in FAKE_LISTED_SYMBOLS),
        ],
        asset_request_rows=[(name, n + (name == 'active')) for name, n in fake.asset_request_rows],
        snapshot_list_rows=[(name, n + (name == 'active')) for name, n in fake.snapshot_list_rows],
        raw_counts={
            RUN_TABLE: [(COUNTS[RUN_TABLE] + 1,)],
            REQUEST_TABLE: [(COUNTS[REQUEST_TABLE] + 1,)],
            SNAPSHOT_TABLE: [(COUNTS[SNAPSHOT_TABLE] + 1,)],
            SNAPSHOT_ROW_TABLE: [(COUNTS[SNAPSHOT_ROW_TABLE] + listed,)],
        },
    )


@pytest.mark.parametrize('step', ['normalise', 'dump snapshot begin'])
def test_boot_snapshot_residue_mid_cast_is_a_drift_and_the_cast_runs_again(no_writes, step):
    """The snapshot's synthetic check runs before its cast check: the residue must pass the first to reach the second."""
    stack = FakeStack(drifts={(1, step): boot_snapshot_residue})
    assert produce(stack, date='2026-09-30').bundle == _clean_bundle()
    assert not stack.drifts, 'the residue landed'
    first, second = _attempts(stack)
    snapshot = first[first.index('dump snapshot begin') :]
    assert snapshot[1] == 'synthetic check' and 'request states' in snapshot, 'passed the synthetic check'
    assert first[-1] == 'dump snapshot end'
    assert second == ['seed dataset lookup', *RECORD_STEPS[1:], 'normalise', *DUMP_STEPS]


def test_boot_snapshot_residue_that_never_settles_raises_cast_drifted_not_a_foreign_refusal(no_writes):
    attempts = producer_module.CAST_ATTEMPTS
    stack = FakeStack(drifts={(n, 'normalise'): boot_snapshot_residue for n in range(1, attempts + 1)})
    with pytest.raises(CastDrifted) as raised:
        produce(stack)
    assert type(raised.value) is CastDrifted
    assert 'outside the synthetic patterns' not in str(raised.value)
    assert stack.kinds().count('seed') == attempts


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
# Since tj-7zjov1.30 the cast adds four SUCCEEDED and one EMPTY asset-list request.
EXPECTED_STATES_TEXT = 'EMPTY=2, FAILED=4, NOT_DELIVERED=1, PENDING=1, QUEUED=1, SUCCEEDED=7'


def _cast_message(found: str) -> str:
    return (
        'the seeded ingest requests are not the expected cast (counts per state differ); '
        f'found {found}, expected {EXPECTED_STATES_TEXT}; no seed written'
    )


def _asset_list_message(what: str, per: str, found: str, expected: str) -> str:
    return (
        f'the seeded asset-list {what} are not the expected cast (counts per {per} differ); '
        f'found {found}, expected {expected}; no seed written'
    )


CAST_REFUSALS = [
    pytest.param(
        {'state_rows': _states(EMPTY=None)},
        _cast_message('FAILED=4, NOT_DELIVERED=1, PENDING=1, QUEUED=1, SUCCEEDED=7'),
        id='a-state-missing',
    ),
    pytest.param(
        {'state_rows': _states(FAILED=3)},
        _cast_message('EMPTY=2, FAILED=3, NOT_DELIVERED=1, PENDING=1, QUEUED=1, SUCCEEDED=7'),
        id='a-count-short',
    ),
    pytest.param(
        {'state_rows': _states(PENDING=2)},
        _cast_message('EMPTY=2, FAILED=4, NOT_DELIVERED=1, PENDING=2, QUEUED=1, SUCCEEDED=7'),
        id='a-count-over',
    ),
    pytest.param(
        {'state_rows': _states(RUNNING=1)},
        _cast_message('EMPTY=2, FAILED=4, NOT_DELIVERED=1, PENDING=1, QUEUED=1, RUNNING=1, SUCCEEDED=7'),
        id='an-extra-state',
    ),
    pytest.param({'state_rows': []}, _cast_message('none'), id='no-requests'),
    # Text counts: still refused (the comparison is typed), and since the builder's change on tj-7zjov1.30 the found
    # side shows WHY, each text count with its type (the P18p tj-7zjov1.162 behaviour, built here).
    pytest.param(
        {'state_rows': [(s, str(n)) for s, n in _states()]},
        _cast_message(
            "EMPTY=str '2', FAILED=str '4', NOT_DELIVERED=str '1', PENDING=str '1', QUEUED=str '1', SUCCEEDED=str '7'"
        ),
        id='text-counts',
    ),
    pytest.param(
        {'attempt_rows': [(14,)]}, '14 attempt rows were written; expected 15; no seed written', id='attempts-14'
    ),
    pytest.param(
        {'attempt_rows': [(16,)]}, '16 attempt rows were written; expected 15; no seed written', id='attempts-16'
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
        {'raw_counts': {RUN_TABLE: [(18,)]}},
        '18 ingest_run rows were written; expected 19; no seed written',
        id='runs-18',
    ),
    pytest.param(
        {'raw_counts': {RUN_TABLE: [(20,)]}},
        '20 ingest_run rows were written; expected 19; no seed written',
        id='runs-20',
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
    # tj-7zjov1.30: the asset-list cast. Each refusal names what was FOUND and what was EXPECTED, counts only.
    pytest.param(
        {'asset_request_rows': [('active', 3), ('inactive', 1)]},
        _asset_list_message('requests', 'list', 'active=3, inactive=1', 'active=3, inactive=2'),
        id='asset-list-requests-short',
    ),
    pytest.param(
        {'asset_request_rows': [('active', 3)]},
        _asset_list_message('requests', 'list', 'active=3', 'active=3, inactive=2'),
        id='asset-list-requests-a-list-missing',
    ),
    pytest.param(
        {'asset_request_rows': [('active', '3'), ('inactive', 2)]},
        _asset_list_message('requests', 'list', "active=str '3', inactive=2", 'active=3, inactive=2'),
        id='asset-list-requests-text-count',
    ),
    pytest.param(
        {'snapshot_list_rows': [('active', 2), ('inactive', 2)]},
        _asset_list_message('snapshots', 'list', 'active=2, inactive=2', 'active=3, inactive=2'),
        id='snapshots-short',
    ),
    pytest.param(
        {'snapshot_list_rows': []},
        _asset_list_message('snapshots', 'list', 'none', 'active=3, inactive=2'),
        id='no-snapshots',
    ),
    pytest.param(
        {'empty_snapshot_rows': [(0,)]},
        'found 0 empty asset-list snapshots, expected 1; no seed written',
        id='no-empty-snapshot',
    ),
    pytest.param(
        {'empty_snapshot_rows': [(2,)]},
        'found 2 empty asset-list snapshots, expected 1; no seed written',
        id='two-empty-snapshots',
    ),
    pytest.param(
        {'raw_counts': {SNAPSHOT_ROW_TABLE: [(8,)]}},
        'found 8 asset-list rows, expected 9; no seed written',
        id='snapshot-rows-8',
    ),
    pytest.param(
        {'snapshot_stray_rows': [(1,)]},
        'found 1 snapshot(s) whose row_count differs from the rows they hold, expected 0; no seed written',
        id='row-count-differs',
    ),
    pytest.param(
        {'event_rows': [('appeared', 2), ('vanished', 2)]},
        _asset_list_message('events', 'kind', 'appeared=2, vanished=2', 'appeared=2, status_flip=1, vanished=2'),
        id='no-flip',
    ),
    pytest.param(
        {'event_rows': [('appeared', 2), ('status_flip', 1), ('vanished', 3)]},
        _asset_list_message(
            'events', 'kind', 'appeared=2, status_flip=1, vanished=3', 'appeared=2, status_flip=1, vanished=2'
        ),
        id='a-vanished-too-many',
    ),
]


@pytest.mark.parametrize(('override', 'message'), CAST_REFUSALS)
def test_a_cast_that_differs_is_refused_before_the_rewrite(no_writes, override, message):
    stack = FakeStack(**override)
    with pytest.raises(SeedRefused) as raised:
        produce(stack)
    assert str(raised.value) == message
    # tj-7zjov1.166: every cast refusal is a CastDrifted, so the whole cast is run CAST_ATTEMPTS times; a cast that
    # differs the same way every time is refused with that one message, unchanged.
    assert isinstance(raised.value, CastDrifted)
    assert stack.kinds().count('seed') == producer_module.CAST_ATTEMPTS
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
        ({'empty_snapshot_rows': [('1',)]}, 'empty snapshots'),
        ({'raw_counts': {SNAPSHOT_ROW_TABLE: [(None,)]}}, 'snapshot rows'),
        ({'snapshot_stray_rows': []}, 'snapshots whose row_count differs from their rows'),
    ],
    ids=[
        'attempts',
        'half-attempts',
        'instances',
        'superseded',
        'observations',
        'revised',
        'interrupted',
        'runs',
        'empty-snapshots',
        'snapshot-rows',
        'snapshot-stray',
    ],
)
def test_a_cast_count_that_is_not_one_integer_fails_without_echoing_it(no_writes, override, what):
    """S9 (5): a count query answering anything but one non-negative integer fails without echoing the result.

    Red at e80bbd1, where _query_count appended 'found <type> <value>' (validator finding on tj-7zjov1.30); green
    since 2e6ac02 removed it.
    """
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
    # tj-7zjov1.30: the asset-list cast's queries.
    assert (
        f"SELECT asset_list_status::text, count(*) FROM {REQUEST_TABLE} WHERE kind = 'ASSET_LIST' "
        'GROUP BY asset_list_status;'
    ) in stack.sql
    assert f'SELECT asset_list::text, count(*) FROM {SNAPSHOT_TABLE} GROUP BY asset_list;' in stack.sql
    assert f'SELECT count(*) FROM {SNAPSHOT_TABLE} WHERE row_count = 0;' in stack.sql
    assert f'SELECT count(*) FROM {SNAPSHOT_ROW_TABLE};' in stack.sql
    assert (
        f'SELECT count(*) FROM {SNAPSHOT_TABLE} s WHERE s.row_count <> '
        f'(SELECT count(*) FROM {SNAPSHOT_ROW_TABLE} r WHERE r.snapshot_id = s.id);'
    ) in stack.sql
    assert f'SELECT kind::text, count(*) FROM {SNAPSHOT_EVENT_TABLE} GROUP BY kind;' in stack.sql
    # tj-7zjov1.166: every request and every instance, by id, unfiltered (a row outside the cast must be seen).
    assert f'SELECT id::text, state::text, instance_id FROM {REQUEST_TABLE};' in stack.sql
    assert f'SELECT instance_id, superseded_at IS NOT NULL FROM {INSTANCE_TABLE};' in stack.sql


def test_the_expected_cast_is_the_cast_ingest_records_names():
    """Sixteen named requests, fifteen attempt rows, two instances.

    Two corporate-actions requests since tj-7zjov1.31, five asset lists since tj-7zjov1.30. One per state but four FAILED, seven SUCCEEDED (the bars success, the two corporate-actions requests and the four
    non-empty asset lists) and two EMPTY (the bars one and the empty asset list); three observations (A, B, A revised),
    one action holding two payloads.
    """
    assert sum(EXPECTED_REQUEST_STATES.values()) == len(REQUEST_IDS) == 16
    assert len(set(REQUEST_IDS.values())) == len(REQUEST_IDS)
    asset_lists = [name for name in REQUEST_IDS if name.startswith('AL_')]
    assert EXPECTED_REQUEST_STATES['FAILED'] == sum(1 for name in REQUEST_IDS if name.startswith('FAILED_'))
    assert EXPECTED_REQUEST_STATES['SUCCEEDED'] == 1 + sum(1 for name in REQUEST_IDS if name.startswith('CA_')) + sum(
        1 for name in asset_lists if not name.endswith('_EMPTY')
    )
    assert EXPECTED_REQUEST_STATES['EMPTY'] == 1 + sum(1 for name in asset_lists if name.endswith('_EMPTY'))
    assert EXPECTED_ATTEMPT_ROWS == 15 and EXPECTED_INSTANCES == 2
    assert (EXPECTED_OBSERVATIONS, EXPECTED_REVISED_ACTIONS) == (3, 1)
    # e5d950e: four legacy-shaped runs (one swept INTERRUPTED), so nineteen ingest_run rows in all.
    assert sum(EXPECTED_LEGACY_STATES.values()) == EXPECTED_LEGACY_RUNS == 4
    assert EXPECTED_LEGACY_STATES == {'EMPTY': 1, 'FAILED': 2, 'SUCCEEDED': 1} and EXPECTED_INTERRUPTED_RUNS == 1
    assert EXPECTED_RUN_ROWS == EXPECTED_ATTEMPT_ROWS + EXPECTED_LEGACY_RUNS == 19


def test_the_expected_asset_list_cast_is_the_one_the_seeder_names():
    """One request and one snapshot per AL_ name, split by list; the empty one among them; the bead's counts."""
    asset_lists = [name for name in REQUEST_IDS if name.startswith('AL_')]
    per_list = Counter('inactive' if '_INACTIVE_' in name else 'active' for name in asset_lists)
    assert EXPECTED_ASSET_LIST_REQUESTS == EXPECTED_SNAPSHOTS == dict(per_list) == {'active': 3, 'inactive': 2}
    assert EXPECTED_EMPTY_SNAPSHOTS == sum(1 for name in asset_lists if name.endswith('_EMPTY')) == 1
    assert EXPECTED_SNAPSHOT_ROWS == 9
    assert EXPECTED_EVENTS == {'appeared': 2, 'status_flip': 1, 'vanished': 2}
    # Synthetic: every symbol of the cast is ZZSEED plus letters, every vendor id is a seed id.
    assert all(symbol.startswith('ZZSEEDX') for _, symbol in _AL_ASSETS.values())
    assert all(vendor_id.startswith('seed-asset-') for vendor_id, _ in _AL_ASSETS.values())


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


# ---------------------------------------------------------------------------------------------
# The store's own loops during the cast (tj-7zjov1.166): each request by id, one snapshot, the retry
# ---------------------------------------------------------------------------------------------
#
# The instance watcher probes the live ingest every pass; the reset made its id a first sighting, so the probe adds that
# instance, supersedes seed-instance-b and moves the QUEUED request to NOT_DELIVERED. Before the fix the checks ran
# before the normalise and the dump was read afterwards, so a probe landing between them was dumped unchecked. The
# drifts below land at a named statement of a named attempt; the fake holds a write made while the snapshot is open
# until it closes, as REPEATABLE READ does.

PENDING_ID = str(REQUEST_IDS['PENDING'])
QUEUED_ID = str(REQUEST_IDS['QUEUED'])
NOT_DELIVERED_ID = str(REQUEST_IDS['NOT_DELIVERED'])
SUCCEEDED_ID = str(REQUEST_IDS['SUCCEEDED'])
# Values of the stack's own, never the cast's: no refusal may carry them.
LIVE_INSTANCE = 'ingest-live-INSTSENTINEL'
OUTSIDE_REQUEST = 'OUTSIDE-REQUEST-SENTINEL'
PROBED_REQUEST_LINE = "INSERT INTO public.ingest_request (id, state) VALUES ('5eed', 'NOT_DELIVERED');"
PROBED_INSTANCE_LINE = (
    f"INSERT INTO public.ingest_instance (instance_id, superseded_at) VALUES ('{LIVE_INSTANCE}', NULL);"
)


def _with_request(rows, request_id, state, instance):
    return [(rid, state, instance) if rid == request_id else (rid, s, i) for rid, s, i in rows]


def watcher_probe(fake):
    """The instance watcher's first sighting of the live ingest (crud/ingest_instance.py steps 2 and 3), as rows."""
    fake.write(
        request_rows=_with_request(fake.request_rows, QUEUED_ID, 'NOT_DELIVERED', INSTANCE_B),
        state_rows=_states(QUEUED=None, NOT_DELIVERED=2),
        instance_rows=[(INSTANCE_A, True), (INSTANCE_B, True), (LIVE_INSTANCE, False)],
        superseded_rows=[(2,)],
        raw_counts={INSTANCE_TABLE: [(3,)]},
        render={REQUEST_TABLE: [(PROBED_REQUEST_LINE,)], INSTANCE_TABLE: [(PROBED_INSTANCE_LINE,)]},
        digests={
            REQUEST_TABLE: [('id', 'q0'), ('state', 'probed')],
            INSTANCE_TABLE: [('instance_id', 'probed'), ('superseded_at', 'probed')],
        },
    )


def count_neutral_flip(fake):
    """A drift no count can see: the QUEUED and the NOT_DELIVERED request trade states, each keeping its instance."""
    rows = _with_request(fake.request_rows, QUEUED_ID, 'NOT_DELIVERED', INSTANCE_B)
    fake.write(
        request_rows=_with_request(rows, NOT_DELIVERED_ID, 'QUEUED', INSTANCE_A),
        render={REQUEST_TABLE: [(PROBED_REQUEST_LINE,)]},
        digests={REQUEST_TABLE: [('id', 'q0'), ('state', 'probed')]},
    )


DRIFTS = [pytest.param(watcher_probe, id='watcher-probe'), pytest.param(count_neutral_flip, id='count-neutral-flip')]


def _clean_bundle():
    return produce(FakeStack(), date='2026-09-30').bundle


def _attempts(stack: FakeStack) -> list[list[str]]:
    """The calls of each cast attempt, from its dataset lookup to the next one (the last runs to the end)."""
    kinds = stack.kinds()
    starts = [i for i, kind in enumerate(kinds) if kind == 'seed dataset lookup']
    return [kinds[start:end] for start, end in zip(starts, [*starts[1:], len(kinds)], strict=True)]


def test_the_snapshot_is_one_repeatable_read_read_only_transaction_closed_by_a_rollback():
    """The design's isolation level (bead tj-7zjov1.166 REQUIRED (1)): a weaker one gives each statement its own view."""
    assert SNAPSHOT_BEGIN == 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;'
    assert SNAPSHOT_END == 'ROLLBACK;'


def test_the_cast_is_run_at_most_five_times():
    assert producer_module.CAST_ATTEMPTS == 5


def test_every_read_the_seed_is_made_of_is_inside_the_snapshot():
    stack = FakeStack()
    produce(stack, date='2026-09-30')
    assert stack.snapshot_reads == DUMP_STEPS[1:-1]
    assert not stack.snapshot_open


@pytest.mark.parametrize('drift', DRIFTS)
@pytest.mark.parametrize('step', ['normalise', 'dump snapshot begin'])
def test_a_drift_after_the_checks_and_before_the_dump_is_refused_and_the_cast_run_again(no_writes, step, drift):
    """The P17 race: the post-cast checks pass, the probe lands, and the snapshot's own checks refuse what it sees."""
    stack = FakeStack(drifts={(1, step): drift})
    result = produce(stack, date='2026-09-30')
    assert result.bundle == _clean_bundle()
    assert PROBED_REQUEST_LINE not in result.bundle.sql
    first, second = _attempts(stack)
    assert 'normalise' in first, 'the first attempt passed the checks after the cast'
    assert f'{ENTRY_TABLE} count' not in first and f'{ENTRY_TABLE} render' not in first, 'refused before any read'
    assert first[-1] == 'dump snapshot end', 'and the snapshot was rolled back before the cast ran again'
    assert first.count('dump snapshot begin') == 1
    assert second == ['seed dataset lookup', *RECORD_STEPS[1:], 'normalise', *DUMP_STEPS]


@pytest.mark.parametrize('drift', DRIFTS)
def test_a_drift_right_after_the_cast_is_refused_before_the_normalise_and_the_cast_run_again(no_writes, drift):
    stack = FakeStack(drifts={(1, 'request states'): drift})
    assert produce(stack, date='2026-09-30').bundle == _clean_bundle()
    first, second = _attempts(stack)
    assert 'normalise' not in first and 'dump snapshot begin' not in first
    assert 'normalise' in second


@pytest.mark.parametrize('drift', DRIFTS)
@pytest.mark.parametrize('step', [f'{ENTRY_TABLE} count', f'{ENTRY_TABLE} render', f'{ENTRY_TABLE} digests'])
def test_a_drift_inside_the_snapshot_is_in_neither_the_checks_nor_the_dump(no_writes, step, drift):
    """The write lands (the rows did change) but after the snapshot opened: the seed is the cast, in one attempt."""
    stack = FakeStack(drifts={(1, step): drift})
    assert produce(stack, date='2026-09-30').bundle == _clean_bundle()
    assert stack.kinds().count('seed') == 1
    assert not stack.drifts, 'the drift ran'
    assert stack.request_rows != REQUEST_ROWS, 'and is visible once the snapshot closed'


@pytest.mark.parametrize('drift', DRIFTS)
def test_a_drift_that_settles_on_the_last_attempt_yields_the_normal_seed(no_writes, drift):
    attempts = producer_module.CAST_ATTEMPTS
    stack = FakeStack(drifts={(n, 'normalise'): drift for n in range(1, attempts)})
    assert produce(stack, date='2026-09-30').bundle == _clean_bundle()
    assert stack.kinds().count('seed') == attempts
    assert stack.kinds().count('dump snapshot begin') == stack.kinds().count('dump snapshot end') == attempts


def _instances_held(count):
    def writer(fake):
        fake.write(raw_counts={INSTANCE_TABLE: [(count,)]})

    return writer


def _never_settling(attempts: int) -> dict:
    """A different drift on every attempt (3, 4, ... instances), so the LAST refusal is told from the others."""
    return {(n, 'normalise'): _instances_held(2 + n) for n in range(1, attempts + 1)}


def test_a_drift_that_never_settles_re_raises_the_last_refusal_unchanged(no_writes):
    attempts = producer_module.CAST_ATTEMPTS
    stack = FakeStack(drifts=_never_settling(attempts))
    with pytest.raises(CastDrifted) as raised:
        produce(stack)
    assert str(raised.value) == f'{2 + attempts} ingest instances are held; expected 2; no seed written'
    assert stack.kinds().count('seed') == attempts
    assert stack.kinds().count('dump snapshot begin') == stack.kinds().count('dump snapshot end') == attempts
    assert f'{ENTRY_TABLE} render' not in stack.kinds()


def test_a_cast_that_never_settles_exits_3_through_the_entry_point(monkeypatch, capsys):
    attempts = producer_module.CAST_ATTEMPTS
    monkeypatch.setattr(entry, 'Stack', lambda: FakeStack(drifts=_never_settling(attempts)))
    assert entry.main([]) == entry.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.err == f'seed refused: {2 + attempts} ingest instances are held; expected 2; no seed written\n'
    assert captured.out == ''


def _request_message(differ: int, outside: int, wrong: str) -> str:
    return (
        f'the seeded ingest requests are not the expected cast ({differ} cast request(s) differ, '
        f'{outside} outside the cast): {wrong}; no seed written'
    )


def _mismatch(*changes, drop=None, add=()):
    rows = list(REQUEST_ROWS)
    for request_id, state, instance in changes:
        rows = _with_request(rows, request_id, state, instance)
    return [row for row in rows if row[0] != drop] + list(add)


# Every case but the last keeps the per-state totals of the cast (asserted in the test), so only the per-request check
# can refuse it; the second trades a missing cast request for one outside the cast in the same state.
REQUEST_MISMATCHES = [
    pytest.param(
        _mismatch((QUEUED_ID, 'NOT_DELIVERED', INSTANCE_B), (NOT_DELIVERED_ID, 'QUEUED', INSTANCE_A)),
        _request_message(
            2,
            0,
            f'{QUEUED_ID} holds NOT_DELIVERED under {INSTANCE_B}, expected QUEUED under {INSTANCE_B}; '
            f'{NOT_DELIVERED_ID} holds QUEUED under {INSTANCE_A}, expected NOT_DELIVERED under {INSTANCE_A}',
        ),
        id='queued-and-not-delivered-trade-states',
    ),
    pytest.param(
        _mismatch(
            (QUEUED_ID, 'NOT_DELIVERED', LIVE_INSTANCE),
            (NOT_DELIVERED_ID, 'QUEUED', INSTANCE_A),
            drop=PENDING_ID,
            add=[(OUTSIDE_REQUEST, 'PENDING', LIVE_INSTANCE)],
        ),
        _request_message(
            3,
            1,
            f'{PENDING_ID} is missing; '
            f'{QUEUED_ID} holds NOT_DELIVERED under another instance, expected QUEUED under {INSTANCE_B}; '
            f'{NOT_DELIVERED_ID} holds QUEUED under {INSTANCE_A}, expected NOT_DELIVERED under {INSTANCE_A}',
        ),
        id='a-stack-instance-and-a-request-outside-the-cast',
    ),
    pytest.param(
        _mismatch((SUCCEEDED_ID, 'SUCCEEDED', INSTANCE_A)),
        _request_message(
            1, 0, f'{SUCCEEDED_ID} holds SUCCEEDED under {INSTANCE_A}, expected SUCCEEDED under {INSTANCE_B}'
        ),
        id='the-instance-alone-differs',
    ),
    pytest.param(
        _mismatch((PENDING_ID, 'PENDING', INSTANCE_B)),
        _request_message(1, 0, f'{PENDING_ID} holds PENDING under {INSTANCE_B}, expected PENDING under none'),
        id='an-instance-where-none-is-expected',
    ),
    pytest.param(
        _mismatch((QUEUED_ID, 'QUEUED', None)),
        _request_message(1, 0, f'{QUEUED_ID} holds QUEUED under none, expected QUEUED under {INSTANCE_B}'),
        id='no-instance-where-one-is-expected',
    ),
    pytest.param(
        [*REQUEST_ROWS, (OUTSIDE_REQUEST, 'SUCCEEDED', INSTANCE_B)],
        _request_message(0, 1, 'none'),
        id='only-a-request-outside-the-cast',
    ),
]


@pytest.mark.parametrize(('rows', 'message'), REQUEST_MISMATCHES)
def test_a_request_that_differs_by_id_is_refused_though_the_counts_per_state_agree(no_writes, rows, message):
    """REQUIRED (2): each cast request against its own state and instance; the message names cast ids only."""
    stack = FakeStack(request_rows=rows)
    if len(rows) == len(REQUEST_ROWS):
        assert dict(Counter(state for _, state, _ in rows)) == EXPECTED_REQUEST_STATES, 'the totals per state agree'
    with pytest.raises(CastDrifted) as raised:
        produce(stack)
    assert str(raised.value) == message
    assert stack.kinds().count('seed') == producer_module.CAST_ATTEMPTS
    assert 'normalise' not in stack.kinds()
    for value in (LIVE_INSTANCE, OUTSIDE_REQUEST, 'seed-owner', 'ZZSEED'):
        assert value not in str(raised.value), value


def _instance_message(outside: int, wrong: str) -> str:
    return f'the instance marker is not the expected cast ({outside} instance(s) outside the cast): {wrong}; no seed written'


INSTANCE_MISMATCHES = [
    pytest.param(
        [(INSTANCE_A, True), (INSTANCE_B, False), (LIVE_INSTANCE, False)],
        _instance_message(1, 'none'),
        id='three-instances',
    ),
    pytest.param(
        [(INSTANCE_A, True), (INSTANCE_B, True), (LIVE_INSTANCE, False)],
        _instance_message(1, f'{INSTANCE_B} is superseded, expected live'),
        id='three-instances-the-live-one-superseded',
    ),
    pytest.param(
        [(INSTANCE_A, False), (INSTANCE_B, True)],
        _instance_message(0, f'{INSTANCE_A} is live, expected superseded; {INSTANCE_B} is superseded, expected live'),
        id='swapped',
    ),
    pytest.param(
        [(INSTANCE_A, True), (LIVE_INSTANCE, False)],
        _instance_message(1, f'{INSTANCE_B} is missing, expected live'),
        id='the-live-one-replaced',
    ),
]


@pytest.mark.parametrize(('rows', 'message'), INSTANCE_MISMATCHES)
def test_an_instance_that_differs_by_id_is_refused_though_the_counts_agree(no_writes, rows, message):
    """The counts (two instances, one superseded) are left as the cast's: only the per-instance check refuses."""
    stack = FakeStack(instance_rows=rows)
    with pytest.raises(CastDrifted) as raised:
        produce(stack)
    assert str(raised.value) == message
    assert LIVE_INSTANCE not in str(raised.value)
    assert stack.kinds().count('seed') == producer_module.CAST_ATTEMPTS


@pytest.mark.parametrize(
    ('override', 'message'),
    [
        ({'request_rows': [(QUEUED_ID, 'QUEUED')]}, 'request rows: a row is not (id, state, instance)'),
        ({'request_rows': [(QUEUED_ID, None, INSTANCE_B)]}, 'request rows: a row is not (id, state, instance)'),
        (
            {'request_rows': [(REQUEST_IDS['QUEUED'], 'QUEUED', INSTANCE_B)]},
            'request rows: a row is not (id, state, instance)',
        ),
        ({'request_rows': [(QUEUED_ID, 'QUEUED', 7)]}, 'request rows: a row is not (id, state, instance)'),
        ({'instance_rows': [(INSTANCE_A, 1)]}, 'instance rows: a row is not (instance, superseded)'),
        ({'instance_rows': [(None, True)]}, 'instance rows: a row is not (instance, superseded)'),
        ({'instance_rows': [(INSTANCE_A, True, 'x')]}, 'instance rows: a row is not (instance, superseded)'),
    ],
    ids=[
        'request-two-values',
        'request-null-state',
        'request-uuid-id',
        'request-int-instance',
        'instance-int-flag',
        'instance-null-id',
        'instance-three-values',
    ],
)
def test_a_request_or_instance_row_of_the_wrong_shape_fails_once_without_a_retry(no_writes, override, message):
    """A malformed answer is a StackError (exit 1): never a drift, so the cast is not run again."""
    stack = FakeStack(**override)
    with pytest.raises(StackError) as raised:
        produce(stack)
    assert str(raised.value) == message
    assert stack.kinds().count('seed') == 1


def _foreign_row_appears(fake):
    fake.write(rows_after_records=[*fake.rows_after_records, ('q', 'AAPL', 'miguel', 2)])


class _Interrupted(FakeStack):
    def query(self, sql, what):
        if what == f'{RUN_TABLE} render':
            raise KeyboardInterrupt
        return super().query(sql, what)


SNAPSHOT_FAILURES = [
    pytest.param(
        lambda: FakeStack(render={ENTRY_TABLE: [(None,)]}),
        StackError,
        f'{ENTRY_TABLE} render: a row is not one text value',
        id='render-shape',
    ),
    pytest.param(
        lambda: FakeStack(raw_counts={BAR_TABLE: [(None,)]}),
        StackError,
        f'{BAR_TABLE} count: the query did not return a single non-negative integer',
        id='row-count',
    ),
    pytest.param(
        lambda: FakeStack(digests={RUN_TABLE: [('id', None)]}),
        StackError,
        f'{RUN_TABLE} digests: a row is not a (column, digest) pair',
        id='digest-shape',
    ),
    pytest.param(
        lambda: FakeStack(render={BAR_TABLE: [("INSERT INTO t (a) VALUES (E'x\n\\connect other');",)]}),
        DumpRefused,
        '1 line(s) starting with a backslash in the dump',
        id='backslash-line',
    ),
    pytest.param(
        lambda: FakeStack(drifts={(1, 'normalise'): _foreign_row_appears}),
        SeedRefused,
        '2 row(s) hold a symbol or owner outside the synthetic patterns; no seed written',
        id='foreign-row-in-the-snapshot',
    ),
    pytest.param(_Interrupted, KeyboardInterrupt, '', id='interrupt'),
]


@pytest.mark.parametrize(('build', 'error', 'message'), SNAPSHOT_FAILURES)
def test_any_failure_inside_the_snapshot_rolls_it_back_and_is_not_retried(no_writes, build, error, message):
    """Not a drift, so no second cast; the snapshot is closed before the failure leaves produce()."""
    stack = build()
    with pytest.raises(error) as raised:
        produce(stack)
    assert type(raised.value) is error, 'a failure that is not a drift is never turned into one'
    assert str(raised.value) == message
    assert stack.kinds().count('seed') == 1
    assert stack.kinds().count('dump snapshot begin') == stack.kinds().count('dump snapshot end') == 1
    assert stack.calls[-1] == ('script', 'dump snapshot end')
    assert not stack.snapshot_open


def test_a_rollback_that_fails_after_a_failure_does_not_replace_it(no_writes):
    stack = FakeStack(
        render={ENTRY_TABLE: [(None,)]}, end_error=StackError('dump snapshot end failed (SQLSTATE 08006)')
    )
    with pytest.raises(StackError) as raised:
        produce(stack)
    assert str(raised.value) == f'{ENTRY_TABLE} render: a row is not one text value'
    assert stack.calls[-1] == ('script', 'dump snapshot end')


def test_a_rollback_that_fails_after_a_clean_dump_is_not_swallowed(no_writes):
    """The close is the last step of the dump: a seed is returned only once the snapshot closed."""
    stack = FakeStack(end_error=StackError('dump snapshot end failed (SQLSTATE 08006)'))
    with pytest.raises(StackError, match=r'^dump snapshot end failed \(SQLSTATE 08006\)$'):
        produce(stack)


def test_expected_request_states_are_the_counts_of_the_per_request_table():
    """EXPECTED_REQUEST_STATES is derived from EXPECTED_REQUESTS, so the two refusals can never disagree."""
    assert dict(Counter(state for state, _ in EXPECTED_REQUESTS.values())) == EXPECTED_REQUEST_STATES
    assert EXPECTED_REQUEST_STATES == {
        'PENDING': 1,
        'QUEUED': 1,
        'NOT_DELIVERED': 1,
        'SUCCEEDED': 7,
        'EMPTY': 2,
        'FAILED': 4,
    }
    assert set(EXPECTED_REQUESTS) == {str(request_id) for request_id in REQUEST_IDS.values()}
    assert {instance for _, instance in EXPECTED_REQUESTS.values()} <= {*INSTANCES, None}
    assert EXPECTED_INSTANCE_SUPERSEDED == {INSTANCE_A: True, INSTANCE_B: False}
    # The three named in the bead: the one never answered, the one live B holds, the one A queued and B superseded.
    assert EXPECTED_REQUESTS[PENDING_ID] == ('PENDING', None)
    assert EXPECTED_REQUESTS[QUEUED_ID] == ('QUEUED', INSTANCE_B)
    assert EXPECTED_REQUESTS[NOT_DELIVERED_ID] == ('NOT_DELIVERED', INSTANCE_A)


_SQL_VALUE = re.compile(r"NULL|'(?:[^']|'')*'")


def _seed_rows(sql: str, table: str) -> list[dict]:
    """The committed seed's rows of one table, column name to value (None for NULL), from its INSERT lines."""
    rows = []
    for line in sql.splitlines():
        match = re.fullmatch(rf'INSERT INTO public\.{table} \(([^)]*)\) VALUES \((.*)\);', line)
        if match is None:
            continue
        names = match.group(1).split(', ')
        values = [
            None if token == 'NULL' else token[1:-1].replace("''", "'") for token in _SQL_VALUE.findall(match.group(2))
        ]
        assert len(values) == len(names), line[:80]
        rows.append(dict(zip(names, values, strict=True)))
    return rows


def test_the_committed_head_seed_holds_each_request_and_instance_as_the_tables_expect():
    """The per-request table against the seed the producer wrote on the real stack: the same states and instances."""
    sql = (REPO_ROOT / 'tests' / 'system' / 'seeds' / f'{HEAD}.sql').read_text(encoding='utf-8')
    requests = {row['id']: (row['state'], row['instance_id']) for row in _seed_rows(sql, REQUEST_TABLE)}
    assert requests == EXPECTED_REQUESTS
    instances = {row['instance_id']: row['superseded_at'] is not None for row in _seed_rows(sql, INSTANCE_TABLE)}
    assert instances == EXPECTED_INSTANCE_SUPERSEDED


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
        # The instance, run, observation and asset-list row counts are also the cast refusal's (the same statements),
        # so they stay.
        raw_counts={
            table: [(0,)]
            for table in SEED_TABLES
            if table not in (INSTANCE_TABLE, RUN_TABLE, CORPORATE_ACTION_TABLE, SNAPSHOT_ROW_TABLE)
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
        scripts = (NORMALISE_SQL, RESET_SQL, SNAPSHOT_BEGIN, SNAPSHOT_END)
        self.rows = [] if sql in scripts else self.driver.fake.answer(sql)

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


def test_end_to_end_the_snapshot_runs_on_the_one_connection_after_the_normalise_and_closes_last(monkeypatch, capsys):
    """tj-7zjov1.166 through the real Stack: BEGIN and ROLLBACK are executed on the same (only) connection."""
    status, _, fake, database, _ = _run(monkeypatch, capsys)
    assert status == 0
    assert len(database.kwargs) == 1
    assert fake.sql.count(SNAPSHOT_BEGIN) == fake.sql.count(SNAPSHOT_END) == 1
    assert fake.sql.index(NORMALISE_SQL) < fake.sql.index(SNAPSHOT_BEGIN)
    assert fake.sql[-1] == SNAPSHOT_END


def test_end_to_end_a_failure_inside_the_snapshot_still_rolls_it_back(monkeypatch, capsys):
    status, captured, fake, _, _ = _run(monkeypatch, capsys, fail_on='quote_nullable', fail_with=SqlStateError('x'))
    assert status == entry.EXIT_FAILED
    assert captured.err == f'seed failed: {ENTRY_TABLE} render failed (SQLSTATE 42501)\n'
    assert fake.sql[-1] == SNAPSHOT_END


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
