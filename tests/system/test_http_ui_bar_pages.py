"""GET /ui/v1/datasets/{id}/bar-pages and the /bars cap on real Postgres (validator, tj-grna9p.120 and .125).

Design: the 2026-10-13 OWNER RULINGS note on decision tj-eozqhi, FIXED BLOCKS, superseding the decision's 950
step; and tj-grna9p.125, one /bars read returns up to 50,000 bars.

The rule: with N bars in the half-open window, q = N div 1000 and r = N mod 1000, pages are fixed 1,000-bar blocks
counted back from the newest bar, at r, r + 1000, ..., N - 1000, plus the earliest page at 0 when r > 0 (it holds
1,000 bars, so it overlaps its newer neighbour by 1000 - r). M = q + (r > 0). overlap is the client's look-ahead
and never moves an offset.

The unit tier is the bar page index section of server/data/store/tests/test_ui_datasets_route.py, which runs the
route over SQLite real rows. This file holds what needs the real database and the real service:

  1. THE OFFSET TABLE, THROUGH THE PROD IMAGE. One 3000-bar one-minute dataset; each N of the ruling's table is a
     window [first bar, first bar + N minutes) over it, so one seed serves every row. The offsets are written out,
     not recomputed, and each page's start is the bar at its offset. A second dataset of 180,437 bars pins the
     ruling's year-of-minutes example: 181 pages, [0, 437, 1437, ..., 179437].
  2. INSTANTS. The window given in -05:00 and +14:00 selects the same bars as its UTC spelling (timestamptz), and the
     bar AT end is excluded.
  3. READ-BACK. Page k read through the bars route (start = its start, end = the window's end, limit = size) is
     exactly bars [offset, offset + size); the full blocks are disjoint and the earliest overlaps by 1000 - r.
  4. THE PLAN SHAPE OF THE PRODUCTION STATEMENT, structurally: with sequential scans priced out, the window is read
     by an index range scan in timestamp order and the row_number() runs as a WindowAgg with NO Sort below it, so
     it streams. WHICH index is not pinned: measured 2026-10-13, the planner picks
     ix_stock_market_activity_symbol_granularity_timestamp and filters dataset_id, source and feed, not the
     natural key the repository docstring names (finding on the bead). The statement is taken from
     read_bar_page_starts itself (a session double that records it), never retyped. Asserted on shape only; the
     plan is printed as a warning. The cost at 100k, 1M and 2.4M bars was measured by hand and is recorded on the
     bead, not here: seeding millions of rows per run is not a test's job.
  5. ONE 20,000-BAR READ (tj-grna9p.125). A 20,000-bar window of the large dataset read in ONE request at limit
     50,000: complete, ascending, no cursor; its wall-clock time is printed as a warning, not asserted. And the cap
     itself on the prod image: 50,000 is a full page, 50,001 a 422.
"""

import asyncio
import json
import time
import warnings
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ClauseElement

from data.store.app.database.crud.stock.dataset_catalog import CatalogRow, read_bar_page_starts
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from routers.data_store.app_endpoints import UiDatasetsInterface
from tests.system.problem_json import assert_problem


pytestmark = pytest.mark.data_store

BAR_TABLE = StockMarketActivity.__table__

# A Monday, long past; nothing about the clock matters to this read.
FIRST_BAR = datetime.fromisoformat('2026-03-02T00:00:00+00:00')
SEEDED = 3000

OFFSET_TABLE = {
    0: [],
    1: [0],
    999: [0],  # N <= size: the single page
    1000: [0],  # r = 0, q = 1: the one block is offset 0, listed once
    1001: [0, 1],  # r = 1: earliest [0, 1000), newest block [1, 1001)
    1950: [0, 950],
    1951: [0, 951],  # was [0, 950, 951] under the 950 step
    1952: [0, 952],  # was [0, 950, 952]
    2500: [0, 500, 1500],  # r = 500
    3000: [0, 1000, 2000],  # r = 0: disjoint blocks; was [0, 950, 1900, 2000]
}

# The ruling's year-of-minutes example, in a dataset of its own (another symbol of this run, a later range).
LARGE_FIRST_BAR = datetime.fromisoformat('2026-04-06T00:00:00+00:00')
LARGE = 180_437
LARGE_OFFSETS = [0, *range(437, LARGE, 1000)]
assert (len(LARGE_OFFSETS), LARGE_OFFSETS[:3], LARGE_OFFSETS[-1]) == (181, [0, 437, 1437], 179_437)

