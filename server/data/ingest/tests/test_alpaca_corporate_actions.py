"""Alpaca's corporate actions read RAW off the data API (P13, tj-7zjov1.23), against builder-ingest's daa41c6.

Checked against the bead's design notes, which bind: the 21:54 UTC note as amended by the owner's ruling at 04:46
UTC (corporate actions are captured as published, what was known and when; the store judges; interpret nothing:
action_type is the vendor's group wording uninterpreted, every role-named symbol and cusip is kept in identifiers
under the vendor's own field name, the record is kept raw, no type is filtered, the page token is followed to the
end), the 14:32 UTC note (the kind on the data-API scope), and the acceptance (the window honoured, pages followed to
the end, raw records with vendor id and payload, paced on the data-API scope).

HOW IT IS DRIVEN. The parser is fed bodies as bytes. The reader is the REAL AlpacaRead over the REAL alpaca-py
CorporateActionsClient, its HTTP answered by the recorded transport (alpaca_recorded.stub_corporate_actions_vendor)
on Alpaca's data host, which fails the test for any other host. The recorded bodies are the P27a s10/s10x captures,
served byte for byte (alpaca_recorded.load_raw); bodies no capture holds (a second page, a vendor fault) are
constructed and say so. Pacing is observed through RecordingScheduler, which records the SCOPE of every acquire; the
deadline case uses a real Scheduler. No vendor key is read: the placeholder pair is the only credential here.

THE WINDOW'S END DAY. The reader sends start.date() and end.date() of the UTC window. Whether the vendor's end is
inclusive is unconfirmed (no capture settles it), so the end's own day is sent and a record on that day may come
back past the exclusive end; the store dedups on the vendor id. What is pinned here is the days sent; whether the
vendor serves the end day, and so whether that over-read happens, is not provable offline.
"""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError

from common.errors.vocabulary import InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.classify import RATE_LIMIT_RESET_HEADER
from data.ingest.app.brokers.alpaca.corporate_actions import (
    CorporateActionsPage,
    action_from_record,
    parse_corporate_actions_page,
)
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError, MissingCredentialsError
from data.ingest.app.brokers.interface import (
    CallTally,
    CorporateAction,
    CorporateActionsFailure,
    CorporateActionsQuery,
    CorporateActionsResponse,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import scheduler as scheduler_module
from data.ingest.tests.alpaca_recorded import (
    ALPACA_DATA_HOST,
    PLACEHOLDER_KEY_ID,
    PLACEHOLDER_SECRET,
    load_raw,
    raw_answer,
    status_answer,
    stub_corporate_actions_vendor,
    stub_trading_vendor,
)
from data.ingest.tests.scheduler_doubles import ONE_PER_SECOND, Acquire, RecordingScheduler, real_scheduler


pytestmark = pytest.mark.data_ingest

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)
START = datetime(2015, 1, 1, tzinfo=UTC)
END = datetime(2026, 10, 24, tzinfo=UTC)
CORPORATE_ACTIONS_PATH = '/v1/corporate-actions'


