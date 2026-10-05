"""The Alpaca adapter's client, feed and single-flight properties, and where each one now lives.

HANDLES-1 (tj-irhy0a.6) replaced broker_api.get_market_stock_data with AlpacaRead.get_bars (the
broker speaks market data) plus ingest_control.store_retrieve_stock (the platform conversion to
the store's batch schema, decision tj-j4wknb addendum 2 B). The tests below that drove the old
function were RE-POINTED to where each property now lives (HANDLES-2, tj-irhy0a.7), and on
tj-3mk3u5.32 they were re-pointed a second time, off the half that is being deleted:

  the injected client, the vendor feed parameter, the single-flight key  -> AlpacaRead.get_bars
  no credentials and no client failing before any call                   -> AlpacaRead.get_bars,
                                                                            production AlpacaRead()

EVERY TEST HERE NOW DRIVES THE ADAPTER DIRECTLY. Four of them went through
ingest_control.store_retrieve_stock, because that was the only caller that built a BarsQuery from a
request; tj-3mk3u5.11 deletes it with the rest of the Kafka edge. The vendor-side property each one
asserted survives unchanged on get_bars, which is where it always lived -- what went with the edge
is the BATCH, and the batch's successor is the gRPC ack. Each retirement below names where its
batch half is now pinned, so the diff can be read without the bead.
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
from common.errors.vocabulary import Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import BarsFailure, BarsQuery, Instrument
from data.ingest.app.brokers.rate_budget import RequestPriority
from schemas.data_ingest.get_dataset_request import StockDatasetRequest


CREDENTIALS = {'ALPACA_API_KEY': 'not-a-real-key', 'ALPACA_API_SECRET': 'not-a-real-secret'}
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_client_leaks_between_tests():
    """Drop any cached client, so one test's stub is never another's vendor."""
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    """A pool for the blocking SDK call, handed to AlpacaRead through its executor_provider seam."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield pool


def alpaca_reader(executor: ThreadPoolExecutor, client: Mock | None = None) -> AlpacaRead:
    """A real AlpacaRead over the given pool.

    Replaced install_alpaca() on tj-3mk3u5.32: the reader used to be installed in the ALPACA_API
    slot because the test then called ingest_control, which dispatched through that slot. These
    tests call the reader directly, so the slot -- and the ingest_control machinery behind it,
    which tj-3mk3u5.11 deletes -- is no longer in the path.

    Args:
        executor (ThreadPoolExecutor): Pool the vendor call runs on.
        client (Mock | None): Client to inject, or None for production's own (broker_api.get_client()).

    Returns:
        AlpacaRead: The reader.
    """
    return AlpacaRead(client=client, executor_provider=lambda: executor)


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


# RETIRED ON tj-3mk3u5.32: test_market_data_is_fetched_through_an_injected_client.
#
# It drove store_retrieve_stock over a real AlpacaRead and asserted three things. That the injected
# client really serves the bars is the test immediately below, which asserts it against the adapter
# without the edge in the way. The other two were the BATCH's: that the vendor's bars arrive in it
# in order, and that it carries the request's dataset_id for the store to correlate on. Both belong
# to a schema tj-3mk3u5.11 deletes; their successors are the gRPC bar pages and the ack, pinned in
# test_fetch_dataset_handler.py and common/tests/rpc/test_rpc_fetch_mapping.py.


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
async def test_the_feed_the_deployment_resolves_reaches_the_vendor_call(
    sip_enabled: str, expected: Feed, executor: ThreadPoolExecutor
):
    """The scope amendment on tj-vhboky.13: ALPACA_SIP_ENABLED decides the tape Alpaca is asked for.

    RE-POINTED TWICE. tj-irhy0a.7 moved it onto store_retrieve_stock over a real AlpacaRead, because
    that reached the vendor call and the batch stamp in one go. tj-3mk3u5.32 moved it onto get_bars,
    because tj-3mk3u5.11 deletes the edge -- and the vendor call was always the adapter's.

    THE HALF THAT WENT, and where it is now: the batch stamp. ingest_control stamped
    BarsResponse.feed onto the batch, and without it bars from whichever tape served them were
    stored carrying no tape at all, with ``feed`` inside the bar's identity. The gRPC ack carries
    the resolved feed instead -- test_fetch_dataset_handler.py's
    test_the_ack_comes_first_carrying_the_feed_the_adapter_resolved_not_the_one_the_request_named --
    and tj-3mk3u5.31 writes it to the entry. Nothing here was its last pin.

    THE HALF THAT STAYS is the one this file is for, and it fails on its own. Before tj-vhboky.9
    ``resolve_feed()`` fed the local single-flight key and nothing else: the vendor call carried no
    ``feed`` parameter, so a SIP-entitled deployment quietly received IEX bars. Both branches are
    driven because only the pair shows the environment is read at all, rather than a constant
    returned.

    The vendor value is LOWERCASE -- alpaca-py's ``DataFeed`` wire vocabulary -- and is compared
    against the uppercase ``Feed`` member lowered, rather than normalised away: collapsing the two
    is how a vendor string ends up in a column typed by the shared enum.

    Args:
        sip_enabled: The value of ALPACA_SIP_ENABLED for this case.
        expected: The Feed the adapter must resolve from it.
        executor: Pool the vendor call runs on.
    """
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0)])
    reader = alpaca_reader(executor, client)

    with patch.dict(os.environ, {'ALPACA_SIP_ENABLED': sip_enabled}):
        response = await reader.get_bars(build_query(request))
        assert [bar async for bar in response.bars], 'the vendor served nothing, so no call was made to inspect'

    assert response.feed is expected, 'the adapter resolved a tape the deployment did not configure'
    sent = client.get_stock_bars.call_args.args[0]
    assert sent.feed == DataFeed(expected.value.lower())


