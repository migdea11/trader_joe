"""The SIP recent-data edge in the Alpaca bars read (tj-7zjov1.308, tj-7zjov1.315; tests tj-7zjov1.309).

Design: decision tj-7zjov1.306 addendum 1 (R1-R3) and addendum 2 (owner ruling on Q tj-7zjov1.314, option A, with
conditions C1 and C2), ADR tj-fa1rpu D3. The rule, as ruled:

    edge = now - SIP_RECENT_WINDOW - SIP_EDGE_MARGIN, now from the reader's injected clock, on a SIP account only.
    An explicit end past the edge is refused, RANGE_TOO_RECENT, before any credential lookup, rate token or vendor
    call, with metadata {feed, range_end} and reset_at = end + window + margin. An end at the edge or older is sent
    unchanged. An open end is sent as the edge and served_range names that edge (C2); a start at or after the edge
    is refused with {feed, range_start} and reset_at = start + window + margin. IEX is never checked. A vendor 403
    that still arrives stays VENDOR_AUTH. The request key keeps the REQUESTED end, None for an open end, so
    identical open-end requests keep sharing one vendor call while the edge moves with the clock (C1).

    bars_attempt.error_class maps a non-REFUSED reason whose REASONS row requires reset_at to RATE_LIMITED, so the
    executor waits for reset_at rather than backing off; another NOT_READY reason stays TRANSIENT.

Constructed vendor answers only, over the recorded transport; never a live key (ADR tj-0rpt9t, tj-izzqub).
"""

import dataclasses
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import REASONS, ExogenousError, Outcome, Reason
from data.ingest.app.brokers import bars_attempt
from data.ingest.app.brokers.alpaca import broker_api, read
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork, error_class
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, BarsResponse, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import ScheduledRequest
from data.ingest.tests.alpaca_recorded import bars_answer, parse_rfc3339, status_answer, stub_vendor, wire_bar
from data.ingest.tests.scheduler_doubles import RecordingScheduler, alpaca_reader
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome
from schemas.data_ingest.submit import BarsRequest


pytestmark = pytest.mark.data_ingest

# The reader's clock. Earlier than the wall clock, so a reader that read the wall clock instead would put the edge
# weeks later and accept every end these tests refuse.
NOW = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=15)
MARGIN = timedelta(minutes=1)
# Written out, never read from the module: a test that derived the edge from read.SIP_* would follow a changed
# constant and stay green.
EDGE = NOW - WINDOW - MARGIN
ONE_US = timedelta(microseconds=1)
START = NOW - timedelta(hours=2)
SYMBOL = 'AAPL'


def query(start: datetime = START, end: datetime | None = None, **overrides) -> BarsQuery:
    fields = {
        'instrument': Instrument(SYMBOL, AssetType.STOCK),
        'granularity': Granularity.ONE_MINUTE,
        'start': start,
        'end': end,
        'priority': RequestPriority.BACKFILL,
    }
    return BarsQuery(**{**fields, **overrides})


def served_page(*stamps: datetime):
    return bars_answer({SYMBOL: [wire_bar(stamp) for stamp in stamps]})


def failure_of(outcome: BarsResponse | BarsFailure):
    assert isinstance(outcome, BarsFailure), f'expected a BarsFailure, got {outcome!r}'
    return outcome.error


def response_of(outcome: BarsResponse | BarsFailure) -> BarsResponse:
    assert isinstance(outcome, BarsResponse), f'expected a BarsResponse, got {outcome!r}'
    return outcome


