"""Alpaca's asset-list and asset-by-symbol reads (P12, tj-7zjov1.22), against builder-ingest's 35b436a.

Checked against the bead's design notes, which bind: the 17:38 UTC P27a s1 inputs (read the list RAW, never
through the alpaca-py Asset model, whose closed exchange enum has no TXSE; send asset_class=us_equity explicitly;
the field mapping; the trading host's own rate-limit headers; the trading host is configuration), as amended at
21:54 UTC (ONE vendor call per request, for the one status it names, never both and never absent; name filled as
given) and at 20:29 UTC (the by-symbol read sits on BrokerRead, paced on the trading-API scope, a vendor 404 its
own typed outcome, transient and rate failures typed with reset_at). Acceptance: the list paced on the
trading-API scope; the vendor asset id opaque data and never a call argument; nothing judged.

HOW IT IS DRIVEN. The REAL alpaca-py TradingClient over the recorded transport (alpaca_recorded.stub_trading_vendor)
on an invented trading host, with constructed bodies served byte for byte. Pacing is observed through
RecordingScheduler, which records the SCOPE of every acquire, so a read charged to the data scope is caught; the
deadline case uses a real Scheduler. No vendor key is read: the placeholder pair is the only credential here.
"""

import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pydantic
import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Asset as SdkAsset

from common.enums.data_select import AssetType
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.assets import (
    CHECK_DUPLICATE_ID,
    CHECK_ENVELOPE,
    CHECK_RECORD_FIELDS,
    CHECK_RECORD_OBJECT,
    AssetListMalformed,
    asset_from_record,
    parse_asset,
    parse_asset_list,
)
from data.ingest.app.brokers.alpaca.classify import RATE_LIMIT_RESET_HEADER
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import (
    Asset,
    AssetListFailure,
    AssetListQuery,
    AssetListResponse,
    AssetLookupFailure,
    AssetLookupQuery,
    AssetNotFound,
    AssetResponse,
    BrokerUnsupportedError,
    CallTally,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import scheduler as scheduler_module
from data.ingest.tests.alpaca_recorded import (
    ALPACA_TRADING_TEST_HOST,
    ALPACA_TRADING_TEST_URL,
    ASSETS_PATH,
    PLACEHOLDER_KEY_ID,
    PLACEHOLDER_SECRET,
    raw_answer,
    status_answer,
    stub_trading_vendor,
)
from data.ingest.tests.scheduler_doubles import ONE_PER_SECOND, Acquire, RecordingScheduler, real_scheduler
from schemas.data_ingest.submit import AssetListStatus


pytestmark = pytest.mark.data_ingest

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)

# Constructed records, shaped as the P27a s1 captures describe them (keys, a TXSE venue, an empty name, the borrow
# fields beside each other, no cusip). Invented ids and names: these are not recordings.
AAPL_ID = 'b0b6dd9d-8b9b-48a9-ba46-b9d54906e415'
OBK_ID = '7e0a1c1e-0d55-4b8b-9f43-2c1f7a6b0a01'
QUIET_ID = '5d2e4f6a-1b3c-4d5e-8f70-9a1b2c3d4e5f'
GONE_ID = '0c3c8d3a-7f0f-4b1c-8f4b-5b3a6f0e9e22'

# Each element is written as the bytes the vendor sent: odd spacing, an escaped and a literal non-ASCII
# character, and brackets and commas inside strings, so a re-serialisation or a naive split cannot match them.
# Every list record carries the fields the integrity guard requires (tj-7zjov1.218 G5): id, symbol, class, status,
# exchange, name and the four boolean flags.
AAPL_RAW = (
    b'{"id": "' + AAPL_ID.encode() + b'", "class": "us_equity", "exchange": "NASDAQ", "symbol": "AAPL",'
    b' "name": "Apple Inc. Common Stock [x], \\u00e9", "status": "active", "tradable": true,'
    b' "marginable": true, "shortable": true, "fractionable": true,'
    b' "easy_to_borrow": true, "borrow_status": "easy", "attributes": ["fractional_eh_enabled", "has_options"]}'
)
OBK_RAW = (
    '{"id":"' + OBK_ID + '","class":"us_equity","exchange":"TXSE","symbol":"OBK",'
    '"name":"Origin Bancorp, Inc. Société Générale","status":"active","tradable":false,'
    '"marginable":true,"shortable":true,"easy_to_borrow":false,"fractionable":true}'
).encode('utf-8')
# An active, untradable OTC record with an empty name, laid out over several lines.
QUIET_RAW = (
    b'{\n  "id": "' + QUIET_ID.encode() + b'",\n  "class": "us_equity",\n  "exchange": "OTC",\n'
    b'  "symbol": "QUIET",\n  "name": "",\n  "status": "active",\n  "tradable": false,\n'
    b'  "marginable": false,\n  "shortable": false,\n  "fractionable": false\n}'
)
GONE_RAW = (
    b'{\n  "id": "' + GONE_ID.encode() + b'",\n  "class": "us_equity",\n  "exchange": "OTC",\n'
    b'  "symbol": "GONE",\n  "name": "",\n  "status": "inactive",\n  "tradable": false,\n'
    b'  "marginable": false,\n  "shortable": false,\n  "fractionable": false\n}'
)
# The active list. The guard refuses a record whose status is not the list's, so the inactive GONE has its own.
LIST_BODY = b' [\n  ' + AAPL_RAW + b' ,\n' + OBK_RAW + b',' + QUIET_RAW + b'\n] '
INACTIVE_BODY = b'[' + GONE_RAW + b']'
LIST_STATUS = AssetListStatus.ACTIVE.value
US_EQUITY = 'us_equity'
REFUSED = 'asset list refused as malformed: '


def list_body(status: AssetListStatus) -> bytes:
    """A well-formed body of the list the status names."""
    return LIST_BODY if status is AssetListStatus.ACTIVE else INACTIVE_BODY


def parse(body: bytes, status: str = LIST_STATUS) -> list[Asset]:
    """parse_asset_list as the list read calls it: the status requested and the us_equity class."""
    return parse_asset_list(body, status, US_EQUITY)


def record(**overrides: object) -> dict:
    """One decoded vendor record, every neutral field present unless overridden; _ABSENT removes a key, None is null."""
    fields_ = {
        'id': AAPL_ID,
        'class': 'us_equity',
        'exchange': 'NASDAQ',
        'symbol': 'AAPL',
        'name': 'Apple Inc.',
        'status': 'active',
        'tradable': True,
        'marginable': True,
        'shortable': True,
        'fractionable': True,
    }
    fields_.update(overrides)
    return {key: value for key, value in fields_.items() if value is not _ABSENT}


