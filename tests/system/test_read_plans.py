"""The two bar reads' EXPLAIN (ANALYZE, BUFFERS) plans, as a committed test (tj-70bxqx).

REPLACES A CREDENTIALED SITTING. tj-3mk3u5.16 Part B step U5 asked a person to paste two EXPLAIN
statements into psql, because the agent holds none of the stack's POSTGRES_* values. That was
never a credential problem: it was a VENUE problem. This suite already has a proven-reachable
engine on the stack's Postgres (conftest.py: pg_settings, pg_engine) and the agent-stack MCP's
run_system_tests verb already runs it from any worktree against a credential-free, disposable
stack. So the plans belong here, where they are taken on every run, rather than in a terminal.

THE QUERIES ARE NOT WRITTEN HERE. Retyping SQL would prove something about the retyping. Both
statements are taken from data/store/app/database/crud/stock/asset_market_activity.py ::
read_market_activity_data, by calling that coroutine with a session double that records the
statement and executes nothing (_StatementCapture). What is EXPLAINed is therefore, by
construction, the statement the service issues -- its predicates, its column list and its ORDER
BY -- and a change to the production read changes these plans without anyone remembering to
update this file. The two reads are the ones tj-3mk3u5.16 A0(b) names:

  * QUERY 1, dataset-scoped:       WHERE dataset_id = <entry>
  * QUERY 2, symbol + range:       WHERE asset_symbol = <symbol> AND granularity = ONE_MINUTE
                                     AND timestamp >= <start> AND timestamp <= <end>

THE ENUM TRAP, paid for once already on tj-3mk3u5.16. granularity is a Postgres enum whose labels
are the Python member NAMES ('ONE_MINUTE'), because base_market_activity.py declares
Enum(Granularity) with no values_callable; the JSON body and the wire carry '1min'. feed is the
opposite -- values_callable, so the label is the VALUE ('IEX'). A wrong-but-valid label returns
ZERO ROWS SILENTLY, and an empty scan's plan says whatever an empty scan says. Two things here
make that unmissable rather than silent: the predicates are built by production code from real
enum members, so no label is ever typed; and every plan assertion requires the measured
`Actual Rows` to equal the row count the seed put there. A plan over zero rows fails.

ASSERTED ON SHAPE, NEVER ON TIMING. Nothing here reads `Actual Total Time`, `Execution Time` or a
buffer count. A wall-clock threshold on shared hardware is a flake generator and says nothing
about the query. BUFFERS is still requested because the printed plan is the artifact U5 wanted
and a reader of it wants the buffer lines; no assertion depends on them.

WHY A VOLUME, AND WHY THIS ONE. An index-use assertion is only as meaningful as the statistics
behind it: on a fresh or tiny table the planner chooses a sequential scan HONESTLY, and a test
that asserted otherwise would be asserting something false. Two things have to hold at once, and
seeding one dataset is not enough for either:

  1. THE TABLE MUST BE BIG ENOUGH that a sequential scan is not simply the cheapest thing
     available. SEEDED_BARS is 30,000 -- the order of magnitude of the real run this replaces
     (entry bdc9e7bb-05a0-4f74-bee5-8a4731e24fd5, 15,424 one-minute AAPL bars) and of the fake
     fetch tj-3mk3u5.16 A3 requires, and measured affordable on the agent stack.
  2. THE PREDICATE MUST BE SELECTIVE. A dataset holding every row in the table is returned in
     full, and a sequential scan is then the correct plan -- so a table containing only the
     target dataset would make assertion 1 vacuous no matter how large it was. The seed therefore
     writes DECOY_DATASETS other datasets, each under its own symbol and over the SAME timestamp
     window, so the symbol and dataset predicates are what narrows the read and not the clock.
     The target is TARGET_SHARE_CEILING of the table at most; the basis for that ceiling is
     Postgres's own cost model, not taste: with the target clustered but its UUIDs uncorrelated,
     a bitmap scan's heap cost approaches the sequential scan's as the share rises, and what
     keeps the index ahead is cpu_tuple_cost over the rows a sequential scan would discard. At a
     4% share over 30,000 rows the measured margin is 1.40x (2026-10-04). That margin is not
     left as a claim in this docstring: MIN_COST_MARGIN asserts it against the counterfactual
     plan on every run, so erosion is reported while the planner is still choosing the index.

  3. THE PHYSICAL STATE MUST BE REPRODUCIBLE, and VACUUM, REINDEX and ANALYZE all run before any
     plan is taken. Without ANALYZE the planner works from whatever statistics the table last had
     -- usually none on a freshly migrated stack -- and every plan below is an accident. Without
     the other two the verdict drifts with how often the suite has been run: see the seed
     fixture, where the measurement that forced this is recorded. The page counts the three leave
     behind are printed with the plans.

WHAT HAPPENS IF THE PLANNER LEGITIMATELY PREFERS A SEQUENTIAL SCAN. It fails, and it says which
of the two possible reasons it is. That is deliberate: a test that skipped, or that only printed,
would be the plan-printer-wearing-a-test's-name this epic keeps finding. The diagnosis is
automatic because the index claim is asserted TWICE, at two different strengths:

  * test_the_planner_can_reach_each_index forces the planner off sequential scans
    (enable_seqscan = off) and asserts the index it then picks is the intended one. This is
    STRUCTURAL and volume-independent: it fails only if the index is gone, renamed, or no longer
    usable by the predicate -- never because the table is small.
  * test_the_default_plan_* takes the plan at default settings, the plan production actually
    gets, and asserts the same index. This is ECONOMIC and volume-dependent.

  Structural green with economic red means the index is fine and the seeded volume (or the cost
  settings) no longer favour it -- raise SEEDED_BARS or lower the target's share. Both red means
  the index or the query changed. The failure messages say so, and carry the plan.

BOTH PLANS ARE PRINTED, in text form, on a GREEN run as well as a red one, so the artifact U5
wanted exists as test output and can be pasted into tj-3mk3u5.16. pytest captures stdout per
test, so the print goes through the capture manager's suspension rather than relying on -s. The
row counts, the target's share and the server version are printed beside each plan, because a
plan without the volume it was taken at is not evidence.

WHY THE SYNCHRONOUS ENGINE when data_store runs asyncpg. The plan is the server's, chosen from
the server's statistics; psycopg2 interpolates parameters client-side, so Postgres plans a custom
plan over literals exactly as it does for a first asyncpg execution. The driver is not the
variable under test, and pg_engine is this suite's idiom for talking to Postgres directly.

ROWS. Every entry comes from insert_entry under a symbol of this run, so the session teardown's
delete-by-id and the ON DELETE CASCADE on stock_market_activity.dataset_id take every bar seeded
here. Nothing is truncated.
"""

