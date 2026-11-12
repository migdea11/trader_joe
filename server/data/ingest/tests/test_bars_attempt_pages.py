"""The bars runner chunks at MAX_PAGE_BARS, imported from its new home (tj-7zjov1.276, for P37b and P37c).

THE MULTI-PAGE PROOF, MOVED. test_fetch_dataset_handler.py pinned both sides of the MAX_PAGE_BARS boundary through
IngestFetchHandler: MAX_PAGE_BARS + 1 bars came out as two pages, neither over the cap and none lost; exactly
MAX_PAGE_BARS came out as one. tj-7zjov1.275 deleted that handler (owner ruling 2026-11-02 on tj-7zjov1.18), and
the one remaining chunker of vendor bars is BarsAttemptRunner (brokers/bars_attempt.py), whose same arithmetic had
no test: mutating its `>= MAX_PAGE_BARS` to `>= MAX_PAGE_BARS + 1` left all 2332 ingest tests green (measured on
tj-7zjov1.276 against d95bac5). This is that proof on the Submit path.

THE CONSTANT'S HOME (P37b, tj-7zjov1.274): MAX_PAGE_BARS moved from mapping/fetch_stream.py to mapping/delivery.py,
re-exported from common.rpc.mapping and, until P37d deletes it, from fetch_stream. bars_attempt reads it from
delivery. The number itself (5000, BarPage's bound) is pinned in common/tests/rpc/test_rpc_delivery_mapping.py.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.rpc import mapping
from common.rpc.mapping import delivery
from data.ingest.app.brokers import bars_attempt
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork
from data.ingest.app.brokers.interface import (
    Asset,
    AssetLookupQuery,
    AssetResponse,
    Bar,
    BarsQuery,
    BarsResponse,
    ServedRange,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import ScheduledRequest
from schemas.data_ingest.delivery import DeliveryOutcome
from schemas.data_ingest.fetch_dataset import BarPage
from schemas.data_ingest.submit import BarsRequest


pytestmark = pytest.mark.data_ingest

MAX_PAGE_BARS = delivery.MAX_PAGE_BARS
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
AS_OF = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)


def bar(index: int) -> Bar:
    at = START + timedelta(minutes=index)
    return Bar(timestamp=at, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0, trade_count=3, vwap=1.2)


class CountedRead:
    """A BrokerRead serving `count` one-minute bars and a found asset, recording how far the stream was read."""

    def __init__(self, count: int) -> None:
        self.__count = count
        self.pulled = 0

    async def get_bars(self, query: BarsQuery) -> BarsResponse:
        query.tally.record(AS_OF)
        query.tally.record_page()
        end = START + timedelta(minutes=self.__count)
        return BarsResponse(feed=Feed.IEX, bars=self.__stream(), served_range=ServedRange(START, end), as_of=AS_OF)

    async def __stream(self) -> AsyncIterator[Bar]:
        for index in range(self.__count):
            self.pulled += 1
            yield bar(index)

    async def get_asset(self, query: AssetLookupQuery) -> AssetResponse:
        asset = Asset(vendor_asset_id='vendor-id', symbol=query.symbol, raw_payload=b'{}')
        return AssetResponse(asset=asset, as_of=AS_OF)

    async def get_assets(self, query):
        raise AssertionError('a bars attempt never reads an asset list')


def scheduled() -> ScheduledRequest:
    request = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_MINUTE,
        start=START,
        end=START + timedelta(days=30),
        update_type=UpdateType.STATIC,
    )
    work = BarsWork(request=request, priority=RequestPriority.BACKFILL, feed=Feed.IEX)
    return ScheduledRequest(request_id='r1', quota_scope='alpaca-test', payload=work, ready_by=None)


async def pages_for(count: int) -> tuple[list[BarPage], CountedRead]:
    read = CountedRead(count)
    result = await BarsAttemptRunner(read)(scheduled(), 1)
    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert all(isinstance(part, BarPage) for part in result.parts)
    return result.parts, read


@pytest.mark.asyncio
async def test_one_bar_past_the_page_size_is_two_pages_neither_over_the_cap_and_none_lost():
    pages, read = await pages_for(MAX_PAGE_BARS + 1)

    assert [len(page.bars) for page in pages] == [MAX_PAGE_BARS, 1]
    served = [item.bar_start for page in pages for item in page.bars]
    assert served == [START + timedelta(minutes=index) for index in range(MAX_PAGE_BARS + 1)], 'a bar lost or moved'
    assert read.pulled == MAX_PAGE_BARS + 1


@pytest.mark.asyncio
async def test_exactly_the_page_size_is_one_page():
    pages, _ = await pages_for(MAX_PAGE_BARS)

    assert [len(page.bars) for page in pages] == [MAX_PAGE_BARS]


@pytest.mark.asyncio
async def test_two_full_pages_and_a_remainder_keep_their_order():
    pages, _ = await pages_for(2 * MAX_PAGE_BARS + 3)

    assert [len(page.bars) for page in pages] == [MAX_PAGE_BARS, MAX_PAGE_BARS, 3]
    assert [page.bars[0].bar_start for page in pages] == [
        START,
        START + timedelta(minutes=MAX_PAGE_BARS),
        START + timedelta(minutes=2 * MAX_PAGE_BARS),
    ]


def test_the_page_size_has_one_value_wherever_it_is_imported_from():
    """P37b moved the constant to mapping/delivery.py; every path that still names it must see the same object.

    bars_attempt's own binding is checked too: it imports from delivery since P37c, and a local 5000 written there
    instead would agree today and drift the day BarPage's bound changes. The old fetch_stream path went with
    the module (tj-7zjov1.280); the descriptor-side pin that it does not import is in common/tests.
    """
    assert MAX_PAGE_BARS == 5000
    assert 'MAX_PAGE_BARS' in delivery.__all__
    assert mapping.MAX_PAGE_BARS is delivery.MAX_PAGE_BARS
    assert bars_attempt.MAX_PAGE_BARS is delivery.MAX_PAGE_BARS