class Lookups:
    """Stands in for broker_api.get_client and counts the credential lookups the reader makes."""

    def __init__(self, client) -> None:
        self.client = client
        self.count = 0

    def __call__(self):
        self.count += 1
        return self.client


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No cached client, no remembered reset, and no feed in the environment: the reader is handed its feed."""
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
def keys(monkeypatch) -> list:
    """Every VendorRequestKey the fetch builds, recorded as built."""
    built: list = []
    real = broker_api.VendorRequestKey

    def recording(**fields):
        key = real(**fields)
        built.append(key)
        return key

    monkeypatch.setattr(broker_api, 'VendorRequestKey', recording)
    return built


def reader_over(vendor, executor, monkeypatch, *, feed: Feed = Feed.SIP, clock=lambda: NOW, pacing=None):
    """A real AlpacaRead whose client comes from a counted credential lookup, as production's does."""
    lookups = Lookups(vendor.client)
    monkeypatch.setattr(broker_api, 'get_client', lookups)
    pacing = pacing if pacing is not None else RecordingScheduler()
    return alpaca_reader(None, executor, clock, scheduler=pacing, feed=feed), lookups, pacing


# ---------------------------------------------------------------------------------------------
# (1) An explicit end past the edge: refused, typed, before anything is spent
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'end',
    [EDGE + ONE_US, EDGE + timedelta(minutes=5), NOW - timedelta(minutes=1), NOW, NOW + timedelta(hours=1)],
    ids=['one-microsecond-past-the-edge', 'inside-the-window', 'a-minute-ago', 'now', 'in-the-future'],
)
async def test_a_sip_end_past_the_edge_is_refused_before_any_lookup_token_or_vendor_call(executor, monkeypatch, end):
    vendor = stub_vendor(served_page(START))
    reader, lookups, pacing = reader_over(vendor, executor, monkeypatch)

    error = failure_of(await reader.get_bars(query(end=end)))

    assert isinstance(error, ExogenousError)
    assert error.reason is Reason.RANGE_TOO_RECENT
    assert dict(error.metadata) == {'feed': Feed.SIP.value, 'range_end': end.isoformat()}
    assert error.reset_at == end + WINDOW + MARGIN
    # retry_after is derived from the reader's clock, not the wall clock.
    assert error.retry_after == -(-(end + WINDOW + MARGIN - NOW) // timedelta(seconds=1))
    assert vendor.sent == []
    assert pacing.acquires == []
    assert lookups.count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'end', [EDGE, EDGE - ONE_US, EDGE - timedelta(hours=1)], ids=['at-the-edge', 'just-older', 'older']
)
async def test_a_sip_end_at_the_edge_or_older_is_sent_unchanged(executor, monkeypatch, keys, end):
    """The boundary direction: an end EXACTLY at the edge is accepted; only a later one is refused."""
    vendor = stub_vendor(served_page(START))
    reader, lookups, pacing = reader_over(vendor, executor, monkeypatch)

    response = response_of(await reader.get_bars(query(end=end)))

    assert len(vendor.sent) == 1
    assert parse_rfc3339(vendor.sent[0].params['end']) == end
    assert vendor.sent[0].params['feed'] == 'sip'
    assert response.feed is Feed.SIP
    assert response.served_range.end == end
    assert len(pacing.acquires) == 1
    assert lookups.count == 1
    assert [key.range_end for key in keys] == [end]


# ---------------------------------------------------------------------------------------------
# (4) The open end, option A: sent as the edge, served up to it (C2)
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sip_open_end_is_sent_as_the_edge_and_served_up_to_it(executor, monkeypatch, keys):
    """C2: served_range.end is the edge sent, never as_of and never None; no bar at or after the edge survives."""
    # Constructed: a vendor answering past the end it was sent, to pin that the filter cuts at the SAME edge.
    vendor = stub_vendor(served_page(EDGE - timedelta(minutes=1), EDGE, EDGE + timedelta(minutes=1)))
    reader, _, _ = reader_over(vendor, executor, monkeypatch)

    response = response_of(await reader.get_bars(query(end=None)))

    assert parse_rfc3339(vendor.sent[0].params['end']) == EDGE
    assert response.served_range.start == START
    assert response.served_range.end == EDGE
    assert response.as_of == NOW
    assert [bar.timestamp async for bar in response.bars] == [EDGE - timedelta(minutes=1)]
    # C1: the request key keeps the REQUESTED end, which was open.
    assert [key.range_end for key in keys] == [None]