import asyncio
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

from common.enums.data_stock import Granularity
from data.store.app.database.crud.stock.asset_market_activity import read_market_activity_data
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from schemas.data_store.stock.market_activity_data import StockDataMarketActivityQuery


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__

# ---------------------------------------------------------------------------------------------
# The volume. See the module docstring for why each number is what it is; the guard test below
# fails rather than letting an edit here quietly make the plan assertions meaningless.

# One dataset entry's bars. 1,200 one-minute bars is about three trading days -- the shape of a
# real fetch, and small enough beside the table that the dataset predicate actually narrows.
TARGET_BARS = 1_200
DECOY_BARS = 1_200
DECOY_DATASETS = 24
SEEDED_BARS = TARGET_BARS + DECOY_BARS * DECOY_DATASETS

# QUERY 2's window: a slice of the target's bars, so the range predicate is a range and not a
# synonym for "every bar this symbol has".
WINDOW_OFFSET_MINUTES = 400
WINDOW_BARS = 400

# The floor the plans must be taken above, from tj-3mk3u5.16 A3 (a fake fetch above 10,000 bars)
# and the user's real 15,424-bar run.
MEANINGFUL_TABLE_ROWS = 10_000

# The target dataset's largest share of the table for the index to be the cheap answer. Basis in
# the module docstring: above roughly a tenth, a bitmap scan's heap cost converges on a sequential
# scan's and the planner is right to stop using the index.
TARGET_SHARE_CEILING = 0.10

