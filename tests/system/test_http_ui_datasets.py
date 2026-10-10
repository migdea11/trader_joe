"""The /ui/v1 dataset reads on real Postgres: what the SQLite real-row suite cannot prove (tj-grna9p.99).

Design: decision tj-grna9p.8 item 4 (the listing computes freshness over ALL rows, with "a measured ceiling
(1000 datasets) in the task's acceptance"), tj-grna9p.20 (the routes, and that acceptance), freshness.py and
crud/stock/dataset_catalog.py (covered_dates), half-open ranges (tj-86g751). The unit tier is
server/data/store/tests/test_ui_datasets_route.py, which runs every route over SQLite real rows; three properties
need the real database, one section each below:

  1. THE 1000-DATASET CEILING. 1000 datasets of this run, with bars, and the wall-clock time of page 1 of
     GET /ui/v1/datasets, of the facets, and of the two lists that judge EVERY row's freshness (a status filter
     and needs_attention). Measured over real HTTP through the prod image's uvicorn, warm, several times; the
     numbers are reported as a pytest warning so a green run still shows them, and the slowest warm sample is
     held under LATENCY_CEILING_SECONDS.
  2. NON-UTC BOUNDS ON THE BAR PAGE. start and end written in -05:00 and +14:00 select the same bars as their UTC
     spelling: timestamptz compares INSTANTS. SQLite stores the wall clock and drops the offset, so only here can
     a server that compared clock readings be caught.
  3. covered_dates RETURNS REAL DATES. date(timezone(zone, timestamp)) on Postgres yields a date, and in the
     calendar's zone (America/New_York for XNYS), so a STATIC dataset with bars on every session reads COMPLETE
     and one missing a session reads GAPS. SQLite returns text there, so the unit tier only has STATIC datasets
     WITHOUT bars. Two cases put a bar where the UTC date and the New York date differ, in both directions, so a
     date taken in UTC rather than the calendar's zone turns one of them the wrong way.

Entries are seeded by SQL through conftest's insert_entry (or, for the 1000, one multi-row INSERT registered with
the same entry_registry), so session teardown deletes them by id and the cascade takes their bars. Bars are
written by SQL too: the property under test is the read. Every dataset here is STATIC or DAILY over [Monday
2026-02-02 open, Tuesday 2026-02-03 close) (two XNYS sessions, both long closed), so the server's real clock decides
nothing that a test asserts: STATIC is judged on those two sessions alone and a DAILY dataset whose last bar is that
Tuesday is OVERDUE at any instant after the next session opened.

NOT conftest's BASE_START / BASE_END (2001), on purpose: those entries belong to other suites and are judged in
the same listing. The calendar is built from freshness.CALENDAR_START (1990-01-01, tj-grna9p.102), so a 2001 range
holds its real sessions and reads GAPS without bars, and a range or instant before 1990 raises CalendarRangeError
rather than reading COMPLETE or FRESH over no sessions. The unit tier pins both, in
server/data/store/tests/test_freshness.py. These tests use 2026 dates and do not depend on that bound.
"""

import statistics
import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_stock import UpdateType
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from routers.data_store.app_endpoints import UiDatasetsInterface


pytestmark = pytest.mark.data_store

ENTRY_TABLE = StoreDatasetEntry.__table__
BAR_TABLE = StockMarketActivity.__table__

LIST_PATH = UiDatasetsInterface.GET_UI_DATASETS.value
FACETS_PATH = UiDatasetsInterface.GET_UI_DATASET_FACETS.value

# Two full XNYS sessions, Monday 2026-02-02 and Tuesday 2026-02-03, each 09:30-16:00 New York (14:30-21:00 UTC;
# February, so EST, -05:00). Every entry here spans [MONDAY_OPEN, TUESDAY_CLOSE).
MONDAY_OPEN = datetime.fromisoformat('2026-02-02T14:30:00+00:00')
TUESDAY_OPEN = datetime.fromisoformat('2026-02-03T14:30:00+00:00')
TUESDAY_CLOSE = datetime.fromisoformat('2026-02-03T21:00:00+00:00')
SPAN = {'start': MONDAY_OPEN, 'end': TUESDAY_CLOSE}

