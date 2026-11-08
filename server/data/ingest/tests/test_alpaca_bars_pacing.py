"""Every Alpaca bars HTTP request is paced by one scheduler token and counted, pages and retries included (P11d).

Bead tj-7zjov1.56 against builder-ingest's 3682c0d, checked against the bead's REQUIRED BEHAVIOUR (1) to (4) and
its ACCEPTANCE, the architect rulings of 2026-10-24 on P11 tj-7zjov1.20 (ruling (b): a 429 naming no reset waits
for the reset of the last good response of the same scope, and only without one for the declared window; ruling
(d): this bead), and ADR tj-fa1rpu D5, D6 and D8.

WHAT THE BUILDER BUILT, and what is pinned here: the bars page loop moved out of alpaca-py into
broker_api.read_bars_pages, which sends ONE request per broker_api.request_page through the client's own
session, acquiring a scheduler token before each one and counting it on the query's CallTally.

  (1) one acquire per HTTP request, every page and every retry, under the request's deadline; a page whose
      acquire misses the deadline fails the attempt RATE_BUDGET and the bars already read are dropped;
  (2) VendorCallStats carries the true calls, pages, retries and throttled_429;
  (3) a 429 is never retried and surfaces at once; a 504 is retried up to 3 times, 3 s apart, each through the
      scheduler and only while the wait fits before the deadline; the SDK's own retry loop is never entered;
  (4) the reset header of every response is read and the last one per scope remembered, so a header-less 429
      waits for it, if it is still ahead, and otherwise for the declared window.

THE COUPLING. request_page reads four private attributes of the alpaca-py client. The last section pins them,
and the SDK version they were read from, so an upgrade fails here by name rather than in production.

HOW IT IS DRIVEN. The REAL alpaca-py client over the recorded transport (alpaca_recorded): the recorded two-page
fixtures where a page is needed, constructed answers (provenance 'constructed') where a status or header is, and
headers ATTACHED rather than recorded, as everywhere else in this suite. The reader's waits before a 504 retry are
recorded, never slept.
"""

import asyncio
import importlib.metadata
import inspect
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from alpaca.common import rest as alpaca_rest
from alpaca.common.constants import DEFAULT_RETRY_ATTEMPTS, DEFAULT_RETRY_WAIT_SECONDS
from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.classify import RATE_LIMIT_RESET_HEADER, RETRY_AFTER_HEADER
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, BarsResponse, CallTally, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import ScheduledRequest
from data.ingest.app.scheduler import scheduler as scheduler_module
from data.ingest.tests.alpaca_recorded import (
    ALPACA_DATA_HOST,
    BARS_PATH,
    PLACEHOLDER_KEY_ID,
    PLACEHOLDER_SECRET,
    RecordedResponse,
    StubTradingVendor,
    asset_answer,
    bars_answer,
    load,
    record_page_retry_sleeps,
    sdk_retry_is_never_entered,
    status_answer,
    stub_trading_vendor,
    stub_vendor,
    with_headers,
)
from data.ingest.tests.scheduler_doubles import (
    ONE_PER_SECOND,
    Acquire,
    RecordingScheduler,
    alpaca_reader,
    real_scheduler,
)
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome
from schemas.data_ingest.submit import BarsRequest


pytestmark = pytest.mark.data_ingest

# The reader's clock, after every recorded bar (all in 2022).
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SYMBOL = 'AAPL'
PAGE1, PAGE2 = load('bars_1Day_page1'), load('bars_1Day_page2')
# The production Alpaca data scope declares a sixty-second window (handle.py); RecordingScheduler answers the same.
DECLARED_WINDOW = timedelta(seconds=60)


def reset_header(at: datetime) -> dict[str, str]:
    return {RATE_LIMIT_RESET_HEADER: str(at.timestamp())}


def page_one_naming(reset_at: datetime) -> RecordedResponse:
    """The recorded page 1, answering with a reset header attached (no recording carries one)."""
    return with_headers(PAGE1, reset_header(reset_at))