_ABSENT = object()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No cached trading client, no remembered reset, and no credential or trading host in the environment."""
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()
    yield
    broker_api.set_trading_client(None)
    broker_api.forget_resets()


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


def reader(
    executor: ThreadPoolExecutor,
    trading_client: TradingClient | None = None,
    *,
    scheduler=None,
    trading_scope: str | None = ALPACA_TRADING_SCOPE,
    clock=lambda: NOW,
) -> AlpacaRead:
    """A real AlpacaRead on the production scope names, reading the given trading client."""
    return AlpacaRead(
        executor_provider=lambda: executor,
        clock=clock,
        scheduler=scheduler if scheduler is not None else RecordingScheduler(),
        scope=ALPACA_DATA_SCOPE,
        trading_client=trading_client,
        trading_scope=trading_scope,
    )


def list_query(status: AssetListStatus = AssetListStatus.ACTIVE, **overrides) -> AssetListQuery:
    fields_ = {
        'asset_type': AssetType.STOCK,
        'status': status,
        'priority': RequestPriority.BACKFILL,
        'deadline': DEADLINE,
    }
    return AssetListQuery(**{**fields_, **overrides})


def lookup(symbol: str = 'AAPL') -> AssetLookupQuery:
    return AssetLookupQuery(symbol, priority=RequestPriority.INTERACTIVE, deadline=DEADLINE)


def list_failure(outcome) -> ExogenousError | InvalidRequestError:
    assert isinstance(outcome, AssetListFailure), f'expected an AssetListFailure, got {outcome!r}'
    return outcome.error


def lookup_failure(outcome) -> ExogenousError | InvalidRequestError:
    assert isinstance(outcome, AssetLookupFailure), f'expected an AssetLookupFailure, got {outcome!r}'
    return outcome.error


# ---------------------------------------------------------------------------------------------
# The shape of the interface: the vendor id is data a read returns, never an argument
# ---------------------------------------------------------------------------------------------


def test_the_lookup_takes_a_symbol_and_never_a_vendor_asset_id():
    """Acceptance: the handle reports the vendor asset id as opaque data and never takes it as a call argument."""
    assert [field.name for field in fields(AssetLookupQuery)] == ['symbol', 'priority', 'deadline']
    assert [field.name for field in fields(AssetListQuery)] == ['asset_type', 'status', 'priority', 'deadline', 'tally']


def test_an_asset_carries_the_neutral_fields_and_the_raw_payload_and_nothing_else():
    """Design input (4): easy_to_borrow, borrow_status and every other key stay in raw_payload only; no cusip."""
    assert [field.name for field in fields(Asset)] == [
        'vendor_asset_id',
        'symbol',
        'raw_payload',
        'active',
        'tradable',
        'exchange',
        'name',
    ]


# ---------------------------------------------------------------------------------------------
# Read raw: each element's own bytes, and the SDK model never touched
# ---------------------------------------------------------------------------------------------


def test_each_listed_asset_keeps_its_own_bytes_exactly_as_served_in_the_order_served():
    assets = parse(LIST_BODY)

    assert [asset.raw_payload for asset in assets] == [AAPL_RAW, OBK_RAW, QUIET_RAW]
    assert [asset.symbol for asset in assets] == ['AAPL', 'OBK', 'QUIET']
    assert [asset.vendor_asset_id for asset in assets] == [AAPL_ID, OBK_ID, QUIET_ID]


def test_the_neutral_fields_are_copies_of_what_the_vendor_said():
    aapl, obk, quiet = parse(LIST_BODY)
    [gone] = parse(INACTIVE_BODY, AssetListStatus.INACTIVE.value)

    assert aapl == Asset(
        vendor_asset_id=AAPL_ID,
        symbol='AAPL',
        raw_payload=AAPL_RAW,
        active=True,
        tradable=True,
        exchange='NASDAQ',
        name='Apple Inc. Common Stock [x], é',
    )
    # A TXSE venue is carried as the vendor's string; an untradable active asset is still listed, unjudged.
    assert (obk.exchange, obk.active, obk.tradable, obk.name) == (
        'TXSE',
        True,
        False,
        'Origin Bancorp, Inc. Société Générale',
    )
    # Design input (4): an empty vendor name stays empty, and an untradable active asset is listed, unjudged.
    assert (quiet.exchange, quiet.active, quiet.tradable, quiet.name) == ('OTC', True, False, '')
    # An inactive asset, read off the inactive list, is listed like any other.
    assert (gone.exchange, gone.active, gone.tradable, gone.name) == ('OTC', False, False, '')
    assert gone.raw_payload == GONE_RAW


def test_the_sdk_asset_model_still_refuses_a_txse_row_which_is_why_the_list_is_read_raw():
    """Design input (1). If this goes green the hazard has moved, not gone: any new venue breaks a closed enum."""
    with pytest.raises(pydantic.ValidationError):
        SdkAsset(**json.loads(OBK_RAW))


def test_a_list_holding_a_txse_row_parses_whole():
    assert [asset.exchange for asset in parse(LIST_BODY)] == ['NASDAQ', 'TXSE', 'OTC']


@pytest.mark.parametrize('body', [b'[]', b' [ \n\t] ', b'\n[\r\n]\n'])
@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
def test_an_empty_array_is_an_empty_list(body, status):
    """tj-7zjov1.218 D4: an empty array is well formed, never refused (the store files it as a non-base snapshot)."""
    assert parse(body, status.value) == []


@pytest.mark.parametrize(
    ('status', 'active'),
    [
        pytest.param('active', True, id='active'),
        pytest.param('inactive', False, id='inactive'),
        pytest.param(_ABSENT, None, id='absent'),
        pytest.param(None, None, id='null'),
        pytest.param(1, None, id='not-a-string'),
    ],
)
def test_active_is_the_status_equal_to_active_and_none_when_the_vendor_gave_none(status, active):
    raw = json.dumps(record(status=status)).encode()

    assert asset_from_record(json.loads(raw), raw).active is active


@pytest.mark.parametrize(
    ('tradable', 'expected'),
    [
        pytest.param(True, True, id='true'),
        pytest.param(False, False, id='false'),
        pytest.param(_ABSENT, None, id='absent'),
        pytest.param(None, None, id='null'),
        pytest.param('true', None, id='a-string'),
    ],
)
def test_tradable_is_the_vendors_flag_and_none_never_false_when_it_gave_none(tradable, expected):
    """The P31 note: leave active or tradable unset, never false, when the vendor gives no value."""
    raw = json.dumps(record(tradable=tradable)).encode()

    assert asset_from_record(json.loads(raw), raw).tradable is expected


@pytest.mark.parametrize('key', ['exchange', 'name'])
@pytest.mark.parametrize('value', [_ABSENT, None, 7], ids=['absent', 'null', 'not-a-string'])
def test_a_venue_or_name_the_vendor_did_not_give_as_a_string_is_empty(key, value):
    raw = json.dumps(record(**{key: value})).encode()

    assert getattr(asset_from_record(json.loads(raw), raw), key) == ''


@pytest.mark.parametrize('exchange', ['NASDAQ', 'NYSE', 'ARCA', 'BATS', 'OTC', 'AMEX', 'TXSE', 'NYSEARCA', 'NEWVENUE'])
def test_every_venue_string_is_carried_as_given(exchange):
    raw = json.dumps(record(exchange=exchange)).encode()

    assert asset_from_record(json.loads(raw), raw).exchange == exchange


def test_the_by_symbol_answer_keeps_the_whole_body_as_its_raw_payload():
    asset = parse_asset(GONE_RAW)

    assert asset.raw_payload == GONE_RAW
    assert (asset.vendor_asset_id, asset.symbol, asset.active, asset.name) == (GONE_ID, 'GONE', False, '')


SENTINEL = 'body-text-that-must-not-reach-a-message'


@pytest.mark.parametrize(
    ('body', 'check', 'count'),
    [
        pytest.param(b'\xff\xfe[]', CHECK_ENVELOPE, 0, id='not-utf8'),
        pytest.param(
            b'{"id": "x", "symbol": "' + SENTINEL.encode() + b'"}', CHECK_ENVELOPE, 0, id='an-object-not-an-array'
        ),
        pytest.param(b'', CHECK_ENVELOPE, 0, id='empty'),
        pytest.param(b'[' + AAPL_RAW, CHECK_ENVELOPE, 1, id='unterminated'),
        pytest.param(b'[' + AAPL_RAW + b' ' + OBK_RAW + b']', CHECK_ENVELOPE, 1, id='missing-comma'),
        pytest.param(b'[' + AAPL_RAW + b',]', CHECK_ENVELOPE, 1, id='trailing-comma'),
        pytest.param(b'["' + SENTINEL.encode() + b'"]', CHECK_RECORD_OBJECT, 1, id='an-element-not-an-object'),
        pytest.param(b'[{"symbol": "' + SENTINEL.encode() + b'"}]', CHECK_RECORD_FIELDS, 1, id='no-id'),
        pytest.param(b'[{"id": "", "symbol": "AAPL"}]', CHECK_RECORD_FIELDS, 1, id='empty-id'),
        pytest.param(b'[{"id": 7, "symbol": "AAPL"}]', CHECK_RECORD_FIELDS, 1, id='id-not-a-string'),
        pytest.param(b'[{"id": "x"}]', CHECK_RECORD_FIELDS, 1, id='no-symbol'),
        pytest.param(b'[{"id": "x", "symbol": ""}]', CHECK_RECORD_FIELDS, 1, id='empty-symbol'),
        # tj-7zjov1.150: one array and only whitespace after its closing bracket, at both return points.
        pytest.param(b'[]' + SENTINEL.encode(), CHECK_ENVELOPE, 0, id='empty-array-then-junk'),
        pytest.param(b'[] []', CHECK_ENVELOPE, 0, id='empty-array-then-a-second-array'),
        pytest.param(
            b'[]\n["' + SENTINEL.encode() + b'"]', CHECK_ENVELOPE, 0, id='empty-array-then-a-second-array-with-a-body'
        ),
        pytest.param(b'[' + AAPL_RAW + b'][' + OBK_RAW + b']', CHECK_ENVELOPE, 1, id='array-then-a-second-array'),
        pytest.param(b'[' + AAPL_RAW + b'] x', CHECK_ENVELOPE, 1, id='array-then-a-stray-character'),
        pytest.param(b'[' + AAPL_RAW + b'],', CHECK_ENVELOPE, 1, id='array-then-a-comma'),
        pytest.param(b'[' + AAPL_RAW + b']\n' + SENTINEL.encode(), CHECK_ENVELOPE, 1, id='array-then-junk'),
    ],
)
def test_a_list_that_is_not_an_array_of_asset_records_is_refused_whole_naming_its_check(body, check, count):
    """tj-7zjov1.218 G4, G5, G7: the old raise path now refuses with the typed AssetListMalformed (a ValueError).

    The message is the check and a count, nothing of the body: it reaches the run history (D2).
    """
    with pytest.raises(AssetListMalformed) as raised:
        parse(body)

    assert isinstance(raised.value, ValueError)
    assert (raised.value.check, raised.value.count) == (check, count)
    assert str(raised.value) == f'{check} ({count})'
    assert SENTINEL not in str(raised.value), 'the vendor body reached the error message'


TRAILING_WHITESPACE = [
    pytest.param(b' ', id='space'),
    pytest.param(b'\t', id='tab'),
    pytest.param(b'\r', id='cr'),
    pytest.param(b'\n', id='lf'),
    pytest.param(b' \t\r\n \r\n', id='mixed'),
]


@pytest.mark.parametrize('tail', TRAILING_WHITESPACE)
def test_an_empty_array_followed_only_by_whitespace_is_an_empty_list(tail):
    """tj-7zjov1.150: the trailing-content check refuses content, never the whitespace a body may end with."""
    assert parse(b'[]' + tail) == []


@pytest.mark.parametrize('tail', TRAILING_WHITESPACE)
def test_a_list_followed_only_by_whitespace_parses_whole_with_each_element_sliced_byte_for_byte(tail):
    assets = parse(LIST_BODY + tail)

    assert [asset.raw_payload for asset in assets] == [AAPL_RAW, OBK_RAW, QUIET_RAW]
    assert [asset.symbol for asset in assets] == ['AAPL', 'OBK', 'QUIET']


# ---------------------------------------------------------------------------------------------
# The integrity guard (tj-7zjov1.218): G5 required fields, G6 duplicate id, and what is NOT checked (D4)
# ---------------------------------------------------------------------------------------------


def body_of(*records: dict) -> bytes:
    return json.dumps(list(records)).encode()


# Each a record that breaks G5 once. Where the bad value is a string it is the SENTINEL, so a message that quoted
# the vendor's value would be caught.
G5_VIOLATIONS = [
    *[
        pytest.param({key: _ABSENT}, id=f'{key}-missing')
        for key in (
            'id',
            'symbol',
            'class',
            'status',
            'exchange',
            'name',
            'tradable',
            'marginable',
            'shortable',
            'fractionable',
        )
    ],
    *[pytest.param({key: None}, id=f'{key}-null') for key in ('id', 'symbol', 'class', 'status', 'exchange', 'name')],
    pytest.param({'id': SENTINEL}, id='id-not-a-uuid'),
    pytest.param({'id': AAPL_ID[:-1]}, id='id-a-uuid-cut-short'),
    pytest.param({'id': 7}, id='id-a-number'),
    pytest.param({'id': ''}, id='id-empty'),
    pytest.param({'symbol': ''}, id='symbol-empty'),
    pytest.param({'symbol': 7}, id='symbol-a-number'),
    pytest.param({'class': SENTINEL}, id='class-not-us-equity'),
    pytest.param({'class': 'crypto'}, id='class-crypto'),
    pytest.param({'class': 'US_EQUITY'}, id='class-other-case'),
    pytest.param({'status': 'inactive'}, id='status-another-list'),
    pytest.param({'status': 'Active'}, id='status-other-case'),
    pytest.param({'status': SENTINEL}, id='status-unknown'),
    pytest.param({'exchange': ''}, id='exchange-empty'),
    pytest.param({'exchange': 7}, id='exchange-a-number'),
    pytest.param({'name': 7}, id='name-a-number'),
    pytest.param({'name': ['x']}, id='name-a-list'),
    *[
        pytest.param({key: value}, id=f'{key}-{label}')
        for key in ('tradable', 'marginable', 'shortable', 'fractionable')
        for value, label in (('true', 'a-string'), (1, 'a-number'), (None, 'null'))
    ],
]


@pytest.mark.parametrize('violation', G5_VIOLATIONS)
def test_a_record_missing_a_required_field_or_of_the_wrong_type_refuses_the_whole_list_g5(violation):
    """G5: two bad records among good ones refuse the list, counting the bad ones, quoting none of their values."""
    body = body_of(
        record(),
        record(**{'id': OBK_ID, 'symbol': 'OBK', **violation}),
        record(**{'id': GONE_ID, 'symbol': 'GONE', **violation}),
    )

    with pytest.raises(AssetListMalformed) as raised:
        parse(body)

    assert (raised.value.check, raised.value.count) == (CHECK_RECORD_FIELDS, 2)
    assert SENTINEL not in str(raised.value)


def test_a_record_of_the_active_list_on_the_inactive_list_is_refused_g5():
    """The status compared is the one requested, not 'active': the inactive list refuses an active record."""
    with pytest.raises(AssetListMalformed) as raised:
        parse(body_of(record(status='inactive', id=GONE_ID), record()), AssetListStatus.INACTIVE.value)

    assert (raised.value.check, raised.value.count) == (CHECK_RECORD_FIELDS, 1)


def test_the_class_compared_is_the_one_requested():
    assert parse_asset_list(body_of(record(**{'class': 'us_option'})), LIST_STATUS, 'us_option')[0].symbol == 'AAPL'
    with pytest.raises(AssetListMalformed):
        parse_asset_list(body_of(record()), LIST_STATUS, 'us_option')


@pytest.mark.parametrize(
    ('ids', 'count'),
    [
        pytest.param([AAPL_ID, AAPL_ID], 1, id='two-records-one-id'),
        pytest.param([AAPL_ID, OBK_ID, AAPL_ID, AAPL_ID], 2, id='three-records-one-id'),
        pytest.param([AAPL_ID, OBK_ID, AAPL_ID, OBK_ID], 2, id='two-ids-each-twice'),
    ],
)
def test_a_vendor_id_served_twice_refuses_the_whole_list_g6(ids, count):
    """G6: one id under two symbols is a malformed list; the count is the surplus records, never the id."""
    body = body_of(*[record(id=vendor_id, symbol=f'S{n}') for n, vendor_id in enumerate(ids)])

    with pytest.raises(AssetListMalformed) as raised:
        parse(body)

    assert (raised.value.check, raised.value.count) == (CHECK_DUPLICATE_ID, count)
    assert AAPL_ID not in str(raised.value)


def test_a_non_object_element_is_counted_g7():
    with pytest.raises(AssetListMalformed) as raised:
        parse(body_of(record())[:-1] + b', 1, null, "' + SENTINEL.encode() + b'"]')

    assert (raised.value.check, raised.value.count) == (CHECK_RECORD_OBJECT, 3)
    assert SENTINEL not in str(raised.value)


@pytest.mark.parametrize(
    'records',
    [
        pytest.param([record(exchange='TXSE')], id='a-txse-row'),
        pytest.param([record(exchange='NEWVENUE')], id='an-unknown-exchange'),
        pytest.param([record(attributes=['an_attribute_nobody_has_seen', 'has_options'])], id='an-unknown-attribute'),
        pytest.param([record(attributes='not-even-a-list')], id='attributes-of-any-shape'),
        pytest.param(
            [record(easy_to_borrow='maybe', borrow_status=7, cusip=None, maintenance_margin_requirement=30)],
            id='optional-and-extra-keys-of-any-type',
        ),
        pytest.param([record(name='')], id='an-empty-name'),
        pytest.param([record(), record(id=OBK_ID)], id='two-ids-one-symbol'),
        pytest.param([record(tradable=False, marginable=False, shortable=False, fractionable=False)], id='all-false'),
    ],
)
def test_what_the_guard_does_not_judge_parses_whole(records):
    """tj-7zjov1.218 D3 and D4: no exchange or attribute value list, extra keys pass, a duplicate SYMBOL is no signal."""
    body = body_of(*records)

    assets = parse(body)

    assert [asset.vendor_asset_id for asset in assets] == [item['id'] for item in records]
    assert [json.loads(asset.raw_payload) for asset in assets] == records


# The canonical vendor id form (tj-7zjov1.225, owner ruling 2026-11-01): G5 accepts an id only in the lowercase,
# hyphenated 8-4-4-4-12 spelling, because the store keys listings by the id string as served and a changed spelling
# would read as every listing vanishing and reappearing. Each spelling below is one uuid.UUID parses (it accepts all
# of them) applied to a canonical id, so only the canonical-form condition can refuse it. 'an-upper-case-uuid' was a
# must-parse case in test_what_the_guard_does_not_judge_parses_whole until the ruling; it is a refusal now.
UUID_PARSES_SPELLINGS = [
    pytest.param(str.upper, id='an-upper-case-uuid'),
    pytest.param(lambda value: value[:9] + value[9:].upper(), id='a-mixed-case-uuid'),
    pytest.param(lambda value: '{' + value + '}', id='a-braced-uuid'),
    pytest.param(lambda value: 'urn:uuid:' + value, id='a-urn-uuid'),
    pytest.param(lambda value: 'uuid:' + value, id='a-uuid-prefix'),
    pytest.param(lambda value: value.replace('-', ''), id='a-32-hex-no-hyphen-uuid'),
    pytest.param(lambda value: value.replace('-', '')[:4] + '-' + value.replace('-', '')[4:], id='misplaced-hyphens'),
]
# Whitespace around a canonical id. uuid.UUID already refuses these, so they guard a check that strips before it
# compares, which the parse alone would not catch.
WHITESPACE_SPELLINGS = [
    pytest.param(lambda value: ' ' + value, id='a-leading-space'),
    pytest.param(lambda value: value + '\n', id='a-trailing-newline'),
    pytest.param(lambda value: '\t' + value + ' ', id='whitespace-on-both-sides'),
]
NON_CANONICAL_SPELLINGS = [*UUID_PARSES_SPELLINGS, *WHITESPACE_SPELLINGS]

# The spellings G5 must still accept: the canonical form of any 128-bit value, the nil and max UUIDs included.
NIL_UUID = '00000000-0000-0000-0000-000000000000'
MAX_UUID = 'ffffffff-ffff-ffff-ffff-ffffffffffff'


@pytest.mark.parametrize('spell', UUID_PARSES_SPELLINGS)
def test_a_non_canonical_spelling_still_parses_as_a_uuid_so_only_the_canonical_rule_refuses_it(spell):
    """The precondition of the refusals below: uuid.UUID reads it as the same UUID, so a parse alone would accept it."""
    assert uuid.UUID(spell(AAPL_ID)) == uuid.UUID(AAPL_ID)
    assert spell(AAPL_ID) != AAPL_ID


@pytest.mark.parametrize('spell', NON_CANONICAL_SPELLINGS)
def test_a_vendor_id_in_a_non_canonical_spelling_refuses_the_whole_list_g5(spell):
    """Two records spelled otherwise among a canonical one refuse the list under G5, counted, quoting neither id."""
    body = body_of(record(), record(id=spell(OBK_ID), symbol='OBK'), record(id=spell(GONE_ID), symbol='GONE'))

    with pytest.raises(AssetListMalformed) as raised:
        parse(body)

    assert (raised.value.check, raised.value.count) == (CHECK_RECORD_FIELDS, 2)
    message = str(raised.value).lower()
    for vendor_id in (OBK_ID, GONE_ID, spell(OBK_ID), spell(GONE_ID)):
        assert vendor_id.strip().lower() not in message


@pytest.mark.parametrize(
    'vendor_id',
    [
        pytest.param(AAPL_ID, id='lowercase-canonical'),
        pytest.param(NIL_UUID, id='the-nil-uuid'),
        pytest.param(MAX_UUID, id='the-max-uuid'),
        pytest.param('0123abcd-ef01-2345-6789-abcdef012345', id='not-an-rfc-version-or-variant'),
    ],
)
def test_a_vendor_id_in_canonical_form_parses_whole_and_is_carried_as_served(vendor_id):
    """The rule is spelling only: any canonical id passes, and the id is carried exactly as served, never rewritten."""
    [asset] = parse(body_of(record(id=vendor_id)))

    assert asset.vendor_asset_id == vendor_id
    assert json.loads(asset.raw_payload)['id'] == vendor_id


@pytest.mark.parametrize(
    'vendor_id',
    [
        pytest.param(True, id='a-boolean'),
        pytest.param(['b0b6dd9d-8b9b-48a9-ba46-b9d54906e415'], id='a-list-holding-a-canonical-id'),
        pytest.param({'id': 'b0b6dd9d-8b9b-48a9-ba46-b9d54906e415'}, id='an-object-holding-a-canonical-id'),
        pytest.param(int(AAPL_ID.replace('-', ''), 16), id='the-uuid-as-an-integer'),
    ],
)
def test_a_vendor_id_that_is_not_a_string_refuses_the_whole_list_g5(vendor_id):
    with pytest.raises(AssetListMalformed) as raised:
        parse(body_of(record(id=OBK_ID, symbol='OBK'), record(id=vendor_id)))

    assert (raised.value.check, raised.value.count) == (CHECK_RECORD_FIELDS, 1)


@pytest.mark.parametrize('spell', NON_CANONICAL_SPELLINGS)
def test_one_id_served_in_two_spellings_is_refused_by_g5_before_it_can_reach_g6(spell):
    """G6 compares served strings, so it could never see these two as one id; G5 refuses the second spelling first."""
    with pytest.raises(AssetListMalformed) as raised:
        parse(body_of(record(), record(id=spell(AAPL_ID), symbol='AAPL2')))

    assert (raised.value.check, raised.value.count) == (CHECK_RECORD_FIELDS, 1)


def test_one_canonical_id_served_twice_is_still_g6():
    """The canonical rule does not swallow G6: the same canonical id twice passes G5 and is refused as a duplicate."""
    with pytest.raises(AssetListMalformed) as raised:
        parse(body_of(record(id=NIL_UUID), record(id=NIL_UUID, symbol='AAPL2')))

    assert (raised.value.check, raised.value.count) == (CHECK_DUPLICATE_ID, 1)


@pytest.mark.parametrize(
    'body',
    [
        pytest.param(b'\xff', id='not-utf8'),
        pytest.param(b'{"id": ', id='not-json'),
        pytest.param(b'[' + AAPL_RAW + b']', id='a-list-not-a-record'),
        pytest.param(b'{"symbol": "AAPL"}', id='no-id'),
    ],
)
def test_a_by_symbol_answer_that_is_not_one_record_is_raised_loud(body):
    with pytest.raises(ValueError):
        parse_asset(body)


# ---------------------------------------------------------------------------------------------
# get_assets: one call, the one status, asset_class=us_equity, on the trading scope
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_a_list_is_one_get_for_the_one_status_named_with_us_equity_on_the_trading_host(executor, status):
    """Design inputs (2) and (3) as amended at 21:54 UTC: one call, never both statuses, never absent."""
    vendor = stub_trading_vendor(raw_answer(list_body(status)))

    outcome = await reader(executor, vendor.client).get_assets(list_query(status))

    assert isinstance(outcome, AssetListResponse), outcome
    [sent] = vendor.sent
    assert sent.method == 'GET'
    assert (sent.host, sent.path) == (ALPACA_TRADING_TEST_HOST, ASSETS_PATH)
    assert sent.params == {'status': status.value, 'asset_class': 'us_equity'}
    assert sent.headers['APCA-API-KEY-ID'] == PLACEHOLDER_KEY_ID
    assert sent.headers['APCA-API-SECRET-KEY'] == PLACEHOLDER_SECRET


@pytest.mark.asyncio
async def test_a_list_answer_is_every_asset_raw_in_order_stamped_with_the_readers_clock(executor):
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert outcome == AssetListResponse(assets=parse(LIST_BODY), as_of=NOW)
    assert [asset.raw_payload for asset in outcome.assets] == [AAPL_RAW, OBK_RAW, QUIET_RAW]


@pytest.mark.asyncio
async def test_an_empty_list_is_served_with_no_assets(executor):
    vendor = stub_trading_vendor(raw_answer(b'[]'))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert outcome == AssetListResponse(assets=[], as_of=NOW)


@pytest.mark.asyncio
async def test_a_list_is_paced_by_one_acquire_on_the_trading_scope_and_never_the_data_scope(executor):
    """Acceptance: paced on the trading-API scope. The priority and deadline are the query's own."""
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))
    pacing = RecordingScheduler()

    await reader(executor, vendor.client, scheduler=pacing).get_assets(list_query())

    assert pacing.acquires == [Acquire(ALPACA_TRADING_SCOPE, RequestPriority.BACKFILL, DEADLINE)]


