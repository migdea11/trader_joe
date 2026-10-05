"""classify_vendor_error: Alpaca's failures, classified on the HTTP status alone (TE-4 tj-3mk3u5.37.5 item 4).

Design: ADR tj-fa1rpu D3, D5, D8 and its 2026-10-02 addenda (Q-EMPTY item 3: the 400 is
VENDOR_INVALID_REQUEST, never VENDOR_REJECTED; 16:22 UTC item 5: the caught exception is the returned
error's __cause__); the TE-1 gate rulings on tj-3mk3u5.37.3 (detail is ours, never str() of an
APIError; a REFUSED reason carries no reset_at; the two rate limits always do); tj-vz1eta s3 (a broker
rate limit says WHEN its window resets).

EVERY APIError HERE IS BUILT THE WAY alpaca-py 0.44.0 BUILDS ONE (common/rest.py _one_request): a
requests.Response with a status and a body, raise_for_status(), and APIError(response.text,
http_error). So status_code, message and response.headers are the SDK's own properties over a real
response, not stubs of them. The bodies are the recorded and documented fixtures under
fixtures/alpaca/, loaded through the harness: error_400.json is RECORDED ({"message": ...} and no
code field); error_429/500/504.json are DOCUMENTED guesses. Which headers a real 429 carries is not
known (no recording exists), so the header precedence is pinned on constructed headers and named as
such.

The reader's path through the real SDK (retries, the transport, a BarsFailure out of get_bars) is
the recorded-Alpaca suite's (TE-5); this file pins the classifier's own table.
"""

import json
import math
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http import HTTPStatus
from typing import Any

import pytest
import requests
from alpaca.common.exceptions import APIError
from requests.structures import CaseInsensitiveDict

from common.errors.vocabulary import (
    REASONS,
    Disposition,
    ExogenousError,
    InvalidRequestError,
    Outcome,
    Reason,
    TraderJoeError,
)
from data.ingest.app.brokers.alpaca.classify import (
    RATE_LIMIT_RESET_HEADER,
    RETRY_AFTER_HEADER,
    VENDOR_FAILURES,
    classify_vendor_error,
)
from data.ingest.app.brokers.rate_budget import RateBudget
from data.ingest.tests.alpaca_recorded import load


pytestmark = pytest.mark.data_ingest

# The reader's clock, fixed: every reset_at and retry_after below is measured from it.
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

# A marker no detail and no metadata value may ever contain: it travels in the vendor's body beside
# the message, so it reaches a detail only if the raw body (str(APIError)) does.
RAW_BODY_MARKER = 'RAW-VENDOR-BODY-7f3c'

# The detail the classifier writes when a 4xx body has no readable message, for a given status.
NO_MESSAGE = 'Alpaca answered HTTP {status} without a readable message.'


def clock() -> datetime:
    return NOW


def api_error(status: int, body: Any = None, *, headers: Mapping[str, str] | None = None, raw: bytes | None = None):
    """Build the APIError alpaca-py raises for an error answer, exactly as its _one_request does.

    Args:
        status (int): HTTP status of the answer.
        body (Any): JSON body, encoded as the vendor would send it. Ignored when raw is given.
        headers (Mapping[str, str] | None): Response headers beside Content-Type.
        raw (bytes | None): A body that is not JSON at all.

    Returns:
        APIError: The SDK's error, carrying the response.
    """
    response = requests.Response()
    response.status_code = status
    try:
        response.reason = HTTPStatus(status).phrase
    except ValueError:
        response.reason = 'Unassigned'
    response._content = raw if raw is not None else json.dumps(body).encode()
    response.headers = CaseInsensitiveDict({'Content-Type': 'application/json', **(headers or {})})
    response.encoding = 'utf-8'
    response.url = 'https://data.alpaca.markets/v2/stocks/bars'
    try:
        response.raise_for_status()
    except requests.HTTPError as http_error:
        return APIError(response.text, http_error)
    raise AssertionError(f'status {status} did not raise; it is not an error answer')