# RETIRED ON tj-3mk3u5.32, and this one is not a move -- the behaviour it pinned is GONE, replaced
# by its opposite: test_a_tape_named_by_the_request_does_not_override_the_deployment.
#
# It asserted that GetDatasetRequest.feed was INERT. That was never a property anyone wanted; it was
# a consequence of ingest_control building its BarsQuery with feed=None, so a request naming SIP
# against an IEX deployment was served IEX. tj-rh4b7f had deferred caller-selected feed, and the
# test existed to pin the deferral where it could actually be observed. Its own docstring named
# tj-3mk3u5.32 and tj-3mk3u5.11 as the point it retires.
#
# On the gRPC path the named feed DOES reach the adapter, which serves it or refuses it with
# FEED_NOT_AVAILABLE before any vendor call (TE-4, tj-3mk3u5.37.5). Re-pointing the assertion would
# have meant inverting it, and the inverted form already exists: test_fetch_dataset_handler.py's
# test_the_ack_comes_first_carrying_the_feed_the_adapter_resolved_not_the_one_the_request_named,
# with the refusal in test_typed_outcomes.py. So this is deleted rather than moved.


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
    """The PRODUCTION AlpacaRead, no client injected, no credentials: it refuses before the vendor.

    The reader is production's own, so the credential is resolved by broker_api.get_client() exactly
    as in a deployment. 'Before any call' is the half that needs observing and it is observed rather
    than inferred: the single flight is the only way to the vendor, and it is never entered.

    RE-POINTED ON tj-3mk3u5.32 from store_retrieve_stock to get_bars. What changed with it is the
    SHAPE of the refusal, not the fact of it. Since TE-4 the adapter RETURNS a BarsFailure carrying
    a VENDOR_AUTH MissingCredentialsError; it was the Kafka edge that re-raised it by name, so that
    an operator got the variable's name instead of an empty batch -- builder-ingest's flagged
    deviation from 'a bare {} for ANY BarsFailure', ruled at the TE-4 gate. The edge goes on
    tj-3mk3u5.11 and the re-raise goes with it: on the gRPC path the typed error is the reply, and
    nothing has to be re-raised to survive the hop. So the returned form is what is asserted here.
    """
    request = build_request()
    reader = alpaca_reader(executor, client=None)
    recorder = KeyRecordingSingleFlight(getattr(broker_api, '__SINGLE_FLIGHT'))

    with patch.object(broker_api, '__SINGLE_FLIGHT', recorder), patch.dict(os.environ, {}, clear=True):
        outcome = await reader.get_bars(build_query(request))

    assert isinstance(outcome, BarsFailure), f'a missing credential produced {type(outcome).__name__}, not a refusal'
    assert isinstance(outcome.error, MissingCredentialsError)
    assert outcome.error.reason is Reason.VENDOR_AUTH
    assert 'ALPACA_API_KEY' in outcome.error.detail, 'the refusal does not name the variable an operator must set'
    assert recorder.keys == [], 'a vendor call was attempted before the missing credential surfaced'