# The granularity every seeded bar carries, and the one QUERY 2 filters on. Named once: the label
# that reaches Postgres is derived from this member by the model's bind processor, never typed.
SEEDED_GRANULARITY = Granularity.ONE_MINUTE

# The ORDER BY the service issues and nothing else (user ruling, tj-vhboky.25 addendum, 2026-09-27).
# A plan whose sort key is not this, less the columns below, is not a plan of the query the
# service issues.
ORDER_BY = ['timestamp', 'dataset_id']


def expected_sort_key(query: 'StockDataMarketActivityQuery') -> list[str]:
    """ORDER_BY as the PLAN will show it: a column pinned to one value by the WHERE clause is dropped.

    Postgres removes a sort column that an equality predicate has already constrained to a
    constant -- the rows cannot differ in it, so sorting on it is a no-op. QUERY 1 filters
    dataset_id = <one entry>, so its plan sorts by timestamp alone even though the statement says
    ORDER BY timestamp, dataset_id. Measured on the agent stack, 2026-10-04.

    This is derived rather than hard-coded per query so that the elision is tied to the predicate
    that causes it: a read that stopped binding dataset_id would be expected to sort by both
    columns again, and a plan that still sorted by one would red. The statement's own ORDER BY is
    checked separately and unconditionally in test_the_statements_come_from_the_production_read,
    so nothing about the ruling rests on the planner's behaviour here.
    """
    return [column for column in ORDER_BY if not (column == 'dataset_id' and query.dataset_id is not None)]


EXPLAIN_OPTIONS_TEXT = 'ANALYZE, BUFFERS'
EXPLAIN_OPTIONS_JSON = 'ANALYZE, BUFFERS, FORMAT JSON'

# Planner settings forced for the two counterfactual plans. Neither FORBIDS the path it names --
# each only prices it out of reach -- which is what makes SEQUENTIAL_SCANS_OFF a test of whether
# an index path EXISTS rather than of whether one is cheap.
SEQUENTIAL_SCANS_OFF = ('enable_seqscan = off',)
INDEX_SCANS_OFF = ('enable_indexscan = off', 'enable_bitmapscan = off', 'enable_indexonlyscan = off')

# How much cheaper the index plan must be than the sequential scan it beats. The measurement, on
# the agent stack 2026-10-04 at the volume below: QUERY 1's chosen plan cost 760.74 against the
# 1062.38 of the same read with index paths priced out -- 1.40x. 1.25 leaves headroom for the
# ordinary drift of a shared stack while still going red BEFORE the planner actually flips, which
# is the point: a margin that has quietly eroded to 1.01 is a test one row away from being a
# coin-flip, and it should say so while it is still green on the index.
MIN_COST_MARGIN = 1.25


# ---------------------------------------------------------------------------------------------
# Taking the statement off the production read, and EXPLAINing it.


class _CapturedResult:
    """What read_market_activity_data does with its result: .scalars().all(). Nothing was executed."""

    def scalars(self) -> '_CapturedResult':
        return self

    def all(self) -> list[Any]:
        return []


class _StatementCapture:
    """An AsyncSession stand-in that records the statement the production read builds, and runs nothing.

    The point is fidelity, not mocking for its own sake: the predicates, the column list and the
    ORDER BY below are whatever read_market_activity_data put in them today. A real session would
    work too and would cost a round trip per query, but it would also hide a statement the read
    built and then discarded -- here there is exactly one, and the test asserts it.
    """

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _CapturedResult:
        self.statements.append(statement)
        return _CapturedResult()


def production_statement(query: StockDataMarketActivityQuery) -> Any:
    """The SELECT read_market_activity_data issues for `query`, taken from the production coroutine."""
    capture = _StatementCapture()
    asyncio.run(read_market_activity_data(capture, query))  # type: ignore[arg-type]
    assert len(capture.statements) == 1, (
        f'read_market_activity_data issued {len(capture.statements)} statements for one read; this module '
        'EXPLAINs exactly one and would otherwise be explaining the wrong one'
    )
    return capture.statements[0]