def midnight(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# Constructed bodies: written as the bytes a vendor could send, so a re-serialisation cannot match them
# ---------------------------------------------------------------------------------------------

# Odd spacing, an escaped and a literal non-ASCII character, and brackets, braces and commas inside strings.
NAME_CHANGE_RAW = (
    '{ "id" : "nc-1", "old_symbol":"FB",  "new_symbol": "META", "old_cusip": "30303M102",'
    ' "new_cusip":"30303M102", "process_date": "2022-06-09", "note": "a [b], {c} \\u00e9 é" }'
).encode()
MERGER_RAW = (
    b'{\n  "id": "m-1",\n  "acquirer_symbol": "AMD",\n  "acquirer_cusip": "007903107",\n'
    b'  "acquiree_symbol": "XLNX",\n  "acquiree_cusip": "983919101",\n  "acquirer_rate": 1.7234,\n'
    b'  "effective_date": "2022-02-14",\n  "process_date": "2022-02-14"\n}'
)
DIVIDEND_RAW = (
    b'{"id":"d-1","symbol":"VFV","cusip":"","rate":0.5,"ex_date":"2026-01-02","record_date":"2026-01-03",'
    b'"payable_date":"2026-01-10","special":false,"foreign":[1,2]}'
)
SEVERAL_GROUPS_BODY = (
    b' {"corporate_actions": {\n "name_changes": [ '
    + NAME_CHANGE_RAW
    + b' ] ,"stock_mergers":['
    + MERGER_RAW
    + b'],\n"cash_dividends" : [\n'
    + DIVIDEND_RAW
    + b' , '
    + DIVIDEND_RAW.replace(b'd-1', b'd-2')
    + b']},\n "next_page_token": "tok-2"}\n'
)


def page(groups: dict[str, list[dict] | None] | None, token: str | None = None) -> bytes:
    """A constructed page body, compact as the vendor sends it."""
    return json.dumps({'corporate_actions': groups, 'next_page_token': token}, separators=(',', ':')).encode()


def page_answer(groups: dict[str, list[dict] | None] | None, token: str | None = None):
    return raw_answer(page(groups, token))


def record(action_id: str = 'a-1', **fields: object) -> dict:
    return {'id': action_id, **fields}


# ---------------------------------------------------------------------------------------------
# The parser: each record sliced byte for byte, the group as the type, nothing filtered
# ---------------------------------------------------------------------------------------------


def test_each_record_keeps_its_own_bytes_exactly_as_served_group_by_group_in_the_order_served():
    parsed = parse_corporate_actions_page(SEVERAL_GROUPS_BODY)

    assert [(action.action_type, action.vendor_action_id) for action in parsed.actions] == [
        ('name_changes', 'nc-1'),
        ('stock_mergers', 'm-1'),
        ('cash_dividends', 'd-1'),
        ('cash_dividends', 'd-2'),
    ]
    assert [action.raw_payload for action in parsed.actions] == [
        NAME_CHANGE_RAW,
        MERGER_RAW,
        DIVIDEND_RAW,
        DIVIDEND_RAW.replace(b'd-1', b'd-2'),
    ]
    # Each one is a slice of the body itself.
    assert all(action.raw_payload in SEVERAL_GROUPS_BODY for action in parsed.actions)
    assert parsed.next_page_token == 'tok-2'


@pytest.mark.parametrize(
    'name', ['corporate_actions_sivb_untyped', 'corporate_actions_xlnx_stock_merger', 'corporate_actions_name_changes']
)
def test_a_recorded_page_keeps_each_records_bytes_as_a_slice_of_the_served_body(name):
    body = load_raw(name).body

    parsed = parse_corporate_actions_page(body)

    decoded = json.loads(body)['corporate_actions']
    expected_ids = [item['id'] for records in decoded.values() for item in records]
    assert [action.vendor_action_id for action in parsed.actions] == expected_ids
    for action in parsed.actions:
        assert action.raw_payload in body, 'a raw payload is not the bytes served'
        assert json.loads(action.raw_payload)['id'] == action.vendor_action_id
        # Compact as served: a re-serialisation with json.dumps' default separators would not be.
        assert action.raw_payload.startswith(b'{"')
        assert b'": ' not in action.raw_payload


def test_the_recorded_merger_is_carried_with_every_role_named_identifier_and_both_dates():
    """The s10 XLNX stock_merger capture: acquirer and acquiree, symbol and cusip each, effective and process dates."""
    [action] = parse_corporate_actions_page(load_raw('corporate_actions_xlnx_stock_merger').body).actions

    assert action.vendor_action_id == '15459aa5-b297-4e33-9624-c9d59b75ba46'
    assert action.action_type == 'stock_mergers'
    assert action.identifiers == {
        'acquiree_cusip': '983919101',
        'acquiree_symbol': 'XLNX',
        'acquirer_cusip': '007903107',
        'acquirer_symbol': 'AMD',
    }
    assert (action.effective_date, action.process_date) == (midnight(2022, 2, 14), midnight(2022, 2, 14))
    assert (action.ex_date, action.record_date, action.payable_date) == (None, None, None)


def test_the_recorded_name_changes_keep_old_and_new_symbols_and_cusips_and_the_process_date_only():
    """The s10x capture: a name change gives a process_date only, and both sides of the change by role."""
    actions = parse_corporate_actions_page(load_raw('corporate_actions_name_changes').body).actions

    assert [action.action_type for action in actions] == ['name_changes'] * 3
    first = actions[0]
    assert first.identifiers == {
        'new_cusip': '30303M102',
        'new_symbol': 'META',
        'old_cusip': '30303M102',
        'old_symbol': 'FB',
    }
    assert first.process_date == midnight(2022, 6, 9)
    assert (first.ex_date, first.record_date, first.payable_date, first.effective_date) == (None, None, None, None)


def test_a_recorded_empty_window_is_no_actions_and_no_token():
    parsed = parse_corporate_actions_page(load_raw('corporate_actions_empty').body)

    assert parsed == CorporateActionsPage(actions=[], next_page_token=None)


@pytest.mark.parametrize(
    'body',
    [
        pytest.param(b'{"corporate_actions":{},"next_page_token":null}', id='empty-object'),
        pytest.param(b'{"corporate_actions":null,"next_page_token":null}', id='null'),
        pytest.param(b'{"next_page_token":null}', id='absent'),
        pytest.param(b'{"corporate_actions":{"name_changes":[]},"next_page_token":null}', id='empty-group'),
        pytest.param(b'{"corporate_actions":{"name_changes":null}}', id='null-group'),
        pytest.param(b' \n{ "corporate_actions" : { } } \r\n\t', id='whitespace-around'),
    ],
)
def test_a_page_with_no_record_is_an_empty_page(body):
    assert parse_corporate_actions_page(body) == CorporateActionsPage(actions=[], next_page_token=None)


def test_a_null_group_is_passed_and_the_groups_beside_it_are_read():
    body = page({'name_changes': None, 'stock_mergers': [record('m-1')], 'spin_offs': None})

    assert [action.vendor_action_id for action in parse_corporate_actions_page(body).actions] == ['m-1']


def test_a_group_nobody_here_has_heard_of_is_carried_like_any_other_with_its_wording_as_the_type():
    body = page({'Some_New-Type ': [record('x-1')]})

    [action] = parse_corporate_actions_page(body).actions

    assert action.action_type == 'Some_New-Type '


def test_unknown_top_level_members_are_passed_over():
    body = b'{"extra":{"corporate_actions":[1]},"corporate_actions":{"g":[{"id":"a"}]},"more":[null]}'

    assert [action.vendor_action_id for action in parse_corporate_actions_page(body).actions] == ['a']


@pytest.mark.parametrize(
    ('token', 'expected'),
    [
        pytest.param('"next-token=="', 'next-token==', id='present'),
        pytest.param('null', None, id='null'),
        pytest.param('""', None, id='empty-string'),
    ],
)
def test_the_page_token_is_the_vendors_string_and_none_when_null_or_empty(token, expected):
    body = f'{{"corporate_actions":{{}},"next_page_token":{token}}}'.encode()

    assert parse_corporate_actions_page(body).next_page_token == expected


def test_an_absent_page_token_is_the_last_page():
    assert parse_corporate_actions_page(b'{"corporate_actions":{}}').next_page_token is None


@pytest.mark.parametrize(
    'body',
    [
        pytest.param(b'{"corporate_actions":{}} x', id='trailing-garbage'),
        pytest.param(b'{"corporate_actions":{}}{}', id='second-object'),
        pytest.param(b'{"corporate_actions":{}},', id='trailing-comma'),
        pytest.param(b'[]', id='array'),
        pytest.param(b'"corporate_actions"', id='string'),
        pytest.param(b'null', id='null'),
        pytest.param(b'', id='empty'),
        pytest.param(b'{"corporate_actions":{}', id='unterminated'),
        pytest.param(b'{"corporate_actions":[]}', id='actions-an-array'),
        pytest.param(b'{"corporate_actions":"x"}', id='actions-a-string'),
        pytest.param(b'{"corporate_actions":{"g":{"id":"a"}}}', id='group-an-object'),
        pytest.param(b'{"corporate_actions":{"g":"a"}}', id='group-a-string'),
        pytest.param(b'{"corporate_actions":{"g":["a"]}}', id='record-a-string'),
        pytest.param(b'{"corporate_actions":{"g":[null]}}', id='record-null'),
        pytest.param(b'{"corporate_actions":{"g":[{"id":123}]}}', id='id-a-number'),
        pytest.param(b'{"corporate_actions":{"g":[{"id":null}]}}', id='id-null'),
        pytest.param(b'{"corporate_actions":{"g":[{"id":""}]}}', id='id-empty'),
        pytest.param(b'{"corporate_actions":{"g":[{"symbol":"A"}]}}', id='id-absent'),
        pytest.param(b'{"corporate_actions":{},"next_page_token":7}', id='token-a-number'),
        pytest.param(b'{"corporate_actions":{},"next_page_token":["t"]}', id='token-an-array'),
        pytest.param(b'{"corporate_actions":{"g":[{"id":"a"}]} "x":1}', id='missing-comma'),
        pytest.param(b'\xff{"corporate_actions":{}}', id='not-utf8'),
    ],
)
def test_a_body_that_is_not_one_corporate_actions_page_is_vendor_corruption_raised_loud(body):
    with pytest.raises(ValueError):
        parse_corporate_actions_page(body)


def test_the_corruption_message_carries_no_vendor_body():
    with pytest.raises(ValueError) as raised:
        parse_corporate_actions_page(b'{"corporate_actions":{"g":[{"id":"a","secret_looking":"s3cr3t"}, 5]}}')

    assert 's3cr3t' not in str(raised.value)


DATE_FIELDS = ['ex_date', 'record_date', 'payable_date', 'process_date', 'effective_date']


@pytest.mark.parametrize('name', DATE_FIELDS)
def test_a_date_the_vendor_gave_is_midnight_utc_of_that_day_and_every_other_date_is_none(name):
    action = action_from_record('g', record(**{name: '2021-07-01'}), b'{}')

    for other in DATE_FIELDS:
        expected = midnight(2021, 7, 1) if other == name else None
        assert getattr(action, other) == expected, other
    assert getattr(action, name).tzinfo is UTC


@pytest.mark.parametrize('name', DATE_FIELDS)
def test_a_null_date_is_none(name):
    assert getattr(action_from_record('g', record(**{name: None}), b'{}'), name) is None


@pytest.mark.parametrize('name', DATE_FIELDS)
@pytest.mark.parametrize(
    'value',
    [
        pytest.param('2022-13-01', id='no-such-month'),
        pytest.param('2022-02-30', id='no-such-day'),
        pytest.param('2022/02/14', id='slashes'),
        pytest.param('2022-2-14', id='unpadded'),
        pytest.param('2022-02-14T00:00:00Z', id='an-instant'),
        pytest.param('', id='empty'),
        pytest.param(20220214, id='a-number'),
        pytest.param(['2022-02-14'], id='a-list'),
    ],
)
def test_a_malformed_date_is_vendor_corruption_raised_loud_never_a_guessed_day(name, value):
    with pytest.raises(ValueError, match=name):
        action_from_record('g', record(**{name: value}), b'{}')


@pytest.mark.parametrize(
    'value',
    [
        # date.fromisoformat on Python 3.12 accepts these two, so only the shape check refuses them.
        pytest.param('20220214', id='basic-format'),
        pytest.param('2022-W07-1', id='week-date'),
        # Digits outside ASCII: the shape is ASCII digits only.
        # Built by code point shift (FULLWIDTH DIGIT ZERO is U+FF10, ARABIC-INDIC DIGIT ZERO is U+0660).
        pytest.param(
            '2022-02-14'.translate({ord(d): ord(d) - ord('0') + 0xFF10 for d in '0123456789'}), id='fullwidth'
        ),
        pytest.param(
            '2022-02-14'.translate({ord(d): ord(d) - ord('0') + 0x0660 for d in '0123456789'}), id='arabic-indic'
        ),
        # The whole value must be the day: a trailing newline is not stripped or ignored.
        pytest.param('2022-02-14\n', id='trailing-newline'),
    ],
)
def test_a_date_not_written_yyyy_mm_dd_is_refused(value):
    with pytest.raises(ValueError, match='process_date'):
        action_from_record('g', record(process_date=value), b'{}')


def test_role_named_identifiers_are_kept_under_the_vendors_own_names_and_nothing_else_is():
    vendor = record(
        'r-1',
        symbol='VFV',
        cusip='922908363',
        isin='US9229083632',
        old_symbol='FB',
        new_symbol='META',
        old_cusip='30303M102',
        new_cusip='30303M103',
        acquirer_symbol='AMD',
        acquirer_cusip='007903107',
        acquiree_symbol='XLNX',
        acquiree_cusip='983919101',
        source_symbol='SRC',
        new_isin='US0000000001',
        # Not identifiers: rates, dates, flags, the id, and fields that only look like one.
        acquirer_rate='1.7234',
        cash_rate=2.1,
        process_date='2022-02-14',
        symbols='VFV,XEQT',
        symbology='x',
        cusip_note='y',
        description='a symbol change',
    )

    action = action_from_record('stock_mergers', vendor, b'raw')

    assert action.identifiers == {
        'symbol': 'VFV',
        'cusip': '922908363',
        'isin': 'US9229083632',
        'old_symbol': 'FB',
        'new_symbol': 'META',
        'old_cusip': '30303M102',
        'new_cusip': '30303M103',
        'acquirer_symbol': 'AMD',
        'acquirer_cusip': '007903107',
        'acquiree_symbol': 'XLNX',
        'acquiree_cusip': '983919101',
        'source_symbol': 'SRC',
        'new_isin': 'US0000000001',
    }
    assert action.raw_payload == b'raw'
    assert action.action_type == 'stock_mergers'


@pytest.mark.parametrize(
    'value', [pytest.param('', id='empty-string'), pytest.param(None, id='null'), pytest.param(123, id='a-number')]
)
def test_an_identifier_field_with_no_string_value_is_skipped_and_the_record_still_parses(value):
    action = action_from_record('g', record(old_symbol=value, new_symbol='META', acquirer_cusip=value), b'{}')

    assert action.identifiers == {'new_symbol': 'META'}


def test_the_parsed_action_is_the_whole_neutral_record():
    raw = b'{"id":"x","old_symbol":"A","process_date":"2020-01-02"}'

    assert parse_corporate_actions_page(b'{"corporate_actions":{"name_changes":[' + raw + b']}}').actions == [
        CorporateAction(
            vendor_action_id='x',
            action_type='name_changes',
            raw_payload=raw,
            process_date=midnight(2020, 1, 2),
            identifiers={'old_symbol': 'A'},
        )
    ]


# ---------------------------------------------------------------------------------------------
# The reader: harness
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No credential in the environment, no cached client, no remembered reset."""
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)
    broker_api.set_corporate_actions_client(None)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()
    yield
    broker_api.set_corporate_actions_client(None)
    broker_api.set_trading_client(None)
    broker_api.forget_resets()


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        yield pool


def reader(executor, client=None, *, scheduler=None, trading_client=None, clock=lambda: NOW) -> AlpacaRead:
    """A real AlpacaRead on the production scope names, reading the given corporate-actions client."""
    return AlpacaRead(
        executor_provider=lambda: executor,
        clock=clock,
        scheduler=scheduler if scheduler is not None else RecordingScheduler(),
        scope=ALPACA_DATA_SCOPE,
        trading_client=trading_client,
        corporate_actions_client=client,
        trading_scope=ALPACA_TRADING_SCOPE,
    )


def query(symbol: str = 'SIVB', start: datetime = START, end: datetime = END, **overrides) -> CorporateActionsQuery:
    fields = {'priority': RequestPriority.BACKFILL, 'deadline': DEADLINE, **overrides}
    return CorporateActionsQuery(symbol, start, end, **fields)


def failure(outcome) -> Exception:
    assert isinstance(outcome, CorporateActionsFailure), f'expected a CorporateActionsFailure, got {outcome!r}'
    return outcome.error


# The production request's parameters for SIVB over START..END, as the s10x capture sent them.
SIVB_PARAMS = {'symbols': 'SIVB', 'start': '2015-01-01', 'end': '2026-10-24', 'sort': 'asc', 'limit': '1000'}


# ---------------------------------------------------------------------------------------------
# The reader: the request, the window, the answer
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_read_is_one_get_on_the_data_host_v1_with_the_symbol_the_days_ascending_and_no_type_filter(executor):
    vendor = stub_corporate_actions_vendor(load_raw('corporate_actions_sivb_untyped'))

    await reader(executor, vendor.client).get_corporate_actions(query())

    [sent] = vendor.sent
    assert (sent.method, sent.host, sent.path) == ('GET', ALPACA_DATA_HOST, CORPORATE_ACTIONS_PATH)
    # Exactly the parameters the s10x capture sent: no types (the store decides which it wants), no page_token.
    assert sent.params == SIVB_PARAMS
    assert 'types' not in sent.params


@pytest.mark.asyncio
async def test_a_recorded_answer_is_every_action_raw_stamped_with_the_readers_clock(executor):
    recorded = load_raw('corporate_actions_sivb_untyped')
    vendor = stub_corporate_actions_vendor(recorded)

    outcome = await reader(executor, vendor.client).get_corporate_actions(query())

    assert isinstance(outcome, CorporateActionsResponse)
    assert outcome.as_of == NOW
    [action] = outcome.actions
    assert action.vendor_action_id == '69dbd57d-28b8-41bb-bb0b-c34e9409ba62'
    assert action.action_type == 'stock_and_cash_mergers'
    assert action.raw_payload in recorded.body
    assert json.loads(action.raw_payload) == json.loads(recorded.body)['corporate_actions']['stock_and_cash_mergers'][0]
    # The symbol asked for matched the ACQUIRER side; the other side is carried as given, nothing is filtered.
    assert action.identifiers['acquirer_symbol'] == 'SIVB'
    assert action.identifiers['acquiree_symbol'] == 'BPFH'
    assert action.effective_date == action.process_date == midnight(2021, 7, 1)


@pytest.mark.asyncio
async def test_an_empty_window_is_served_with_no_actions(executor):
    vendor = stub_corporate_actions_vendor(load_raw('corporate_actions_empty'))

    outcome = await reader(executor, vendor.client).get_corporate_actions(query('ABMD'))

    assert outcome == CorporateActionsResponse(actions=[], as_of=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('start', 'end', 'sent_start', 'sent_end'),
    [
        pytest.param(START, END, '2015-01-01', '2026-10-24', id='utc-midnights'),
        # The start is its UTC day rounded DOWN; the end its UTC day rounded UP (its own day only when it is exactly
        # midnight UTC). The vendor's end-day treatment is unconfirmed, so the read is a superset either way
        # (tj-7zjov1.154, ruling on tj-7zjov1.23).
        pytest.param(
            midnight(2026, 1, 1), midnight(2026, 2, 1), '2026-01-01', '2026-02-01', id='midnight-end-sends-its-own-day'
        ),
        pytest.param(
            midnight(2026, 1, 1),
            datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
            '2026-01-01',
            '2026-02-02',
            id='mid-day-end-sends-the-next-day',
        ),
        pytest.param(
            midnight(2026, 1, 1),
            datetime(2026, 2, 1, 0, 0, 0, 1, tzinfo=UTC),
            '2026-01-01',
            '2026-02-02',
            id='one-microsecond-past-midnight-sends-the-next-day',
        ),
        pytest.param(
            datetime(2026, 3, 5, 9, 30, tzinfo=UTC),
            datetime(2026, 3, 5, 16, 0, tzinfo=UTC),
            '2026-03-05',
            '2026-03-06',
            id='within-one-day',
        ),
        # Days are UTC days, converted BEFORE rounding: 20:00 at UTC-5 is the next UTC day (start, floor); 01:00 at
        # UTC+3 is 22:00 UTC the previous day, rounded up to the local day.
        pytest.param(
            datetime(2026, 1, 1, 20, 0, tzinfo=timezone(timedelta(hours=-5))),
            datetime(2026, 1, 10, 1, 0, tzinfo=timezone(timedelta(hours=3))),
            '2026-01-02',
            '2026-01-10',
            id='offsets-read-as-utc',
        ),
        # Local midnight at UTC-5 is 05:00 UTC: not a UTC midnight, so the next UTC day.
        pytest.param(
            midnight(2026, 1, 1),
            datetime(2026, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=-5))),
            '2026-01-01',
            '2026-01-11',
            id='local-midnight-end-is-not-a-utc-midnight',
        ),
        # 03:00 at UTC+3 IS midnight UTC: its own UTC day, not the next.
        pytest.param(
            midnight(2026, 1, 1),
            datetime(2026, 1, 10, 3, 0, tzinfo=timezone(timedelta(hours=3))),
            '2026-01-01',
            '2026-01-10',
            id='offset-end-at-utc-midnight-sends-its-own-day',
        ),
    ],
)
async def test_the_window_is_sent_as_the_utc_days_of_its_start_and_its_end(executor, start, end, sent_start, sent_end):
    vendor = stub_corporate_actions_vendor(load_raw('corporate_actions_empty'))

    await reader(executor, vendor.client).get_corporate_actions(query('VFV', start, end))

    [sent] = vendor.sent
    assert (sent.params['start'], sent.params['end']) == (sent_start, sent_end)


@pytest.mark.asyncio
async def test_nothing_the_vendor_served_is_filtered_by_the_window_or_the_symbol(executor):
    """The store judges: a record dated outside the window, or naming other symbols only, is carried."""
    body = page({'name_changes': [record('n-1', old_symbol='X', new_symbol='Y', process_date='1999-01-01')]})
    vendor = stub_corporate_actions_vendor(raw_answer(body))

    outcome = await reader(executor, vendor.client).get_corporate_actions(query('VFV'))

    assert [action.vendor_action_id for action in outcome.actions] == ['n-1']


# ---------------------------------------------------------------------------------------------
# The reader: paging, pacing and the tally
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pages_are_followed_to_the_end_each_sending_the_last_token_with_the_same_parameters(executor):
    vendor = stub_corporate_actions_vendor(
        page_answer({'name_changes': [record('p1-a'), record('p1-b')]}, 'tok-1'),
        page_answer({'stock_mergers': [record('p2-a')]}, 'tok-2'),
        page_answer({'name_changes': [record('p3-a')]}, None),
    )

    outcome = await reader(executor, vendor.client).get_corporate_actions(query())

    assert [action.vendor_action_id for action in outcome.actions] == ['p1-a', 'p1-b', 'p2-a', 'p3-a']
    assert [sent.params for sent in vendor.sent] == [
        SIVB_PARAMS,
        {**SIVB_PARAMS, 'page_token': 'tok-1'},
        {**SIVB_PARAMS, 'page_token': 'tok-2'},
    ]


@pytest.mark.asyncio
async def test_an_empty_string_token_ends_the_read(executor):
    vendor = stub_corporate_actions_vendor(page_answer({'g': [record('a')]}, ''))

    outcome = await reader(executor, vendor.client).get_corporate_actions(query())

    assert [action.vendor_action_id for action in outcome.actions] == ['a']
    assert len(vendor.sent) == 1


@pytest.mark.asyncio
async def test_every_page_acquires_once_on_the_data_scope_under_the_querys_priority_and_deadline(executor):
    pacing = RecordingScheduler()
    trading = stub_trading_vendor()
    vendor = stub_corporate_actions_vendor(page_answer({}, 'tok-1'), page_answer({}, None))

    await reader(executor, vendor.client, scheduler=pacing, trading_client=trading.client).get_corporate_actions(
        query(priority=RequestPriority.INTERACTIVE)
    )

    assert pacing.acquires == [Acquire(ALPACA_DATA_SCOPE, RequestPriority.INTERACTIVE, DEADLINE)] * 2
    assert trading.sent == [], 'the trading host was reached'


@pytest.mark.asyncio
async def test_the_tally_counts_every_call_and_every_page(executor):
    vendor = stub_corporate_actions_vendor(page_answer({}, 'tok-1'), page_answer({}, 'tok-2'), page_answer({}))
    tally = CallTally()

    await reader(executor, vendor.client).get_corporate_actions(query(tally=tally))

    assert (tally.calls, tally.pages, tally.throttled_429, tally.retries) == (3, 3, 0, 0)
    assert tally.first_call_at is not None and tally.last_call_at >= tally.first_call_at


@pytest.mark.asyncio
async def test_a_failure_on_page_two_fails_the_whole_read_and_never_returns_the_first_page(executor):
    vendor = stub_corporate_actions_vendor(page_answer({'g': [record('p1')]}, 'tok-1'), status_answer(503))
    tally = CallTally()

    outcome = await reader(executor, vendor.client).get_corporate_actions(query(tally=tally))

    assert failure(outcome).reason is Reason.VENDOR_UNAVAILABLE
    assert (tally.calls, tally.pages) == (2, 1)
    assert len(vendor.sent) == 2, 'the failed page was retried by the reader'


# ---------------------------------------------------------------------------------------------
# The reader: typed failures
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_429_is_rate_limited_with_the_reset_the_data_host_named_remembered_for_the_data_scope(executor):
    reset = NOW + timedelta(seconds=42)
    vendor = stub_corporate_actions_vendor(
        status_answer(429, headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())})
    )
    tally = CallTally()

    outcome = await reader(executor, vendor.client).get_corporate_actions(query(tally=tally))

    error = failure(outcome)
    assert error.reason is Reason.VENDOR_RATE_LIMITED
    assert error.reset_at == reset
    assert isinstance(error.__cause__, APIError)
    assert (tally.calls, tally.pages, tally.throttled_429) == (1, 0, 1)
    assert broker_api.last_reset(ALPACA_DATA_SCOPE, NOW) == reset
    assert broker_api.last_reset(ALPACA_TRADING_SCOPE, NOW) is None, 'a data answer moved the trading scope reset'


@pytest.mark.asyncio
async def test_a_header_less_429_waits_the_data_scopes_declared_window(executor):
    vendor = stub_corporate_actions_vendor(status_answer(429))
    pacing = RecordingScheduler(declared_window_seconds=60.0)

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_corporate_actions(query())

    assert failure(outcome).reset_at == NOW + timedelta(seconds=60)
    assert pacing.windows == [ALPACA_DATA_SCOPE]


@pytest.mark.asyncio
async def test_a_header_less_429_on_page_two_waits_for_the_reset_page_one_named(executor):
    reset = NOW + timedelta(seconds=37)
    vendor = stub_corporate_actions_vendor(
        raw_answer(page({}, 'tok-1'), headers={RATE_LIMIT_RESET_HEADER: str(reset.timestamp())}), status_answer(429)
    )

    outcome = await reader(executor, vendor.client).get_corporate_actions(query())

    assert failure(outcome).reset_at == reset


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('answer', 'reason'),
    [
        pytest.param(status_answer(401), Reason.VENDOR_AUTH, id='401'),
        pytest.param(status_answer(403), Reason.VENDOR_AUTH, id='403'),
        pytest.param(status_answer(500), Reason.VENDOR_UNAVAILABLE, id='500'),
        pytest.param(status_answer(502), Reason.VENDOR_UNAVAILABLE, id='502'),
        pytest.param(status_answer(503), Reason.VENDOR_UNAVAILABLE, id='503'),
        pytest.param(requests.exceptions.ConnectionError('down'), Reason.VENDOR_UNAVAILABLE, id='connection'),
        pytest.param(requests.exceptions.ReadTimeout('slow'), Reason.VENDOR_UNAVAILABLE, id='timeout'),
        pytest.param(requests.exceptions.ChunkedEncodingError('cut'), Reason.VENDOR_UNAVAILABLE, id='cut'),
    ],
)
async def test_a_vendor_that_refuses_or_cannot_answer_is_a_typed_failure_with_its_cause_counted_once(
    executor, answer, reason
):
    vendor = stub_corporate_actions_vendor(answer)
    tally = CallTally()

    outcome = await reader(executor, vendor.client).get_corporate_actions(query(tally=tally))

    error = failure(outcome)
    assert error.reason is reason
    assert error.__cause__ is not None
    if isinstance(answer, BaseException):
        assert error.__cause__ is answer
    else:
        assert error.__cause__.status_code == answer.status
    assert (tally.calls, tally.pages) == (1, 0)
    assert len(vendor.sent) == 1, 'the reader retried a page'


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
async def test_a_page_whose_data_token_misses_the_deadline_is_rate_budget_with_no_request(
    executor, scheduler_never_sleeps
):
    """The data scope's own budget gates every page; the trading scope is full and is never asked."""
    scheduler = real_scheduler(
        {ALPACA_DATA_SCOPE: ONE_PER_SECOND, ALPACA_TRADING_SCOPE: ONE_PER_SECOND},
        clock=lambda: NOW,
        budget_clock=Frozen(),
    )
    vendor = stub_corporate_actions_vendor(page_answer({'g': [record('p1')]}, 'tok-1'))
    tally = CallTally()

    outcome = await reader(executor, vendor.client, scheduler=scheduler).get_corporate_actions(
        query(deadline=NOW + timedelta(milliseconds=500), tally=tally)
    )

    assert failure(outcome).reason is Reason.RATE_BUDGET
    assert len(vendor.sent) == 1, 'page two was asked for without a token'
    assert (tally.calls, tally.pages) == (1, 1)


@pytest.mark.asyncio
async def test_without_credentials_a_read_is_vendor_auth_with_no_acquire_and_no_request(executor):
    pacing = RecordingScheduler()

    outcome = await reader(executor, None, scheduler=pacing).get_corporate_actions(query())

    error = failure(outcome)
    assert type(error) is MissingCredentialsError
    assert error.reason is Reason.VENDOR_AUTH
    assert pacing.acquires == []


@pytest.mark.asyncio
@pytest.mark.parametrize('which', ['key', 'secret'])
async def test_with_malformed_credentials_a_read_is_vendor_auth_with_no_acquire(executor, monkeypatch, which):
    # get_client registers each value with the process-wide log scrub before it checks the shape, and nothing
    # unregisters it. So the malformed values are made-up and long, never a short word another test's text may hold.
    malformed_key, malformed_secret = 'zzCorporateActionsMadeUp key', 'zzCorporateActionsMadeUpSecret\n'
    key, secret = (malformed_key, PLACEHOLDER_SECRET) if which == 'key' else (PLACEHOLDER_KEY_ID, malformed_secret)
    monkeypatch.setenv('ALPACA_API_KEY', key)
    monkeypatch.setenv('ALPACA_API_SECRET', secret)
    pacing = RecordingScheduler()

    outcome = await reader(executor, None, scheduler=pacing).get_corporate_actions(query())

    error = failure(outcome)
    assert type(error) is MalformedCredentialsError
    assert error.reason is Reason.VENDOR_AUTH
    assert pacing.acquires == []


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['', '   '], ids=['empty', 'blank'])
async def test_a_read_with_no_symbol_is_refused_before_any_acquire_or_request(executor, symbol):
    pacing = RecordingScheduler()
    vendor = stub_corporate_actions_vendor()

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_corporate_actions(query(symbol))

    error = failure(outcome)
    assert type(error) is InvalidRequestError
    assert error.reason is Reason.INVALID_REQUEST
    assert pacing.acquires == []
    assert vendor.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'end', [pytest.param(START, id='end-at-start'), pytest.param(START - timedelta(seconds=1), id='end-before-start')]
)
async def test_a_window_that_does_not_end_after_it_starts_is_refused_before_any_acquire_or_request(executor, end):
    pacing = RecordingScheduler()
    vendor = stub_corporate_actions_vendor()

    outcome = await reader(executor, vendor.client, scheduler=pacing).get_corporate_actions(query(end=end))

    error = failure(outcome)
    assert type(error) is InvalidRequestError
    assert error.reason is Reason.INVALID_REQUEST
    assert pacing.acquires == []
    assert vendor.sent == []