@pytest.mark.asyncio
async def test_a_list_counts_one_call_and_one_page_on_its_tally(executor):
    vendor = stub_trading_vendor(raw_answer(LIST_BODY))
    tally = CallTally()

    before = datetime.now(UTC)
    await reader(executor, vendor.client).get_assets(list_query(tally=tally))
    after = datetime.now(UTC)

    assert (tally.calls, tally.pages, tally.retries, tally.throttled_429) == (1, 1, 0, 0)
    assert before <= tally.first_call_at == tally.last_call_at <= after


@pytest.mark.asyncio
@pytest.mark.parametrize('asset_type', [kind for kind in AssetType if kind is not AssetType.STOCK])
async def test_a_list_of_a_kind_other_than_stock_is_refused_before_any_acquire_or_request(executor, asset_type):
    vendor = stub_trading_vendor()
    pacing = RecordingScheduler()

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_assets(list_query(asset_type=asset_type))

    error = list_failure(outcome)
    assert type(error) is BrokerUnsupportedError
    assert error.reason is Reason.UNSUPPORTED_ASSET_TYPE
    assert vendor.sent == []
    assert pacing.acquires == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'reason'),
    [
        pytest.param(401, Reason.VENDOR_AUTH, id='401'),
        pytest.param(403, Reason.VENDOR_AUTH, id='403'),
        pytest.param(404, Reason.VENDOR_REJECTED, id='404'),
        pytest.param(422, Reason.VENDOR_REJECTED, id='422'),
        pytest.param(500, Reason.VENDOR_UNAVAILABLE, id='500'),
        pytest.param(503, Reason.VENDOR_UNAVAILABLE, id='503'),
    ],
)
async def test_a_list_the_vendor_refuses_is_a_typed_failure_with_its_cause(executor, status, reason):
    """A 404 on the LIST endpoint is not a not-found answer, and never an empty list.

    The list has no symbol to be missing: it is the vendor refusing the request.
    """
    vendor = stub_trading_vendor(status_answer(status))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    error = list_failure(outcome)
    assert error.reason is reason
    assert isinstance(error.__cause__, APIError)
    assert error.__cause__.status_code == status
    assert len(vendor.sent) == 1, 'a refused list was retried by the reader'