def two_page_query(**overrides) -> BarsQuery:
    fields = {
        'instrument': Instrument(SYMBOL, AssetType.STOCK),
        'granularity': Granularity.ONE_DAY,
        'start': PAGE1.timestamps[0],
        'end': PAGE2.timestamps[-1] + Granularity.ONE_DAY.offset,
        'priority': RequestPriority.BACKFILL,
    }
    return BarsQuery(**{**fields, **overrides})


def two_page_work() -> BarsWork:
    request = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol=SYMBOL,
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_DAY,
        start=PAGE1.timestamps[0],
        end=PAGE2.timestamps[-1] + Granularity.ONE_DAY.offset,
        update_type=UpdateType.STATIC,
    )
    return BarsWork(request=request, priority=RequestPriority.BACKFILL, feed=Feed.IEX)


def scheduled(ready_by: datetime | None = None) -> ScheduledRequest:
    return ScheduledRequest(request_id='r1', quota_scope=ALPACA_DATA_SCOPE, payload=two_page_work(), ready_by=ready_by)


def served_runner(
    vendor_client, executor: ThreadPoolExecutor, pacing: RecordingScheduler
) -> tuple[BarsAttemptRunner, StubTradingVendor]:
    """A bars runner over a real AlpacaRead that can also look its asset up, as a SUCCEEDED attempt now does.

    The lookup after the bars (tj-7zjov1.128) goes to its own trading stub, paced on the trading scope, so the
    data scope's acquires and the attempt's vendor_stats still count the bars calls alone.
    """
    trading = stub_trading_vendor(asset_answer(SYMBOL, 'aapl-vendor-id'))
    reader = alpaca_reader(
        vendor_client, executor, scheduler=pacing, trading_client=trading.client, trading_scope=ALPACA_TRADING_SCOPE
    )
    return BarsAttemptRunner(reader), trading


def data_acquires(pacing: RecordingScheduler) -> list[Acquire]:
    return [acquire for acquire in pacing.acquires if acquire.scope == ALPACA_DATA_SCOPE]


def trading_acquires(pacing: RecordingScheduler) -> list[Acquire]:
    return [acquire for acquire in pacing.acquires if acquire.scope == ALPACA_TRADING_SCOPE]


def failure_of(outcome: BarsResponse | BarsFailure):
    assert isinstance(outcome, BarsFailure), f'expected a BarsFailure, got {outcome!r}'
    return outcome.error


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """An IEX deployment, no cached client, and no remembered reset, around every test."""
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


# ---------------------------------------------------------------------------------------------
# (1) and (2): one acquire and one count per page
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_two_page_read_acquires_twice_under_the_same_priority_and_deadline(executor, sleeps, sdk_loop):
    """ACCEPTANCE: recorded fixtures for a two-page read show two acquires."""
    vendor = stub_vendor(PAGE1, PAGE2)
    pacing = RecordingScheduler()
    deadline = NOW + timedelta(minutes=5)

    response = await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=pacing).get_bars(
        two_page_query(deadline=deadline)
    )

    assert [bar.timestamp async for bar in response.bars] == PAGE1.timestamps + PAGE2.timestamps
    assert len(vendor.sent) == 2
    assert pacing.acquires == [Acquire(ALPACA_DATA_SCOPE, RequestPriority.BACKFILL, deadline)] * 2
    assert sleeps == []
    assert sdk_loop == []


@pytest.mark.asyncio
async def test_a_two_page_attempt_reports_calls_2_pages_2(executor, sdk_loop):
    """ACCEPTANCE: calls 2, pages 2, through the attempt runner's VendorCallStats."""
    vendor = stub_vendor(PAGE1, PAGE2)
    pacing = RecordingScheduler()
    runner, trading = served_runner(vendor.client, executor, pacing)

    before = datetime.now(UTC)
    result = await runner(scheduled(), 1)
    after = datetime.now(UTC)

    stats = result.trailer.vendor_stats
    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (2, 2, 0, 0)
    assert before <= stats.first_call_at <= stats.last_call_at <= after
    assert len(data_acquires(pacing)) == 2
    # The one lookup after the bars, on the trading scope: last, and the only acquire that is not the data scope's.
    assert len(trading_acquires(pacing)) == 1
    assert pacing.acquires[-1].scope == ALPACA_TRADING_SCOPE
    assert len(pacing.acquires) == 3
    assert len(trading.sent) == 1
    assert sdk_loop == []