def recorded(name: str, *, headers: Mapping[str, str] | None = None, marker: bool = True) -> APIError:
    """The APIError for a fixture's status and body, with RAW_BODY_MARKER added to the body."""
    fixture = load(name)
    body = {**fixture.body, 'note': RAW_BODY_MARKER} if marker else fixture.body
    return api_error(fixture.status, body, headers=headers)


def budget(rate_per_sec: float | None = 3.0, burst: float | None = 3.0) -> RateBudget:
    return RateBudget('ALPACA', rate_per_sec=rate_per_sec, burst=burst)


def classify(error: BaseException, rate_budget: RateBudget | None = None) -> TraderJoeError | None:
    return classify_vendor_error(error, rate_budget=rate_budget or budget(), clock=clock)


@pytest.fixture(autouse=True)
def code_is_never_read(monkeypatch) -> Iterator[list[str]]:
    """Fail every test here that reads APIError.code (TE-5 item 3's guard, applied to the whole table).

    Alpaca's error bodies carry no code field: the recorded 400 is {"message": ...} alone, and .code
    json-loads the body and raises KeyError on it. The documented 429/500/504 bodies DO carry a code,
    so on those a read would succeed silently; this property makes it loud on every body.
    """
    reads: list[str] = []

    def refuse(self: APIError) -> None:
        reads.append(self.status_code)
        raise AssertionError('APIError.code was read; classify on status_code only (TE-4 item 4)')

    monkeypatch.setattr(APIError, 'code', property(refuse))
    yield reads
    assert reads == [], f'APIError.code read for statuses {reads}'


# ---------------------------------------------------------------------------------------------
# The status table
# ---------------------------------------------------------------------------------------------

# (status, fixture or None for a constructed {"message": ...} body, reason, branch, outcome, disposition)
STATUS_TABLE = [
    pytest.param(
        400,
        'error_400',
        Reason.VENDOR_INVALID_REQUEST,
        InvalidRequestError,
        Outcome.REFUSED,
        Disposition.CLIENT_FIX,
        id='400-recorded',
    ),
    pytest.param(401, None, Reason.VENDOR_AUTH, ExogenousError, Outcome.NOT_READY, Disposition.PAGE, id='401'),
    pytest.param(403, None, Reason.VENDOR_AUTH, ExogenousError, Outcome.NOT_READY, Disposition.PAGE, id='403'),
    pytest.param(404, None, Reason.VENDOR_REJECTED, ExogenousError, Outcome.REFUSED, Disposition.PAGE, id='404'),
    pytest.param(409, None, Reason.VENDOR_REJECTED, ExogenousError, Outcome.REFUSED, Disposition.PAGE, id='409'),
    pytest.param(422, None, Reason.VENDOR_REJECTED, ExogenousError, Outcome.REFUSED, Disposition.PAGE, id='422'),
    pytest.param(499, None, Reason.VENDOR_REJECTED, ExogenousError, Outcome.REFUSED, Disposition.PAGE, id='499'),
    pytest.param(
        429,
        'error_429',
        Reason.VENDOR_RATE_LIMITED,
        ExogenousError,
        Outcome.NOT_READY,
        Disposition.PAGE,
        id='429-documented',
    ),
    pytest.param(
        500,
        'error_500',
        Reason.VENDOR_UNAVAILABLE,
        ExogenousError,
        Outcome.NOT_READY,
        Disposition.RECORD,
        id='500-documented',
    ),
    pytest.param(502, None, Reason.VENDOR_UNAVAILABLE, ExogenousError, Outcome.NOT_READY, Disposition.RECORD, id='502'),
    pytest.param(503, None, Reason.VENDOR_UNAVAILABLE, ExogenousError, Outcome.NOT_READY, Disposition.RECORD, id='503'),
    pytest.param(
        504,
        'error_504',
        Reason.VENDOR_UNAVAILABLE,
        ExogenousError,
        Outcome.NOT_READY,
        Disposition.RECORD,
        id='504-documented',
    ),
    pytest.param(599, None, Reason.VENDOR_UNAVAILABLE, ExogenousError, Outcome.NOT_READY, Disposition.RECORD, id='599'),
]