@pytest.mark.asyncio
async def test_a_list_rate_limited_is_returned_with_the_reset_the_trading_host_named_and_counted(executor):
    reset = NOW + timedelta(seconds=42)
    vendor = stub_trading_vendor(status_answer(429, headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())}))
    tally = CallTally()

    outcome = await reader(executor, vendor.client).get_assets(list_query(tally=tally))

    error = list_failure(outcome)
    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == reset
    assert (tally.calls, tally.pages, tally.throttled_429) == (1, 0, 1)
    assert len(vendor.sent) == 1


@pytest.mark.asyncio
async def test_a_header_less_429_waits_for_the_reset_the_last_trading_answer_named_and_the_data_scope_learns_nothing(
    executor,
):
    """Design input (5): the trading host sends its own rate-limit headers; they are remembered for ITS scope."""
    reset = NOW + timedelta(seconds=37)
    vendor = stub_trading_vendor(
        raw_answer(b'[]', headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())}), status_answer(429)
    )
    read = reader(executor, vendor.client)

    await read.get_assets(list_query())
    outcome = await read.get_assets(list_query(AssetListStatus.INACTIVE))

    assert list_failure(outcome).reset_at == reset
    assert broker_api.last_reset(ALPACA_TRADING_SCOPE, NOW) == reset
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, NOW) is None, 'a trading answer moved the data scope reset'


