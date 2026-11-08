"""The DAILY refresh tail follows the newest STORED bar; freshness follows the newest REAL bar (validator, tj-7zjov1.115).

DESIGN: task tj-7zjov1.114 (its RULE paragraph and acceptance), built on P40 (tj-7zjov1.103, ADR tj-26jt9w addendum (7)
item 7) and the DAILY refresh rules in server/data/store/CLAUDE.md (tj-grna9p.22, tj-grna9p.167). The rule as built in
62b27c2 and documented there:
    - the tail starts just after the newest STORED bar, a placeholder (volume 0 and trade count 0) included;
    - it reaches BACK to the newest real bar when that bar is provisional (written before its period closed plus the
      settle margin), and to every stored placeholder not yet SETTLED: written before its period's close plus the
      settle margin plus PLACEHOLDER_GRACE (7 days). A period holding no session has no close, so its pad is settled;
    - it is clamped to the declared start; the declared start is used with no bars at all; a tail that starts at or
      after the end (or now) is not requested and is counted as skipped;
    - since abc1361, only a bar that CAN be a real period counts: a bar stamped at or after now, a bar outside the
      declared range [start, end), and a placeholder in a period with no session (or one the calendar cannot place)
      are ignored by the tail (still stored); a real bar with trades always counts. A HOLE (a session period with no
      bar between two consecutive bars of the run: the newest real bar, then the pads after it) is reached back to.
      The placeholder read pages (5,000 a page, newest first) while the page just read ends in an unsettled pad;
    - freshness, the LATE or OVERDUE selection and the pass order still judge the newest REAL bar;
    - DOCUMENTED RESIDUAL: placeholders are read from PLACEHOLDER_LOOKBACK (42 days) before the newest real bar, at
      most PLACEHOLDER_SCAN_LIMIT (5,000) per dataset; an unsettled placeholder outside that is not asked for again.
      A placeholder the vendor turns real only after it has settled is not asked for again either.

THE OWNER'S FIRST CONCERN IS INTEGRITY: no gap and no stale bar. So beyond the builder's cases this file pins, as
literal instants never computed by the code under test: the grace boundary one second either side and its settle
margin; DST, a holiday, an early close and a stray weekend pad; the 1min session close and the 1week and 1month
period close; two placeholder runs separated by real bars; the clamp; the empty-tail skip; the 42-day and 5,000
edges (what is NOT re-fetched, so the residual cannot grow silently); a seeded property over random stores (every
unsettled bar and every period after the newest stored bar lies in the requested window, and the window is no wider
than that); and a day-by-day simulation of passes against a vendor that pads a dead series and later turns some pads
real, asserting no session is left uncovered, every pad turned real within the grace is stored real, the window
never grows, and a repeated pass is idempotent.

TIERS. The pure tail rule (_fetch_start) over CatalogRows with the real XNYS calendar. recent_placeholder_bars over
SQLite. The real pass (catalog read, placeholder read, plan, worker, run recorder) over SQLite with an injected
clock; the Postgres-only writes are replaced: the entry upsert answers the existing id, and the bar upsert is
EMULATED (delete then insert on the natural key, updated_at set to the injected clock), which is what
build_market_activity_upsert's ON CONFLICT DO UPDATE does (test_market_activity_idempotency.py pins that statement:
every correctable column, volume and trade count included, and updated_at = now()). That Postgres runs it is
tests/system/test_daily_refresh_pass.py's subject.

The settle margin is the default, 30 minutes, written by hand; the environment variable is cleared for every test.
NYSE closes are 16:00 local: 20:00Z under EDT (until Sun 1 Nov 2026), 21:00Z under EST; Fri 27 Nov 2026 closes
early, 13:00 local (18:00Z); Thu 26 Nov 2026 (Thanksgiving) is no session. A 1day bar is stamped at local midnight:
04:00Z under EDT, 05:00Z under EST.
"""

import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app import daily_refresh as refresh_module
from data.store.app.daily_refresh import (
    PLACEHOLDER_GRACE,
    PLACEHOLDER_LOOKBACK,
    PLACEHOLDER_SCAN_LIMIT,
    DailyRefresher,
    PassResult,
    _fetch_start,
    _request_for,
)
from data.store.app.database.crud.stock import dataset_catalog as catalog_repo
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow, PlaceholderBar
from data.store.app.database.models.base_market_activity import BaseMarketActivity
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.ingest import data_action_request, run_recording
from data.store.app.trading_calendar import calendar_for_source
from data.store.tests.test_daily_refresh import _engine
from data.store.tests.test_ui_datasets_route import (  # noqa: F401 (aware_sqlite_datetimes is a fixture)
    aware_sqlite_datetimes,
    bar,
    entry,
)
from schemas.data_ingest import fetch_dataset


pytestmark = pytest.mark.data_store

XNYS = calendar_for_source(DataSource.ALPACA_API)
NEW_YORK = ZoneInfo('America/New_York')
DAY = Granularity.ONE_DAY
MINUTE = Granularity.ONE_MINUTE
WEEK = Granularity.ONE_WEEK
MONTH = Granularity.ONE_MONTH
SETTLE = timedelta(minutes=30)


def z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


# Written long after every period in this file closed, plus the grace: a real bar written then is final and a
# placeholder written then is settled.
LONG_AFTER = z('2027-06-01T00:00:00Z')
SEPT_1 = z('2026-09-01T04:00:00Z')


@pytest.fixture(autouse=True)
def default_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(refresh_module.SETTLE_ENV, raising=False)


def catalog_row(
    granularity: Granularity = DAY,
    *,
    start: datetime = SEPT_1,
    last_bar: str | None = None,
    written: str | datetime = LONG_AFTER,
) -> CatalogRow:
    """A DAILY row whose newest REAL bar is `last_bar`, written at `written` (final unless a test says otherwise)."""
    last = None if last_bar is None else z(last_bar)
    return CatalogRow(
        id=uuid.uuid4(),
        asset_symbol='STL',
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        feed=Feed.IEX,
        granularity=granularity,
        start=start,
        end=None,
        update_type=UpdateType.DAILY,
        expiry_type=ExpiryType.ROLLING,
        expiry=None,
        owner='operator',
        last_bar=last,
        last_bar_written_at=None if last is None else (z(written) if isinstance(written, str) else written),
        collect=True,
    )


def pads(*stamps: tuple[str, str | datetime]) -> list[PlaceholderBar]:
    """Stored placeholders as recent_placeholder_bars returns them: newest first."""
    bars = [
        PlaceholderBar(timestamp=z(at), written_at=z(written) if isinstance(written, str) else written)
        for at, written in stamps
    ]
    return sorted(bars, key=lambda placeholder: placeholder.timestamp, reverse=True)


# ------------------------------------------------------------------------------------------------- the rule, by case
# (granularity, declared start, newest real bar, its write time, stored placeholders, the tail start expected).