# ---------------------------------------------------------------------------------------------
# The reader: the progress guard and corruption raise
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repeated_page_token_raises_rather_than_loop_on_the_quota(executor):
    vendor = stub_corporate_actions_vendor(
        page_answer({}, 'tok-a'), page_answer({}, 'tok-b'), page_answer({}, 'tok-a'), request_cap=3
    )

    with pytest.raises(ValueError, match='repeated'):
        await reader(executor, vendor.client).get_corporate_actions(query())

    assert [sent.params.get('page_token') for sent in vendor.sent] == [None, 'tok-a', 'tok-b']


@pytest.mark.asyncio
async def test_a_vendor_that_never_ends_raises_at_the_page_cap(executor, monkeypatch):
    monkeypatch.setattr(broker_api, 'CORPORATE_ACTIONS_MAX_PAGES', 3)
    vendor = stub_corporate_actions_vendor(*(page_answer({}, f'tok-{n}') for n in range(3)), request_cap=3)

    with pytest.raises(ValueError, match='more than 3'):
        await reader(executor, vendor.client).get_corporate_actions(query())

    assert len(vendor.sent) == 3


def test_the_page_cap_and_page_size_are_the_vendors_limit_and_a_thousand_pages():
    assert broker_api.CORPORATE_ACTIONS_PAGE_SIZE == 1000
    assert broker_api.CORPORATE_ACTIONS_MAX_PAGES == 1000