# ---------------------------------------------------------------------------------------------
# (4): the reset of the last response of the scope is a header-less 429's first fallback
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_429_on_page_two_fails_the_attempt_rate_limited_with_the_reset_from_page_one(executor):
    """ACCEPTANCE: a 429 on page two fails the attempt RATE_LIMITED with the reset from page one's headers.

    42 s is neither the declared window (60 s) nor anything page two said, so only page one can have named it.
    """
    page_one_reset = NOW + timedelta(seconds=42)
    vendor = stub_vendor(page_one_naming(page_one_reset), status_answer(429))
    runner = BarsAttemptRunner(alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()))

    result = await runner(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is DeliveryErrorClass.RATE_LIMITED
    assert result.reset_at == page_one_reset
    assert result.parts == [], 'page one was delivered from a failed attempt'
    stats = result.trailer.vendor_stats
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (2, 1, 0, 1)
    assert [sent.params.get('page_token') for sent in vendor.sent] == [None, PAGE1.next_page_token]


@pytest.mark.asyncio
async def test_the_429s_own_header_still_wins_over_the_remembered_reset(executor):
    vendor = stub_vendor(
        page_one_naming(NOW + timedelta(seconds=42)), status_answer(429, headers={RETRY_AFTER_HEADER: '7'})
    )

    error = failure_of(
        await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )
    )

    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == NOW + timedelta(seconds=7)


@pytest.mark.asyncio
async def test_a_header_less_429_with_no_reset_seen_falls_back_to_the_sixty_second_window(executor):
    # Page one names no reset, so nothing is remembered: the declared window of the scope is all there is.
    vendor = stub_vendor(PAGE1, status_answer(429))

    error = failure_of(
        await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )
    )

    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == NOW + DECLARED_WINDOW


@pytest.mark.asyncio
async def test_a_remembered_reset_already_past_falls_back_to_the_declared_window(executor):
    vendor = stub_vendor(page_one_naming(NOW - timedelta(seconds=5)), status_answer(429))

    error = failure_of(
        await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )
    )

    assert error.reset_at == NOW + DECLARED_WINDOW
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, NOW) is None
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, NOW - timedelta(seconds=10)) == NOW - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_the_reset_of_an_earlier_attempt_of_the_same_scope_is_remembered_and_another_scopes_is_not(executor):
    """'The last good response of the same scope' (ruling (b)): across attempts, and never across scopes."""
    remembered = NOW + timedelta(seconds=42)
    served = stub_vendor(with_headers(load('bars_1Day'), reset_header(remembered)))
    await alpaca_reader(served.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(two_page_query())

    same_scope = stub_vendor(status_answer(429))
    other_scope = stub_vendor(status_answer(429))
    same = failure_of(
        await alpaca_reader(same_scope.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )
    )
    other = failure_of(
        await alpaca_reader(
            other_scope.client, executor, lambda: NOW, scheduler=RecordingScheduler(), scope='ALPACA_OTHER'
        ).get_bars(two_page_query())
    )

    assert same.reset_at == remembered
    assert other.reset_at == NOW + DECLARED_WINDOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('first_s', 'second_s'),
    [
        # A later instant: a memory that keeps the first reset, or never overwrites one, says 20 s.
        pytest.param(20, 50, id='the-later-response-names-a-later-reset'),
        # An earlier instant: a memory that keeps the furthest reset says 50 s. Ruling (b) names the LAST response.
        pytest.param(50, 20, id='the-later-response-names-an-earlier-reset'),
    ],
)
async def test_the_reset_remembered_is_the_one_the_last_response_named_not_the_first(executor, first_s, second_s):
    """Ruling (b): 'the last good response of the same scope'. Page two's reset replaces page one's.

    Architect gate, tj-7zjov1.79 note 16:51 UTC: a first-wins memory survived the whole suite.
    """
    first, second = NOW + timedelta(seconds=first_s), NOW + timedelta(seconds=second_s)
    served = stub_vendor(page_one_naming(first), with_headers(PAGE2, reset_header(second)))
    response = await alpaca_reader(served.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
        two_page_query()
    )
    assert [bar.timestamp async for bar in response.bars] == PAGE1.timestamps + PAGE2.timestamps

    assert broker_api.last_reset(ALPACA_DATA_SCOPE, NOW) == second

    throttled = stub_vendor(status_answer(429))
    error = failure_of(
        await alpaca_reader(throttled.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )
    )
    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == second