@pytest.mark.asyncio
async def test_a_header_less_429_with_nothing_remembered_waits_the_trading_scopes_declared_window(executor):
    vendor = stub_trading_vendor(status_answer(429))
    pacing = RecordingScheduler(declared_window_seconds=60.0)

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_assets(list_query())

    assert list_failure(outcome).reset_at == NOW + timedelta(seconds=60)
    assert pacing.windows == [ALPACA_TRADING_SCOPE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'cut',
    [
        pytest.param(requests.exceptions.ConnectionError('down'), id='connection'),
        pytest.param(requests.exceptions.ReadTimeout('slow'), id='timeout'),
        pytest.param(requests.exceptions.ChunkedEncodingError('cut'), id='cut'),
    ],
)
async def test_a_list_the_vendor_cannot_answer_is_vendor_unavailable_counted_as_one_call(executor, cut):
    vendor = stub_trading_vendor(cut)
    tally = CallTally()

    outcome = await reader(executor, vendor.client).get_assets(list_query(tally=tally))

    error = list_failure(outcome)
    assert error.reason is Reason.VENDOR_UNAVAILABLE
    assert error.__cause__ is cut
    assert (tally.calls, tally.pages) == (1, 0)


class Frozen:
    """A monotonic clock that never moves, so a scope's single token never refills."""

    def __call__(self) -> float:
        return 0.0