@pytest.mark.asyncio
async def test_a_body_that_is_not_a_corporate_actions_page_raises_out_of_the_read(executor):
    vendor = stub_corporate_actions_vendor(raw_answer(b'{"corporate_actions": []}'))

    with pytest.raises(ValueError, match='not valid JSON of the expected shape'):
        await reader(executor, vendor.client).get_corporate_actions(query())


# ---------------------------------------------------------------------------------------------
# The client: built lazily on the data host, from the checked credentials
# ---------------------------------------------------------------------------------------------


def test_the_corporate_actions_client_is_never_built_without_credentials():
    with pytest.raises(MissingCredentialsError):
        broker_api.get_corporate_actions_client()


def test_the_corporate_actions_client_is_built_once_on_the_data_host_v1_and_cleared_by_the_seam(monkeypatch):
    monkeypatch.setenv('ALPACA_API_KEY', PLACEHOLDER_KEY_ID)
    monkeypatch.setenv('ALPACA_API_SECRET', PLACEHOLDER_SECRET)

    client = broker_api.get_corporate_actions_client()

    assert broker_api.get_corporate_actions_client() is client
    assert client._base_url == f'https://{ALPACA_DATA_HOST}'
    assert client._api_version == 'v1'
    broker_api.set_corporate_actions_client(None)
    assert broker_api.get_corporate_actions_client() is not client


@pytest.mark.asyncio
async def test_the_reader_uses_the_processs_own_client_when_built_without_one(executor):
    vendor = stub_corporate_actions_vendor(load_raw('corporate_actions_empty'))
    broker_api.set_corporate_actions_client(vendor.client)

    outcome = await reader(executor, None).get_corporate_actions(query())

    assert isinstance(outcome, CorporateActionsResponse)
    assert len(vendor.sent) == 1
