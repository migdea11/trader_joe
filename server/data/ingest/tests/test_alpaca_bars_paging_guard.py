"""The bars page loop ends on an empty token and refuses a repeated token or an endless read (P13g).

Bead tj-7zjov1.155 against builder-ingest's 31bb978, checked against the bead's WORK and ACCEPTANCE: a repeated
token raises after exactly the pages before the repeat; an empty token ends the read like a missing one; the page
cap (broker_api.BARS_MAX_PAGES, 1000) raises; the 504 retry of a page resends that page's token and is not a
repeat; nothing else changes.

HOW IT IS DRIVEN. The REAL alpaca-py client over the recorded transport (alpaca_recorded), through
AlpacaRead.get_bars, as the corporate-actions guard tests in test_alpaca_corporate_actions drive theirs. The pages
are constructed (provenance 'constructed'), since no recording repeats a token. The reader's waits before a 504
retry are recorded, never slept. Each scripted vendor answers past the guard's stopping point where it can, so a
read that ignored the guard would visibly ask for, or return, more.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from common.enums.data_select import AssetType
from common.enums.data_stock import Granularity
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.interface import BarsQuery, BarsResponse, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.tests.alpaca_recorded import (
    bars_answer,
    record_page_retry_sleeps,
    sdk_retry_is_never_entered,
    status_answer,
    stub_vendor,
    wire_bar,
)
from data.ingest.tests.scheduler_doubles import RecordingScheduler, alpaca_reader


pytestmark = pytest.mark.data_ingest

# The reader's clock, after every constructed bar.
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SYMBOL = 'AAPL'
FIRST_DAY = datetime(2022, 1, 3, 5, 0, tzinfo=UTC)
# Ten daily bars' room: every page below serves one bar of its own day inside [START, END).
START = FIRST_DAY
END = FIRST_DAY + timedelta(days=10)


def day(n: int) -> datetime:
    return FIRST_DAY + timedelta(days=n)


def page(n: int, next_page_token: str | None):
    """A constructed bars page carrying one bar on day n, closing at n, and the given next token."""
    return bars_answer({SYMBOL: [wire_bar(day(n), close=float(n))]}, next_page_token)


def query() -> BarsQuery:
    return BarsQuery(
        instrument=Instrument(SYMBOL, AssetType.STOCK),
        granularity=Granularity.ONE_DAY,
        start=START,
        end=END,
        priority=RequestPriority.BACKFILL,
    )


def sent_tokens(vendor) -> list[str | None]:
    return [sent.params.get('page_token') for sent in vendor.sent]


async def bars_of(response) -> list[tuple[datetime, float]]:
    assert isinstance(response, BarsResponse), f'expected a BarsResponse, got {response!r}'
    return [(bar.timestamp, bar.close) async for bar in response.bars]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """An IEX deployment, no cached client, and no remembered reset, around every test."""
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    broker_api.set_client(None)
    broker_api.forget_resets()
    yield
    broker_api.set_client(None)
    broker_api.forget_resets()


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    return record_page_retry_sleeps(monkeypatch)


@pytest.fixture
def sdk_loop(monkeypatch) -> list[str]:
    return sdk_retry_is_never_entered(monkeypatch)


def read(vendor, executor, pacing: RecordingScheduler | None = None):
    return alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=pacing or RecordingScheduler()).get_bars(
        query()
    )


# ---------------------------------------------------------------------------------------------
# A repeated token
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_repeated_after_two_pages_raises_after_exactly_three_requests(executor, sdk_loop):
    """ACCEPTANCE: A, B, then A again raises after exactly the pages before the repeat, each acquired once."""
    # A fourth answer is scripted, so a loop that did not raise would ask for it and be seen asking.
    vendor = stub_vendor(page(0, 'tok-a'), page(1, 'tok-b'), page(2, 'tok-a'), page(3, None), request_cap=4)
    pacing = RecordingScheduler()

    with pytest.raises(ValueError, match='repeated'):
        await read(vendor, executor, pacing)

    assert sent_tokens(vendor) == [None, 'tok-a', 'tok-b']
    assert len(pacing.acquires) == 3
    assert sdk_loop == []


@pytest.mark.asyncio
async def test_a_token_repeating_the_one_just_sent_raises_on_the_second_page(executor):
    """The vendor answering page A with A again is the shortest loop there is."""
    vendor = stub_vendor(page(0, 'tok-a'), page(1, 'tok-a'), page(2, None), request_cap=3)

    with pytest.raises(ValueError, match='repeated'):
        await read(vendor, executor)

    assert sent_tokens(vendor) == [None, 'tok-a']


# ---------------------------------------------------------------------------------------------
# An empty token
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_string_token_ends_the_read_with_the_bars_merged_so_far(executor):
    """ACCEPTANCE: '' ends the read like null; it is never sent back as page_token=''."""
    # A third answer is scripted, so a loop that sent the empty token back would be served it and be seen.
    vendor = stub_vendor(page(0, 'tok-a'), page(1, ''), page(2, None), request_cap=3)

    response = await read(vendor, executor)

    assert await bars_of(response) == [(day(0), 0.0), (day(1), 1.0)]
    assert sent_tokens(vendor) == [None, 'tok-a']


@pytest.mark.asyncio
async def test_an_empty_string_token_on_the_first_page_is_a_one_page_read(executor):
    vendor = stub_vendor(page(0, ''), page(1, None), request_cap=2)

    response = await read(vendor, executor)

    assert await bars_of(response) == [(day(0), 0.0)]
    assert sent_tokens(vendor) == [None]


# ---------------------------------------------------------------------------------------------
# The page cap
# ---------------------------------------------------------------------------------------------


def test_the_bars_page_cap_is_a_thousand_pages_beside_the_page_size():
    assert broker_api.BARS_MAX_PAGES == 1000
    assert broker_api.BARS_PAGE_SIZE == 10_000


@pytest.mark.asyncio
async def test_a_vendor_that_always_gives_a_fresh_token_raises_after_exactly_the_cap(executor, monkeypatch):
    """ACCEPTANCE: the cap raises. Patched down to 3; a read ignoring it is served a last page and returns."""
    monkeypatch.setattr(broker_api, 'BARS_MAX_PAGES', 3)
    vendor = stub_vendor(page(0, 'tok-0'), page(1, 'tok-1'), page(2, 'tok-2'), page(3, None), request_cap=4)
    pacing = RecordingScheduler()

    with pytest.raises(ValueError, match='more than 3 bars pages'):
        await read(vendor, executor, pacing)

    assert sent_tokens(vendor) == [None, 'tok-0', 'tok-1']
    assert len(pacing.acquires) == 3


@pytest.mark.asyncio
async def test_a_read_that_ends_on_the_last_page_the_cap_allows_is_served(executor, monkeypatch):
    """The cap counts pages read, not pages refused: the cap-th page ending the read is a whole read."""
    monkeypatch.setattr(broker_api, 'BARS_MAX_PAGES', 3)
    vendor = stub_vendor(page(0, 'tok-0'), page(1, 'tok-1'), page(2, None), request_cap=3)

    response = await read(vendor, executor)

    assert await bars_of(response) == [(day(0), 0.0), (day(1), 1.0), (day(2), 2.0)]
    assert len(vendor.sent) == 3


# ---------------------------------------------------------------------------------------------
# A 504 retry is not a repeat
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_504_then_200_on_page_two_resends_its_token_and_is_not_a_repeat(executor, sleeps, sdk_loop):
    """ACCEPTANCE: a 504 retry of a page still succeeds, sending that page's own token again."""
    vendor = stub_vendor(page(0, 'tok-a'), status_answer(504), page(1, 'tok-b'), page(2, None), request_cap=4)
    pacing = RecordingScheduler()

    response = await read(vendor, executor, pacing)

    assert await bars_of(response) == [(day(0), 0.0), (day(1), 1.0), (day(2), 2.0)]
    assert sent_tokens(vendor) == [None, 'tok-a', 'tok-a', 'tok-b']
    assert len(pacing.acquires) == 4
    assert sleeps == [3.0]
    assert sdk_loop == []


# ---------------------------------------------------------------------------------------------
# Nothing else changes
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_normal_multi_page_read_merges_every_page_in_order(executor, sleeps, sdk_loop):
    vendor = stub_vendor(page(0, 'tok-a'), page(1, 'tok-b'), page(2, 'tok-c'), page(3, None), request_cap=4)

    response = await read(vendor, executor)

    assert await bars_of(response) == [(day(n), float(n)) for n in range(4)]
    assert sent_tokens(vendor) == [None, 'tok-a', 'tok-b', 'tok-c']
    assert {sent.params.get('limit') for sent in vendor.sent} == {str(broker_api.BARS_PAGE_SIZE)}
    assert sleeps == []
    assert sdk_loop == []


@pytest.mark.asyncio
async def test_a_missing_next_page_token_still_ends_the_read(executor):
    """A page with no next_page_token member at all (not null, absent) is the last page, as before."""
    last = page(1, None)
    del last.body['next_page_token']
    vendor = stub_vendor(page(0, 'tok-a'), last, page(2, None), request_cap=3)

    response = await read(vendor, executor)

    assert await bars_of(response) == [(day(0), 0.0), (day(1), 1.0)]
    assert sent_tokens(vendor) == [None, 'tok-a']