def error_for(status: int, fixture: str | None) -> APIError:
    if fixture is not None:
        return recorded(fixture)
    return api_error(status, {'message': f'the vendor says {status}', 'note': RAW_BODY_MARKER})


@pytest.mark.parametrize(('status', 'fixture', 'reason', 'branch', 'outcome', 'disposition'), STATUS_TABLE)
def test_each_status_is_classified_to_its_reason_branch_and_outcome(
    status, fixture, reason, branch, outcome, disposition
):
    error = error_for(status, fixture)
    assert error.status_code == status

    classified = classify(error)

    assert type(classified) is branch
    assert classified.reason is reason
    # The outcome and disposition are read from the one table, never stated again (D4); the expected
    # values are written out here so a row that moved would show.
    assert (REASONS[classified.reason].outcome, REASONS[classified.reason].disposition) == (outcome, disposition)


@pytest.mark.parametrize(('status', 'fixture', 'reason', 'branch', 'outcome', 'disposition'), STATUS_TABLE)
def test_every_classified_error_keeps_the_vendor_error_as_its_cause_and_never_its_body(
    status, fixture, reason, branch, outcome, disposition
):
    """D8 and the 16:22 UTC addendum item 5: the chain goes to the log through __cause__, never the wire."""
    error = error_for(status, fixture)

    classified = classify(error)

    assert classified.__cause__ is error
    assert RAW_BODY_MARKER not in classified.detail
    assert str(error) not in classified.detail
    assert all(RAW_BODY_MARKER not in str(value) for value in classified.metadata.values())
    assert dict(classified.metadata) == {'vendor': 'ALPACA'}


@pytest.mark.parametrize(
    ('status', 'fixture', 'reason', 'branch', 'outcome', 'disposition'),
    [param for param in STATUS_TABLE if param.values[4] is Outcome.REFUSED],
)
def test_a_refused_vendor_answer_carries_no_reset_at(status, fixture, reason, branch, outcome, disposition):
    """TE-1 ruling (b): REFUSED means do not retry unchanged, so no delay is ever attached to one."""
    classified = classify(error_for(status, fixture))

    assert classified.reset_at is None
    assert classified.retry_after is None


def test_the_recorded_400_body_has_no_code_and_its_message_becomes_the_detail():
    # The recorded body is exactly the vendor's: no code field at all.
    assert 'code' not in load('error_400').body
    error = recorded('error_400', marker=False)

    classified = classify(error)

    assert classified.reason is Reason.VENDOR_INVALID_REQUEST
    assert classified.detail == 'end should not be before start'


def test_another_4xx_gives_the_vendors_message_as_its_detail():
    classified = classify(api_error(404, {'message': 'not found: endpoint', 'note': RAW_BODY_MARKER}))

    assert classified.reason is Reason.VENDOR_REJECTED
    assert classified.detail == 'not found: endpoint'


@pytest.mark.parametrize(
    ('body', 'raw'),
    [
        pytest.param(None, b'<html>' + RAW_BODY_MARKER.encode() + b'</html>', id='not-json'),
        pytest.param({'note': RAW_BODY_MARKER}, None, id='no-message'),
        pytest.param([RAW_BODY_MARKER], None, id='a-list'),
        pytest.param(RAW_BODY_MARKER, None, id='a-string'),
        pytest.param({'message': '   ', 'note': RAW_BODY_MARKER}, None, id='blank-message'),
        pytest.param({'message': 42, 'note': RAW_BODY_MARKER}, None, id='non-str-message'),
    ],
)
@pytest.mark.parametrize('status', [400, 404, 422])
def test_a_4xx_without_a_readable_message_gets_a_fixed_sentence_naming_the_status(status, body, raw):
    """TE-4 item 3a: APIError.message json-loads the body; on failure, a fixed sentence, never the body."""
    classified = classify(api_error(status, body, raw=raw))

    assert classified.detail == NO_MESSAGE.format(status=status)
    assert RAW_BODY_MARKER not in classified.detail