class _Explain(Executable, ClauseElement):
    """EXPLAIN (<options>) <statement>, with the statement's own bind parameters left intact.

    Compiling the statement through the connection rather than rendering it to a string keeps
    SQLAlchemy's bind processors in play -- which is what turns Granularity.ONE_MINUTE into the
    enum LABEL 'ONE_MINUTE' rather than its wire value '1min'. Rendering literals by hand is
    exactly the substitution the module docstring's enum trap is about.
    """

    inherit_cache = False

    def __init__(self, statement: Any, options: str) -> None:
        self.statement = statement
        self.options = options


@compiles(_Explain, 'postgresql')
def _compile_explain(element: _Explain, compiler: Any, **kw: Any) -> str:
    return f'EXPLAIN ({element.options}) ' + compiler.process(element.statement, **kw)


def explain_text(engine: Engine, statement: Any) -> str:
    """The human-readable EXPLAIN (ANALYZE, BUFFERS) plan: the artifact, not the assertion."""
    with engine.connect() as conn:
        rows = conn.execute(_Explain(statement, EXPLAIN_OPTIONS_TEXT)).scalars().all()
    return '\n'.join(str(row) for row in rows)


def explain_json(engine: Engine, statement: Any, *, settings: tuple[str, ...] = ()) -> dict[str, Any]:
    """The same plan as structure, for assertions, optionally with planner settings forced.

    Every setting is SET LOCAL, inside the transaction the EXPLAIN runs in, so none can leak to
    another test or to the service: they are discarded when the transaction ends.
    """
    with engine.begin() as conn:
        for setting in settings:
            conn.execute(sa.text(f'SET LOCAL {setting}'))
        raw = conn.execute(_Explain(statement, EXPLAIN_OPTIONS_JSON)).scalar_one()
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return plan[0]


# ---------------------------------------------------------------------------------------------
# Reading a plan.


def plan_nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every node of the plan, root first."""
    pending = [plan['Plan']]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(node.get('Plans', ()))


def indexes_used(plan: dict[str, Any]) -> set[str]:
    return {node['Index Name'] for node in plan_nodes(plan) if 'Index Name' in node}


def bar_table_sequential_scans(plan: dict[str, Any]) -> list[str]:
    """The node types that read stock_market_activity without an index, as they are named in the plan."""
    return [
        node['Node Type']
        for node in plan_nodes(plan)
        if node.get('Relation Name') == BAR_TABLE.name and node.get('Node Type', '').endswith('Seq Scan')
    ]


def sort_keys(plan: dict[str, Any]) -> list[str]:
    """The sort key, normalised: no quoting and no table qualifier, so the comparison is about columns."""
    for node in plan_nodes(plan):
        if 'Sort Key' in node:
            return [re.sub(r'^.*\.', '', key).strip('"') for key in node['Sort Key']]
    return []


def actual_rows(plan: dict[str, Any]) -> int:
    return int(plan['Plan']['Actual Rows'])


def total_cost(plan: dict[str, Any]) -> float:
    """The planner's estimated cost of the whole plan -- an estimate, never a measured time."""
    return float(plan['Plan']['Total Cost'])


def index_conditions(plan: dict[str, Any]) -> str:
    """Every Index Cond and Filter in the plan, joined -- the predicates as POSTGRES received them."""
    return ' '.join(
        node[key] for node in plan_nodes(plan) for key in ('Index Cond', 'Recheck Cond', 'Filter') if key in node
    )


# ---------------------------------------------------------------------------------------------
# The seed.


@dataclass(frozen=True)
class SeededBars:
    """What the table holds when the plans below are taken."""

    target_entry: sa.Row
    target_symbol: str
    window_start: datetime
    window_end: datetime
    table_rows: int
    server_version: str
    pages: dict[str, int]

    @property
    def target_share(self) -> float:
        return TARGET_BARS / self.table_rows

    def describe(self) -> str:
        pages = ', '.join(f'{name} {count}' for name, count in sorted(self.pages.items()))
        return (
            f'Postgres {self.server_version} | {BAR_TABLE.name}: {self.table_rows} rows | '
            f'target dataset {self.target_entry.id}: {TARGET_BARS} rows '
            f'({self.target_share:.2%} of the table) | {DECOY_DATASETS} decoy datasets of {DECOY_BARS} rows | '
            f"QUERY 2's window: {WINDOW_BARS} rows | pages after VACUUM and REINDEX: {pages}"
        )