COMPLETE = 'FRESHNESS_STATUS_COMPLETE'
GAPS = 'FRESHNESS_STATUS_GAPS'
OVERDUE = 'FRESHNESS_STATUS_OVERDUE'


def _dataset_path(dataset_id: UUID) -> str:
    return UiDatasetsInterface.GET_UI_DATASET.format(dataset_id=dataset_id)


def _bars_path(dataset_id: UUID) -> str:
    return UiDatasetsInterface.GET_UI_DATASET_BARS.format(dataset_id=dataset_id)


def _instant(value: str) -> datetime:
    """A protobuf JSON Timestamp ('...Z') as an aware datetime."""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f'{value!r} came back without an offset'
    return parsed


def _ok(data_store, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = data_store.get(path, params=params)
    assert response.status_code == 200, data_store.describe(response)
    return response.json()


def _status_counts(facets: dict[str, Any]) -> dict[str, int]:
    """Status -> count from a DatasetFacets body. Canonical JSON omits a zero int64 and prints the rest as text."""
    return {facet['status']: int(facet.get('count', '0')) for facet in facets.get('statuses', [])}


def _insert_bars(pg_engine: Engine, rows: list[dict[str, Any]]) -> None:
    """Write bar rows in one committed executemany. Each belongs to an entry this run registered."""
    if rows:
        with pg_engine.begin() as conn:
            conn.execute(sa.insert(BAR_TABLE), rows)


def _bars_at(entry: sa.Row, bar_values: Callable[..., dict[str, Any]], instants: list[datetime]) -> list[dict]:
    return [bar_values(entry, timestamp=instant) for instant in instants]


# ---------------------------------------------------------------------------------------------
# 1. The 1000-dataset ceiling (tj-grna9p.20 acceptance; tj-grna9p.8 item 4).

CATALOG_SIZE = 1000
# Bars per dataset: BARS_PER_SESSION one-minute bars from each session's open. Enough that covered_dates, which
# reads every bar of every STATIC dataset it is asked about, does real work; small enough to seed in seconds.
BARS_PER_SESSION = 30
# Every 10th STATIC dataset lacks Tuesday's bars (GAPS); the other STATIC ones are COMPLETE; DAILY ones hold
# Monday's only and are OVERDUE.
GAPS_EVERY = 10
WARM_SAMPLES = 5
# PROPOSED, not ruled: no number was ever recorded (tj-grna9p.8 item 4 asks for "a measured ceiling", the
# tj-grna9p.20 acceptance for "the ceiling recorded in the note"). This is the bound this suite enforces on the
# SLOWEST warm sample of any of the four reads; the measured values are in the warning this test emits and in the
# tj-grna9p.99 verdict note, for the architect to confirm or replace.
LATENCY_CEILING_SECONDS = 2.0


@dataclass(frozen=True)
class Catalog:
    prefix: str  # every symbol of the 1000 starts with this; nothing else in the database does
    ids: list[UUID]
    expected: dict[str, int]  # freshness status -> how many of the 1000 should read it
    bars: int


@pytest.fixture(scope='module')
def catalog(
    pg_engine: Engine,
    entry_values: Callable[..., dict[str, Any]],
    bar_values: Callable[..., dict[str, Any]],
    entry_registry,
    run_identity,
) -> Catalog:
    """1000 datasets of this run, half STATIC and half DAILY, each with bars, in one multi-row INSERT each table."""
    prefix = run_identity.alt_symbol('K')
    values = []
    for index in range(CATALOG_SIZE):
        update_type = UpdateType.STATIC if index % 2 == 0 else UpdateType.DAILY
        values.append(entry_values(asset_symbol=f'{prefix}{index:04d}', update_type=update_type.value, **SPAN))
    with pg_engine.begin() as conn:
        entries = conn.execute(sa.insert(ENTRY_TABLE).returning(*ENTRY_TABLE.c), values).all()
    for entry in entries:
        entry_registry.add(entry.id)

    expected = {COMPLETE: 0, GAPS: 0, OVERDUE: 0}
    bars: list[dict[str, Any]] = []
    for index, entry in enumerate(sorted(entries, key=lambda row: row.asset_symbol)):
        sessions = [MONDAY_OPEN, TUESDAY_OPEN]
        if UpdateType(entry.update_type) is UpdateType.DAILY:
            # Monday only: OVERDUE under today's rule (the last completed session is unstored) and under the
            # tj-grna9p.100 ruling (Tuesday, inside [start, end), is unstored and its deadline passed in February).
            sessions = [MONDAY_OPEN]
            expected[OVERDUE] += 1
        elif (index // 2) % GAPS_EVERY == 0:
            sessions = [MONDAY_OPEN]
            expected[GAPS] += 1
        else:
            expected[COMPLETE] += 1
        instants = [opened + timedelta(minutes=m) for opened in sessions for m in range(BARS_PER_SESSION)]
        bars.extend(_bars_at(entry, bar_values, instants))
    _insert_bars(pg_engine, bars)
    return Catalog(prefix=prefix, ids=[entry.id for entry in entries], expected=expected, bars=len(bars))


def _timed(data_store, path: str, params: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    began = time.perf_counter()
    response = data_store.get(path, params=params)
    elapsed = time.perf_counter() - began
    assert response.status_code == 200, data_store.describe(response)
    return elapsed, response.json()


def _table_counts(pg_engine: Engine) -> tuple[int, int]:
    with pg_engine.connect() as conn:
        entries = conn.execute(sa.select(sa.func.count()).select_from(ENTRY_TABLE)).scalar_one()
        bars = conn.execute(sa.select(sa.func.count()).select_from(BAR_TABLE)).scalar_one()
    return entries, bars


def test_catalog_of_1000_reads_page_one_and_the_all_row_judgements_under_the_ceiling(
    catalog: Catalog, data_store, pg_engine: Engine
) -> None:
    """Page 1, facets, a status filter and needs_attention, over 1000 datasets with bars: timed, and correct.

    Correctness first, so a fast wrong answer cannot pass: the prefix-scoped facets count exactly the seeded
    statuses, and page 1 is the first 100 of the 1000 by symbol. Then each read is timed: one cold call (the
    first after the seed), then WARM_SAMPLES warm calls; the slowest warm call of each must be under the ceiling.
    The unscoped list reads every entry in the database, not only these 1000, so its time is the honest one for
    "the catalog holds at least 1000"; the table sizes are in the report.
    """
    scoped = {'asset_symbol': catalog.prefix}
    reads: dict[str, tuple[str, dict[str, Any]]] = {
        'page 1, whole catalog': (LIST_PATH, {}),
        'page 1, the 1000': (LIST_PATH, scoped),
        'facets, the 1000': (FACETS_PATH, scoped),
        'status=failed, the 1000': (LIST_PATH, {**scoped, 'status': 'failed'}),
        'needs_attention, the 1000': (LIST_PATH, {**scoped, 'needs_attention': 'true'}),
    }

    samples: dict[str, list[float]] = {}
    bodies: dict[str, dict[str, Any]] = {}
    for name, (path, params) in reads.items():
        cold, bodies[name] = _timed(data_store, path, params)
        samples[name] = [cold] + [_timed(data_store, path, params)[0] for _ in range(WARM_SAMPLES)]

    entries_in_db, bars_in_db = _table_counts(pg_engine)
    report = '; '.join(
        f'{name}: cold {values[0] * 1000:.0f} ms, warm median {statistics.median(values[1:]) * 1000:.0f} ms, '
        f'warm max {max(values[1:]) * 1000:.0f} ms'
        for name, values in samples.items()
    )
    report = (
        f'tj-grna9p.99 latency over {CATALOG_SIZE} seeded datasets ({catalog.bars} bars), database holding '
        f'{entries_in_db} entries and {bars_in_db} bars: {report}'
    )
    warnings.warn(report, stacklevel=1)

    facets = bodies['facets, the 1000']
    assert int(facets['all']) == CATALOG_SIZE, report
    assert {status: count for status, count in _status_counts(facets).items() if count} == catalog.expected, report
    failed = catalog.expected[GAPS] + catalog.expected[OVERDUE]
    assert int(facets['needsAttention']) == failed, report

    page_one = bodies['page 1, the 1000']
    symbols = [item['assetSymbol'] for item in page_one['items']]
    assert symbols == [f'{catalog.prefix}{index:04d}' for index in range(100)], report
    assert page_one['nextCursor'], report
    # The all-row judgements return their first page of the 'failed' set, which is every DAILY and GAPS dataset.
    for name in ('status=failed, the 1000', 'needs_attention, the 1000'):
        statuses = {item['freshness']['status'] for item in bodies[name]['items']}
        assert len(bodies[name]['items']) == 100 and statuses <= {GAPS, OVERDUE}, f'{name}: {statuses}; {report}'

    too_slow = {name: max(values[1:]) for name, values in samples.items() if max(values[1:]) >= LATENCY_CEILING_SECONDS}
    assert not too_slow, f'over the {LATENCY_CEILING_SECONDS} s ceiling: {too_slow}; {report}'


# ---------------------------------------------------------------------------------------------
# 2. Non-UTC bounds on GET /ui/v1/datasets/{id}/bars.

# -05:00 in February, and +14:00: the second puts the bound's LOCAL DATE a day after its UTC date.
WEST = ZoneInfo('America/Toronto')
FAR_EAST = ZoneInfo('Pacific/Kiritimati')
BAR_MINUTES = range(6)


@pytest.fixture(scope='module')
def bar_entry(
    insert_entry: Callable[..., sa.Row], bar_values: Callable[..., dict[str, Any]], pg_engine: Engine, run_identity
) -> sa.Row:
    entry = insert_entry(asset_symbol=run_identity.alt_symbol('BARS'), **SPAN)
    _insert_bars(pg_engine, _bars_at(entry, bar_values, [_minute(m) for m in BAR_MINUTES]))
    return entry


def _minute(minute: int) -> datetime:
    return MONDAY_OPEN + timedelta(minutes=minute)


def _bar_starts(data_store, dataset_id: UUID, **params: Any) -> list[datetime]:
    """Every bar in the window, following nextCursor to the end."""
    starts: list[datetime] = []
    query = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in params.items()}
    for _ in range(100):
        body = _ok(data_store, _bars_path(dataset_id), query)
        starts.extend(_instant(bar['barStart']) for bar in body.get('bars', []))
        if not body.get('nextCursor'):
            return starts
        query['cursor'] = body['nextCursor']
    raise AssertionError(f'more than 100 pages: {starts}')


@pytest.mark.parametrize('zone', [WEST, FAR_EAST], ids=['minus-05', 'plus-14'])
def test_bar_window_in_a_non_utc_offset_compares_instants_half_open(
    zone: ZoneInfo, bar_entry: sa.Row, data_store
) -> None:
    """[minute 1, minute 4) written in another offset selects minutes 1-3, exactly as its UTC spelling does.

    A server that read the bounds as clock readings would shift the window by the offset: five hours early for
    -05:00 and fourteen hours late for +14:00, and either selects nothing (mutation-checked). The bar AT the end is
    out and the bar AT the start is in, in that offset too; paging with limit=1 carries the cursor across the
    same window.
    """
    start, end = _minute(1).astimezone(zone), _minute(4).astimezone(zone)
    assert start.utcoffset() != timedelta(0), f'precondition: {zone} is not UTC in February'
    expected = [_minute(m) for m in (1, 2, 3)]

    in_offset = _bar_starts(data_store, bar_entry.id, start=start, end=end)
    in_utc = _bar_starts(data_store, bar_entry.id, start=_minute(1), end=_minute(4))
    paged = _bar_starts(data_store, bar_entry.id, start=start, end=end, limit=1)

    assert in_utc == expected, in_utc
    assert in_offset == expected, f'start {start.isoformat()} end {end.isoformat()}: {in_offset}'
    assert paged == expected, f'limit=1 in {zone}: {paged}'


def test_bar_window_one_microsecond_either_side_in_a_non_utc_offset(bar_entry: sa.Row, data_store) -> None:
    """The edges, in -05:00: a microsecond past the end admits the end bar; a microsecond past the start drops it."""
    nudge = timedelta(microseconds=1)
    first, last = _minute(1).astimezone(WEST), _minute(4).astimezone(WEST)

    past_end = _bar_starts(data_store, bar_entry.id, start=first, end=last + nudge)
    past_start = _bar_starts(data_store, bar_entry.id, start=first + nudge, end=last)

    assert past_end == [_minute(m) for m in (1, 2, 3, 4)], past_end
    assert past_start == [_minute(m) for m in (2, 3)], past_start


# ---------------------------------------------------------------------------------------------
# 3. covered_dates on Postgres: STATIC with bars reads COMPLETE or GAPS.

# Late-evening New York bars: 01:00 UTC Tuesday is 20:00 Monday in New York; 03:00 UTC Tuesday is 22:00 Monday.
MONDAY_EVENING_AS_TUESDAY_UTC = datetime.fromisoformat('2026-02-03T01:00:00+00:00')
MONDAY_LATE_AS_TUESDAY_UTC = datetime.fromisoformat('2026-02-03T03:00:00+00:00')

# name -> (bar instants, expected status, expected gap count)
STATIC_CASES: dict[str, tuple[list[datetime], str, int]] = {
    # Both sessions held, in session hours.
    'every-session': ([MONDAY_OPEN, TUESDAY_OPEN], COMPLETE, 0),
    # Tuesday missing.
    'tuesday-missing': ([MONDAY_OPEN, MONDAY_OPEN + timedelta(hours=1)], GAPS, 1),
    # No bars at all: the one STATIC case the SQLite tier can reach, kept here as the control.
    'no-bars': ([], GAPS, 2),
    # Monday's only bar has a TUESDAY UTC date. In New York it is Monday: COMPLETE. A UTC date reads GAPS.
    'monday-bar-on-a-tuesday-utc-date': ([MONDAY_EVENING_AS_TUESDAY_UTC, TUESDAY_OPEN], COMPLETE, 0),
    # Two bars, two UTC dates, ONE New York date (Monday): GAPS 1. A UTC date reads COMPLETE.
    'two-utc-dates-one-session': ([MONDAY_OPEN, MONDAY_LATE_AS_TUESDAY_UTC], GAPS, 1),
}


@dataclass(frozen=True)
class StaticSeed:
    prefix: str
    ids: dict[str, UUID]


@pytest.fixture(scope='module')
def static_seed(
    insert_entry: Callable[..., sa.Row], bar_values: Callable[..., dict[str, Any]], pg_engine: Engine, run_identity
) -> Iterator[StaticSeed]:
    prefix = run_identity.alt_symbol('ST')
    ids: dict[str, UUID] = {}
    bars: list[dict[str, Any]] = []
    for position, (name, (instants, _, _)) in enumerate(STATIC_CASES.items()):
        entry = insert_entry(asset_symbol=f'{prefix}{position}', update_type=UpdateType.STATIC.value, **SPAN)
        ids[name] = entry.id
        bars.extend(_bars_at(entry, bar_values, instants))
    _insert_bars(pg_engine, bars)
    yield StaticSeed(prefix=prefix, ids=ids)


@pytest.mark.parametrize('case', list(STATIC_CASES))
def test_static_dataset_with_bars_reads_complete_or_gaps_by_new_york_session(
    case: str, static_seed: StaticSeed, data_store
) -> None:
    """GET /ui/v1/datasets/{id}: freshness of a STATIC dataset from the session dates Postgres computes."""
    _, status, gap_count = STATIC_CASES[case]

    body = _ok(data_store, _dataset_path(static_seed.ids[case]))

    freshness = body['freshness']
    assert (freshness['status'], int(freshness.get('gapCount', 0))) == (status, gap_count), freshness


def test_static_listing_status_filters_and_facets_agree_with_the_detail(static_seed: StaticSeed, data_store) -> None:
    """The list and the facets judge the same rows in one covered_dates statement; they must say what detail says."""
    scoped = {'asset_symbol': static_seed.prefix}
    by_status: dict[str, set[UUID]] = {}
    for name, (_, status, _) in STATIC_CASES.items():
        by_status.setdefault(status, set()).add(static_seed.ids[name])

    healthy = _ok(data_store, LIST_PATH, {**scoped, 'status': 'healthy'})
    failed = _ok(data_store, LIST_PATH, {**scoped, 'status': 'failed'})
    facets = _ok(data_store, FACETS_PATH, scoped)

    assert {UUID(item['id']) for item in healthy.get('items', [])} == by_status[COMPLETE], healthy
    assert {UUID(item['id']) for item in failed.get('items', [])} == by_status[GAPS], failed
    counts = {status: count for status, count in _status_counts(facets).items() if count}
    assert counts == {status: len(ids) for status, ids in by_status.items()}, facets
