import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from schemas.data_ingest.get_dataset_request import StockDatasetRequest


CREDENTIALS = {'ALPACA_API_KEY': 'not-a-real-key', 'ALPACA_API_SECRET': 'not-a-real-secret'}
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_client_leaks_between_tests():
    """Drop any cached client, so one test's stub is never another test's vendor."""
    broker_api.set_client(None)
    yield
    broker_api.set_client(None)


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
        'source': DataSource.ALPACA_API,
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
    return SimpleNamespace(open=1.0, high=2.0, low=0.5, close=close, volume=10, trade_count=3, timestamp=START)


class StubBarSet:
    """The slice of alpaca-py's BarSet that convert_bars_to_batch_schema actually touches."""

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
async def test_market_data_is_fetched_through_an_injected_client():
    # The path that was unreachable before this fix: a whole request served end to end,
    # single flight and rate budget included, against a client that is not the vendor.
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0), build_bar(11.0)])

    with ThreadPoolExecutor(max_workers=1) as executor:
        batch = await broker_api.get_market_stock_data(executor, request, client=client)

    client.get_stock_bars.assert_called_once()
    assert [entry.data.close for entry in batch.dataset[DataType.MARKET_ACTIVITY]] == [10.0, 11.0]
    assert batch.dataset_id == request.dataset_id


@pytest.mark.asyncio
async def test_an_injected_client_is_used_without_any_credentials():
    # The injected client is the credential. Nothing reads the environment on this path.
    request = build_request()
    client = Mock()
    client.get_stock_bars.return_value = StubBarSet(request.asset_symbol, [build_bar(10.0)])

    with patch.dict(os.environ, {}, clear=True), ThreadPoolExecutor(max_workers=1) as executor:
        batch = await broker_api.get_market_stock_data(executor, request, client=client)

    assert len(batch.dataset[DataType.MARKET_ACTIVITY]) == 1


@pytest.mark.asyncio
async def test_a_request_without_credentials_or_a_client_fails_before_any_call():
    request = build_request()

    with (
        patch.dict(os.environ, {}, clear=True),
        ThreadPoolExecutor(max_workers=1) as executor,
        pytest.raises(MissingCredentialsError),
    ):
        await broker_api.get_market_stock_data(executor, request)