@pytest.fixture(scope='module')
def seeded_bars(
    pg_engine: Engine, insert_entry: Callable[..., sa.Row], bar_values: Callable[..., dict[str, Any]], run_identity: Any
) -> SeededBars:
    """One target dataset and DECOY_DATASETS others over the same window, then VACUUM, REINDEX, ANALYZE.

    The decoys share the target's timestamps on purpose: it makes the symbol and the dataset the
    only selective predicates, which is what QUERY 1 and QUERY 2 are actually asking Postgres to
    exploit. Were the decoys placed in a different window, the range predicate alone would narrow
    the read and the plans would say nothing about either index.

    WHY VACUUM AND REINDEX, AND NOT ANALYZE ALONE. This was measured, not anticipated (2026-10-04).
    ANALYZE alone left the module passing on a fresh stack and FAILING on the same stack after a
    handful of runs: each run inserts thirty thousand bars and the session teardown deletes them,
    and neither the heap nor the indexes give those pages back. The natural key's Bitmap Index Scan
    cost climbed run over run -- 173, 213, 293 -- until a Seq Scan was cheaper and QUERY 1's plan
    flipped, on code that had not changed. That is a property of the harness's own churn, not of
    the query, the data or the schema, and a test whose verdict drifts with how many times it has
    been run is not measuring what it claims to.

    So the physical state is made reproducible before anything is planned: VACUUM reclaims the dead
    tuples earlier runs left, REINDEX rebuilds both indexes at the size their live contents need,
    and ANALYZE refreshes the statistics the planner costs from. The page counts this leaves are
    printed beside each plan, so a reader can see the state the plan was taken on rather than
    trusting that it was clean. The lock REINDEX takes is acceptable only because this suite is
    serial and points at a disposable stack, which make test-system enforces.

    Module-scoped: the seed is thirty thousand rows and the plans do not mutate it.
    """
    # BEFORE the inserts as well as after: this reclaims the space earlier runs' teardown left, so
    # the thirty thousand rows below reuse it instead of extending the heap past what they need.
    # Without it the table grows run over run (619, then 737, then 1523 pages for the same 30,330
    # live rows, measured 2026-10-04) and the costs printed with the plans are not comparable
    # between runs. Growth here happens to favour the index, so it is reproducibility that is at
    # stake rather than the verdict -- the index bloat the fixture docstring describes is the half
    # that moved the verdict.
    with pg_engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        conn.execute(sa.text(f'VACUUM {BAR_TABLE.name}'))

    target_symbol = run_identity.alt_symbol('TGT')
    target_entry = insert_entry(asset_symbol=target_symbol, granularity=SEEDED_GRANULARITY)
    start: datetime = target_entry.start

    def _bars(entry: sa.Row, count: int) -> list[dict[str, Any]]:
        return [bar_values(entry, timestamp=start + timedelta(minutes=minute)) for minute in range(count)]

    with pg_engine.begin() as conn:
        conn.execute(sa.insert(BAR_TABLE), _bars(target_entry, TARGET_BARS))
    for index in range(DECOY_DATASETS):
        decoy = insert_entry(asset_symbol=run_identity.alt_symbol(f'D{index:02d}'), granularity=SEEDED_GRANULARITY)
        with pg_engine.begin() as conn:
            conn.execute(sa.insert(BAR_TABLE), _bars(decoy, DECOY_BARS))

    # Outside a transaction (VACUUM refuses one), and before anything is planned.
    with pg_engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        conn.execute(sa.text(f'VACUUM (ANALYZE) {BAR_TABLE.name}'))
        conn.execute(sa.text(f'REINDEX TABLE {BAR_TABLE.name}'))
        conn.execute(sa.text(f'ANALYZE {BAR_TABLE.name}'))
    with pg_engine.connect() as conn:
        table_rows = conn.execute(sa.select(sa.func.count()).select_from(BAR_TABLE)).scalar_one()
        server_version = conn.execute(sa.text('SHOW server_version')).scalar_one()
        pages = dict(
            conn.execute(
                sa.text('SELECT relname, relpages FROM pg_class WHERE relname = ANY(:names)'),
                {
                    'names': [
                        BAR_TABLE.name,
                        StockMarketActivity.NATURAL_KEY_CONSTRAINT,
                        StockMarketActivity.SYMBOL_GRANULARITY_TIMESTAMP_INDEX,
                    ]
                },
            ).all()
        )

    return SeededBars(
        target_entry=target_entry,
        target_symbol=target_symbol,
        window_start=start + timedelta(minutes=WINDOW_OFFSET_MINUTES),
        # Half-open [start, end) since tj-86g751: the end is the instant AFTER the window's last bar. The
        # former `- 1` was the closed read's end and left this window one bar short (399 of 400).
        window_end=start + timedelta(minutes=WINDOW_OFFSET_MINUTES + WINDOW_BARS),
        table_rows=int(table_rows),
        server_version=str(server_version),
        pages={str(name): int(count) for name, count in pages.items()},
    )