# tj-grna9p.125: the /bars cap. The single-request read below is a 20,000-bar window of the large dataset.
MAX_BARS = 50_000
ONE_READ = 20_000


def minute(index: int) -> datetime:
    return FIRST_BAR + timedelta(minutes=index)


def large_minute(index: int) -> datetime:
    return LARGE_FIRST_BAR + timedelta(minutes=index)


def zulu(at: datetime) -> str:
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


def _pages_path(dataset_id: UUID) -> str:
    return UiDatasetsInterface.GET_UI_DATASET_BAR_PAGES.format(dataset_id=dataset_id)


def _bars_path(dataset_id: UUID) -> str:
    return UiDatasetsInterface.GET_UI_DATASET_BARS.format(dataset_id=dataset_id)


def _ok(data_store, path: str, params: dict[str, Any]) -> dict[str, Any]:
    response = data_store.get(path, params=params)
    assert response.status_code == 200, data_store.describe(response)
    return response.json()


def _offsets(body: dict[str, Any]) -> list[int]:
    """Canonical JSON prints an int64 as text and omits a zero: an absent offset is 0."""
    return [int(page.get('offset', '0')) for page in body.get('pages', [])]


@pytest.fixture(scope='module')
def series(insert_entry: Callable[..., sa.Row], bar_values: Callable[..., dict[str, Any]], pg_engine: Engine) -> sa.Row:
    """One entry of this run with SEEDED one-minute bars from FIRST_BAR. insert_entry registers it for cleanup."""
    entry = insert_entry(start=FIRST_BAR, end=minute(SEEDED))
    rows = [bar_values(entry, timestamp=minute(index), close=float(index)) for index in range(SEEDED)]
    with pg_engine.begin() as conn:
        conn.execute(sa.insert(BAR_TABLE), rows)
    return entry


@pytest.fixture(scope='module')
def large_series(
    insert_entry: Callable[..., sa.Row], bar_values: Callable[..., dict[str, Any]], pg_engine: Engine, run_identity
) -> sa.Row:
    """LARGE one-minute bars from LARGE_FIRST_BAR under a symbol of its own; cleanup cascades from the entry."""
    entry = insert_entry(asset_symbol=run_identity.alt_symbol('PG'), start=LARGE_FIRST_BAR, end=large_minute(LARGE))
    rows = [bar_values(entry, timestamp=large_minute(index), close=float(index)) for index in range(LARGE)]
    with pg_engine.begin() as conn:
        conn.execute(sa.insert(BAR_TABLE), rows)
    return entry


# ---------------------------------------------------------------------------------------------
# 1. The offset table.


@pytest.mark.parametrize(('count', 'expected'), OFFSET_TABLE.items(), ids=[f'N={n}' for n in OFFSET_TABLE])
def test_the_page_offsets_are_the_owner_s_fixed_block_table_on_postgres(data_store, series, count, expected):
    body = _ok(data_store, _pages_path(series.id), {'start': zulu(minute(0)), 'end': zulu(minute(count))})
    assert int(body.get('barCount', '0')) == count, body
    assert _offsets(body) == expected
    assert [page['start'] for page in body.get('pages', [])] == [zulu(minute(offset)) for offset in expected]
    assert (body['pageSize'], body['overlap']) == (1000, 50)
    if expected:
        assert 'offset' not in body['pages'][0], 'canonical JSON omits a zero offset'


@pytest.mark.parametrize('overlap', [0, 999])
def test_the_overlap_never_moves_an_offset_on_postgres(data_store, series, overlap):
    window = {'start': zulu(minute(0)), 'end': zulu(minute(2500)), 'overlap': overlap}
    assert _offsets(_ok(data_store, _pages_path(series.id), window)) == [0, 500, 1500]


def test_a_year_of_minutes_is_181_pages_on_postgres(data_store, large_series):
    body = _ok(data_store, _pages_path(large_series.id), {})
    assert int(body['barCount']) == LARGE
    assert _offsets(body) == LARGE_OFFSETS
    assert [page['start'] for page in body['pages']] == [zulu(large_minute(offset)) for offset in LARGE_OFFSETS]