CASES = {
    # (1) The builder's first case and the bead's acceptance: a final placeholder tail is not asked for again. The old
    # rule (after the newest REAL bar) gives 2 Oct.
    'final placeholder tail: after the newest stored placeholder': (
        DAY,
        SEPT_1,
        '2026-10-01T04:00:00Z',
        LONG_AFTER,
        pads(
            ('2026-10-02T04:00:00Z', LONG_AFTER),
            ('2026-10-05T04:00:00Z', LONG_AFTER),
            ('2026-10-06T04:00:00Z', LONG_AFTER),
        ),
        '2026-10-07T04:00:00Z',
    ),
    'placeholder-only dataset with final placeholders: after the newest one, not the declared start': (
        DAY,
        SEPT_1,
        None,
        LONG_AFTER,
        pads(('2026-10-05T04:00:00Z', LONG_AFTER), ('2026-10-06T04:00:00Z', LONG_AFTER)),
        '2026-10-07T04:00:00Z',
    ),
    'no bars at all: the declared start': (DAY, SEPT_1, None, LONG_AFTER, [], '2026-09-01T04:00:00Z'),
    # (2) A provisional real bar OLDER than a newer final placeholder: the tail reaches back to the real bar.
    'provisional real bar older than a newer final placeholder: at the real bar': (
        DAY,
        SEPT_1,
        '2026-10-05T04:00:00Z',
        '2026-10-05T15:00:00Z',
        pads(('2026-10-06T04:00:00Z', LONG_AFTER)),
        '2026-10-05T04:00:00Z',
    ),
    # Grace is not the provisional rule: written after close + margin (final for a real bar) but within the grace.
    'placeholder written after its close but within the grace: re-fetched from it': (
        DAY,
        SEPT_1,
        '2026-10-05T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-06T04:00:00Z', '2026-10-06T20:31:00Z')),
        '2026-10-06T04:00:00Z',
    ),
    'placeholder written part-way through its session: re-fetched from it': (
        DAY,
        SEPT_1,
        '2026-10-05T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-06T04:00:00Z', '2026-10-06T15:00:00Z')),
        '2026-10-06T04:00:00Z',
    ),
    # (3) The OLDEST unsettled wins, whatever settled ones lie between or after it.
    'oldest unsettled placeholder wins over settled ones around it': (
        DAY,
        SEPT_1,
        '2026-10-01T04:00:00Z',
        LONG_AFTER,
        pads(
            ('2026-10-02T04:00:00Z', '2026-10-03T12:00:00Z'),
            ('2026-10-05T04:00:00Z', LONG_AFTER),
            ('2026-10-06T04:00:00Z', '2026-10-08T12:00:00Z'),
            ('2026-10-07T04:00:00Z', LONG_AFTER),
        ),
        '2026-10-02T04:00:00Z',
    ),
    # Two placeholder runs separated by real bars (the newest real bar is 5 Oct): the older run reaches back past it.
    'two runs, the older run (before the newest real bar) unsettled': (
        DAY,
        SEPT_1,
        '2026-10-05T04:00:00Z',
        LONG_AFTER,
        pads(
            ('2026-10-01T04:00:00Z', '2026-10-01T15:00:00Z'),
            ('2026-10-06T04:00:00Z', LONG_AFTER),
            ('2026-10-07T04:00:00Z', LONG_AFTER),
        ),
        '2026-10-01T04:00:00Z',
    ),
    'two runs, only the newer run unsettled': (
        DAY,
        SEPT_1,
        '2026-10-05T04:00:00Z',
        LONG_AFTER,
        pads(
            ('2026-10-01T04:00:00Z', LONG_AFTER),
            ('2026-10-06T04:00:00Z', LONG_AFTER),
            ('2026-10-07T04:00:00Z', '2026-10-07T21:00:00Z'),
        ),
        '2026-10-07T04:00:00Z',
    ),
    'two runs, both settled: after the newest stored': (
        DAY,
        SEPT_1,
        '2026-10-05T04:00:00Z',
        LONG_AFTER,
        pads(
            ('2026-10-01T04:00:00Z', LONG_AFTER),
            ('2026-10-06T04:00:00Z', LONG_AFTER),
            ('2026-10-07T04:00:00Z', LONG_AFTER),
        ),
        '2026-10-08T04:00:00Z',
    ),
    # (5) The clamp: never before the declared start, from an unsettled placeholder or a provisional real bar.
    'unsettled placeholder before the declared start: the declared start': (
        DAY,
        z('2026-10-09T04:00:00Z'),
        None,
        LONG_AFTER,
        pads(('2026-10-06T04:00:00Z', '2026-10-06T15:00:00Z')),
        '2026-10-09T04:00:00Z',
    ),
    'provisional real bar before the declared start: the declared start': (
        DAY,
        z('2026-10-09T04:00:00Z'),
        '2026-10-05T04:00:00Z',
        '2026-10-05T15:00:00Z',
        [],
        '2026-10-09T04:00:00Z',
    ),
    # (7) Calendar edges. A holiday holds no session: its pad can be no real period, so it is IGNORED by the tail
    # (abc1361) and the tail starts after the real bar of Wed 25 Nov, at Thanksgiving's own local midnight.
    'Thanksgiving placeholder is ignored': (
        DAY,
        SEPT_1,
        '2026-11-25T05:00:00Z',
        LONG_AFTER,
        pads(('2026-11-26T05:00:00Z', '2026-11-26T15:00:00Z')),
        '2026-11-26T05:00:00Z',
    ),
    'Thanksgiving placeholder ignored, settled, after the early close one': (
        DAY,
        SEPT_1,
        '2026-11-25T05:00:00Z',
        LONG_AFTER,
        pads(('2026-11-26T05:00:00Z', LONG_AFTER), ('2026-11-27T05:00:00Z', LONG_AFTER)),
        '2026-11-28T05:00:00Z',
    ),
    # Early close 18:00Z: settled at 4 Dec 18:30Z, one second either side.
    'early-close placeholder one second inside the grace': (
        DAY,
        SEPT_1,
        '2026-11-25T05:00:00Z',
        LONG_AFTER,
        pads(('2026-11-27T05:00:00Z', '2026-12-04T18:29:59Z')),
        '2026-11-27T05:00:00Z',
    ),
    'early-close placeholder at the grace boundary': (
        DAY,
        SEPT_1,
        '2026-11-25T05:00:00Z',
        LONG_AFTER,
        pads(('2026-11-27T05:00:00Z', '2026-12-04T18:30:00Z')),
        '2026-11-28T05:00:00Z',
    ),
    'stray Saturday placeholder is ignored: after the real Friday': (
        DAY,
        SEPT_1,
        '2026-10-09T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-10T04:00:00Z', '2026-10-10T12:00:00Z')),
        '2026-10-10T04:00:00Z',
    ),
    # The past-dated half of F1: a Saturday pad past a Friday the vendor never served no longer hides the Friday.
    'stray Saturday placeholder past a missing Friday: the Friday is asked for': (
        DAY,
        SEPT_1,
        '2026-10-08T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-10T04:00:00Z', LONG_AFTER)),
        '2026-10-09T04:00:00Z',
    ),
    # Real bars always count, even in a period with no session: data the vendor served.
    'real bar on a Saturday counts': (DAY, SEPT_1, '2026-10-10T04:00:00Z', LONG_AFTER, [], '2026-10-11T04:00:00Z'),
    # HOLES inside the trailing run (the newest real bar, then the pads after it): the tail reaches back to the bar
    # before the first session with no bar of its own.
    'hole between two pads of the run: after the earlier pad': (
        DAY,
        SEPT_1,
        '2026-10-01T04:00:00Z',
        LONG_AFTER,
        pads(
            ('2026-10-02T04:00:00Z', LONG_AFTER),
            ('2026-10-06T04:00:00Z', LONG_AFTER),
            ('2026-10-07T04:00:00Z', LONG_AFTER),
        ),
        '2026-10-03T04:00:00Z',
    ),
    'hole between the real bar and the first pad: after the real bar': (
        DAY,
        SEPT_1,
        '2026-10-01T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-05T04:00:00Z', LONG_AFTER), ('2026-10-06T04:00:00Z', LONG_AFTER)),
        '2026-10-02T04:00:00Z',
    ),
    'two holes: after the bar before the OLDER one': (
        DAY,
        SEPT_1,
        '2026-10-01T04:00:00Z',
        LONG_AFTER,
        pads(
            ('2026-10-02T04:00:00Z', LONG_AFTER),
            ('2026-10-06T04:00:00Z', LONG_AFTER),
            ('2026-10-08T04:00:00Z', LONG_AFTER),
        ),
        '2026-10-03T04:00:00Z',
    ),
    'a weekend between two daily bars is no hole': (
        DAY,
        SEPT_1,
        '2026-10-02T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-05T04:00:00Z', LONG_AFTER), ('2026-10-06T04:00:00Z', LONG_AFTER)),
        '2026-10-07T04:00:00Z',
    ),
    'a holiday between two daily bars is no hole': (
        DAY,
        SEPT_1,
        '2026-11-25T05:00:00Z',
        LONG_AFTER,
        pads(('2026-11-27T05:00:00Z', LONG_AFTER), ('2026-11-30T05:00:00Z', LONG_AFTER)),
        '2026-12-01T05:00:00Z',
    ),
    # A hole BEFORE the newest real bar is not the tail's (the run starts at the newest real bar).
    'a missing session before the newest real bar is not reached back to': (
        DAY,
        SEPT_1,
        '2026-10-06T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-01T04:00:00Z', LONG_AFTER), ('2026-10-07T04:00:00Z', LONG_AFTER)),
        '2026-10-08T04:00:00Z',
    ),
    # 1min: the period is the session, so a whole session with no bar is a hole; a missing MINUTE is not.
    '1min whole session missing inside the run: after the last bar before it': (
        MINUTE,
        SEPT_1,
        '2026-10-05T14:00:00Z',
        LONG_AFTER,
        pads(('2026-10-05T19:59:00Z', LONG_AFTER), ('2026-10-07T14:00:00Z', LONG_AFTER)),
        '2026-10-05T20:00:00Z',
    ),
    '1min missing minute inside a session is no hole': (
        MINUTE,
        SEPT_1,
        '2026-10-05T14:00:00Z',
        LONG_AFTER,
        pads(('2026-10-05T14:02:00Z', LONG_AFTER), ('2026-10-05T14:05:00Z', LONG_AFTER)),
        '2026-10-05T14:06:00Z',
    ),
    '1week missing week inside the run: the Monday after the earlier week': (
        WEEK,
        SEPT_1,
        '2026-09-21T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-05T04:00:00Z', LONG_AFTER)),
        '2026-09-28T04:00:00Z',
    ),
    # OUT OF RANGE: a pad before the declared start is ignored, settled or not; the declared start stands.
    'settled pad before the declared start is ignored': (
        DAY,
        z('2026-10-09T04:00:00Z'),
        None,
        LONG_AFTER,
        pads(('2026-10-06T04:00:00Z', LONG_AFTER), ('2026-10-08T04:00:00Z', LONG_AFTER)),
        '2026-10-09T04:00:00Z',
    ),
    'real bar before the declared start is ignored': (
        DAY,
        z('2026-10-09T04:00:00Z'),
        '2026-10-08T04:00:00Z',
        LONG_AFTER,
        [],
        '2026-10-09T04:00:00Z',
    ),
    # A pad the calendar cannot place (before 1990-01-01, its first day) is ignored.
    'pad the calendar cannot place is ignored': (
        DAY,
        z('1989-12-01T05:00:00Z'),
        None,
        LONG_AFTER,
        pads(('1989-12-04T05:00:00Z', LONG_AFTER)),
        '1989-12-01T05:00:00Z',
    ),
    # DST (fall back Sun 1 Nov 2026, spring forward Sun 8 Mar 2026): by local date, never a fixed 24 h.
    '1day final placeholder Friday before fall back': (
        DAY,
        SEPT_1,
        '2026-10-29T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-30T04:00:00Z', LONG_AFTER)),
        '2026-10-31T04:00:00Z',
    ),
    '1day final placeholder first EST Monday': (
        DAY,
        SEPT_1,
        '2026-10-29T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-30T04:00:00Z', LONG_AFTER), ('2026-11-02T05:00:00Z', LONG_AFTER)),
        '2026-11-03T05:00:00Z',
    ),
    '1day unsettled EDT placeholder behind a settled EST one': (
        DAY,
        SEPT_1,
        '2026-10-29T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-30T04:00:00Z', '2026-10-30T15:00:00Z'), ('2026-11-02T05:00:00Z', LONG_AFTER)),
        '2026-10-30T04:00:00Z',
    ),
    '1day final placeholders across spring forward': (
        DAY,
        z('2026-02-02T05:00:00Z'),
        '2026-03-05T05:00:00Z',
        LONG_AFTER,
        pads(('2026-03-06T05:00:00Z', LONG_AFTER), ('2026-03-09T04:00:00Z', LONG_AFTER)),
        '2026-03-10T04:00:00Z',
    ),
    # 1min: judged by the SESSION close (20:00Z on Tue 20 Oct), settled at 27 Oct 20:30Z.
    '1min last-minute placeholder written before the session close': (
        MINUTE,
        SEPT_1,
        '2026-10-20T18:00:00Z',
        LONG_AFTER,
        pads(('2026-10-20T19:59:00Z', '2026-10-20T19:59:30Z')),
        '2026-10-20T19:59:00Z',
    ),
    '1min last-minute placeholder one second inside the grace': (
        MINUTE,
        SEPT_1,
        '2026-10-20T18:00:00Z',
        LONG_AFTER,
        pads(('2026-10-20T19:59:00Z', '2026-10-27T20:29:59Z')),
        '2026-10-20T19:59:00Z',
    ),
    '1min last-minute placeholder at the grace boundary: the session close': (
        MINUTE,
        SEPT_1,
        '2026-10-20T18:00:00Z',
        LONG_AFTER,
        pads(('2026-10-20T19:59:00Z', '2026-10-27T20:30:00Z')),
        '2026-10-20T20:00:00Z',
    ),
    '1min mid-session placeholder written after the close is still in the grace': (
        MINUTE,
        SEPT_1,
        '2026-10-20T13:59:00Z',
        '2026-10-20T20:31:00Z',
        pads(('2026-10-20T14:00:00Z', '2026-10-20T20:31:00Z')),
        '2026-10-20T14:00:00Z',
    ),
    '1min real bar written after the close is final: the next minute': (
        MINUTE,
        SEPT_1,
        '2026-10-20T14:00:00Z',
        '2026-10-20T20:31:00Z',
        [],
        '2026-10-20T14:01:00Z',
    ),
    # 1week: the week of Mon 5 Oct closes Fri 9 Oct 20:00Z; settled at 16 Oct 20:30Z.
    '1week placeholder one second inside the grace': (
        WEEK,
        SEPT_1,
        '2026-09-28T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-05T04:00:00Z', '2026-10-16T20:29:59Z')),
        '2026-10-05T04:00:00Z',
    ),
    '1week placeholder at the grace boundary: the next Monday': (
        WEEK,
        SEPT_1,
        '2026-09-28T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-05T04:00:00Z', '2026-10-16T20:30:00Z')),
        '2026-10-12T04:00:00Z',
    ),
    # 1month: October's last session closes Fri 30 Oct 20:00Z; settled at 6 Nov 20:30Z; the step is 4 weeks.
    '1month placeholder one second inside the grace': (
        MONTH,
        z('2026-08-01T04:00:00Z'),
        '2026-09-01T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-01T04:00:00Z', '2026-11-06T20:29:59Z')),
        '2026-10-01T04:00:00Z',
    ),
    '1month placeholder at the grace boundary: the 4-week step': (
        MONTH,
        z('2026-08-01T04:00:00Z'),
        '2026-09-01T04:00:00Z',
        LONG_AFTER,
        pads(('2026-10-01T04:00:00Z', '2026-11-06T20:30:00Z')),
        '2026-10-29T04:00:00Z',
    ),
}