# ---------------------------------------------------------------------------------------------
# The two reads, named once, built by production code.


@dataclass(frozen=True)
class Read:
    """One of the two reads: its name, the query the service would be given, and the index it must use."""

    label: str
    query: StockDataMarketActivityQuery
    index: str
    rows: int


@pytest.fixture(scope='module')
def dataset_read(seeded_bars: SeededBars) -> Read:
    """QUERY 1: every bar of one dataset entry.

    uq_stock_market_activity_natural_key is expected because dataset_id LEADS it
    (base_market_activity.py: NATURAL_KEY), which is the stated reason that model carries NO
    standalone dataset_id index. Naming the index rather than only forbidding a sequential scan is
    what makes this test notice if that reasoning stops holding -- a redundant dataset_id index
    added later would serve this read and red here, which is the regression the comment at
    base_market_activity.py's dataset_id column is about.
    """
    return Read(
        label='QUERY 1 -- dataset-scoped bar read',
        query=StockDataMarketActivityQuery(dataset_id=seeded_bars.target_entry.id),
        index=StockMarketActivity.NATURAL_KEY_CONSTRAINT,
        rows=TARGET_BARS,
    )


@pytest.fixture(scope='module')
def symbol_range_read(seeded_bars: SeededBars) -> Read:
    """QUERY 2: one symbol, one granularity, a timestamp range.

    ix_stock_market_activity_symbol_granularity_timestamp is expected because the read binds
    asset_symbol and granularity for equality and timestamp as a range, which is that index's
    column order exactly (stock_market_activity.py: SYMBOL_GRANULARITY_TIMESTAMP_INDEX, and the
    comment above it explaining why the value-identity columns do NOT lead).
    """
    return Read(
        label='QUERY 2 -- symbol + granularity + range bar read',
        query=StockDataMarketActivityQuery(
            asset_symbol=seeded_bars.target_symbol,
            granularity=SEEDED_GRANULARITY,
            start=seeded_bars.window_start,
            end=seeded_bars.window_end,
        ),
        index=StockMarketActivity.SYMBOL_GRANULARITY_TIMESTAMP_INDEX,
        rows=WINDOW_BARS,
    )


@pytest.fixture(scope='module')
def reads(dataset_read: Read, symbol_range_read: Read) -> dict[str, Read]:
    return {'dataset': dataset_read, 'symbol-range': symbol_range_read}


def emit(config: pytest.Config, text: str) -> None:
    """Write past pytest's capture, so the plans are output of a GREEN run and not only of a red one."""
    capture = config.pluginmanager.getplugin('capturemanager')
    if capture is None:  # pragma: no cover -- capture is always installed under this suite's pytest
        print(text)
        return
    with capture.global_and_fixture_disabled():
        print(text)


# ---------------------------------------------------------------------------------------------
# The tests.


