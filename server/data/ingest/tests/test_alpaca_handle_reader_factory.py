"""AlpacaHandle's reader seam: an optional, keyword-only reader factory, called once at build (P53, tj-7zjov1.158).

Against builder-ingest's 9162968, checked against P53's body, ADR tj-26jt9w addendum 1 item 1 and the P14 gate verdict
note on tj-7zjov1.24. Pinned: omitted, the handle builds AlpacaRead with the scheduler, the data scope and the
trading scope, all keywords; given, the factory is called ONCE with those same keywords and its reader is handle.reader
and the reader every runner calls, bars, asset list and corporate actions alike, with no AlpacaRead built;
reader_factory is keyword-only. RE-POINTED by tj-7zjov1.343 (ADR tj-xn3qa6 addendum 3, owner ruling 2026-11-06): the
feed is decided per request, so no feed is resolved at build and the factory is handed none; a bars request naming
no feed reaches the factory's reader as SIP.

WHAT IS REAL. The production AlpacaHandle, its Scheduler, its AckGatedExecutor and its runners. The factory's reader is
a FakeRead paced on the handle's own scheduler, as the fake-mode launcher builds it; its call log is what reached the
"vendor". AlpacaRead is replaced in the handle module only where a test must see how it is constructed, or that it is
not. The store is a double that answers every report FINAL. No vendor key is read and no network is reached.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca import handle as handle_module
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE, AlpacaHandle
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.scheduler.executor import ExecutorSettings
from data.ingest.app.scheduler.retry import RetryPolicy
from schemas.data_ingest.delivery import AttemptDelivery, DeliverAttemptResponse, DeliveryOutcome, DeliveryVerdict
from schemas.data_ingest.submit import (
    AssetListRequest,
    AssetListStatus,
    BarsRequest,
    CorporateActionsRequest,
    SubmitRequest,
)
from tests.fakes.market_data import FakeRead


pytestmark = pytest.mark.data_ingest

SYMBOL = 'VFV'
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)
GUARD_S = 5.0
INSTANCE = 'instance-under-test'

# ---------------------------------------------------------------------------------------------
# Doubles and harness
# ---------------------------------------------------------------------------------------------


class RecordingFactory:
    """A reader factory that records each call and returns a FakeRead paced on the scheduler it was handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.built: list[FakeRead] = []

    def __call__(self, *args, **kwargs) -> FakeRead:
        self.calls.append((args, dict(kwargs)))
        reader = FakeRead(scheduler=kwargs['scheduler'], scope=kwargs['scope'], trading_scope=kwargs['trading_scope'])
        self.built.append(reader)
        return reader


class AnsweringStore:
    """A StoreDeliveryClient that answers every delivery FINAL at once and records it."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        return DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=attempt.header.attempt_no)

    async def register_instance(self, request):
        raise AssertionError('the executor never registers an instance')

    async def report_for(self, request_id: str) -> AttemptDelivery:
        """The first report of the given request, waiting for it within the guard."""

        async def wait() -> AttemptDelivery:
            while True:
                for attempt in self.received:
                    if attempt.header.request_id == request_id:
                        return attempt
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(wait(), GUARD_S)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No credential, no trading host, no rate-limit override, no cached client or reset."""
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)
    broker_api.set_client(None)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()
    yield
    broker_api.set_client(None)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()


def settings() -> ExecutorSettings:
    return ExecutorSettings(
        buffer_size=8,
        workers=1,
        stall_timeout_s=300.0,
        stall_check_interval_s=3600.0,
        redelivery_base_s=1.0,
        redelivery_max_s=1.5,
        retry=RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=60.0, factor=2.0),
    )


def refuse_alpaca_read(monkeypatch) -> Mock:
    """Replace AlpacaRead in the handle module with a double that fails the test if it is constructed."""
    built = Mock(name='AlpacaRead', side_effect=AssertionError('AlpacaRead was built although a factory was given'))
    monkeypatch.setattr(handle_module, 'AlpacaRead', built)
    return built


def count_resolutions(monkeypatch) -> list[Feed | None]:
    """Pass broker_api.resolve_feed through, recording every feed it resolves.

    Set before the handle is built, so the policy the handle takes is this pass-through.
    """
    resolved: list[Feed | None] = []
    real = broker_api.resolve_feed

    def counted(*args, **kwargs):
        feed = real(*args, **kwargs)
        resolved.append(feed)
        return feed

    monkeypatch.setattr(broker_api, 'resolve_feed', counted)
    return resolved


BARS = BarsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol=SYMBOL,
    asset_type=AssetType.STOCK,
    data_types=[DataType.MARKET_ACTIVITY],
    granularity=Granularity.THIRTY_MINUTES,
    start=START,
    end=END,
    update_type=UpdateType.STATIC,
)
LISTING = AssetListRequest(source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE)
ACTIONS = CorporateActionsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol=SYMBOL,
    start=datetime(2026, 1, 1, tzinfo=UTC),
    end=datetime(2026, 2, 1, tzinfo=UTC),
)
# One request of each kind, the vendor call its runner must make through the reader, and the scope it is charged to.
EACH_KIND = (
    ('bars-1', {'bars': BARS}, ('bars', SYMBOL), ALPACA_DATA_SCOPE),
    ('assets-1', {'asset_list': LISTING}, ('assets', AssetListStatus.ACTIVE.value), ALPACA_TRADING_SCOPE),
    ('actions-1', {'corporate_actions': ACTIONS}, ('corporate_actions', SYMBOL), ALPACA_DATA_SCOPE),
)