@pytest.mark.parametrize(
    ('granularity', 'start', 'last_bar', 'written', 'placeholders', 'expected'), CASES.values(), ids=CASES.keys()
)
def test_the_tail_start(granularity, start, last_bar, written, placeholders, expected):
    row = catalog_row(granularity, start=start, last_bar=last_bar, written=written)

    tail = _fetch_start(row, XNYS, placeholders)

    assert tail == z(expected)
    assert tail.utcoffset() is not None, 'a naive tail names no instant'


# The grace boundary of a placeholder of Tue 6 Oct (close 20:00Z), one second either side, under three margins. The
# boundary is close + margin + 7 days: the margin is part of it, so a mutant that drops it moves the boundary.
GRACE_EDGES = {
    'default margin, one second inside': (None, '2026-10-13T20:29:59Z', '2026-10-06T04:00:00Z'),
    'default margin, at the boundary': (None, '2026-10-13T20:30:00Z', '2026-10-07T04:00:00Z'),
    'default margin, one second past': (None, '2026-10-13T20:30:01Z', '2026-10-07T04:00:00Z'),
    'zero margin, one second inside': ('0', '2026-10-13T19:59:59Z', '2026-10-06T04:00:00Z'),
    'zero margin, at the boundary': ('0', '2026-10-13T20:00:00Z', '2026-10-07T04:00:00Z'),
    '45 minutes, one second inside': ('45', '2026-10-13T20:44:59Z', '2026-10-06T04:00:00Z'),
    '45 minutes, at the boundary': ('45', '2026-10-13T20:45:00Z', '2026-10-07T04:00:00Z'),
}


@pytest.mark.parametrize(('settle', 'written', 'expected'), GRACE_EDGES.values(), ids=GRACE_EDGES.keys())
def test_the_grace_boundary_is_the_close_plus_the_settle_margin_plus_seven_days(monkeypatch, settle, written, expected):
    if settle is not None:
        monkeypatch.setenv(refresh_module.SETTLE_ENV, settle)
    row = catalog_row(last_bar='2026-10-05T04:00:00Z')

    assert _fetch_start(row, XNYS, pads(('2026-10-06T04:00:00Z', written))) == z(expected)


# A stored bar that cannot be a real period never advances the start (abc1361). These need `now`.
TUE_14_OCT = z('2026-10-14T22:00:00Z')
NOW_CASES = {
    'future weekday placeholder is ignored': (
        DAY,
        None,
        '2026-10-02T04:00:00Z',
        pads(('2026-10-19T04:00:00Z', LONG_AFTER)),
        '2026-10-03T04:00:00Z',
    ),
    'future placeholder ignored, the past ones still count': (
        DAY,
        None,
        '2026-10-02T04:00:00Z',
        pads(('2026-10-05T04:00:00Z', LONG_AFTER), ('2026-10-19T04:00:00Z', LONG_AFTER)),
        '2026-10-06T04:00:00Z',
    ),
    '1min placeholder stamped exactly now is ignored': (
        MINUTE,
        None,
        '2026-10-14T13:00:00Z',
        pads(('2026-10-14T22:00:00Z', LONG_AFTER)),
        '2026-10-14T13:01:00Z',
    ),
    '1min placeholder one minute before now counts': (
        MINUTE,
        None,
        '2026-10-14T13:00:00Z',
        pads(('2026-10-14T21:59:00Z', LONG_AFTER)),
        '2026-10-14T22:00:00Z',
    ),
    # The two cases below expected the pads to decide (abc1361). Since 8305d94 (B2) a newest real bar that does not
    # count gives the declared start, which is why the declared-end case already failed there; b41354d keeps that for
    # every case but a bar written before its own stamp. Both bars here were written LONG AFTER their stamp, so they
    # fail only the now test or the range test: the declared start (tj-7zjov1.122 is the bound for this residual).
    'real bar stamped after now, write time after its stamp: the declared start': (
        DAY,
        None,
        '2026-10-19T04:00:00Z',
        pads(('2026-10-05T04:00:00Z', LONG_AFTER), ('2026-10-06T04:00:00Z', LONG_AFTER)),
        '2026-09-01T04:00:00Z',
    ),
    'real bar at the declared end: the declared start': (
        DAY,
        z('2026-10-12T04:00:00Z'),
        '2026-10-12T04:00:00Z',
        pads(('2026-10-05T04:00:00Z', LONG_AFTER)),
        '2026-09-01T04:00:00Z',
    ),
    'pad at the declared end is ignored, one before it counts': (
        DAY,
        z('2026-10-12T04:00:00Z'),
        '2026-10-08T04:00:00Z',
        pads(('2026-10-09T04:00:00Z', LONG_AFTER), ('2026-10-12T04:00:00Z', LONG_AFTER)),
        '2026-10-10T04:00:00Z',
    ),
}


@pytest.mark.parametrize(
    ('granularity', 'end', 'last_bar', 'placeholders', 'expected'), NOW_CASES.values(), ids=NOW_CASES
)
def test_a_bar_that_cannot_be_real_never_advances_the_tail(granularity, end, last_bar, placeholders, expected):
    row = replace(catalog_row(granularity, last_bar=last_bar), end=end)

    assert _fetch_start(row, XNYS, placeholders, TUE_14_OCT) == z(expected)


