"""FakeRead's asset reads (tj-7zjov1.22: the validator brings the base-interface read fake to the extended interface).

FakeRead (tests/fakes/market_data.py) stands for BrokerRead in tests and the fake-mode stack. Its asset reads are
deterministic and judge nothing: the ACTIVE list is FAKE_LISTED_SYMBOLS, the INACTIVE list FAKE_DELISTED_SYMBOLS,
every non-empty symbol has a record, and each record is a pure function of its symbol. Expected values are written
out where the test is about them.
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from common.enums.data_select import AssetType
from common.errors.vocabulary import InvalidRequestError, Reason
from data.ingest.app.brokers.interface import (
    AssetListFailure,
    AssetListQuery,
    AssetListResponse,
    AssetLookupFailure,
    AssetLookupQuery,
    AssetResponse,
    BrokerUnsupportedError,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from schemas.data_ingest.submit import AssetListStatus
from tests.fakes.market_data import FAKE_DELISTED_SYMBOLS, FAKE_LISTED_SYMBOLS, FakeRead, fake_asset


pytestmark = pytest.mark.data_ingest

# A clock in another offset, so as_of is shown converted to UTC.
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone(timedelta(hours=-4)))


def list_query(status: AssetListStatus, asset_type: AssetType = AssetType.STOCK) -> AssetListQuery:
    return AssetListQuery(asset_type, status, priority=RequestPriority.BACKFILL)


def test_the_fake_lists_are_the_published_constants():
    assert FAKE_LISTED_SYMBOLS == ('VFV', 'XEQT', 'AAPL')
    assert FAKE_DELISTED_SYMBOLS == ('GONE',)


@pytest.mark.asyncio
async def test_the_active_list_is_every_listed_symbol_in_order_active_and_tradable():
    outcome = await FakeRead(clock=lambda: NOW).get_assets(list_query(AssetListStatus.ACTIVE))

    assert isinstance(outcome, AssetListResponse)
    assert [asset.symbol for asset in outcome.assets] == ['VFV', 'XEQT', 'AAPL']
    assert all(asset.active is True and asset.tradable is True for asset in outcome.assets)
    assert outcome.as_of == datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
    assert outcome.as_of.tzinfo is UTC


@pytest.mark.asyncio
async def test_the_inactive_list_is_every_delisted_symbol_inactive_and_untradable():
    outcome = await FakeRead(clock=lambda: NOW).get_assets(list_query(AssetListStatus.INACTIVE))

    assert [(asset.symbol, asset.active, asset.tradable) for asset in outcome.assets] == [('GONE', False, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize('asset_type', [kind for kind in AssetType if kind is not AssetType.STOCK])
async def test_a_list_of_a_kind_other_than_stock_is_refused(asset_type):
    outcome = await FakeRead().get_assets(list_query(AssetListStatus.ACTIVE, asset_type))

    assert isinstance(outcome, AssetListFailure)
    assert type(outcome.error) is BrokerUnsupportedError
    assert outcome.error.reason is Reason.UNSUPPORTED_ASSET_TYPE


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['VFV', 'NEVER_LISTED', 'GONE'])
async def test_every_non_empty_symbol_has_a_record_equal_to_the_listed_one(symbol):
    """A fake-mode bars attempt looking its symbol up after the bars (P12h) always finds it."""
    outcome = await FakeRead(clock=lambda: NOW).get_asset(AssetLookupQuery(symbol, priority=RequestPriority.BACKFILL))

    assert outcome == AssetResponse(asset=fake_asset(symbol), as_of=datetime(2026, 10, 26, 14, 0, tzinfo=UTC))


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['', '  '], ids=['empty', 'blank'])
async def test_a_lookup_with_no_symbol_is_refused(symbol):
    outcome = await FakeRead().get_asset(AssetLookupQuery(symbol, priority=RequestPriority.BACKFILL))

    assert isinstance(outcome, AssetLookupFailure)
    assert type(outcome.error) is InvalidRequestError
    assert outcome.error.reason is Reason.INVALID_REQUEST


def test_a_fake_asset_is_a_pure_function_of_its_symbol_with_an_opaque_stable_id():
    first, again, other = fake_asset('VFV'), fake_asset('VFV'), fake_asset('XEQT')

    assert first == again
    assert first.vendor_asset_id != other.vendor_asset_id
    # Written out: a change to the namespace or the derivation moves every fake id, which a seed would notice.
    assert first.vendor_asset_id == '3e820341-b4f9-5e1d-85de-5ea88b77bcd3'


def test_a_fake_assets_raw_payload_is_its_own_record_as_json():
    asset = fake_asset('GONE')

    assert json.loads(asset.raw_payload) == {
        'id': asset.vendor_asset_id,
        'class': 'us_equity',
        'exchange': 'FAKE',
        'symbol': 'GONE',
        'name': 'GONE fake listing',
        'status': 'inactive',
        'tradable': False,
    }
    assert (asset.exchange, asset.name) == ('FAKE', 'GONE fake listing')