# ---------------------------------------------------------------------------------------------
# (1) Omitted: AlpacaRead, built exactly as before
# ---------------------------------------------------------------------------------------------


def test_without_a_factory_the_handle_builds_alpaca_read_with_the_scheduler_and_both_scopes_and_no_feed(monkeypatch):
    built = Mock(name='AlpacaRead')
    monkeypatch.setattr(handle_module, 'AlpacaRead', built)

    handle = AlpacaHandle(settings=settings(), calls_per_minute=60)

    built.assert_called_once_with(
        scheduler=handle.scheduler, scope=ALPACA_DATA_SCOPE, trading_scope=ALPACA_TRADING_SCOPE
    )
    assert built.call_args.args == (), 'AlpacaRead was given a positional argument'
    assert handle.reader is built.return_value


def test_without_a_factory_the_handles_reader_is_a_real_alpaca_read():
    # Unpatched: the production default. That it serves each request's feed is pinned end to end in test_handle_feed.py.
    handle = AlpacaHandle(settings=settings(), calls_per_minute=60)

    assert type(handle.reader) is AlpacaRead


# ---------------------------------------------------------------------------------------------
# (2) Given: called exactly once, with the handle's own scheduler and both scopes, and no feed
# ---------------------------------------------------------------------------------------------


def test_a_given_factory_is_called_once_with_keywords_scheduler_scope_and_trading_scope_only(monkeypatch):
    refuse_alpaca_read(monkeypatch)
    factory = RecordingFactory()

    handle = AlpacaHandle(settings=settings(), calls_per_minute=60, reader_factory=factory)

    assert len(factory.calls) == 1, f'the factory was called {len(factory.calls)} times at build'
    args, kwargs = factory.calls[0]
    assert args == (), 'the factory was given a positional argument'
    assert kwargs == {'scheduler': handle.scheduler, 'scope': ALPACA_DATA_SCOPE, 'trading_scope': ALPACA_TRADING_SCOPE}
    assert kwargs['scheduler'] is handle.scheduler, "the factory was handed a scheduler other than the handle's"


# ---------------------------------------------------------------------------------------------
# (3) The factory's reader is handle.reader and the one every runner calls; no AlpacaRead is built
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_factorys_reader_is_the_handles_reader_and_serves_a_submit_of_every_kind(monkeypatch):
    built = refuse_alpaca_read(monkeypatch)
    factory = RecordingFactory()
    handle = AlpacaHandle(settings=settings(), calls_per_minute=60, reader_factory=factory)
    [reader] = factory.built
    store = AnsweringStore()

    assert handle.reader is reader

    handle.start(store, INSTANCE)
    try:
        acks = {}
        for request_id, body, _call, _scope in EACH_KIND:
            acks[request_id] = await handle.submit(SubmitRequest(request_id=request_id, owner='o', **body))
        reports = {request_id: await store.report_for(request_id) for request_id, *_ in EACH_KIND}
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    for request_id, _body, call, scope in EACH_KIND:
        assert acks[request_id].accepted is not None, f'{request_id} was not accepted'
        assert acks[request_id].accepted.quota_key == scope
        assert reports[request_id].trailer.outcome is DeliveryOutcome.SUCCEEDED, (
            f"{request_id}'s runner did not read through the factory's reader: {reports[request_id].trailer!r}"
        )
        assert call in reader.calls, f"{request_id}'s vendor call never reached the factory's reader: {reader.calls}"
    # The bars request named no feed: it reached the factory's reader as SIP, the production default.
    assert reports['bars-1'].header.feed is Feed.SIP
    assert acks['bars-1'].accepted.feed is Feed.SIP
    # Every request was served after build, and the factory was still called once: never per request or per runner.
    assert len(factory.calls) == 1
    built.assert_not_called()


# ---------------------------------------------------------------------------------------------
# (4) No feed is resolved at build, with a factory or without; the first resolution is the first bars Submit
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('with_factory', [False, True], ids=['default-reader', 'factory'])
async def test_no_feed_is_resolved_at_build_and_each_bars_submit_resolves_once(monkeypatch, with_factory):
    resolved = count_resolutions(monkeypatch)
    factory = RecordingFactory() if with_factory else None

    handle = AlpacaHandle(settings=settings(), calls_per_minute=60, reader_factory=factory)
    assert resolved == [], f'a feed was resolved at build: {resolved}'

    # Not started: the Submit intake alone runs, so the only resolution is the intake's.
    first = await handle.submit(SubmitRequest(request_id='b-1', owner='o', bars=BARS))
    second = await handle.submit(
        SubmitRequest(request_id='b-2', owner='o', bars=BARS.model_copy(update={'feed': Feed.IEX}))
    )

    assert resolved == [Feed.SIP, Feed.IEX], 'each bars Submit did not resolve its own feed exactly once'
    assert (first.accepted.feed, second.accepted.feed) == (Feed.SIP, Feed.IEX)


# ---------------------------------------------------------------------------------------------
# (5) Keyword-only
# ---------------------------------------------------------------------------------------------


def test_reader_factory_cannot_be_passed_positionally(monkeypatch):
    refuse_alpaca_read(monkeypatch)
    factory = RecordingFactory()

    with pytest.raises(TypeError):
        AlpacaHandle(settings(), 60, factory)
    assert factory.calls == [], 'a positional factory was called'