@pytest.mark.parametrize('status', [401, 403, 500, 503, 504])
def test_the_detail_of_an_auth_or_server_failure_is_ours_and_names_the_status_only(status):
    classified = classify(api_error(status, {'message': f'secret-ish vendor text {RAW_BODY_MARKER}'}))

    assert f'HTTP {status}' in classified.detail
    assert 'vendor text' not in classified.detail


# ---------------------------------------------------------------------------------------------
# 429: VENDOR_RATE_LIMITED always says when the window resets
# ---------------------------------------------------------------------------------------------


def unix(instant: datetime) -> str:
    return str(instant.timestamp())


def rate_limited(headers: Mapping[str, str] | None = None) -> APIError:
    return recorded('error_429', headers=headers)


def test_x_ratelimit_reset_wins_over_retry_after_and_the_budget():
    reset = NOW + timedelta(seconds=42)

    classified = classify(rate_limited({RATE_LIMIT_RESET_HEADER: unix(reset), RETRY_AFTER_HEADER: '7'}))

    assert classified.reason is Reason.VENDOR_RATE_LIMITED
    assert classified.reset_at == reset
    assert classified.retry_after == 42


def test_the_reset_header_is_read_whatever_its_case():
    # requests keeps headers case-insensitively; the wire may spell the name in lower case.
    reset = NOW + timedelta(seconds=9)

    classified = classify(rate_limited({'x-ratelimit-reset': unix(reset), 'retry-after': '1'}))

    assert classified.reset_at == reset


@pytest.mark.parametrize(
    ('retry_after', 'expected'),
    [
        pytest.param('7', NOW + timedelta(seconds=7), id='whole-seconds'),
        pytest.param('1.5', NOW + timedelta(seconds=1.5), id='fractional-seconds'),
        pytest.param('0', NOW, id='zero'),
        pytest.param(
            format_datetime(NOW + timedelta(minutes=3), usegmt=True), NOW + timedelta(minutes=3), id='http-date'
        ),
    ],
)
def test_without_a_reset_header_retry_after_gives_the_reset(retry_after, expected):
    classified = classify(rate_limited({RETRY_AFTER_HEADER: retry_after}))

    assert classified.reset_at == expected
    assert classified.reset_at.tzinfo is not None


@pytest.mark.parametrize(
    ('rate', 'burst', 'refill_seconds'),
    [pytest.param(3.0, 3.0, 1.0, id='the-deployed-default'), pytest.param(2.0, 10.0, 5.0, id='burst-10-at-2-per-s')],
)
def test_without_either_header_the_reset_is_the_budgets_refill_from_empty(rate, burst, refill_seconds):
    rate_budget = budget(rate, burst)
    assert rate_budget.full_refill_seconds == refill_seconds

    classified = classify(rate_limited(), rate_budget)

    assert classified.reset_at == NOW + timedelta(seconds=refill_seconds)
    assert classified.retry_after == math.ceil(refill_seconds)


def test_an_unconfigured_budget_and_no_header_give_a_reset_of_now():
    # The builder's open point (4) on TE-4: an unthrottled budget models no window and invents none.
    # Pinned as built; reset_at is still always present, so the rate-limit error can be built.
    rate_budget = budget(None, None)
    assert rate_budget.full_refill_seconds == 0.0

    classified = classify(rate_limited(), rate_budget)

    assert classified.reset_at == NOW
    assert classified.retry_after == 0