@pytest.mark.asyncio
async def test_a_reset_named_after_the_first_has_passed_replaces_it_so_a_header_less_429_waits_for_the_new_one(
    executor,
):
    """The stale case: without overwrite, the memory dies after one window and every 429 falls to the 60 s default.

    Attempt one remembers A; the reader's clock passes A; attempt two's good response names B; a header-less 429
    then fails RATE_LIMITED with B, not now plus the declared window.
    """
    clock = SimpleNamespace(at=NOW)
    reset_a = NOW + timedelta(seconds=30)
    reset_b = NOW + timedelta(seconds=90)

    first = stub_vendor(with_headers(load('bars_1Day'), reset_header(reset_a)))
    await alpaca_reader(first.client, executor, lambda: clock.at, scheduler=RecordingScheduler()).get_bars(
        two_page_query()
    )
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, clock.at) == reset_a

    clock.at = NOW + timedelta(seconds=40)
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, clock.at) is None, 'A has passed'

    second = stub_vendor(with_headers(load('bars_1Day'), reset_header(reset_b)))
    await alpaca_reader(second.client, executor, lambda: clock.at, scheduler=RecordingScheduler()).get_bars(
        two_page_query()
    )
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, clock.at) == reset_b

    clock.at = NOW + timedelta(seconds=45)
    throttled = stub_vendor(status_answer(429))
    error = failure_of(
        await alpaca_reader(throttled.client, executor, lambda: clock.at, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )
    )

    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == reset_b
    assert error.reset_at != clock.at + DECLARED_WINDOW


@pytest.mark.asyncio
async def test_the_reset_on_an_error_response_is_remembered_too(executor):
    """'The rate-limit headers of every response, good or bad, are read' (requirement 4)."""
    named = NOW + timedelta(seconds=42)
    first = stub_vendor(status_answer(500, headers=reset_header(named)))
    await alpaca_reader(first.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(two_page_query())

    assert broker_api.last_reset(ALPACA_DATA_SCOPE, NOW) == named


# ---------------------------------------------------------------------------------------------
# (1): a page whose acquire misses the deadline ends the attempt, and nothing of it is served
# ---------------------------------------------------------------------------------------------


class Frozen:
    """A monotonic clock that never moves, so the scheduler's single token never refills."""

    def __call__(self) -> float:
        return 0.0


@pytest.fixture
def scheduler_never_sleeps(monkeypatch) -> None:
    async def never_sleep(seconds: float) -> None:
        raise AssertionError(f'the scheduler slept {seconds}s instead of refusing at once')

    monkeypatch.setattr(scheduler_module, 'asyncio', SimpleNamespace(sleep=never_sleep))


@pytest.mark.asyncio
async def test_a_deadline_missed_on_page_two_fails_rate_budget_with_no_partial_bars(executor, scheduler_never_sleeps):
    # One token, refilled in a second on a clock that never moves: page one spends it, page two must wait 1 s,
    # and the deadline is half a second away.
    scheduler = real_scheduler({ALPACA_DATA_SCOPE: ONE_PER_SECOND}, clock=lambda: NOW, budget_clock=Frozen())
    vendor = stub_vendor(PAGE1, PAGE2)

    outcome = await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=scheduler).get_bars(
        two_page_query(deadline=NOW + timedelta(milliseconds=500))
    )

    error = failure_of(outcome)
    assert type(error) is ExogenousError
    assert error.reason is Reason.RATE_BUDGET
    assert error.reset_at == NOW + timedelta(seconds=1)
    assert error.__cause__ is None
    assert not hasattr(outcome, 'bars'), 'a partial read was handed back as a response'
    assert [sent.params.get('page_token') for sent in vendor.sent] == [None], 'page two went out without a token'