# WRITTEN BEFORE ITS OWN STAMP (decision tj-7zjov1.120 rulings 2, 5 and 7; b41354d). The write time is a lower bound
# on serve time: such a bar proves the dataset was requested through its WRITE TIME and no further. When the NEWEST
# REAL bar is written early (inside the range, write time known), the tail is the local midnight of its write time's
# period, or an earlier unsettled counted pad, never before the declared start. Equality is NOT early: it counts.
# (granularity, declared start, declared end, newest real bar, its write time, placeholders, now, expected)
SEPT_1_TEXT = '2026-09-01T04:00:00Z'
WRITTEN_CASES = {
    'real bar a week ahead, written 13 Oct: the period of its write': (
        DAY,
        SEPT_1,
        None,
        '2026-10-19T04:00:00Z',
        '2026-10-13T15:00:00Z',
        [],
        TUE_14_OCT,
        '2026-10-13T04:00:00Z',
    ),
    'the same once now passes its stamp: still the period of its write (B1, downtime)': (
        DAY,
        SEPT_1,
        None,
        '2026-10-19T04:00:00Z',
        '2026-10-13T15:00:00Z',
        [],
        z('2026-10-20T22:00:00Z'),
        '2026-10-13T04:00:00Z',
    ),
    'written early plus an unsettled counted pad before its write: the earlier, the pad': (
        DAY,
        SEPT_1,
        None,
        '2026-10-19T04:00:00Z',
        '2026-10-13T15:00:00Z',
        pads(('2026-10-06T04:00:00Z', '2026-10-13T12:00:00Z')),
        TUE_14_OCT,
        '2026-10-06T04:00:00Z',
    ),
    'written early plus a settled counted pad before its write: the write period': (
        DAY,
        SEPT_1,
        None,
        '2026-10-19T04:00:00Z',
        '2026-10-13T15:00:00Z',
        pads(('2026-10-05T04:00:00Z', '2026-10-13T12:00:00Z')),
        TUE_14_OCT,
        '2026-10-13T04:00:00Z',
    ),
    'written early plus an unsettled pad that is itself written early: not counted, the write period': (
        DAY,
        SEPT_1,
        None,
        '2026-10-19T04:00:00Z',
        '2026-10-13T15:00:00Z',
        pads(('2026-10-16T04:00:00Z', '2026-10-13T15:00:00Z')),
        TUE_14_OCT,
        '2026-10-13T04:00:00Z',
    ),
    'written early, the write period before the declared start: the declared start': (
        DAY,
        z('2026-10-14T04:00:00Z'),
        None,
        '2026-10-19T04:00:00Z',
        '2026-10-13T15:00:00Z',
        [],
        TUE_14_OCT,
        '2026-10-14T04:00:00Z',
    ),
    # F7: a live 1min fetch whose last bar outran the transaction start; at most one extra session is re-requested.
    'F7 1min last bar 20:31Z written 20:30:10Z: the session of the write': (
        MINUTE,
        SEPT_1,
        None,
        '2026-10-20T20:31:00Z',
        '2026-10-20T20:30:10Z',
        [],
        z('2026-10-21T20:30:00Z'),
        '2026-10-20T04:00:00Z',
    ),
    '1week bar written early: the Monday of the week of its write': (
        WEEK,
        SEPT_1,
        None,
        '2026-10-19T04:00:00Z',
        '2026-10-14T15:00:00Z',
        [],
        z('2026-10-16T22:00:00Z'),
        '2026-10-12T04:00:00Z',
    ),
    # Ruling 5, equality counts: written AT its stamp the bar counts (here provisional: the bar itself); a tick
    # before, it is early (the write falls on 8 Oct local, so the period of the write is 8 Oct).
    'written exactly at its stamp counts: provisional, the bar itself': (
        DAY,
        SEPT_1,
        None,
        '2026-10-09T04:00:00Z',
        '2026-10-09T04:00:00Z',
        [],
        TUE_14_OCT,
        '2026-10-09T04:00:00Z',
    ),
    'written a tick before its stamp: the period of the write, 8 Oct': (
        DAY,
        SEPT_1,
        None,
        '2026-10-09T04:00:00Z',
        z('2026-10-09T04:00:00Z') - timedelta(microseconds=1),
        [],
        TUE_14_OCT,
        '2026-10-08T04:00:00Z',
    ),
    # Unknown write time: only the now and range tests decide.
    'unknown write time, stamped before now: counts': (
        DAY,
        SEPT_1,
        None,
        '2026-10-09T04:00:00Z',
        None,
        [],
        TUE_14_OCT,
        '2026-10-10T04:00:00Z',
    ),
    'unknown write time, stamped after now: the declared start': (
        DAY,
        SEPT_1,
        None,
        '2026-10-19T04:00:00Z',
        None,
        [],
        TUE_14_OCT,
        SEPT_1_TEXT,
    ),
    # Outside the range: the declared start, written early or not.
    'written early, stamped at the declared end: the declared start': (
        DAY,
        SEPT_1,
        z('2026-10-12T04:00:00Z'),
        '2026-10-12T04:00:00Z',
        '2026-10-11T15:00:00Z',
        [],
        TUE_14_OCT,
        SEPT_1_TEXT,
    ),
    'written early, stamped before the declared start: the declared start': (
        DAY,
        z('2026-10-12T04:00:00Z'),
        None,
        '2026-10-09T04:00:00Z',
        '2026-10-08T15:00:00Z',
        [],
        TUE_14_OCT,
        '2026-10-12T04:00:00Z',
    ),
    # A PAD written early does not count; here it leaves a session with no counted bar inside the run, so the hole
    # rule reaches back to it (B1 with the hole rule).
    'pad written early inside the run: a hole, after the bar before it': (
        DAY,
        SEPT_1,
        None,
        '2026-10-01T04:00:00Z',
        '2026-10-01T21:00:00Z',
        pads(
            ('2026-10-02T04:00:00Z', '2026-10-13T12:00:00Z'),
            ('2026-10-05T04:00:00Z', '2026-10-03T12:00:00Z'),
            ('2026-10-06T04:00:00Z', '2026-10-14T12:00:00Z'),
        ),
        TUE_14_OCT,
        '2026-10-03T04:00:00Z',
    ),
}


@pytest.mark.parametrize(
    ('granularity', 'start', 'end', 'last_bar', 'written', 'placeholders', 'now', 'expected'),
    WRITTEN_CASES.values(),
    ids=WRITTEN_CASES,
)
def test_a_newest_real_bar_written_before_its_stamp_starts_the_tail_at_its_write(
    granularity, start, end, last_bar, written, placeholders, now, expected
):
    written_at = z(written) if isinstance(written, str) else written
    row = replace(catalog_row(granularity, start=start, last_bar=last_bar), end=end, last_bar_written_at=written_at)

    assert _fetch_start(row, XNYS, placeholders, now) == z(expected)


def test_the_declared_end_half_of_the_empty_tail_skip_is_unreachable_for_a_daily_row():
    """F3, documented in server/data/store/CLAUDE.md: the end half of the guard cannot fire today.

    A DAILY row with a declared end never validates as a request, so the pass skips it before the empty-tail guard is
    reached. Pinned so a change to that rule is noticed and the guard's end half gets a real case.
    """
    row = replace(catalog_row(last_bar='2026-10-05T04:00:00Z'), end=z('2026-10-07T04:00:00Z'))

    with pytest.raises(ValidationError):
        _request_for(row, XNYS, [], TUE_14_OCT)


def test_the_documented_residual_is_seven_days_of_grace_42_days_of_lookback_and_5000_bars():
    """server/data/store/CLAUDE.md names these; a change to any widens or narrows what is NOT re-fetched."""
    assert timedelta(days=7) == PLACEHOLDER_GRACE
    assert timedelta(days=42) == PLACEHOLDER_LOOKBACK
    assert PLACEHOLDER_SCAN_LIMIT == 5000


# ------------------------------------------------------------------------------------------------- the property


def _ny_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=NEW_YORK).astimezone(UTC)


def _close_of(stamp: datetime) -> datetime | None:
    """The session close of a 1day bar's local date, straight from the calendar; None on a day with no session."""
    day = stamp.astimezone(NEW_YORK).date()
    sessions = XNYS.sessions_overlapping(_ny_midnight(day), _ny_midnight(day + timedelta(days=1)))
    return XNYS.session_close(day) if day in sessions else None


PROPERTY_SESSIONS = XNYS.sessions_overlapping(z('2026-08-03T04:00:00Z'), z('2026-11-14T05:00:00Z'))


def _local_date(stamp: datetime) -> date:
    return stamp.astimezone(NEW_YORK).date()


def _next_midnight(stamp: datetime) -> datetime:
    return _ny_midnight(_local_date(stamp) + timedelta(days=1))


@dataclass(frozen=True)
class RandomStore:
    row: CatalogRow
    placeholders: list[PlaceholderBar]
    now: datetime
    stored_dates: set[date]
    truncated: bool


def _random_store(rng: random.Random) -> RandomStore:
    """A random 1day store, a random now and a random truncation flag.

    A stored history of real bars and pads over a run of sessions, with random holes (a session the vendor never
    served), stray weekend pads (sometimes past a hole), random write times (some BEFORE the bar's own stamp, some
    exactly at it), bars stamped after `now` (whose write, like every write, is at or before now, so they are written
    early), a declared start before or inside the stored history, sometimes a declared end inside it, and sometimes no
    known write time for the newest real bar. Every write time is at or before now: a write after now cannot exist.
    """
    first = rng.randrange(0, 15)
    cut = rng.randrange(first, len(PROPERTY_SESSIONS))
    declared = _ny_midnight(PROPERTY_SESSIONS[max(0, first + rng.randrange(-3, 5))])
    now = _ny_midnight(PROPERTY_SESSIONS[rng.randrange(first, len(PROPERTY_SESSIONS))]) + timedelta(
        hours=rng.randrange(0, 48)
    )
    stored: list[tuple[datetime, bool, datetime]] = []
    for day in PROPERTY_SESSIONS[first : cut + 1]:
        stamp, close = _ny_midnight(day), XNYS.session_close(day)
        if rng.random() < 0.1:  # a hole: the vendor never served this session
            continue
        written = rng.choice(
            [
                stamp + timedelta(hours=rng.randrange(10, 16)),  # part-way
                close + SETTLE + timedelta(seconds=rng.choice([-1, 0, 1])),  # around the provisional edge
                close + SETTLE + PLACEHOLDER_GRACE + timedelta(seconds=rng.choice([-1, 0, 1])),  # around the grace
                close + timedelta(hours=rng.randrange(1, 24 * 12)),  # anywhere in the next 12 days
                close + timedelta(days=60),  # long after
                stamp - timedelta(hours=rng.randrange(1, 72)),  # before its own stamp: served early
                stamp + timedelta(microseconds=rng.choice([-1, 0, 1])),  # at its stamp, a tick either side
            ]
        )
        stored.append((stamp, rng.random() < 0.5, min(written, now)))
        if day.weekday() == 4 and rng.random() < 0.2:  # a stray Saturday pad
            saturday = _ny_midnight(day + timedelta(days=1))
            stored.append((saturday, False, min(saturday + timedelta(hours=rng.randrange(-30, 300)), now)))
    end = None
    if stored and rng.random() < 0.2:
        end = rng.choice(stored)[0] + timedelta(hours=rng.choice([0, 1]))
    real = [(stamp, written) for stamp, is_real, written in stored if is_real]
    last_bar, last_written = max(real) if real else (None, None)
    if last_bar is not None and rng.random() < 0.1:
        last_written = None  # an unknown write time
    row = replace(catalog_row(start=declared), end=end, last_bar=last_bar, last_bar_written_at=last_written)
    placeholders = sorted(
        (PlaceholderBar(stamp, written) for stamp, is_real, written in stored if not is_real),
        key=lambda placeholder: placeholder.timestamp,
        reverse=True,
    )
    dates = {_local_date(stamp) for stamp, _real, _written in stored}
    return RandomStore(row, placeholders, now, dates, rng.random() < 0.25)


def _in_range(stamp: datetime, row: CatalogRow) -> bool:
    return row.start <= stamp and (row.end is None or stamp < row.end)


def _counts(stamp: datetime, written: datetime | None, row: CatalogRow, now: datetime) -> bool:
    """Decision tj-7zjov1.120 rulings 2 and 5: before now, inside [start, end), not written before its own stamp."""
    return stamp < now and _in_range(stamp, row) and not (written is not None and written < stamp)