# ---------------------------------------------------------------------------------------------
# 2. Instants and the half-open window.


@pytest.mark.parametrize('zone', ['+00:00', '-05:00', '+14:00'])
def test_a_window_in_any_offset_selects_the_same_bars(data_store, series, zone):
    """[minute 10, minute 1962) in three spellings: N = 1952 each time, the bar AT end never counted."""
    offset = timedelta(hours=int(zone[:3]))
    start = (minute(10) + offset).strftime('%Y-%m-%dT%H:%M:%S') + zone
    end = (minute(1962) + offset).strftime('%Y-%m-%dT%H:%M:%S') + zone
    body = _ok(data_store, _pages_path(series.id), {'start': start, 'end': end})
    assert (int(body['barCount']), _offsets(body)) == (1952, [0, 952])
    assert [page['start'] for page in body['pages']] == [zulu(minute(10 + k)) for k in (0, 952)]


def test_an_inverted_window_is_an_empty_index_and_an_unknown_dataset_a_404(data_store, series):
    body = _ok(data_store, _pages_path(series.id), {'start': zulu(minute(20)), 'end': zulu(minute(5))})
    assert body == {'pageSize': 1000, 'overlap': 50}
    response = data_store.get(_pages_path(UUID(int=0)))
    assert_problem(response, status=404, reason='NOT_FOUND', title='Not Found', describe=data_store.describe)


def test_overlap_not_below_size_is_a_422(data_store, series):
    response = data_store.get(_pages_path(series.id), params={'size': 100, 'overlap': 100})
    assert_problem(
        response, status=422, reason='INVALID_REQUEST', title='Unprocessable Entity', describe=data_store.describe
    )


# ---------------------------------------------------------------------------------------------
# 3. Read-back through the bars route.


@pytest.mark.parametrize(('first', 'count'), [(10, 1952), (0, 2500), (0, 3000)], ids=['r=952', 'r=500', 'r=0'])
def test_each_page_read_back_is_one_fixed_block_of_a_thousand_bars(data_store, series, first, count):
    """Every page is 1000 bars; the full blocks are disjoint; the earliest overlaps by 1000 - r when r > 0."""
    end = zulu(minute(first + count))
    body = _ok(data_store, _pages_path(series.id), {'start': zulu(minute(first)), 'end': end})
    window = [zulu(minute(index)) for index in range(first, first + count)]
    pages: list[list[str]] = []
    for offset, page in zip(_offsets(body), body['pages'], strict=True):
        bars = _ok(data_store, _bars_path(series.id), {'start': page['start'], 'end': end, 'limit': 1000})
        read = [item['barStart'] for item in bars['bars']]
        assert read == window[offset : offset + 1000]
        assert len(read) == 1000
        pages.append(read)
    shared = [len(set(before) & set(after)) for before, after in pairwise(pages)]
    remainder = count % 1000
    assert shared == ([1000 - remainder] if remainder else [0]) + [0] * (len(pages) - 2), shared
    assert pages[-1][-1] == window[-1], 'the window ends on the newest page'
    assert sorted({start for page in pages for start in page}) == window


# ---------------------------------------------------------------------------------------------
# 4. The plan shape of the production statement.