@pytest.mark.asyncio
async def test_the_attempt_that_missed_its_deadline_on_page_two_delivers_nothing(executor, scheduler_never_sleeps):
    scheduler = real_scheduler({ALPACA_DATA_SCOPE: ONE_PER_SECOND}, clock=lambda: NOW, budget_clock=Frozen())
    vendor = stub_vendor(PAGE1, PAGE2)
    runner = BarsAttemptRunner(alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=scheduler))

    result = await runner(scheduled(ready_by=NOW + timedelta(milliseconds=500)), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.served_range is None
    assert result.parts == []
    stats = result.trailer.vendor_stats
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (1, 1, 0, 0)


# ---------------------------------------------------------------------------------------------
# (3): a 504 is retried through the scheduler, at most three times and only while the wait fits
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_504_then_a_page_is_retried_with_an_acquire_each_and_counted(executor, sleeps, sdk_loop):
    vendor = stub_vendor(status_answer(504), status_answer(504), load('bars_1Day'))
    pacing = RecordingScheduler()
    runner, trading = served_runner(vendor.client, executor, pacing)

    result = await runner(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    stats = result.trailer.vendor_stats
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (3, 1, 2, 0)
    assert len(vendor.sent) == 3
    assert len(data_acquires(pacing)) == 3
    # The lookup after the bars is one more acquire, on the trading scope, and one request to the trading stub.
    assert [acquire.scope for acquire in pacing.acquires] == [ALPACA_DATA_SCOPE] * 3 + [ALPACA_TRADING_SCOPE]
    assert len(trading.sent) == 1
    assert sleeps == [3.0, 3.0]
    assert sdk_loop == []
    # A retry asks for the same page again: no token, as the page that failed was the first.
    assert [sent.params.get('page_token') for sent in vendor.sent] == [None, None, None]


@pytest.mark.asyncio
async def test_a_504_retried_three_times_then_fails_vendor_unavailable_counting_every_request(executor, sleeps):
    vendor = stub_vendor(*(status_answer(504) for _ in range(4)))
    pacing = RecordingScheduler()
    runner = BarsAttemptRunner(alpaca_reader(vendor.client, executor, scheduler=pacing))

    result = await runner(scheduled(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is DeliveryErrorClass.TRANSIENT
    stats = result.trailer.vendor_stats
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (4, 0, 3, 0)
    assert len(pacing.acquires) == 4
    assert sleeps == [3.0, 3.0, 3.0]


@pytest.mark.asyncio
async def test_a_504_on_page_two_retries_page_two_with_its_token(executor, sleeps):
    vendor = stub_vendor(PAGE1, status_answer(504), PAGE2)

    response = await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
        two_page_query()
    )

    assert [bar.timestamp async for bar in response.bars] == PAGE1.timestamps + PAGE2.timestamps
    assert [sent.params.get('page_token') for sent in vendor.sent] == [
        None,
        PAGE1.next_page_token,
        PAGE1.next_page_token,
    ]
    assert sleeps == [3.0]


class SteppedClock:
    """Wall time for broker_api, moved only by the page loop's own retry waits.

    broker_api reads datetime.now(UTC) to decide whether a retry wait fits before the deadline (_wait_fits). Its
    datetime reference is replaced by this class, and its asyncio.sleep by one that advances the same instant, so
    each wait the loop takes really spends its seconds against the deadline.
    """

    def __init__(self, start: datetime) -> None:
        self.at = start
        self.slept: list[float] = []

    def install(self, monkeypatch) -> None:
        clock = self

        class Now(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.at.astimezone(tz) if tz is not None else clock.at

        async def sleep(seconds: float) -> None:
            clock.slept.append(seconds)
            clock.at += timedelta(seconds=seconds)

        monkeypatch.setattr(broker_api, 'datetime', Now)
        monkeypatch.setattr(
            broker_api, 'asyncio', SimpleNamespace(sleep=sleep, get_running_loop=asyncio.get_running_loop)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('deadline_s', 'requests_made', 'waits'),
    [
        # Each wait is 3 s and must END strictly before the deadline.
        pytest.param(2.0, 1, [], id='no-wait-fits'),
        pytest.param(7.0, 3, [3.0, 3.0], id='two-waits-fit-the-third-would-end-at-9s'),
        pytest.param(60.0, 4, [3.0, 3.0, 3.0], id='all-three-fit'),
    ],
)
async def test_a_504_stops_being_retried_at_the_deadline_then_fails_vendor_unavailable(
    executor, monkeypatch, deadline_s, requests_made, waits
):
    clock = SteppedClock(datetime.now(UTC))
    clock.install(monkeypatch)
    vendor = stub_vendor(*(status_answer(504) for _ in range(4)))
    pacing = RecordingScheduler()
    deadline = clock.at + timedelta(seconds=deadline_s)

    error = failure_of(
        await alpaca_reader(vendor.client, executor, scheduler=pacing).get_bars(two_page_query(deadline=deadline))
    )

    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert error.__cause__.status_code == 504
    assert len(vendor.sent) == requests_made
    assert len(pacing.acquires) == requests_made
    assert clock.slept == waits


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 403, 404, 500])
async def test_no_other_status_is_retried(executor, sleeps, status):
    vendor = stub_vendor(status_answer(status))
    tally = CallTally()

    failure_of(
        await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query(tally=tally)
        )
    )

    assert len(vendor.sent) == 1
    assert (tally.calls, tally.pages, tally.retries, tally.throttled_429) == (1, 0, 0, 0)
    assert sleeps == []


@pytest.mark.asyncio
async def test_a_transport_failure_is_counted_as_a_call_and_not_retried(executor, sleeps):
    cut = requests.exceptions.ConnectionError('refused')
    vendor = stub_vendor(cut)
    tally = CallTally()

    error = failure_of(
        await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query(tally=tally)
        )
    )

    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert error.__cause__ is cut
    assert (tally.calls, tally.pages, tally.retries) == (1, 0, 0)
    assert sleeps == []