@pytest.fixture
def scheduler_never_sleeps(monkeypatch) -> None:
    async def never_sleep(seconds: float) -> None:
        raise AssertionError(f'the scheduler slept {seconds}s instead of refusing at once')

    monkeypatch.setattr(scheduler_module, 'asyncio', SimpleNamespace(sleep=never_sleep))


@pytest.mark.asyncio
async def test_a_list_whose_trading_token_misses_the_deadline_is_rate_budget_with_no_request(
    executor, scheduler_never_sleeps
):
    """The trading scope's own budget gates the call: the data scope is full and is never asked."""
    scheduler = real_scheduler(
        {ALPACA_DATA_SCOPE: ONE_PER_SECOND, ALPACA_TRADING_SCOPE: ONE_PER_SECOND},
        clock=lambda: NOW,
        budget_clock=Frozen(),
    )
    vendor = stub_trading_vendor(raw_answer(b'[]'))
    read = reader(executor, vendor.client, scheduler=scheduler)

    first = await read.get_assets(list_query(deadline=NOW + timedelta(milliseconds=500)))
    second = await read.get_assets(list_query(deadline=NOW + timedelta(milliseconds=500)))

    assert isinstance(first, AssetListResponse)
    error = list_failure(second)
    assert error.reason is Reason.RATE_BUDGET
    assert len(vendor.sent) == 1, 'the call was made without a token'


@pytest.mark.asyncio
async def test_without_credentials_a_list_is_vendor_auth_with_no_acquire_and_no_request(executor):
    pacing = RecordingScheduler()

    outcome = await reader(executor, None, scheduler=pacing).get_assets(list_query())

    error = list_failure(outcome)
    assert type(error) is MissingCredentialsError
    assert error.reason is Reason.VENDOR_AUTH
    assert pacing.acquires == []


@pytest.mark.asyncio
async def test_a_reader_built_without_a_trading_scope_raises_rather_than_charge_another(executor):
    vendor = stub_trading_vendor()

    with pytest.raises(RuntimeError, match='trading scope'):
        await reader(executor, vendor.client, trading_scope=None).get_assets(list_query())
    with pytest.raises(RuntimeError, match='trading scope'):
        await reader(executor, vendor.client, trading_scope=None).get_asset(lookup())
    assert vendor.sent == []


# ---------------------------------------------------------------------------------------------
# get_assets under the integrity guard (tj-7zjov1.218): every violation a returned, retried failure
# ---------------------------------------------------------------------------------------------


async def refused(executor, answer, query: AssetListQuery | None = None) -> tuple[ExogenousError, CallTally, list]:
    """Read the list once against one answer; give the failure (asserted to be the guard's), the tally and the calls."""
    vendor = stub_trading_vendor(answer)
    query = query if query is not None else list_query(tally=CallTally())
    tally = query.tally

    outcome = await reader(executor, vendor.client).get_assets(query)

    error = list_failure(outcome)
    assert type(error) is ExogenousError
    assert error.reason is Reason.VENDOR_UNAVAILABLE, 'a refused list must be retried as transient (D2)'
    assert error.detail.startswith(REFUSED), error.detail
    assert isinstance(error.__cause__, AssetListMalformed)
    assert error.detail == f'{REFUSED}{error.__cause__.check} ({error.__cause__.count})'
    return error, tally, vendor.sent


@pytest.mark.asyncio
async def test_a_list_body_that_is_not_an_array_is_a_refused_list_never_a_raise_out_of_the_read(executor):
    """Rewritten for tj-7zjov1.218 D2: a non-array body is the guard's typed transient failure, not a raise.

    This test used to expect a ValueError out of get_assets. That raise reached the executor's crash path as
    PERMANENT with a generic detail and no vendor stats, so it was never retried, which contradicts the owner's
    'the existing retry rules apply'.
    """
    error, tally, sent = await refused(executor, raw_answer(b'{"message": "' + SENTINEL.encode() + b'"}'))

    assert error.detail == 'asset list refused as malformed: G4 body is one UTF-8 JSON array (0)'
    assert SENTINEL not in error.detail
    assert (tally.calls, len(sent)) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [201, 203, 204, 206, 301, 302, 304, 307], ids=str)
async def test_a_list_answered_with_any_status_but_200_is_refused_g1(executor, status):
    """G1: raise_for_status passes every 2xx and, with redirects off, a 3xx; only 200 is a list."""
    error, tally, sent = await refused(executor, raw_answer(LIST_BODY, status=status))

    assert error.detail == 'asset list refused as malformed: G1 HTTP status is 200 (1)'
    assert (tally.calls, len(sent)) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'content_type',
    [
        'text/html',
        'text/plain; charset=utf-8',
        'application/problem+json',
        'application/jsonx',
        'application/octet-stream',
        '',
    ],
)
async def test_a_list_served_as_anything_but_application_json_is_refused_g2(executor, content_type):
    error, tally, _ = await refused(executor, raw_answer(LIST_BODY, headers={'Content-Type': content_type}))

    assert error.detail == 'asset list refused as malformed: G2 content type is application/json (1)'
    assert tally.calls == 1