@pytest.mark.asyncio
async def test_identical_open_end_requests_share_one_key_while_the_edge_moves_with_the_clock(
    executor, monkeypatch, keys
):
    """Keying on the edge would split two identical open-end requests a tick apart onto two vendor calls."""
    ticks = iter([NOW, NOW + timedelta(seconds=1)])
    current = {'now': NOW}

    def clock() -> datetime:
        return current['now']

    vendor = stub_vendor(served_page(START), served_page(START))
    reader, _, _ = reader_over(vendor, executor, monkeypatch, clock=clock)

    sent_ends = []
    for tick in ticks:
        current['now'] = tick
        response = response_of(await reader.get_bars(query(end=None)))
        sent_ends.append(parse_rfc3339(vendor.sent[-1].params['end']))
        assert response.served_range.end == tick - WINDOW - MARGIN

    assert sent_ends == [EDGE, EDGE + timedelta(seconds=1)], 'the edge follows the injected clock'
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert keys[0].range_end is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'start', [EDGE, EDGE + ONE_US, NOW - timedelta(minutes=1)], ids=['at-the-edge', 'just-past', 'a-minute-ago']
)
async def test_a_sip_open_end_whose_start_is_at_or_after_the_edge_is_refused(executor, monkeypatch, start):
    vendor = stub_vendor(served_page(START))
    reader, lookups, pacing = reader_over(vendor, executor, monkeypatch)

    error = failure_of(await reader.get_bars(query(start=start, end=None)))

    assert error.reason is Reason.RANGE_TOO_RECENT
    assert dict(error.metadata) == {'feed': Feed.SIP.value, 'range_start': start.isoformat()}
    assert error.reset_at == start + WINDOW + MARGIN
    assert vendor.sent == []
    assert pacing.acquires == []
    assert lookups.count == 0


@pytest.mark.asyncio
async def test_a_sip_open_end_whose_start_is_just_before_the_edge_is_sent_as_the_edge(executor, monkeypatch):
    vendor = stub_vendor(served_page())
    reader, _, _ = reader_over(vendor, executor, monkeypatch)

    response = response_of(await reader.get_bars(query(start=EDGE - ONE_US, end=None)))

    assert parse_rfc3339(vendor.sent[0].params['end']) == EDGE
    assert response.served_range.end == EDGE


@pytest.mark.asyncio
async def test_a_start_at_or_after_now_is_still_range_in_future_first(executor, monkeypatch):
    """The SIP check runs after RANGE_IN_FUTURE, so a start in the future keeps its own REFUSED reason."""
    vendor = stub_vendor()
    reader, lookups, _ = reader_over(vendor, executor, monkeypatch)

    error = failure_of(await reader.get_bars(query(start=NOW, end=NOW + timedelta(hours=1))))

    assert error.reason is Reason.RANGE_IN_FUTURE
    assert vendor.sent == []
    assert lookups.count == 0


@pytest.mark.asyncio
async def test_a_named_feed_the_account_cannot_serve_is_still_feed_not_available_first(executor, monkeypatch):
    vendor = stub_vendor()
    reader, _, _ = reader_over(vendor, executor, monkeypatch)

    error = failure_of(await reader.get_bars(query(end=NOW, feed=Feed.IEX)))

    assert error.reason is Reason.FEED_NOT_AVAILABLE


# ---------------------------------------------------------------------------------------------
# (3) IEX is never checked
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'end', [EDGE + ONE_US, NOW - timedelta(minutes=1), NOW, EDGE], ids=['past-the-edge', 'a-minute-ago', 'now', 'edge']
)
async def test_an_iex_explicit_end_is_sent_unchanged_whatever_it_is(executor, monkeypatch, keys, end):
    vendor = stub_vendor(served_page(START))
    reader, _, pacing = reader_over(vendor, executor, monkeypatch, feed=Feed.IEX)

    response = response_of(await reader.get_bars(query(end=end)))

    assert parse_rfc3339(vendor.sent[0].params['end']) == end
    assert vendor.sent[0].params['feed'] == 'iex'
    assert response.served_range.end == min(end, NOW)
    assert len(pacing.acquires) == 1
    assert [key.range_end for key in keys] == [end]