# ---------------------------------------------------------------------------------------------
# A malformed credential spends nothing: no acquire, no HTTP request, nothing on the tally
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('which', ['key', 'secret'])
async def test_a_malformed_credential_spends_no_token_makes_no_request_and_counts_nothing(executor, monkeypatch, which):
    # A trailing carriage return: refused by get_client's shape check (tj-7zjov1.67) before any client is built.
    monkeypatch.setenv('ALPACA_API_KEY', 'not-a-real-key\r' if which == 'key' else 'not-a-real-key')
    monkeypatch.setenv('ALPACA_API_SECRET', 'not-a-real-secret\r' if which == 'secret' else 'not-a-real-secret')
    pacing = RecordingScheduler()
    tally = CallTally()

    with patch.object(broker_api, 'StockHistoricalDataClient') as client_cls:
        with pytest.raises(MalformedCredentialsError):
            await broker_api.fetch_data_type(
                executor,
                two_page_query(tally=tally),
                DataType.MARKET_ACTIVITY,
                False,
                {},
                Feed.IEX,
                None,
                scheduler=pacing,
                scope=ALPACA_DATA_SCOPE,
            )
        runner_result = await BarsAttemptRunner(alpaca_reader(None, executor, scheduler=pacing))(scheduled(), 1)

    assert client_cls.call_count == 0, 'a client was built, so a request could have been made'
    assert pacing.acquires == []
    assert (tally.calls, tally.pages, tally.retries, tally.throttled_429) == (0, 0, 0, 0)
    stats = runner_result.trailer.vendor_stats
    assert runner_result.trailer.outcome is DeliveryOutcome.FAILED
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (0, 0, 0, 0)
    assert stats.first_call_at is None