@pytest.mark.asyncio
async def test_a_list_served_with_no_content_type_at_all_is_refused_g2_never_raised(executor, monkeypatch):
    """G2 with the header ABSENT (architect gate, tj-7zjov1.220): a refused, retried list, never an exception.

    The recorded transport always sets a Content-Type, so the real request_raw runs against it and only that one
    header is taken off the RawPage it returns; the HTTP call, the pacing and the tally stay real. Without the
    guard's None case, None.split would raise out of get_assets to the executor's crash path (PERMANENT, not
    retried), which D2 exists to remove.
    """
    real_request_raw = broker_api.request_raw

    def without_content_type(*args, **kwargs) -> broker_api.RawPage:
        page = real_request_raw(*args, **kwargs)
        headers = {key: value for key, value in page.headers.items() if key.lower() != 'content-type'}
        return broker_api.RawPage(content=page.content, headers=headers, status_code=page.status_code)

    monkeypatch.setattr(broker_api, 'request_raw', without_content_type)

    error, tally, sent = await refused(executor, raw_answer(LIST_BODY))

    assert error.detail == 'asset list refused as malformed: G2 content type is application/json (1)'
    assert (tally.calls, len(sent)) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type', ['application/json; charset=utf-8', 'Application/JSON', ' application/json '])
async def test_a_list_served_as_application_json_with_parameters_or_any_case_is_read(executor, content_type):
    vendor = stub_trading_vendor(raw_answer(LIST_BODY, headers={'Content-Type': content_type}))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert outcome == AssetListResponse(assets=parse(LIST_BODY), as_of=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'length',
    [str(len(LIST_BODY) + 1), str(len(LIST_BODY) - 1), '0', 'many', '-1'],
    ids=['longer-than-the-body', 'shorter-than-the-body', 'zero', 'not-a-number', 'negative'],
)
async def test_a_content_length_that_is_not_the_body_length_is_refused_g3(executor, length):
    error, tally, _ = await refused(executor, raw_answer(LIST_BODY, headers={'Content-Length': length}))

    assert error.detail == 'asset list refused as malformed: G3 content length matches the body (1)'
    assert tally.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'headers',
    [
        pytest.param({'Content-Length': str(len(LIST_BODY))}, id='equal-length'),
        pytest.param({}, id='no-content-length'),
        pytest.param({'Content-Length': '17', 'Content-Encoding': 'gzip'}, id='encoded-length-not-checked'),
        pytest.param({'content-length': str(len(LIST_BODY))}, id='lower-case-header'),
    ],
)
async def test_a_content_length_is_checked_only_when_present_and_no_encoding_is_applied_g3(executor, headers):
    """G3: the header is the encoded length when Content-Encoding is applied, and absent is unchecked."""
    vendor = stub_trading_vendor(raw_answer(LIST_BODY, headers=headers))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert outcome == AssetListResponse(assets=parse(LIST_BODY), as_of=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('body', 'detail'),
    [
        pytest.param(b'[' + AAPL_RAW + b'] x', 'G4 body is one UTF-8 JSON array (1)', id='g4-trailing-content'),
        pytest.param(b'[' + AAPL_RAW + b', 7]', 'G7 element is a JSON object (1)', id='g7-not-an-object'),
        pytest.param(
            body_of(record(), record(id=OBK_ID, symbol='OBK', tradable='yes')), 'G5 record fields (1)', id='g5-a-flag'
        ),
        pytest.param(
            body_of(record(), record(id=OBK_ID, status='inactive')), 'G5 record fields (1)', id='g5-another-list'
        ),
        pytest.param(body_of(record(), record(symbol='AAPL2')), 'G6 duplicate vendor id (1)', id='g6-duplicate-id'),
    ],
)
async def test_a_malformed_list_body_is_a_refused_list_naming_its_check_g4_to_g7(executor, body, detail):
    error, tally, sent = await refused(executor, raw_answer(body))

    assert error.detail == REFUSED + detail
    assert (tally.calls, len(sent)) == (1, 1), 'a refused list was retried by the reader, not the executor'


@pytest.mark.asyncio
async def test_a_refused_list_quotes_no_vendor_value_in_its_detail(executor):
    body = body_of(record(), record(id=OBK_ID, symbol=SENTINEL, exchange=SENTINEL, status=SENTINEL))

    error, _, _ = await refused(executor, raw_answer(body))

    assert SENTINEL not in error.detail
    assert SENTINEL not in str(error)


@pytest.mark.asyncio
async def test_the_inactive_list_refuses_an_active_record_read_through_get_assets(executor):
    """The status compared at the read is the query's own, so the inactive list cannot hold the active list's rows."""
    query = list_query(AssetListStatus.INACTIVE, tally=CallTally())

    error, _, _ = await refused(executor, raw_answer(LIST_BODY), query)

    assert error.detail == REFUSED + 'G5 record fields (3)'


@pytest.mark.asyncio
@pytest.mark.parametrize('spell', NON_CANONICAL_SPELLINGS)
async def test_a_list_holding_a_non_canonical_vendor_id_is_a_refused_list_read_through_get_assets(executor, spell):
    """tj-7zjov1.225: the typed transient G5 failure out of get_assets, nothing raised, one call, no id quoted."""
    served_id = json.dumps(spell(OBK_ID))[1:-1].encode()  # spliced into raw JSON, so a newline is its escape
    body = b'[' + AAPL_RAW + b',' + OBK_RAW.replace(OBK_ID.encode(), served_id) + b']'

    error, tally, sent = await refused(executor, raw_answer(body))

    assert error.detail == REFUSED + 'G5 record fields (1)'
    assert OBK_ID not in error.detail.lower()
    assert (tally.calls, len(sent)) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('vendor_id', [NIL_UUID, MAX_UUID], ids=['the-nil-uuid', 'the-max-uuid'])
async def test_a_list_holding_a_canonical_edge_id_is_read_whole_through_get_assets(executor, vendor_id):
    body = b'[' + AAPL_RAW + b',' + OBK_RAW.replace(OBK_ID.encode(), vendor_id.encode()) + b']'
    vendor = stub_trading_vendor(raw_answer(body))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert isinstance(outcome, AssetListResponse)
    assert [asset.vendor_asset_id for asset in outcome.assets] == [AAPL_ID, vendor_id]


@pytest.mark.asyncio
async def test_a_list_holding_a_duplicate_symbol_under_two_ids_is_read_whole(executor):
    """D3 G6: a duplicate SYMBOL is not a signal (the vendor serves one symbol under two ids)."""
    body = body_of(record(), record(id=OBK_ID))
    vendor = stub_trading_vendor(raw_answer(body))

    outcome = await reader(executor, vendor.client).get_assets(list_query())

    assert isinstance(outcome, AssetListResponse)
    assert [(asset.vendor_asset_id, asset.symbol) for asset in outcome.assets] == [(AAPL_ID, 'AAPL'), (OBK_ID, 'AAPL')]


# ---------------------------------------------------------------------------------------------
# get_asset: one call by symbol, a 404 is an answer, on the trading scope, counted nowhere
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lookup_is_one_get_by_symbol_with_no_parameters_and_keeps_the_body_raw(executor):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW))

    outcome = await reader(executor, vendor.client).get_asset(lookup('AAPL'))

    [sent] = vendor.sent
    assert sent.method == 'GET'
    assert (sent.host, sent.path) == (ALPACA_TRADING_TEST_HOST, f'{ASSETS_PATH}/AAPL')
    assert sent.params == {}
    assert outcome == AssetResponse(asset=parse_asset(AAPL_RAW), as_of=NOW)
    assert outcome.asset.raw_payload == AAPL_RAW
    assert outcome.asset.vendor_asset_id == AAPL_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('symbol', 'path'),
    [
        pytest.param('BRK.B', f'{ASSETS_PATH}/BRK.B', id='a-dot-stays'),
        pytest.param('BRK/B', f'{ASSETS_PATH}/BRK%2FB', id='a-slash-is-escaped'),
        pytest.param('A B', f'{ASSETS_PATH}/A%20B', id='a-space-is-escaped'),
    ],
)
async def test_the_symbol_is_one_escaped_path_segment(executor, symbol, path):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW))

    await reader(executor, vendor.client).get_asset(lookup(symbol))

    assert [sent.path for sent in vendor.sent] == [path]