@pytest.mark.parametrize(
    ('headers', 'expected'),
    [
        pytest.param(
            {RATE_LIMIT_RESET_HEADER: 'soon', RETRY_AFTER_HEADER: '7'},
            NOW + timedelta(seconds=7),
            id='unreadable-reset-falls-to-retry-after',
        ),
        pytest.param(
            {RATE_LIMIT_RESET_HEADER: '1e20', RETRY_AFTER_HEADER: '7'},
            NOW + timedelta(seconds=7),
            id='out-of-range-reset-falls-to-retry-after',
        ),
        pytest.param({RETRY_AFTER_HEADER: 'later'}, NOW + timedelta(seconds=1), id='unreadable-retry-after'),
        pytest.param({RETRY_AFTER_HEADER: '-5'}, NOW + timedelta(seconds=1), id='negative-retry-after'),
        pytest.param({RETRY_AFTER_HEADER: 'nan'}, NOW + timedelta(seconds=1), id='nan-retry-after'),
        pytest.param({RETRY_AFTER_HEADER: 'inf'}, NOW + timedelta(seconds=1), id='infinite-retry-after'),
        pytest.param({RETRY_AFTER_HEADER: '1e300'}, NOW + timedelta(seconds=1), id='overflowing-retry-after'),
    ],
)
def test_a_header_that_cannot_be_read_falls_through_to_the_next_source(headers, expected):
    # The deployed default budget (3/s, burst 3) refills in 1 s: the last source.
    classified = classify(rate_limited(headers))

    assert classified.reset_at == expected


def test_the_rate_limit_error_derives_retry_after_from_the_readers_clock():
    """retry_after is derived on every read through the clock the error was built with, never stored."""
    moving = [NOW]
    classified = classify_vendor_error(
        rate_limited({RETRY_AFTER_HEADER: '30'}), rate_budget=budget(), clock=lambda: moving[0]
    )
    assert classified.retry_after == 30

    moving[0] = NOW + timedelta(seconds=20)

    assert classified.retry_after == 10
    assert classified.reset_at == NOW + timedelta(seconds=30)


# ---------------------------------------------------------------------------------------------
# Connection failures, and what is not classified at all
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'failure',
    [
        pytest.param(requests.exceptions.ConnectionError(f'refused {RAW_BODY_MARKER}'), id='connection-error'),
        pytest.param(requests.exceptions.ConnectTimeout(f'connect {RAW_BODY_MARKER}'), id='connect-timeout'),
        pytest.param(requests.exceptions.ReadTimeout(f'read {RAW_BODY_MARKER}'), id='read-timeout'),
        pytest.param(requests.exceptions.SSLError(f'tls {RAW_BODY_MARKER}'), id='ssl-error'),
    ],
)
def test_a_connection_error_or_timeout_is_vendor_unavailable_with_its_cause(failure):
    classified = classify(failure)

    assert type(classified) is ExogenousError
    assert classified.reason is Reason.VENDOR_UNAVAILABLE
    assert classified.__cause__ is failure
    assert RAW_BODY_MARKER not in classified.detail
    assert classified.reset_at is None


@pytest.mark.parametrize(
    'failure',
    [
        pytest.param(ValueError('a bug'), id='value-error'),
        pytest.param(AttributeError("'NoneType' object has no attribute 'items'"), id='attribute-error'),
        pytest.param(KeyError('code'), id='key-error'),
        pytest.param(RuntimeError('a bug'), id='runtime-error'),
    ],
)
def test_what_is_not_a_vendor_failure_is_not_classified(failure):
    """D5: a bug is never converted to a reason; the caller re-raises a None."""
    assert classify(failure) is None
    assert failure.__cause__ is None


def test_an_api_error_with_no_http_status_is_not_classified():
    # alpaca-py builds APIError(text) without an http_error for a few SDK-side failures; with no status
    # nothing can name the failure, so it is left to propagate as a bug.
    error = APIError('{"message": "no status here"}')
    assert error.status_code is None

    assert classify(error) is None


def test_vendor_failures_names_exactly_what_the_reader_converts():
    # The reader catches exactly this tuple around the vendor call (never a broad except, D6).
    assert (APIError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) == VENDOR_FAILURES