# ---------------------------------------------------------------------------------------------
# Bodies the page loop cannot read stay loud (ADR tj-fa1rpu D5)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_json_null_bars_page_raises_attribute_error(executor):
    """Not guarded, as inside the SDK before: a loud bug. Narrowed from 'any raise' (test_alpaca_read_recorded)."""
    vendor = stub_vendor(load('bars_null'))

    with pytest.raises(AttributeError):
        await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )

    assert len(vendor.sent) == 1


@pytest.mark.asyncio
async def test_a_page_with_no_bars_key_raises_value_error(executor):
    no_bars = RecordedResponse(
        name='constructed_no_bars', status=200, body={'next_page_token': None}, provenance={'kind': 'constructed'}
    )
    vendor = stub_vendor(no_bars)

    with pytest.raises(ValueError, match='does not match any known keys'):
        await alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=RecordingScheduler()).get_bars(
            two_page_query()
        )


@pytest.mark.parametrize(
    'body', [pytest.param([], id='a-json-list'), pytest.param(None, id='json-null'), pytest.param('x', id='a-string')]
)
def test_request_page_refuses_a_body_that_is_not_a_json_object(body):
    vendor = stub_vendor(RecordedResponse(name='constructed', status=200, body=body))

    with pytest.raises(ValueError, match='not a JSON object'):
        broker_api.request_page(vendor.client, broker_api.BARS_PATH, {})


# ---------------------------------------------------------------------------------------------
# request_page: exactly one GET, the client's own URL and auth, the headers kept, the SDK's error
# ---------------------------------------------------------------------------------------------


def test_request_page_makes_one_get_to_the_bars_endpoint_with_the_clients_auth_and_keeps_the_headers(sdk_loop):
    answer = bars_answer({SYMBOL: []}, headers=reset_header(NOW))
    vendor = stub_vendor(answer)

    page = broker_api.request_page(vendor.client, broker_api.BARS_PATH, {'symbols': SYMBOL, 'page_token': None})

    [sent] = vendor.sent
    assert sent.method == 'GET'
    assert (sent.host, sent.path) == (ALPACA_DATA_HOST, BARS_PATH)
    assert sent.params == {'symbols': SYMBOL}, 'a None page_token must be left off the wire, as the SDK leaves it'
    assert sent.headers['APCA-API-KEY-ID'] == PLACEHOLDER_KEY_ID
    assert sent.headers['APCA-API-SECRET-KEY'] == PLACEHOLDER_SECRET
    assert page.body == answer.body
    assert page.headers[RATE_LIMIT_RESET_HEADER] == str(NOW.timestamp())
    assert sdk_loop == []


@pytest.mark.parametrize('status', [429, 504, 500, 403])
def test_request_page_raises_the_sdks_api_error_carrying_the_response_headers_and_never_retries(status, sdk_loop):
    vendor = stub_vendor(status_answer(status, 'slow down', headers={RETRY_AFTER_HEADER: '7'}))

    with pytest.raises(APIError) as raised:
        broker_api.request_page(vendor.client, broker_api.BARS_PATH, {})

    assert raised.value.status_code == status
    assert raised.value.response.headers[RETRY_AFTER_HEADER] == '7'
    assert raised.value.message == 'slow down'
    assert isinstance(raised.value.__cause__, requests.HTTPError)
    assert len(vendor.sent) == 1
    assert sdk_loop == []


def test_request_page_lets_a_session_failure_through_unwrapped():
    cut = requests.exceptions.ChunkedEncodingError('cut')
    vendor = stub_vendor(cut)

    with pytest.raises(requests.exceptions.ChunkedEncodingError) as raised:
        broker_api.request_page(vendor.client, broker_api.BARS_PATH, {})

    assert raised.value is cut