def test_the_property_seeds_exercise_every_part_of_the_rule():
    """The 400 seeds are not vacuous: each kind of stored bar the rule ignores or reaches back to occurs often."""
    kinds: dict[str, int] = dict.fromkeys(
        (
            'future pad',
            'future real',
            'pad before start',
            'pad at or after end',
            'weekend pad',
            'hole',
            'unsettled',
            'pad written early',
            'real written early inside the range',
            'real write unknown',
            'truncated',
        ),
        0,
    )
    for seed in range(400):
        store = _random_store(random.Random(seed))
        row, placeholders, now, stored_dates = store.row, store.placeholders, store.now, store.stored_dates
        stamps = [placeholder.timestamp for placeholder in placeholders]
        kinds['pad written early'] += any(p.written_at < p.timestamp for p in placeholders)
        kinds['real written early inside the range'] += (
            row.last_bar is not None
            and row.last_bar_written_at is not None
            and row.last_bar_written_at < row.last_bar
            and _in_range(row.last_bar, row)
        )
        kinds['real write unknown'] += row.last_bar is not None and row.last_bar_written_at is None
        kinds['truncated'] += store.truncated
        kinds['future pad'] += any(stamp >= now for stamp in stamps)
        kinds['future real'] += row.last_bar is not None and row.last_bar >= now
        kinds['pad before start'] += any(stamp < row.start for stamp in stamps)
        kinds['pad at or after end'] += row.end is not None and any(stamp >= row.end for stamp in stamps)
        kinds['weekend pad'] += any(_close_of(stamp) is None for stamp in stamps)
        first, last = min(stored_dates, default=None), max(stored_dates, default=None)
        kinds['hole'] += first is not None and any(
            first < day < last and day not in stored_dates for day in PROPERTY_SESSIONS
        )
        kinds['unsettled'] += any(
            _close_of(p.timestamp) is not None and p.written_at < _close_of(p.timestamp) + SETTLE + timedelta(days=7)
            for p in placeholders
        )
    assert all(count >= 20 for count in kinds.values()), kinds


@pytest.mark.parametrize('seed', range(400))
def test_property_the_window_covers_every_unsettled_bar_and_every_session_after_the_newest_real_bar(seed):
    """Over a seeded random store: nothing the window must hold lies before it, and it is no wider than that.

    The oracle is written from the rule in server/data/store/CLAUDE.md and decision tj-7zjov1.120 (rulings 1 to 7),
    independently of the code: closes from the calendar, local midnights from zoneinfo, the margin and grace by hand.
    COUNTED bars: stamped before now, inside [start, end), and not written strictly before their own stamp; a
    placeholder also on a session day. Every other stored bar is ignored by the tail.
    LOST ANCHOR (the newest real bar exists and does not count): if it is inside the range with a known write time
    before its stamp, the window must hold the local midnight of its write time's session date and every unsettled
    counted placeholder, and nothing earlier; otherwise the window is the declared start.
    MUST HOLD otherwise: the first local midnight after the newest counted bar; every counted placeholder written
    before its close + 30 min + 7 days; the real bar when written before its close + 30 min; for the first hole (a
    session date strictly between two consecutive bars of the run: the real bar unless the read was TRUNCATED, then
    the counted pads after it), the local midnight after the bar before it. Never before the declared start; the
    declared start with nothing counted. The window is exactly the earliest of these.
    INTEGRITY: every session inside the range and begun before now that lies after the anchor (the real bar; the
    oldest pad of the run when truncated; the write time's date when the anchor is lost; the first counted pad with
    no real bar, ruling 1) holds a counted bar or lies inside the window.
    """
    store = _random_store(random.Random(seed))
    row, placeholders, now = store.row, store.placeholders, store.now

    tail = _fetch_start(row, XNYS, placeholders, now, truncated=store.truncated)

    assert tail >= row.start, 'the tail starts before the declared start'
    counted_pads = sorted(
        (
            p
            for p in placeholders
            if _counts(p.timestamp, p.written_at, row, now) and _close_of(p.timestamp) is not None
        ),
        key=lambda placeholder: placeholder.timestamp,
    )
    unsettled = [
        p.timestamp for p in counted_pads if p.written_at < _close_of(p.timestamp) + SETTLE + timedelta(days=7)
    ]
    limit = now if row.end is None else min(now, row.end)

    def sessions_after(anchor: date) -> list[date]:
        return [day for day in PROPERTY_SESSIONS if row.start <= _ny_midnight(day) < limit and day > anchor]

    last, written = row.last_bar, row.last_bar_written_at
    if last is not None and not _counts(last, written, row, now):
        if _in_range(last, row) and written is not None and written < last:
            floor = _ny_midnight(_local_date(written))
            assert tail == max(row.start, min([floor, *unsettled])), 'written early: not the write-time period'
            missed = [
                day for day in sessions_after(_local_date(written) - timedelta(days=1)) if _ny_midnight(day) < tail
            ]
            assert not missed, f'a session at or after the write time left outside the window: {missed}'
        else:
            assert tail == row.start, 'a newest real bar that does not count, not written early: the declared start'
        return

    real = last
    counted = [p.timestamp for p in counted_pads] + ([real] if real is not None else [])
    if not counted:
        assert tail == row.start
        return
    must_hold = [_next_midnight(max(counted)), *unsettled]
    real_close = None if real is None else _close_of(real)
    if real_close is not None and written is not None and written < real_close + SETTLE:
        must_hold.append(real)
    after_real = [p.timestamp for p in counted_pads if real is None or p.timestamp > real]
    run = after_real if (store.truncated or real is None) else [real, *after_real]
    for earlier, later in pairwise(run):
        if any(_local_date(earlier) < day < _local_date(later) for day in PROPERTY_SESSIONS):
            must_hold.append(_next_midnight(earlier))
            break

    uncovered = [stamp for stamp in must_hold if stamp >= row.start and stamp < tail]
    assert not uncovered, f'left outside the window (a gap or a stale bar): {uncovered}'
    assert tail == max(row.start, min(must_hold)), 'the window is wider than the bars it must hold'

    anchor = run[0] if run else real
    counted_dates = {_local_date(stamp) for stamp in counted}
    gaps = [day for day in sessions_after(_local_date(anchor)) if day not in counted_dates and _ny_midnight(day) < tail]
    assert not gaps, f'sessions neither stored nor requested (a gap): {gaps}'


# ------------------------------------------------------------------------------------------------- the database tier


class Vendor:
    """An IngestFetchClient double that serves a truth table: each bar real from an instant on, a placeholder before.

    It serves every bar of the requested symbol stamped in [start, now) (or [start, end)), as of the injected clock.
    """

    def __init__(self, clock: list[datetime]) -> None:
        self.clock = clock
        self.requests: list[Any] = []
        # symbol -> stamp -> the instant the vendor serves it as a REAL bar from (None: always a placeholder)
        self.truth: dict[str, dict[datetime, datetime | None]] = {}

    def starts(self, symbol: str) -> list[datetime]:
        return [request.start for request in self.requests if request.asset_symbol == symbol]

    async def fetch(self, request):
        self.requests.append(request)
        now = self.clock[0]
        feed = request.feed or Feed.IEX
        served = []
        for stamp, real_from in sorted(self.truth.get(request.asset_symbol, {}).items()):
            if stamp < request.start or stamp >= now or (request.end is not None and stamp >= request.end):
                continue
            is_real = real_from is not None and real_from <= now
            served.append(
                fetch_dataset.Bar(
                    bar_start=stamp,
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=100.0 if is_real else 0.0,
                    trade_count=10 if is_real else 0,
                    feed=feed,
                )
            )
        yield fetch_dataset.FetchAccepted(feed=feed)
        if served:
            yield fetch_dataset.BarPage(bars=served)
        yield fetch_dataset.FetchDone(
            bar_count=len(served), served_range=fetch_dataset.ServedRange(start=request.start, end=now), as_of=now
        )


class Sim:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = [datetime(2026, 10, 6, 22, tzinfo=UTC)]
        self.vendor = Vendor(self.clock)
        self.entries: list[StoreDatasetEntry] = []

    async def add(self, *rows: Any) -> None:
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add_all([row for row in rows if isinstance(row, StoreDatasetEntry)])
            await db.flush()
            db.add_all([row for row in rows if not isinstance(row, StoreDatasetEntry)])
            await db.commit()
        self.entries.extend(row for row in rows if isinstance(row, StoreDatasetEntry))

    async def run(self, now: datetime) -> PassResult:
        self.clock[0] = now

        async def sleep(_seconds: float) -> None:
            return None

        return await DailyRefresher(self.vendor, clock=lambda: now, sleep=sleep).run_pass(DataSource.ALPACA_API, XNYS)

    async def stored(self, dataset: StoreDatasetEntry) -> dict[datetime, tuple[bool, datetime]]:
        """Each stored bar of the dataset: stamp -> (is a placeholder, written at)."""
        async with AsyncSession(self.engine) as db:
            rows = (
                await db.execute(select(StockMarketActivity).where(StockMarketActivity.dataset_id == dataset.id))
            ).scalars()
            return {row.timestamp: (row.is_placeholder_bar, row.updated_at) for row in rows}

    async def catalog(self, dataset: StoreDatasetEntry) -> CatalogRow:
        async with AsyncSession(self.engine) as db:
            return await catalog_repo.get_catalog_row(db, dataset.id)

    async def runs(self, dataset: StoreDatasetEntry) -> list[IngestRun]:
        async with AsyncSession(self.engine) as db:
            statement = select(IngestRun).where(IngestRun.asset_symbol == dataset.asset_symbol)
            return list((await db.execute(statement)).scalars())


@pytest_asyncio.fixture
async def sim(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sim]:
    request.getfixturevalue('aware_sqlite_datetimes')
    engine = _engine()
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: StoreDatasetEntry.metadata.create_all(
                sync, tables=[StoreDatasetEntry.__table__, StockMarketActivity.__table__, IngestRun.__table__]
            )
        )
    state = Sim(engine)

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db

    async def upsert(_db: AsyncSession, created: Any, **_kwargs: Any) -> uuid.UUID:
        # The Postgres entry upsert cannot run on SQLite: answer the id an exact identity match resolves to.
        (match,) = [
            row
            for row in state.entries
            if (row.owner, row.asset_symbol, row.feed, row.granularity)
            == (created.owner, created.asset_symbol, created.feed, created.granularity)
        ]
        return match.id

    async def write_bars(db: AsyncSession, batch: Any, requested_chunk_size: int | None = None) -> int:
        # The Postgres ON CONFLICT DO UPDATE, emulated: the row on the natural key is replaced, updated_at is now.
        values = StockMarketActivity.from_batch_create(batch)
        for value in values:
            key = [getattr(StockMarketActivity, column) == value[column] for column in BaseMarketActivity.NATURAL_KEY]
            await db.execute(delete(StockMarketActivity).where(*key))
            db.add(StockMarketActivity(**value, created_at=state.clock[0], updated_at=state.clock[0]))
        await db.flush()
        return len(values)

    for env in (refresh_module.ENABLED_ENV, refresh_module.MAX_PER_PASS_ENV, refresh_module.BACKOFF_ENV):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(refresh_module, 'own_session', own_session)
    monkeypatch.setattr(run_recording, 'own_session', own_session)
    monkeypatch.setattr(refresh_module, 'publish', lambda _event: None)
    monkeypatch.setattr(run_recording, 'publish', lambda _event: None)
    monkeypatch.setattr(data_action_request, 'upsert_entry_in_transaction', upsert)
    monkeypatch.setattr(data_action_request, 'write_market_activity_in_transaction', write_bars)
    try:
        yield state
    finally:
        await engine.dispose()