def test_the_volumes_this_module_relies_on(seeded_bars: SeededBars) -> None:
    """The plans below are taken on a table big enough, and on a slice small enough, to mean something.

    Guards the constants the way test_store_db_layer.py guards its chunk sizes. Without this, an
    edit that dropped DECOY_DATASETS to zero, or raised TARGET_BARS to the whole table, would
    leave every assertion below passing or failing for a reason that has nothing to do with the
    indexes -- and a sequential scan would then be the planner's CORRECT answer.

    table_rows is counted from the database, not from SEEDED_BARS, so a stack that already held
    bars counts toward the floor and toward the target's share. The suite never truncates.
    """
    assert seeded_bars.table_rows >= MEANINGFUL_TABLE_ROWS, (
        f'{seeded_bars.table_rows} rows in {BAR_TABLE.name}: below the {MEANINGFUL_TABLE_ROWS}-row floor '
        f'tj-3mk3u5.16 A3 sets. A sequential scan is the honest plan on a table that size, so the index '
        'assertions below would be asserting something false'
    )
    assert seeded_bars.target_share <= TARGET_SHARE_CEILING, (
        f'the target dataset is {seeded_bars.target_share:.2%} of {BAR_TABLE.name}, above the '
        f'{TARGET_SHARE_CEILING:.0%} ceiling: a read returning that much of a table is a sequential scan '
        'by right. Raise DECOY_DATASETS or lower TARGET_BARS.'
    )
    assert WINDOW_BARS < TARGET_BARS, (
        "QUERY 2's window covers the whole target dataset, so its range predicate constrains nothing"
    )


def test_the_statements_come_from_the_production_read(reads: dict[str, Read]) -> None:
    """Both EXPLAINed statements are read_market_activity_data's, carry its ORDER BY, and nothing else.

    The ORDER BY is a user ruling (tj-vhboky.25 addendum): timestamp, dataset_id, NOTHING ELSE. A
    statement sorting by anything else is not the statement the service issues, and a plan taken
    from it would be evidence about a query no caller can produce. Asserted here on the compiled
    SQL as well as on the plans below, so a sort key the planner managed to elide cannot hide it.
    """
    for read in reads.values():
        compiled = production_statement(read.query).compile(dialect=postgresql.dialect())
        sql = ' '.join(str(compiled).split()).replace('"', '')
        order_by = sql.partition(' ORDER BY ')[2]
        assert order_by, f'{read.label}: the production read issued no ORDER BY\n{sql}'
        assert order_by == f'{BAR_TABLE.name}.timestamp, {BAR_TABLE.name}.dataset_id', (
            f'{read.label}: ORDER BY is "{order_by}", not "timestamp, dataset_id" and nothing else '
            '(user ruling, tj-vhboky.25 addendum)'
        )


def test_the_planner_can_reach_each_index(pg_engine: Engine, reads: dict[str, Read]) -> None:
    """STRUCTURAL, and volume-independent: forced off sequential scans, each read reaches its own index.

    enable_seqscan = off does not forbid a sequential scan, it prices one out of reach, so a plan
    that still shows one here means no index path EXISTS for the predicate -- the index is gone,
    renamed, or its column order no longer matches what the read binds. That is the question this
    test asks, and the answer does not depend on how many rows were seeded.

    It is the other half of the diagnosis when the default-plan tests go red: green here and red
    there means the index is intact and the volume (or the server's cost settings) no longer
    favour it; red in both means the index or the query moved.
    """
    for read in reads.values():
        plan = explain_json(pg_engine, production_statement(read.query), settings=SEQUENTIAL_SCANS_OFF)
        assert read.index in indexes_used(plan), (
            f'{read.label}: with sequential scans priced out, the planner used {sorted(indexes_used(plan))} '
            f'and not {read.index}. No usable index path exists for this read.\n'
            f'{json.dumps(plan, indent=2)}'
        )
        assert actual_rows(plan) == read.rows, (
            f'{read.label}: the forced-index plan returned {actual_rows(plan)} rows, not {read.rows}. '
            'A plan over the wrong row set says nothing about the index.'
        )