# ---------------------------------------------------------------------------------------------
# The single flight still collapses identical reads, and a collapsed read spends and counts nothing
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_identical_reads_in_flight_share_one_paged_read(executor):
    vendor = stub_vendor(PAGE1, PAGE2)
    pacing = RecordingScheduler()
    reader = alpaca_reader(vendor.client, executor, lambda: NOW, scheduler=pacing)
    leader_tally, follower_tally = CallTally(), CallTally()

    first, second = await asyncio.gather(
        reader.get_bars(two_page_query(tally=leader_tally)), reader.get_bars(two_page_query(tally=follower_tally))
    )

    expected = PAGE1.timestamps + PAGE2.timestamps
    assert [bar.timestamp async for bar in first.bars] == expected
    assert [bar.timestamp async for bar in second.bars] == expected
    assert len(vendor.sent) == 2, 'the second read went to the vendor instead of attaching to the first'
    assert len(pacing.acquires) == 2
    assert (leader_tally.calls, leader_tally.pages) == (2, 2)
    assert (follower_tally.calls, follower_tally.pages) == (0, 0)


# ---------------------------------------------------------------------------------------------
# The coupling: the private alpaca-py surface request_page relies on (REVIEW-TOMORROW on the bead)
# ---------------------------------------------------------------------------------------------

# The version request_page's private reads were taken from (broker_api.request_page docstring, uv.lock).
PINNED_ALPACA_PY = '0.44.0'


def test_alpaca_py_is_the_version_the_page_loop_was_written_against():
    """Upgrading alpaca-py fails HERE, by name, until the four reads below are checked against the new SDK.

    request_page reads _session, _base_url, _api_version and _get_default_headers, none of them public. An upgrade
    that renamed or reshaped one would fail every bars read in production; this makes the upgrade itself red, so
    whoever bumps the lock re-reads alpaca/common/rest.py and moves this pin on purpose.
    """
    assert importlib.metadata.version('alpaca-py') == PINNED_ALPACA_PY


def test_the_private_client_attributes_request_page_reads_have_the_shape_it_assumes():
    client = StockHistoricalDataClient(PLACEHOLDER_KEY_ID, PLACEHOLDER_SECRET)

    assert isinstance(client._session, requests.Session)
    assert callable(client._session.request)
    # A str-valued enum: concatenated with '+' (an f-string would render the member name, as the code says).
    assert client._base_url is BaseURL.DATA
    assert isinstance(client._base_url, str)
    assert client._base_url + '/' + client._api_version + broker_api.BARS_PATH == (
        f'https://{ALPACA_DATA_HOST}{BARS_PATH}'
    )
    assert client._api_version == 'v2'
    headers = client._get_default_headers()
    assert headers['APCA-API-KEY-ID'] == PLACEHOLDER_KEY_ID
    assert headers['APCA-API-SECRET-KEY'] == PLACEHOLDER_SECRET
    assert 'User-Agent' in headers


def test_the_sdk_retry_defaults_the_page_loop_copies_are_still_three_and_three():
    # broker_api takes its 504 retry count and wait from these, so a changed default changes the reader.
    assert (DEFAULT_RETRY_ATTEMPTS, DEFAULT_RETRY_WAIT_SECONDS) == (3, 3)
    assert (broker_api.BARS_PAGE_RETRIES, broker_api.BARS_PAGE_RETRY_WAIT_S) == (3, 3.0)
    assert broker_api.BARS_PAGE_SIZE == 10_000


def test_the_sdks_own_retry_still_cannot_be_switched_off_by_configuration():
    """Why the loop bypasses the SDK instead of configuring it (the bead's RECOMMENDED MECHANISM).

    If an SDK release honours retry_attempts=0, or lets the stock data client take a retry setting at all, the
    bypass is no longer the only way, and this says so. In 0.44.0 the stock client exposes none, and RESTClient,
    which holds the loop, falls back to its defaults for 0 and for an empty code list.
    """
    stock_client_parameters = inspect.signature(StockHistoricalDataClient.__init__).parameters
    assert not {'retry_attempts', 'retry_wait_seconds', 'retry_exception_codes'} & set(stock_client_parameters)

    rest = alpaca_rest.RESTClient(
        BaseURL.DATA, PLACEHOLDER_KEY_ID, PLACEHOLDER_SECRET, retry_attempts=0, retry_exception_codes=[]
    )

    assert rest._retry == DEFAULT_RETRY_ATTEMPTS
    assert 429 in rest._retry_codes and 504 in rest._retry_codes