def shaped(of: StoreDatasetEntry, at: datetime, *, real: bool, written: datetime) -> StockMarketActivity:
    row = bar(of, at)
    row.volume, row.trade_count = (100, 10) if real else (0, 0)
    row.created_at = row.updated_at = written
    return row


def daily(symbol: str, granularity: Granularity = DAY, start: datetime = SEPT_1) -> StoreDatasetEntry:
    return entry(symbol, granularity=granularity, update_type=UpdateType.DAILY, start=start)


# --- recent_placeholder_bars


@pytest.mark.asyncio
async def test_recent_placeholder_bars_reads_only_this_dataset_s_placeholders_newest_first(sim: Sim):
    """Only volume 0 AND trade count 0; only this dataset's identity; newest first; written_at is updated_at."""
    mine, other = daily('RPB'), daily('RPO')
    other_feed = entry('RPB', update_type=UpdateType.DAILY, feed=Feed.SIP)
    written = [z(f'2026-10-0{day}T21:00:00Z') for day in range(1, 8)]
    odd_volume = bar(mine, z('2026-10-08T04:00:00Z'))
    odd_volume.volume, odd_volume.trade_count = 0, 3
    odd_trades = bar(mine, z('2026-10-09T04:00:00Z'))
    odd_trades.volume, odd_trades.trade_count = 5, 0
    await sim.add(
        mine,
        other,
        other_feed,
        shaped(mine, z('2026-10-01T04:00:00Z'), real=False, written=written[0]),
        shaped(mine, z('2026-10-02T04:00:00Z'), real=True, written=written[1]),
        shaped(mine, z('2026-10-05T04:00:00Z'), real=False, written=written[4]),
        shaped(mine, z('2026-10-06T04:00:00Z'), real=False, written=written[5]),
        odd_volume,
        odd_trades,
        shaped(other, z('2026-10-07T04:00:00Z'), real=False, written=written[6]),
        shaped(other_feed, z('2026-10-07T04:00:00Z'), real=False, written=written[6]),
    )
    row = await sim.catalog(mine)

    async with AsyncSession(sim.engine) as db:
        every = await catalog_repo.recent_placeholder_bars(db, row, not_before=None, limit=100)
        bounded = await catalog_repo.recent_placeholder_bars(db, row, not_before=z('2026-10-05T04:00:00Z'), limit=100)
        limited = await catalog_repo.recent_placeholder_bars(db, row, not_before=None, limit=2)
        none = await catalog_repo.recent_placeholder_bars(db, row, not_before=z('2026-10-06T04:00:01Z'), limit=100)
        older = await catalog_repo.recent_placeholder_bars(
            db, row, not_before=None, limit=100, before=z('2026-10-06T04:00:00Z')
        )

    assert every == [
        PlaceholderBar(z('2026-10-06T04:00:00Z'), written[5]),
        PlaceholderBar(z('2026-10-05T04:00:00Z'), written[4]),
        PlaceholderBar(z('2026-10-01T04:00:00Z'), written[0]),
    ]
    assert bounded == every[:2], 'not_before is inclusive'
    assert limited == every[:2], 'the limit keeps the NEWEST'
    assert none == []
    assert older == every[1:], 'before is strict: the next older page starts after the bar it names'


# --- the pass


@pytest.mark.asyncio
async def test_a_pass_requests_from_after_the_newest_stored_placeholder_and_records_that_tail(sim: Sim):
    """The bead's acceptance end to end: final placeholders trail the real bar of 1 Oct; the old rule asks for 2 Oct."""
    dataset = daily('PFT')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-01T04:00:00Z'), real=True, written=z('2026-10-01T21:00:00Z')),
        shaped(dataset, z('2026-10-02T04:00:00Z'), real=False, written=z('2026-10-13T12:00:00Z')),
        shaped(dataset, z('2026-10-05T04:00:00Z'), real=False, written=z('2026-10-13T12:00:00Z')),
    )

    result = await sim.run(z('2026-10-14T22:00:00Z'))

    assert sim.vendor.starts('PFT') == [z('2026-10-06T04:00:00Z')]
    (run,) = await sim.runs(dataset)
    assert run.requested_start == z('2026-10-06T04:00:00Z')
    assert result == PassResult(behind=1, refreshed=1)


@pytest.mark.asyncio
async def test_selection_and_order_still_judge_the_newest_real_bar(sim: Sim):
    """The owed set and the pass order read the real bar, never the newest stored placeholder.

    `ahead` holds pads to 13 Oct but its real bar is 1 Oct: it is owed and goes FIRST (oldest real bar), before
    `real` (real bar 2 Oct). `fresh` holds the real bar of the last completed session (13 Oct) and is not requested.
    A freshness that read the newest STORED bar would order `real` first and would not owe `ahead` at all.
    """
    ahead, real_only, fresh = daily('SOA'), daily('SOB'), daily('SOC')
    final = z('2026-10-20T12:00:00Z')
    await sim.add(
        ahead,
        real_only,
        fresh,
        shaped(ahead, z('2026-10-01T04:00:00Z'), real=True, written=z('2026-10-01T21:00:00Z')),
        *(
            shaped(ahead, z(f'2026-10-{day:02d}T04:00:00Z'), real=False, written=final)
            for day in (2, 5, 6, 7, 8, 9, 12)
        ),
        shaped(ahead, z('2026-10-13T04:00:00Z'), real=False, written=z('2026-10-13T21:00:00Z')),
        shaped(real_only, z('2026-10-02T04:00:00Z'), real=True, written=z('2026-10-02T21:00:00Z')),
        shaped(fresh, z('2026-10-13T04:00:00Z'), real=True, written=z('2026-10-13T21:00:00Z')),
    )

    result = await sim.run(z('2026-10-13T22:00:00Z'))

    assert [request.asset_symbol for request in sim.vendor.requests] == ['SOA', 'SOB']
    # `ahead`: the pad of 13 Oct is within its grace, so the tail reaches back to it, and no further.
    assert sim.vendor.starts('SOA') == [z('2026-10-13T04:00:00Z')]
    assert sim.vendor.starts('SOB') == [z('2026-10-03T04:00:00Z')]
    assert result.behind == 2
    assert (await sim.catalog(ahead)).last_bar == z('2026-10-01T04:00:00Z'), 'freshness moved off the real bar'


@pytest.mark.asyncio
async def test_an_empty_tail_is_not_requested_and_is_counted_skipped(sim: Sim):
    """A tail that starts at or after now is not sent; the one honest case left is a mid-month 1month stamp.

    The vendor may stamp a month on any day of it. October's bar, a placeholder stamped Thu 15 Oct, was written on
    7 Nov, after October's last close (Fri 30 Oct 20:00Z) plus the margin plus the grace: settled. The newest real bar
    is September's, so the dataset is owed (October has ended), but the tail is after the October pad: 15 Oct plus
    the 4-week step, 12 Nov 04:00Z, after now (8 Nov 22:00Z). Nothing is requested; one skipped. No future stamp is
    involved: every stored bar here is in the past and can be real.
    """
    dataset = daily('ETS', granularity=MONTH, start=z('2026-08-01T04:00:00Z'))
    await sim.add(
        dataset,
        shaped(dataset, z('2026-09-01T04:00:00Z'), real=True, written=z('2026-09-30T21:00:00Z')),
        shaped(dataset, z('2026-10-15T04:00:00Z'), real=False, written=z('2026-11-07T12:00:00Z')),
    )

    result = await sim.run(z('2026-11-08T22:00:00Z'))

    assert sim.vendor.requests == []
    assert result == PassResult(behind=1, skipped=1)


@pytest.mark.asyncio
async def test_a_future_stamped_placeholder_never_hides_the_sessions_before_it(sim: Sim):
    """F1, fixed in abc1361: a pad stamped Mon 19 Oct, read on 14 Oct, is ignored; the tail is after the real 2 Oct."""
    dataset = daily('FUT')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-02T04:00:00Z'), real=True, written=z('2026-10-02T21:00:00Z')),
        shaped(dataset, z('2026-10-19T04:00:00Z'), real=False, written=z('2026-10-14T21:00:00Z')),
    )

    result = await sim.run(z('2026-10-14T22:00:00Z'))

    assert sim.vendor.starts('FUT') == [z('2026-10-03T04:00:00Z')]
    assert result == PassResult(behind=1, refreshed=1)
    stored = await sim.stored(dataset)
    assert z('2026-10-19T04:00:00Z') in stored, 'the ignored pad is still stored, raw'


@pytest.mark.asyncio
async def test_a_hole_inside_the_padded_run_is_requested_through_the_pass(sim: Sim):
    """Real 1 Oct, pads 2 Oct and 6 Oct (settled): Mon 5 Oct was never served, so the tail starts Sat 3 Oct."""
    dataset = daily('HOL')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-01T04:00:00Z'), real=True, written=z('2026-10-01T21:00:00Z')),
        shaped(dataset, z('2026-10-02T04:00:00Z'), real=False, written=z('2026-10-13T12:00:00Z')),
        shaped(dataset, z('2026-10-06T04:00:00Z'), real=False, written=z('2026-10-14T12:00:00Z')),
    )

    await sim.run(z('2026-10-14T22:00:00Z'))

    assert sim.vendor.starts('HOL') == [z('2026-10-03T04:00:00Z')]