@pytest.mark.parametrize('read_name', ['dataset', 'symbol-range'])
def test_the_default_plan_uses_the_intended_index(
    read_name: str, pg_engine: Engine, reads: dict[str, Read], seeded_bars: SeededBars, pytestconfig: pytest.Config
) -> None:
    """ECONOMIC: at default settings, on a realistic table, the planner chooses the intended index.

    This is the plan production gets. Five things are asserted, and each one kills a way this
    test could be green while saying nothing:

      * the intended index appears -- not merely "an index", which the primary key would satisfy;
      * no sequential scan of stock_market_activity survives anywhere in the plan;
      * the measured row count matches the seed, so the plan was not taken over an empty scan --
        the enum trap in the module docstring, where a valid label for the wrong granularity
        returns nothing at all;
      * the sort key is timestamp, dataset_id and nothing else, so the plan belongs to the query
        the service issues (user ruling, tj-vhboky.25 addendum);
      * the index plan beats the sequential scan it replaces by at least MIN_COST_MARGIN. This is
        the assertion that keeps the four above from being a coin-flip. "The planner chose the
        index" is a boolean sitting on a continuous quantity, so it can be true by 0.1% and
        nobody would know until the day it flipped. The counterfactual is measured here --
        the same statement with index paths priced out -- and the ratio is asserted, so erosion
        is reported while the test is still green on the index rather than after it is not.

    The plan is printed whatever the verdict: it is the artifact tj-3mk3u5.16 U5 asked for.
    """
    read = reads[read_name]
    statement = production_statement(read.query)
    plan_text = explain_text(pg_engine, statement)
    plan = explain_json(pg_engine, statement)

    emit(
        pytestconfig,
        f'\n===== EXPLAIN ({EXPLAIN_OPTIONS_TEXT}) -- {read.label} =====\n'
        f'{seeded_bars.describe()}\n'
        f'expected index: {read.index}\n\n{plan_text}\n',
    )

    assert actual_rows(plan) == read.rows, (
        f'{read.label}: the plan was taken over {actual_rows(plan)} rows, not the {read.rows} seeded. '
        f'A plan over an empty or wrong row set is not evidence about an index -- check the enum labels '
        f'the predicates carry: {index_conditions(plan)}\n{plan_text}'
    )
    expected_keys = expected_sort_key(read.query)
    assert sort_keys(plan) == expected_keys, (
        f'{read.label}: the plan sorts by {sort_keys(plan)}, not {expected_keys}; this is not the '
        f'query the service issues (user ruling, tj-vhboky.25 addendum)\n{plan_text}'
    )
    assert read.index in indexes_used(plan), (
        f'{read.label}: the planner used {sorted(indexes_used(plan)) or "no index"} and not {read.index}, on '
        f'{seeded_bars.describe()}. If test_the_planner_can_reach_each_index is GREEN the index is intact and '
        f'the seeded volume no longer favours it -- raise DECOY_DATASETS. If it is RED the index or the query '
        f'moved.\n{plan_text}'
    )
    sequential = bar_table_sequential_scans(plan)
    assert not sequential, (
        f'{read.label}: the plan falls back to {sequential} on {BAR_TABLE.name} on '
        f'{seeded_bars.describe()}\n{plan_text}'
    )

    without_indexes = explain_json(pg_engine, statement, settings=INDEX_SCANS_OFF)
    assert bar_table_sequential_scans(without_indexes), (
        f'{read.label}: with index paths priced out the planner STILL did not scan {BAR_TABLE.name} '
        f'sequentially, so there is no counterfactual to measure the margin against\n'
        f'{json.dumps(without_indexes, indent=2)}'
    )
    margin = total_cost(without_indexes) / total_cost(plan)
    assert margin >= MIN_COST_MARGIN, (
        f'{read.label}: the index plan costs {total_cost(plan):.2f} against {total_cost(without_indexes):.2f} '
        f'for the sequential scan -- a margin of {margin:.2f}x, below the {MIN_COST_MARGIN}x this module '
        f'requires. The planner still chose the index, so nothing is broken YET; the margin has eroded to '
        f'where the choice is nearly a tie and the index assertions above are about to stop meaning anything. '
        f'On {seeded_bars.describe()}\n{plan_text}'
    )