@pytest.mark.asyncio
async def test_a_lookup_judges_nothing_an_inactive_untradable_record_is_answered_like_any_other(executor):
    vendor = stub_trading_vendor(raw_answer(GONE_RAW))

    outcome = await reader(executor, vendor.client).get_asset(lookup('GONE'))

    assert isinstance(outcome, AssetResponse)
    assert (outcome.asset.active, outcome.asset.tradable, outcome.asset.vendor_asset_id) == (False, False, GONE_ID)


@pytest.mark.asyncio
async def test_a_lookup_answers_the_vendors_own_symbol_even_when_it_differs_from_the_one_asked(executor):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW))

    outcome = await reader(executor, vendor.client).get_asset(lookup('aapl'))

    assert outcome.asset.symbol == 'AAPL'


@pytest.mark.asyncio
async def test_a_vendor_404_on_a_lookup_is_the_typed_not_found_answer_not_a_failure(executor):
    """21:54 and 20:29 UTC notes: the 404 is its own typed outcome; P12h reads it as the id having gone."""
    vendor = stub_trading_vendor(status_answer(404, 'asset not found for GONE'))

    outcome = await reader(executor, vendor.client).get_asset(lookup('GONE'))

    assert outcome == AssetNotFound(symbol='GONE', as_of=NOW)


@pytest.mark.asyncio
async def test_a_lookup_is_paced_by_one_acquire_on_the_trading_scope_under_its_priority_and_deadline(executor):
    vendor = stub_trading_vendor(raw_answer(AAPL_RAW), status_answer(404))
    pacing = RecordingScheduler()
    read = reader(executor, vendor.client, scheduler=pacing)

    await read.get_asset(lookup('AAPL'))
    await read.get_asset(lookup('GONE'))

    assert pacing.acquires == [Acquire(ALPACA_TRADING_SCOPE, RequestPriority.INTERACTIVE, DEADLINE)] * 2


@pytest.mark.asyncio
async def test_a_lookup_rate_limited_is_a_failure_carrying_the_reset(executor):
    reset = NOW + timedelta(seconds=19)
    vendor = stub_trading_vendor(status_answer(429, headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())}))

    outcome = await reader(executor, vendor.client).get_asset(lookup())

    error = lookup_failure(outcome)
    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == reset


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('answer', 'reason'),
    [
        pytest.param(status_answer(503), Reason.VENDOR_UNAVAILABLE, id='503'),
        pytest.param(requests.exceptions.ConnectionError('down'), Reason.VENDOR_UNAVAILABLE, id='connection'),
        pytest.param(status_answer(403), Reason.VENDOR_AUTH, id='403'),
        pytest.param(status_answer(422), Reason.VENDOR_REJECTED, id='422'),
    ],
)
async def test_a_lookup_that_is_neither_found_nor_not_found_is_a_typed_failure(executor, answer, reason):
    vendor = stub_trading_vendor(answer)

    outcome = await reader(executor, vendor.client).get_asset(lookup())

    assert lookup_failure(outcome).reason is reason


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['', '   '], ids=['empty', 'blank'])
async def test_a_lookup_with_no_symbol_is_refused_before_any_acquire_or_request(executor, symbol):
    vendor = stub_trading_vendor()
    pacing = RecordingScheduler()

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_asset(lookup(symbol))

    error = lookup_failure(outcome)
    assert type(error) is InvalidRequestError
    assert error.reason is Reason.INVALID_REQUEST
    assert vendor.sent == []
    assert pacing.acquires == []


@pytest.mark.asyncio
async def test_without_credentials_a_lookup_is_vendor_auth(executor):
    outcome = await reader(executor, None).get_asset(lookup())

    assert type(lookup_failure(outcome)) is MissingCredentialsError


# ---------------------------------------------------------------------------------------------
# broker_api: the trading client is configuration, and request_raw makes one GET and keeps the bytes
# ---------------------------------------------------------------------------------------------


def test_the_trading_client_is_never_built_without_a_configured_host(monkeypatch):
    """Design input (6): the trading host is configuration, never a hard-coded paper or live host."""
    monkeypatch.setenv('ALPACA_API_KEY', PLACEHOLDER_KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', PLACEHOLDER_SECRET)

    with pytest.raises(MissingCredentialsError, match=broker_api.ALPACA_TRADING_HOST_VAR):
        broker_api.get_trading_client()


def test_the_trading_client_is_never_built_without_credentials(monkeypatch):
    monkeypatch.setenv(broker_api.ALPACA_TRADING_HOST_VAR, ALPACA_TRADING_TEST_URL)

    with pytest.raises(MissingCredentialsError):
        broker_api.get_trading_client()


def test_the_trading_client_is_built_once_on_the_configured_host_and_cleared_by_the_seam(monkeypatch):
    monkeypatch.setenv('ALPACA_API_KEY', PLACEHOLDER_KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', PLACEHOLDER_SECRET)
    monkeypatch.setenv(broker_api.ALPACA_TRADING_HOST_VAR, ALPACA_TRADING_TEST_URL + '/')

    client = broker_api.get_trading_client()

    assert client._base_url == ALPACA_TRADING_TEST_URL, 'the trailing slash would double the path separator'
    assert broker_api.get_trading_client() is client
    broker_api.set_trading_client(None)
    assert broker_api.get_trading_client() is not client


def test_request_raw_makes_one_get_and_keeps_the_body_bytes_and_the_headers():
    vendor = stub_trading_vendor(raw_answer(LIST_BODY, headers={RATE_LIMIT_RESET_HEADER: '123'}))

    page = broker_api.request_raw(vendor.client, broker_api.ASSETS_PATH, {'status': 'active'})

    [sent] = vendor.sent
    assert (sent.method, sent.path, sent.params) == ('GET', ASSETS_PATH, {'status': 'active'})
    assert page.content == LIST_BODY
    assert page.headers[RATE_LIMIT_RESET_HEADER] == '123'
    assert page.status_code == 200


@pytest.mark.parametrize('status', [200, 203, 302])
def test_request_raw_carries_the_status_it_was_answered_with(status):
    """tj-7zjov1.218 G1: raise_for_status passes these, so the list read needs the status itself."""
    vendor = stub_trading_vendor(raw_answer(b'[]', status=status))

    assert broker_api.request_raw(vendor.client, broker_api.ASSETS_PATH).status_code == status


@pytest.mark.parametrize('status', [404, 429, 503])
def test_request_raw_raises_the_sdks_api_error_carrying_the_response_and_never_retries(status):
    vendor = stub_trading_vendor(status_answer(status, 'no', headers={RATE_LIMIT_RESET_HEADER: '9'}))

    with pytest.raises(APIError) as raised:
        broker_api.request_raw(vendor.client, broker_api.ASSETS_PATH)

    assert raised.value.status_code == status
    assert raised.value.response.headers[RATE_LIMIT_RESET_HEADER] == '9'
    assert len(vendor.sent) == 1