def test_f4_ruling_the_leading_gap_of_a_placeholder_only_dataset_is_not_asked_for_again():
    """Decision tj-7zjov1.120 ruling 1: the sessions before the first stored bar were requested by the first fetch.

    Declared start Thu 1 Oct, settled pads 5 and 6 Oct: the tail is after the newest counted pad, 7 Oct, not the
    declared start. The ruling rests on the invariant pinned by the next two tests.
    """
    row = catalog_row(start=z('2026-10-01T04:00:00Z'))

    tail = _fetch_start(row, XNYS, pads(('2026-10-05T04:00:00Z', LONG_AFTER), ('2026-10-06T04:00:00Z', LONG_AFTER)))

    assert tail == z('2026-10-07T04:00:00Z')


def test_f4_ruling_a_dataset_with_no_counted_bar_is_requested_from_its_declared_start():
    """The invariant's first half: nothing counted, the declared start, whatever is stored and does not count.

    A future pad, a weekend pad, a pad written before its stamp, a pad before the declared start: none counts.
    """
    row = catalog_row(start=z('2026-10-01T04:00:00Z'))
    stored = pads(
        ('2026-10-19T04:00:00Z', '2026-10-13T12:00:00Z'),
        ('2026-10-10T04:00:00Z', '2026-10-12T12:00:00Z'),
        ('2026-10-09T04:00:00Z', '2026-10-08T12:00:00Z'),
        ('2026-09-30T04:00:00Z', '2026-10-12T12:00:00Z'),
    )

    assert _fetch_start(row, XNYS, stored, TUE_14_OCT) == z('2026-10-01T04:00:00Z')


def test_f4_ruling_the_atomic_fetch_it_rests_on_is_pinned():
    """The invariant's second half: every fetch is ONE write transaction, so a broken stream writes nothing.

    Pinned in test_dataset_fetch_transaction.py; this guards that the pin is still there, since ruling 1 is only
    as good as it.
    """
    import data.store.tests.test_dataset_fetch_transaction as transaction_tests

    assert callable(transaction_tests.test_a_stream_that_breaks_after_a_page_commits_nothing_and_rolls_back)


def test_f4_ruling_a_real_dataset_whose_first_bar_is_after_its_declared_start_is_not_asked_for_again():
    """The same class (a listing date): declared start 1 Oct, first and newest real bar 5 Oct, final: tail 6 Oct."""
    row = catalog_row(start=z('2026-10-01T04:00:00Z'), last_bar='2026-10-05T04:00:00Z')

    assert _fetch_start(row, XNYS, [], TUE_14_OCT) == z('2026-10-06T04:00:00Z')


@pytest.mark.asyncio
async def test_downtime_after_a_real_bar_served_early_requests_every_session_after_its_write(sim: Sim):
    """B1 through the pass: the vendor served a bar stamped Mon 12 Oct on Wed 7 Oct (written before its stamp).

    No pass ran until Tue 13 Oct (downtime). The bar is now in the past, but it proves only that the dataset was
    requested through its write time: the tail is 7 Oct, so 7 to 9 Oct are requested. With the now test alone the
    tail would be 13 Oct and 7 to 9 Oct never requested.
    """
    dataset = daily('DWN')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-06T04:00:00Z'), real=True, written=z('2026-10-06T21:00:00Z')),
        shaped(dataset, z('2026-10-12T04:00:00Z'), real=True, written=z('2026-10-07T15:00:00Z')),
    )

    await sim.run(z('2026-10-13T22:00:00Z'))

    assert sim.vendor.starts('DWN') == [z('2026-10-07T04:00:00Z')]


@pytest.mark.asyncio
async def test_downtime_after_a_pad_served_early_requests_its_session(sim: Sim):
    """B1 for a pad: a pad stamped Fri 9 Oct, written Wed 7 Oct, after the real Tue 6 Oct; no pass until 13 Oct."""
    dataset = daily('DWP')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-06T04:00:00Z'), real=True, written=z('2026-10-06T21:00:00Z')),
        shaped(dataset, z('2026-10-09T04:00:00Z'), real=False, written=z('2026-10-07T15:00:00Z')),
    )

    await sim.run(z('2026-10-13T22:00:00Z'))

    (start,) = sim.vendor.starts('DWP')
    assert start <= z('2026-10-07T04:00:00Z'), 'Wed 7 and Thu 8 Oct were never requested'


# The architect's B3 probe (tj-7zjov1.114): a 1min real bar on Tue 1 Sep at 19:59Z, then settled placeholders every
# regular-session minute (13:30Z to 19:59Z under EDT) from Wed 2 Sep to Fri 9 Oct: 27 sessions, 10,530 pads, more than
# two pages. The read stops after one page (it ends in a settled pad) and says truncated.
PROBE_SESSIONS = XNYS.sessions_overlapping(z('2026-09-02T04:00:00Z'), z('2026-10-10T04:00:00Z'))


def _probe_pads(dataset: StoreDatasetEntry, *, skip: date | None = None) -> list[StockMarketActivity]:
    rows = []
    for day in PROBE_SESSIONS:
        if day == skip:
            continue
        opening = datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC)
        rows += [shaped(dataset, opening + timedelta(minutes=m), real=False, written=LONG_AFTER) for m in range(390)]
    return rows


@pytest.mark.asyncio
async def test_b3_probe_a_truncated_read_does_not_pair_the_real_bar_with_the_oldest_pad_read(sim: Sim):
    """Tail after the newest pad, 9 Oct 20:00Z; without the truncated flag it would be 1 Sep 20:00Z, every pass."""
    dataset = daily('PRB', granularity=MINUTE)
    assert len(PROBE_SESSIONS) == 27
    await sim.add(
        dataset,
        shaped(dataset, z('2026-09-01T19:59:00Z'), real=True, written=z('2026-09-01T21:00:00Z')),
        *_probe_pads(dataset),
    )

    await sim.run(z('2026-10-12T22:00:00Z'))

    assert sim.vendor.starts('PRB') == [z('2026-10-09T20:00:00Z')]


@pytest.mark.asyncio
async def test_b3_probe_pairs_among_the_pads_read_are_still_checked(sim: Sim):
    """The same probe with Tue 6 Oct (inside the page read) missing: a hole among the pads read, after 5 Oct 19:59Z."""
    dataset = daily('PRH', granularity=MINUTE)
    await sim.add(
        dataset,
        shaped(dataset, z('2026-09-01T19:59:00Z'), real=True, written=z('2026-09-01T21:00:00Z')),
        *_probe_pads(dataset, skip=date(2026, 10, 6)),
    )

    await sim.run(z('2026-10-12T22:00:00Z'))

    assert sim.vendor.starts('PRH') == [z('2026-10-05T20:00:00Z')]


def test_truncated_both_ways_on_the_pure_rule():
    """The flag alone decides the pairing: real 1 Sep 19:59Z and settled pads 8 and 9 Oct read (1min)."""
    row = catalog_row(MINUTE, last_bar='2026-09-01T19:59:00Z')
    read = pads(('2026-10-08T19:59:00Z', LONG_AFTER), ('2026-10-09T19:59:00Z', LONG_AFTER))

    assert _fetch_start(row, XNYS, read, z('2026-10-12T22:00:00Z'), truncated=True) == z('2026-10-09T20:00:00Z')
    assert _fetch_start(row, XNYS, read, z('2026-10-12T22:00:00Z'), truncated=False) == z('2026-09-01T20:00:00Z')


def test_truncated_with_an_unsettled_pad_still_reaches_back_to_it():
    """Truncation removes one pairing only: an unsettled pad read (8 Oct 14:00Z, written that evening) still counts."""
    row = catalog_row(MINUTE, last_bar='2026-09-01T19:59:00Z')
    read = pads(('2026-10-08T14:00:00Z', '2026-10-08T21:00:00Z'), ('2026-10-09T19:59:00Z', LONG_AFTER))

    assert _fetch_start(row, XNYS, read, z('2026-10-12T22:00:00Z'), truncated=True) == z('2026-10-08T14:00:00Z')


@pytest.mark.asyncio
async def test_b5_a_repeating_page_raises_instead_of_hanging(sim: Sim, monkeypatch: pytest.MonkeyPatch):
    """A read that ignores `before` returns the same full page forever; the progress guard raises at the second.

    The fake gives up after a few calls with an AssertionError of its own, so a removed guard reds as a failure, not
    a hang.
    """
    dataset = daily('RPT', granularity=MINUTE)
    first = z('2026-10-05T08:00:00Z')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-02T19:59:00Z'), real=True, written=z('2026-10-02T21:00:00Z')),
        *(
            shaped(dataset, first + timedelta(minutes=m), real=False, written=z('2026-10-05T21:00:00Z'))
            for m in range(5000)
        ),
    )
    real_read = refresh_module.recent_placeholder_bars
    calls = [0]

    async def ignores_before(db, row, *, not_before, limit, before=None):
        calls[0] += 1
        assert calls[0] <= 5, 'the paging read the same page five times: no progress guard'
        return await real_read(db, row, not_before=not_before, limit=limit)

    monkeypatch.setattr(refresh_module, 'recent_placeholder_bars', ignores_before)

    with pytest.raises(RuntimeError, match='no progress'):
        await sim.run(z('2026-10-09T22:00:00Z'))
    assert calls[0] == 2


def _extended_hours_pads(dataset: StoreDatasetEntry, days: list[str]) -> list[StockMarketActivity]:
    """960 one-minute pads a session, all unsettled.

    04:00 to 20:00 local (08:00Z to 24:00Z under EDT), each written four hours after its session's 20:00Z close: its
    grace ends 7 days and 30 minutes after that close.
    """
    rows = []
    for day in days:
        opening = z(f'{day}T08:00:00Z')
        written = z(f'{day}T20:00:00Z') + timedelta(hours=4)
        rows += [shaped(dataset, opening + timedelta(minutes=m), real=False, written=written) for m in range(960)]
    return rows