class _Capture:
    """A session double: records the statement read_bar_page_starts builds and returns no rows."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> '_Capture':
        self.statements.append(statement)
        return self

    def all(self) -> list[Any]:
        return []


class _Explain(Executable, ClauseElement):
    """EXPLAIN (<options>) <statement>, compiled through the connection so the enum bind processors apply."""

    inherit_cache = False

    def __init__(self, statement: Any, options: str) -> None:
        self.statement = statement
        self.options = options


@compiles(_Explain, 'postgresql')
def _compile_explain(element: _Explain, compiler: Any, **kw: Any) -> str:
    return f'EXPLAIN ({element.options}) ' + compiler.process(element.statement, **kw)


def index_statement(entry: sa.Row, start: datetime | None, end: datetime | None) -> Any:
    """The one SELECT read_bar_page_starts issues for this entry and window, taken from the production coroutine."""
    row = CatalogRow(
        id=entry.id,
        asset_symbol=entry.asset_symbol,
        asset_type=entry.asset_type,
        data_type=entry.data_type,
        source=entry.source,
        feed=entry.feed,
        granularity=entry.granularity,
        start=entry.start,
        end=entry.end,
        update_type=entry.update_type,
        expiry_type=entry.expiry_type,
        expiry=entry.expiry,
        owner=entry.owner,
        last_bar=None,
    )
    capture = _Capture()
    asyncio.run(read_bar_page_starts(capture, row, start=start, end=end, size=1000, overlap=50))  # type: ignore[arg-type]
    assert len(capture.statements) == 1, 'the page index is one statement'
    return capture.statements[0]


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    pending = [plan['Plan']]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(node.get('Plans', ()))


def test_the_index_streams_a_window_agg_over_an_index_range_with_no_sort(pg_engine: Engine, series):
    statement = index_statement(series, minute(0), minute(SEEDED))
    with pg_engine.begin() as conn:
        conn.execute(sa.text(f'ANALYZE {BAR_TABLE.name}'))
        conn.execute(sa.text('SET LOCAL enable_seqscan = off'))
        raw = conn.execute(_Explain(statement, 'ANALYZE, BUFFERS, FORMAT JSON')).scalar_one()
        text = '\n'.join(conn.execute(_Explain(statement, 'ANALYZE, BUFFERS')).scalars().all())
    plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]
    warnings.warn(f'bar-pages index plan, {SEEDED} bars, enable_seqscan off:\n{text}', stacklevel=1)

    (window_agg,) = [node for node in _nodes(plan) if node['Node Type'] == 'WindowAgg']
    # The WindowAgg's own input, InitPlans (the count) excluded: the ranked rows must arrive in timestamp order
    # from an index range scan, with no Sort between, so the ranks stream. The ORDER BY rk above the filter sorts
    # only the page rows (3 here) and is not the concern.
    (feed,) = [child for child in window_agg.get('Plans', ()) if child.get('Parent Relationship') != 'InitPlan']
    below = list(_nodes({'Plan': feed}))
    assert not [node for node in below if node['Node Type'] in ('Sort', 'Incremental Sort')], (
        f'the ranks are sorted rather than streamed in index order:\n{text}'
    )
    assert feed['Node Type'] in ('Index Scan', 'Index Only Scan'), text
    assert feed.get('Relation Name') == BAR_TABLE.name, text
    assert feed['Actual Rows'] == SEEDED, text
    # Was 4 under the 950 step: N = 3000 is now three disjoint blocks, r = 0.
    assert plan['Plan']['Actual Rows'] == 3, f'the rows leaving the database for N={SEEDED} are its 3 pages:\n{text}'


# ---------------------------------------------------------------------------------------------
# 5. One 20,000-bar read, and the cap (tj-grna9p.125).


def test_a_20000_bar_window_reads_complete_and_ordered_in_one_request(data_store, large_series):
    first = 1_234
    window = {'start': zulu(large_minute(first)), 'end': zulu(large_minute(first + ONE_READ)), 'limit': MAX_BARS}
    began = time.perf_counter()
    response = data_store.get(_bars_path(large_series.id), params=window)
    elapsed = time.perf_counter() - began
    assert response.status_code == 200, data_store.describe(response)
    body = response.json()
    warnings.warn(
        f'tj-grna9p.125: one /bars read of {ONE_READ} bars at limit {MAX_BARS}: {elapsed * 1000:.0f} ms, '
        f'{len(response.content)} bytes',
        stacklevel=1,
    )
    starts = [item['barStart'] for item in body['bars']]
    assert starts == [zulu(large_minute(index)) for index in range(first, first + ONE_READ)], 'complete, ascending'
    assert [item['close'] for item in (body['bars'][0], body['bars'][-1])] == [1234.0, 21233.0]
    assert 'nextCursor' not in body, 'the whole window fits one read: nothing left to page'


def test_the_bars_cap_is_50000_on_the_prod_image(data_store, large_series):
    accepted = _ok(data_store, _bars_path(large_series.id), {'limit': MAX_BARS})
    assert len(accepted['bars']) == MAX_BARS, 'a full page at the cap'
    assert accepted.get('nextCursor'), 'the dataset holds more than the cap, so the full page carries a cursor'
    response = data_store.get(_bars_path(large_series.id), params={'limit': MAX_BARS + 1})
    assert_problem(
        response, status=422, reason='INVALID_REQUEST', title='Unprocessable Entity', describe=data_store.describe
    )
