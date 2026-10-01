"""The Alpaca adapter's client, feed and single-flight properties, and where each one now lives.

HANDLES-1 (tj-irhy0a.6) replaced broker_api.get_market_stock_data with AlpacaRead.get_bars (the
broker speaks market data) plus ingest_control.store_retrieve_stock (the platform conversion to
the store's batch schema, decision tj-j4wknb addendum 2 B). The tests below that drove the old
function were RE-POINTED to where each property now lives, with every assertion kept (HANDLES-2,
tj-irhy0a.7):

  the injected client, the vendor feed parameter, the single-flight key  -> AlpacaRead.get_bars
  the feed stamped on the batch, a request-named tape not overriding      -> store_retrieve_stock
  no credentials and no client failing before any call                    -> store_retrieve_stock,
                                                                             production AlpacaRead()

Where one test asserts both a vendor-side and a batch-side property, it drives store_retrieve_stock
with a real AlpacaRead underneath, so the two halves are still asserted by one test.
"""

import importlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from alpaca.data.enums import DataFeed

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.ingest.app import ingest_control
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import BarsQuery, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from schemas.data_ingest.get_dataset_request import StockDatasetRequest


CREDENTIALS = {'ALPACA_API_KEY': 'not-a-real-key', 'ALPACA_API_SECRET': 'not-a-real-secret'}
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_client_leaks_between_tests():
    """Drop any cached client and installed reader, so one test's stub is never another's vendor."""
    broker_api.set_client(None)
    ingest_control.clear_readers()
    yield
    broker_api.set_client(None)
    ingest_control.clear_readers()


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    """A pool for the blocking SDK call, handed to AlpacaRead through its executor_provider seam."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


def install_alpaca(executor: ThreadPoolExecutor, client: Mock | None = None) -> None:
    """Install a real AlpacaRead in the ALPACA_API slot, as the app's lifespan would.

    Args:
        executor (ThreadPoolExecutor): Pool the vendor call runs on.
        client (Mock | None): Client to inject, or None for production's own (broker_api.get_client()).
    """
    ingest_control.install_readers(
        {DataSource.ALPACA_API: AlpacaRead(client=client, executor_provider=lambda: executor)}
    )


def build_query(request: StockDatasetRequest) -> BarsQuery:
    """The BarsQuery store_retrieve_stock builds for a request, written out for direct get_bars calls."""
    return BarsQuery(
        instrument=Instrument(symbol=request.asset_symbol, asset_type=AssetType.STOCK),
        granularity=request.granularity,
        start=request.start,
        end=request.end,
        priority=RequestPriority.INTERACTIVE,
    )


@pytest.fixture
def reimport_broker_api():
    """Re-execute the module body, then restore the real module for everyone else.

    Import-time behaviour is the whole subject here (tj-84jfb9), and a reload is the only
    way to observe it from inside a session that has already imported the module.
    """
    yield lambda: importlib.reload(broker_api)
    importlib.reload(broker_api)


def build_request(**overrides) -> StockDatasetRequest:
    fields = {
        'dataset_id': uuid4(),
        # owner is carried through from the store request and is required with no default
        # (tj-vhboky.1 section 2): it is identity on the dataset entry, so a fetch may not invent
        # a principal.
        #
        # feed is passed here even though it is OPTIONAL on the request (`Feed | None = None`
        # since 77f3a6c) and even though the adapter does not read it. It is set deliberately so
        # that the tests below which override it to SIP are overriding a populated field rather
        # than filling an empty one -- "the adapter ignores the request's tape" is only worth
        # asserting against a request that actually names one.
        'owner': 'rebalancer',
        'source': DataSource.ALPACA_API,
        'feed': Feed.IEX,
        'granularity': Granularity.ONE_DAY,
        'start': START,
        'end': None,
        'expiry': START,
        'expiry_type': ExpiryType.ROLLING,
        'update_type': UpdateType.DAILY,
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY],
    }
    return StockDatasetRequest(**{**fields, **overrides})


def build_bar(close: float) -> SimpleNamespace:
    # vwap is carried because a real alpaca-py Bar always has it and AlpacaRead reads it: without
    # it the conversion raises AttributeError, which store_retrieve_stock swallows into {}.
    return SimpleNamespace(
        open=1.0, high=2.0, low=0.5, close=close, volume=10, trade_count=3, vwap=1.5, timestamp=START
    )


class StubBarSet:
    """The slice of alpaca-py's BarSet that AlpacaRead's conversion actually touches."""

    def __init__(self, symbol: str, bars: list[SimpleNamespace]):
        self.data = {symbol: bars}

    def __getitem__(self, symbol: str) -> list[SimpleNamespace]:
        return self.data[symbol]


def test_module_body_runs_with_an_empty_environment(reimport_broker_api):
    # The defect this file exists for: the module built its client at import, so with no
    # credentials set alpaca-py raised and nothing in the package could be imported at all.
    with patch.dict(os.environ, {}, clear=True):
        module = reimport_broker_api()

    assert module.ALPACA_ADJUSTMENT == 'raw'


def test_no_client_is_built_at_import(reimport_broker_api):
    # Credentials ARE set here, so a client could be built -- the point is that importing
    # the module does not build one even when it could.
    with patch('alpaca.data.historical.StockHistoricalDataClient') as client_cls, patch.dict(os.environ, CREDENTIALS):
        module = reimport_broker_api()

        # The reload really did pick the stub up, so a count of zero means 'not built'
        # rather than 'patched the wrong name'.
        assert module.StockHistoricalDataClient is client_cls
        assert client_cls.call_count == 0


def test_client_is_built_on_first_use_and_then_reused():
    with patch.object(broker_api, 'StockHistoricalDataClient') as client_cls, patch.dict(os.environ, CREDENTIALS):
        first = broker_api.get_client()
        second = broker_api.get_client()

    assert first is second
    client_cls.assert_called_once_with(CREDENTIALS['ALPACA_API_KEY'], CREDENTIALS['ALPACA_API_SECRET'])


def test_clearing_the_client_rebuilds_it_on_the_next_call():
    # A rotated secret takes effect without a process restart, which is the other half of
    # why the client is not a module-level constant.
    with patch.object(broker_api, 'StockHistoricalDataClient') as client_cls, patch.dict(os.environ, CREDENTIALS):
        broker_api.get_client()
        broker_api.set_client(None)
        broker_api.get_client()

    assert client_cls.call_count == 2


def test_missing_credential_names_the_variable_that_is_unset():
    # alpaca-py's own complaint is 'You must supply a method of authentication', which
    # names nothing. Ours names the variable the operator has to go and set.
    with (
        patch.dict(os.environ, {'ALPACA_API_KEY': 'not-a-real-key'}, clear=True),
        pytest.raises(MissingCredentialsError) as err,
    ):
        broker_api.get_client()

    assert 'ALPACA_API_SECRET' in str(err.value)
    assert 'ALPACA_API_KEY' not in str(err.value)


def test_sip_feed_is_read_at_call_time_not_at_import():
    with patch.dict(os.environ, {'ALPACA_SIP_ENABLED': 'true'}):
        assert broker_api.sip_enabled() is True
    with patch.dict(os.environ, {'ALPACA_SIP_ENABLED': 'false'}):
        assert broker_api.sip_enabled() is False


@pytest.mark.asyncio
async def test_market_data_is_fetched_through_an_injected_client(executor: ThreadPoolExecutor):
    # The path that was unreachable before tj-84jfb9: a whole request served end to end, single
    # flight and rate budget included, against a client that is not the vendor. Re-pointed from
    # get_market_stock_data to store_retrieve_stock over a real AlpacaRead, so it stays END TO END:
    # dataset_id is ingest_control's to stamp now, the bars are AlpacaRead's to fetch.
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0), build_bar(11.0)])
    install_alpaca(executor, client)

    batch = await ingest_control.store_retrieve_stock(request)

    client.get_stock_bars.assert_called_once()
    assert [entry.data.close for entry in batch.dataset[DataType.MARKET_ACTIVITY]] == [10.0, 11.0]
    assert batch.dataset_id == request.dataset_id


@pytest.mark.asyncio
async def test_an_injected_client_is_used_without_any_credentials(executor: ThreadPoolExecutor):
    # The injected client is the credential. Nothing reads the environment on this path.
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0)])
    reader = AlpacaRead(client=client, executor_provider=lambda: executor)

    with patch.dict(os.environ, {}, clear=True):
        response = await reader.get_bars(build_query(request))
        bars = [bar async for bar in response.bars]

    assert len(bars) == 1
    client.get_stock_bars.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(('sip_enabled', 'expected'), [('false', Feed.IEX), ('true', Feed.SIP)], ids=['iex', 'sip'])
async def test_the_resolved_feed_reaches_the_vendor_call_and_is_stamped_on_the_batch(
    sip_enabled: str, expected: Feed, executor: ThreadPoolExecutor
):
    """The scope amendment on tj-vhboky.13, both halves, against a real adapter call.

    RE-POINTED (tj-irhy0a.7): the vendor call is now made by AlpacaRead.get_bars and the batch is
    now stamped by ingest_control.store_retrieve_stock from BarsResponse.feed. Driving
    store_retrieve_stock over a real AlpacaRead reaches both sites in one call, so the two halves
    are still asserted by one test.

    THE TWO HALVES FAIL SEPARATELY, which is why one test asserts both. Before tj-vhboky.9,
    ``resolve_feed()`` fed the local single-flight key and nothing else: the vendor call carried no
    ``feed`` parameter at all, so Alpaca served whatever it defaults to, and the batch went out
    unstamped. Either half alone is a silent wrong-tape bug --

      VENDOR CALL  without it, a SIP-entitled deployment quietly receives IEX bars.
      BATCH STAMP  without it, bars from whichever tape did serve them are stored carrying no tape
                   at all, and ``feed`` is inside the bar's identity.

    The vendor value is LOWERCASE and the stamped value is the uppercase ``Feed`` member: the
    first is alpaca-py's ``DataFeed`` wire vocabulary and the second is the stored contract. That
    difference is asserted rather than normalised away -- collapsing them is how a vendor string
    ends up in a column typed by the shared enum.

    Args:
        sip_enabled: The value of ALPACA_SIP_ENABLED for this case.
        expected: The Feed the adapter must resolve from it.
        executor: Pool the vendor call runs on.
    """
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0)])
    install_alpaca(executor, client)

    with patch.dict(os.environ, {'ALPACA_SIP_ENABLED': sip_enabled}):
        batch = await ingest_control.store_retrieve_stock(request)

    assert batch.feed is expected
    sent = client.get_stock_bars.call_args.args[0]
    assert sent.feed == DataFeed(expected.value.lower())


@pytest.mark.asyncio
async def test_a_tape_named_by_the_request_does_not_override_the_deployment(executor: ThreadPoolExecutor):
    """``GetDatasetRequest.feed`` is DECLARED BUT INERT, pinned where it can actually be observed.

    tj-rh4b7f deferred caller-selected feed: the store has no feed to forward, so the field stays
    on the request as the landing site for the transport work rather than as a working selection.
    Its comment says nothing in data/ingest reads it. A comment is not a test, and the failure it
    describes is silent -- a request naming SIP against an IEX deployment would simply be served
    IEX with nobody told.

    So this drives the request path -- store_retrieve_stock over a real AlpacaRead since
    tj-irhy0a.6 -- with a request that explicitly names SIP, in a deployment configured for IEX,
    and asserts IEX wins at both sites. WHEN THE DEFERRED TRANSPORT WORK LANDS, THIS IS THE TEST
    THAT MUST FAIL, and inverting it is the deliberate act that records the field becoming live.
    It is not a test to repair around.
    """
    request = build_request(feed=Feed.SIP)
    assert request.feed is Feed.SIP, 'the request really does name the other tape'
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0)])
    install_alpaca(executor, client)

    with patch.dict(os.environ, {'ALPACA_SIP_ENABLED': 'false'}):
        batch = await ingest_control.store_retrieve_stock(request)

    assert batch.feed is Feed.IEX
    assert client.get_stock_bars.call_args.args[0].feed == DataFeed('iex')


class KeyRecordingSingleFlight:
    """The real collapse behaviour, with the keys it was asked to collapse on recorded.

    Delegates rather than replaces: the point is to observe the key the adapter BUILDS, so
    substituting a stub that never collapses would remove the behaviour under test.
    """

    def __init__(self, inner):
        self.inner = inner
        self.keys = []

    async def run(self, key, factory):
        self.keys.append(key)
        return await self.inner.run(key, factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(('sip_enabled', 'expected'), [('false', 'iex'), ('true', 'sip')], ids=['iex', 'sip'])
async def test_the_resolved_feed_is_written_into_the_single_flight_key(
    sip_enabled: str, expected: str, executor: ThreadPoolExecutor
):
    """broker_api.fetch_data_type's key, the third leg of resolve_feed() and the one that was untested.

    Re-pointed to AlpacaRead.get_bars, which resolves the feed once and hands it to fetch_data_type.

    The other two legs -- the vendor call and the batch stamp -- are asserted above. This one is
    the single-flight key, and it fails DIFFERENTLY from both: the tape reaching the vendor and the
    tape stamped on the bar can be perfectly correct while two callers on DIFFERENT tapes still
    collapse onto one vendor response, because collapse is decided by the key alone. The second
    caller would then be served the first caller's tape and stamped with its own -- a bar labelled
    SIP carrying IEX prices, which is worse than an unlabelled one.

    Lowercase is asserted deliberately. The key is built from ``feed.value.lower()`` to match the
    vendor's wire vocabulary, while the batch carries the uppercase ``Feed`` member; that split is
    a documented choice in fetch_data_type and normalising it here would stop testing it.
    """
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0)])
    recorder = KeyRecordingSingleFlight(getattr(broker_api, '__SINGLE_FLIGHT'))
    reader = AlpacaRead(client=client, executor_provider=lambda: executor)

    with (
        patch.object(broker_api, '__SINGLE_FLIGHT', recorder),
        patch.dict(os.environ, {'ALPACA_SIP_ENABLED': sip_enabled}),
    ):
        await reader.get_bars(build_query(request))

    assert len(recorder.keys) == 1, 'the adapter did not go through the single flight at all'
    assert recorder.keys[0].feed == expected


@pytest.mark.asyncio
async def test_two_tapes_do_not_share_one_single_flight_key(executor: ThreadPoolExecutor):
    """Two deployments' tapes produce UNEQUAL keys, stated as the property that matters.

    The parametrized test above pins each key's feed field; this pins the consequence, and it is
    the one the bead actually cares about: "two callers asking for different feeds must NOT
    collapse onto one vendor call". Asserted as key inequality rather than by racing two coroutines
    because the collapse window is exactly the leader's in-flight period -- a race would be timing
    dependent and could pass while the keys were identical.

    Everything except the tape is held constant: same symbol, same granularity, same range, same
    adjustment. So if these keys are equal, feed is missing from the key.
    """
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0)])
    recorder = KeyRecordingSingleFlight(getattr(broker_api, '__SINGLE_FLIGHT'))
    reader = AlpacaRead(client=client, executor_provider=lambda: executor)

    with patch.object(broker_api, '__SINGLE_FLIGHT', recorder):
        for sip_enabled in ('false', 'true'):
            with patch.dict(os.environ, {'ALPACA_SIP_ENABLED': sip_enabled}):
                await reader.get_bars(build_query(request))

    iex_key, sip_key = recorder.keys
    assert iex_key != sip_key, 'an IEX request and a SIP request collapse onto one vendor call'
    # And the tape is the ONLY thing that differs, so the inequality is not coming from elsewhere.
    assert iex_key._replace(feed=sip_key.feed) == sip_key


@pytest.mark.asyncio
async def test_a_request_without_credentials_or_a_client_fails_before_any_call(executor: ThreadPoolExecutor):
    # Re-pointed to the request path with the PRODUCTION AlpacaRead (no client injected), so the
    # credential is resolved by broker_api.get_client() exactly as in a deployment. The error must
    # reach the caller by name -- store_retrieve_stock swallows every other exception into {} --
    # and 'before any call' is observed: the single flight, the only way to the vendor, is never
    # entered.
    request = build_request()
    install_alpaca(executor, client=None)
    recorder = KeyRecordingSingleFlight(getattr(broker_api, '__SINGLE_FLIGHT'))

    with (
        patch.object(broker_api, '__SINGLE_FLIGHT', recorder),
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(MissingCredentialsError),
    ):
        await ingest_control.store_retrieve_stock(request)

    assert recorder.keys == [], 'a vendor call was attempted before the missing credential surfaced'