@pytest.mark.asyncio
async def test_paging_reads_every_unsettled_pad_of_a_dead_extended_hours_1min_series(sim: Sim):
    """F2, fixed in abc1361: every unsettled pad is read, past one page.

    Six sessions of 960 unsettled pads (5,760, more than one page of 5,000). The tail starts at the OLDEST, Mon 5 Oct 08:00Z; one page would stop at the 5,000th newest (5 Oct 20:40Z).
    """
    dataset = daily('PGX', granularity=MINUTE)
    days = ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-12']
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-02T19:59:00Z'), real=True, written=z('2026-10-02T21:00:00Z')),
        *_extended_hours_pads(dataset, days),
    )

    await sim.run(z('2026-10-13T22:00:00Z'))

    assert sim.vendor.starts('PGX') == [z('2026-10-05T08:00:00Z')]


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [5000, 5001], ids=['exactly one page', 'one past a page'])
async def test_the_page_boundary_loses_no_pad(sim: Sim, count):
    """No pad is lost or skipped at the page boundary.

    `count` consecutive unsettled 1min pads from Mon 5 Oct 08:00Z: the tail is the oldest either way. At 5,001 the
    oldest is alone on the second page, read strictly before the first page's oldest.
    """
    dataset = daily('PGB', granularity=MINUTE)
    first = z('2026-10-05T08:00:00Z')
    await sim.add(
        dataset,
        shaped(dataset, z('2026-10-02T19:59:00Z'), real=True, written=z('2026-10-02T21:00:00Z')),
        *(
            shaped(dataset, first + timedelta(minutes=m), real=False, written=z('2026-10-05T21:00:00Z'))
            for m in range(count)
        ),
    )

    await sim.run(z('2026-10-09T22:00:00Z'))

    assert sim.vendor.starts('PGB') == [first]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('unsettled', 'expected'),
    [
        # The floor is the newest real bar (Thu 29 Oct 04:00Z) minus 42 days: Thu 17 Sep 04:00Z, inclusive.
        (('2026-09-17',), '2026-09-17T04:00:00Z'),
        # DOCUMENTED RESIDUAL: one day older than the floor is not read, so it is NOT re-fetched; the tail is after
        # the newest stored bar. This is the line in server/data/store/CLAUDE.md, pinned so it cannot grow silently.
        (('2026-09-16',), '2026-10-30T04:00:00Z'),
        (('2026-09-16', '2026-09-17'), '2026-09-17T04:00:00Z'),
    ],
    ids=['at the floor', 'one day before the floor (residual)', 'both'],
)
async def test_the_lookback_floor_is_42_days_before_the_newest_real_bar(sim: Sim, unsettled, expected):
    dataset = daily('LKB', start=z('2026-08-03T04:00:00Z'))
    rows = [shaped(dataset, z('2026-10-29T04:00:00Z'), real=True, written=z('2026-10-29T21:00:00Z'))]
    for day in unsettled:  # each written part-way through its own session: unsettled until rewritten
        rows.append(shaped(dataset, z(f'{day}T04:00:00Z'), real=False, written=z(f'{day}T15:00:00Z')))
    await sim.add(dataset, *rows)

    await sim.run(z('2026-11-03T22:00:00Z'))

    assert sim.vendor.starts('LKB') == [z(expected)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('settled_after', 'expected'),
    [(4999, '2026-09-28T14:00:00Z'), (5000, '2026-10-05T20:00:00Z')],
    ids=['4999 newer: read', '5000 newer: not read (residual)'],
)
async def test_the_scan_limit_is_5000_placeholders(sim: Sim, settled_after, expected):
    """An unsettled 1min pad with N settled pads newer than it: read (and re-fetched) only while N < 5,000.

    With 5,000 settled pads newer, the first page is full and ends in a SETTLED pad, so no older page is read: the
    documented residual ("one older than a whole page of settled pads"), pinned at its edge. A paging that read one
    page too many would read it.
    """
    dataset = daily('LIM', granularity=MINUTE)
    first = z('2026-09-28T14:00:00Z')
    rows = [
        shaped(dataset, z('2026-10-05T19:59:00Z'), real=True, written=z('2026-10-05T21:00:00Z')),
        shaped(dataset, first, real=False, written=z('2026-09-28T15:00:00Z')),
    ]
    rows += [
        shaped(dataset, first + timedelta(minutes=minute), real=False, written=LONG_AFTER)
        for minute in range(1, settled_after + 1)
    ]
    await sim.add(dataset, *rows)

    await sim.run(z('2026-10-06T22:00:00Z'))

    assert sim.vendor.starts('LIM') == [z(expected)]


# --- passes over days, against a vendor that pads a dead series


def _pass_times(first: date, last: date) -> list[datetime]:
    """Each session's close plus the settle margin, when the loop runs its pass."""
    sessions = XNYS.sessions_overlapping(_ny_midnight(first), _ny_midnight(last + timedelta(days=1)))
    return [XNYS.session_close(session) + SETTLE for session in sessions]


async def _simulate(sim: Sim, dataset: StoreDatasetEntry, times: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Run a pass at each time; return (pass time, requested start) for the dataset's requests."""
    seen: list[tuple[datetime, datetime]] = []
    for now in times:
        before = len(sim.vendor.requests)
        await sim.run(now)
        seen += [
            (now, request.start)
            for request in sim.vendor.requests[before:]
            if request.asset_symbol == dataset.asset_symbol
        ]
    return seen


async def _assert_integrity(sim: Sim, dataset: StoreDatasetEntry, seen, times, within, beyond) -> None:
    stored = await sim.stored(dataset)
    last_session = XNYS.last_completed_session(times[-1])
    owed = XNYS.sessions_overlapping(dataset.start, _ny_midnight(last_session + timedelta(days=1)))
    missing = [day for day in owed if _ny_midnight(day) not in stored]
    assert not missing, f'sessions left with no stored bar (a gap): {missing}'
    stale = [stamp for stamp in within if stored[stamp][0]]
    assert not stale, f'placeholders the vendor turned real within the grace, still stored as placeholders: {stale}'
    # DOCUMENTED RESIDUAL: a pad the vendor turns real only after it settled is not asked for again.
    assert all(stored[stamp][0] for stamp in beyond), 'a settled placeholder was asked for again'
    # The window is constant, never growing: no request reaches further back than the grace plus a weekend, save the
    # catch-up from the declared start.
    for now, start in seen:
        assert start >= dataset.start
        assert start == dataset.start or start >= now - timedelta(days=9), f'a growing window at {now}: {start}'

    # Idempotent: a second pass at the same instant asks for no wider window and changes no stored value. It may ask
    # for a narrower one (the first pass's rewrite can settle the oldest pad) or none (the series' bar of the last
    # session was real: the dataset is FRESH).
    before = {stamp: placeholder for stamp, (placeholder, _written) in stored.items()}
    again = await _simulate(sim, dataset, [times[-1]])
    assert len(again) <= 1
    if again:
        assert seen and seen[-1][0] == times[-1], 'the repeated pass asked for a dataset the first one did not'
        assert again[0][1] >= seen[-1][1], 'the repeated pass asked for a wider window'
    assert {stamp: placeholder for stamp, (placeholder, _written) in (await sim.stored(dataset)).items()} == before


@pytest.mark.asyncio
async def test_a_dead_series_over_six_weeks_leaves_no_gap_and_no_pad_turned_real_within_the_grace(sim: Sim):
    """A dead series, one pass per close: no gap, no stale bar, a constant window.

    Real to Fri 9 Oct, padded after. The vendor turns the pad of Wed 14 Oct real on Fri 16 Oct (inside its grace)
    and the pad of Thu 15 Oct real on Fri 30 Oct (after its grace, which ended 22 Oct 20:30Z). Across fall back.
    """
    dataset = daily('DED', start=z('2026-09-28T04:00:00Z'))
    await sim.add(dataset)
    sessions = XNYS.sessions_overlapping(z('2026-09-28T04:00:00Z'), z('2026-11-14T05:00:00Z'))
    truth = {
        _ny_midnight(day): (datetime.min.replace(tzinfo=UTC) if day <= date(2026, 10, 9) else None) for day in sessions
    }
    within, beyond = z('2026-10-14T04:00:00Z'), z('2026-10-15T04:00:00Z')
    truth[within] = z('2026-10-16T12:00:00Z')
    truth[beyond] = z('2026-10-30T12:00:00Z')
    sim.vendor.truth['DED'] = truth
    times = _pass_times(date(2026, 9, 28), date(2026, 11, 13))

    seen = await _simulate(sim, dataset, times)

    await _assert_integrity(sim, dataset, seen, times, [within], [beyond])
    assert (await sim.catalog(dataset)).last_bar == within, 'freshness does not read the pad turned real'


@pytest.mark.asyncio
@pytest.mark.parametrize('seed', range(6))
async def test_property_passes_over_random_padded_series_leave_no_gap_and_no_stale_bar(sim: Sim, seed):
    """Seeded random padded series, one pass per close: no gap, no stale bar, a constant window.

    Each session real or a pad; some pads turn real 1 hour to 4 days after their close (must be stored
    real), others 12 to 20 days after (the documented residual); stray Saturday pads; one pass per close.
    """
    rng = random.Random(seed)
    dataset = daily(f'RND{seed}', start=z('2026-09-28T04:00:00Z'))
    await sim.add(dataset)
    sessions = XNYS.sessions_overlapping(z('2026-09-28T04:00:00Z'), z('2026-11-14T05:00:00Z'))
    truth: dict[datetime, datetime | None] = {}
    within: list[datetime] = []
    beyond: list[datetime] = []
    for day in sessions:
        stamp, close = _ny_midnight(day), XNYS.session_close(day)
        roll = rng.random()
        if roll < 0.4:
            truth[stamp] = datetime.min.replace(tzinfo=UTC)
        elif roll < 0.6:
            truth[stamp] = close + timedelta(hours=rng.randrange(1, 24 * 4))
            within.append(stamp)
        elif roll < 0.75:
            truth[stamp] = close + timedelta(days=rng.randrange(12, 21))
            beyond.append(stamp)
        else:
            truth[stamp] = None
        if day.weekday() == 4 and rng.random() < 0.3:
            truth[_ny_midnight(day + timedelta(days=1))] = None
    sim.vendor.truth[dataset.asset_symbol] = truth
    times = _pass_times(date(2026, 9, 28), date(2026, 11, 13))
    # Only what the passes could have seen: a turn after the last pass is not owed to anyone.
    within = [stamp for stamp in within if truth[stamp] <= times[-1]]
    beyond = [stamp for stamp in beyond if truth[stamp] <= times[-1]]

    seen = await _simulate(sim, dataset, times)

    await _assert_integrity(sim, dataset, seen, times, within, beyond)