@pytest.mark.asyncio
@pytest.mark.parametrize('start', [START, EDGE, NOW - timedelta(minutes=1)], ids=['old', 'at-the-sip-edge', 'recent'])
async def test_an_iex_open_end_is_sent_open_and_served_up_to_as_of(executor, monkeypatch, start):
    vendor = stub_vendor(served_page(start))
    reader, _, _ = reader_over(vendor, executor, monkeypatch, feed=Feed.IEX)

    response = response_of(await reader.get_bars(query(start=start, end=None)))

    assert 'end' not in vendor.sent[0].params
    assert response.served_range.end == NOW


# ---------------------------------------------------------------------------------------------
# (6) A 403 that still arrives stays VENDOR_AUTH
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('feed', [Feed.SIP, Feed.IEX], ids=str)
async def test_a_vendor_403_still_classifies_vendor_auth(executor, monkeypatch, feed):
    """Classified on status only (ingest pitfall 4), even with s8b's body: the pre-check does not reclassify."""
    vendor = stub_vendor(status_answer(403, 'subscription does not permit querying recent SIP data'))
    reader, _, _ = reader_over(vendor, executor, monkeypatch, feed=feed)

    error = failure_of(await reader.get_bars(query(end=EDGE - timedelta(hours=1))))

    assert error.reason is Reason.VENDOR_AUTH
    assert len(vendor.sent) == 1


# ---------------------------------------------------------------------------------------------
# (5) The error class, and the attempt result carrying reset_at
# ---------------------------------------------------------------------------------------------


def _error_for(reason: Reason):
    spec = REASONS[reason]
    reset_at = NOW if spec.outcome is Outcome.NOT_READY else None
    return spec.branch(reason, 'detail', reset_at=reset_at)


@pytest.mark.parametrize(
    ('reason', 'klass'),
    [
        (Reason.RANGE_TOO_RECENT, DeliveryErrorClass.RATE_LIMITED),
        (Reason.VENDOR_RATE_LIMITED, DeliveryErrorClass.RATE_LIMITED),
        (Reason.RATE_BUDGET, DeliveryErrorClass.RATE_LIMITED),
        (Reason.VENDOR_UNAVAILABLE, DeliveryErrorClass.TRANSIENT),
        (Reason.VENDOR_AUTH, DeliveryErrorClass.TRANSIENT),
        (Reason.RANGE_IN_FUTURE, DeliveryErrorClass.PERMANENT),
    ],
    ids=str,
)
def test_the_error_class_of_the_named_reasons(reason, klass):
    assert error_class(_error_for(reason)) is klass


@pytest.mark.parametrize('reason', list(Reason), ids=str)
def test_the_error_class_follows_the_reasons_row_for_every_reason(reason):
    """REFUSED -> PERMANENT; NOT_READY with requires_reset_at -> RATE_LIMITED; any other NOT_READY -> TRANSIENT."""
    spec = REASONS[reason]
    if spec.outcome is Outcome.REFUSED:
        expected = DeliveryErrorClass.PERMANENT
    elif spec.requires_reset_at:
        expected = DeliveryErrorClass.RATE_LIMITED
    else:
        expected = DeliveryErrorClass.TRANSIENT
    assert error_class(_error_for(reason)) is expected


def test_the_rate_limited_class_is_read_from_the_requires_reset_at_column_not_a_list_of_names(monkeypatch):
    """The same reason, its row's flag cleared, is an ordinary NOT_READY: TRANSIENT. No hard-coded name survives."""
    error = _error_for(Reason.RANGE_TOO_RECENT)
    cleared = dict(REASONS)
    cleared[Reason.RANGE_TOO_RECENT] = dataclasses.replace(REASONS[Reason.RANGE_TOO_RECENT], requires_reset_at=False)
    monkeypatch.setattr(bars_attempt, 'REASONS', cleared)

    assert error_class(error) is DeliveryErrorClass.TRANSIENT


