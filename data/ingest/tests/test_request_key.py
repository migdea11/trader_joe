from datetime import UTC, datetime

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from data.ingest.app.brokers.request_key import VendorRequestKey


START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = datetime(2026, 1, 3, 14, 30, tzinfo=UTC)


def build_key(**overrides) -> VendorRequestKey:
    fields = {
        'broker': DataSource.ALPACA_API,
        'feed': 'iex',
        'asset_type': AssetType.STOCK,
        'asset_symbol': 'VFV',
        'data_type': DataType.MARKET_ACTIVITY,
        'granularity': Granularity.ONE_DAY,
        'range_start': START,
        'range_end': END,
        'adjustment': 'raw',
    }
    return VendorRequestKey(**{**fields, **overrides})


def test_same_content_is_the_same_key():
    # Equal and equally hashable, or identical requests would never collapse.
    assert build_key() == build_key()
    assert len({build_key(), build_key()}) == 1


def test_feed_separates_two_tapes():
    # IEX is a partial tape and SIP is the consolidated one (tj-wss8a2), so collapsing
    # these two would answer one request with the other's data.
    assert build_key(feed='iex') != build_key(feed='sip')


def test_adjustment_separates_two_answers():
    assert build_key(adjustment='raw') != build_key(adjustment='split')


def test_range_bounds_separate_two_answers():
    assert build_key(range_start=END) != build_key(range_start=START)
    assert build_key(range_end=None) != build_key(range_end=END)