def _work(start: datetime, end: datetime | None) -> BarsWork:
    request = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol=SYMBOL,
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_MINUTE,
        start=start,
        end=end,
        update_type=UpdateType.STATIC,
    )
    return BarsWork(request=request, priority=RequestPriority.BACKFILL, feed=Feed.SIP)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('start', 'end', 'reset_at'),
    [
        (START, NOW - timedelta(minutes=1), NOW - timedelta(minutes=1) + WINDOW + MARGIN),
        (NOW - timedelta(minutes=5), None, NOW - timedelta(minutes=5) + WINDOW + MARGIN),
    ],
    ids=['explicit-end', 'open-end-recent-start'],
)
async def test_a_refused_sip_attempt_is_rate_limited_and_carries_its_reset_at(
    executor, monkeypatch, start, end, reset_at
):
    """The executor waits reset_wait(reset_at), not a backoff that would burn ALPACA_MAX_ATTEMPTS in the window."""
    vendor = stub_vendor()
    reader, lookups, pacing = reader_over(vendor, executor, monkeypatch)
    scheduled = ScheduledRequest(
        request_id='r1', quota_scope=ALPACA_DATA_SCOPE, payload=_work(start, end), ready_by=None
    )

    result = await BarsAttemptRunner(reader)(scheduled, 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is DeliveryErrorClass.RATE_LIMITED
    assert result.reset_at == reset_at
    assert result.parts == []
    assert result.trailer.vendor_stats.calls == 0
    assert vendor.sent == []
    assert pacing.acquires == []
    assert lookups.count == 0


# ---------------------------------------------------------------------------------------------
# C1: the vendor rule lives in the Alpaca read and nowhere else
# ---------------------------------------------------------------------------------------------

SERVER = Path(__file__).resolve().parents[3]
ALPACA = SERVER / 'data' / 'ingest' / 'app' / 'brokers' / 'alpaca'
# The constants by name, and the cut-off arithmetic in any spelling a second copy would take.
VENDOR_RULE = re.compile(
    r'SIP_RECENT_WINDOW|SIP_EDGE_MARGIN|timedelta\(\s*minutes\s*=\s*1[56]\s*\)|timedelta\(\s*seconds\s*=\s*9[06]0\s*\)'
)


def _production_modules() -> list[Path]:
    return [
        path
        for path in SERVER.rglob('*.py')
        if 'tests' not in path.relative_to(SERVER).parts and '.venv' not in str(path) and ALPACA not in path.parents
    ]


def test_the_two_constants_are_the_ruled_values_and_kept_separate():
    """Two constants, never a fused 16, so the boundary probe can shrink the margin alone (addendum 1 R1)."""
    assert read.SIP_RECENT_WINDOW == WINDOW
    assert read.SIP_EDGE_MARGIN == MARGIN


def test_no_module_outside_the_alpaca_read_names_the_sip_edge_or_its_arithmetic():
    """C1: bars_attempt, the executor, the servicer, the store and the schemas see only the generic reason."""
    modules = _production_modules()
    assert len(modules) > 50, 'the scan found too few modules to mean anything'
    offenders = [
        f'{path.relative_to(SERVER)}:{number}: {line.strip()}'
        for path in modules
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1)
        if VENDOR_RULE.search(line)
    ]
    assert offenders == []


def test_within_the_alpaca_package_only_read_py_holds_the_rule():
    """broker_api.py is NOT touched (C1): the key keeps the requested end, so it has no use for the edge."""
    holders = sorted(path.name for path in ALPACA.rglob('*.py') if VENDOR_RULE.search(path.read_text(encoding='utf-8')))
    assert holders == ['read.py']
