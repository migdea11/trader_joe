"""Alpaca's failures, classified into the typed errors a BarsFailure carries (ADR tj-fa1rpu D1(a), D3, D8).

CLASSIFIED ON THE HTTP STATUS ALONE (APIError.status_code). Alpaca's error bodies carry no `code` field:
the recorded 400, data/ingest/tests/fixtures/alpaca/error_400.json, is {"message": "end should not be
before start"} and nothing else. APIError.code json-loads the body and raises KeyError on one like that,
so it is NEVER read here. The message is human text for a detail and is never parsed.

The 429, 500 and 504 fixtures are documented guesses, not recordings. What a real 429 carries, a reset
header or a Retry-After, is therefore unknown, which is why _rate_limited tries each in turn.

THE DETAIL IS OURS (D8). It is never str() of an APIError, which IS the raw vendor body, and never a
credential or an account identifier. The one vendor-supplied sentence that reaches a detail is
APIError.message on a 400 or another 4xx, where the vendor says what was wrong with the request.

THE CAUSE CHAIN (D8, the 16:22 UTC 2026-10-02 addendum, item 5): the caught exception becomes the returned
error's __cause__, the returned form of 'raise ... from e', so the edge that renders it can log the chain
under the error_id it mints. It is never put in a detail or in metadata.
"""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Final

from alpaca.common.exceptions import APIError
from requests import exceptions as requests_exceptions

from common.enums.data_stock import DataSource
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason, TraderJoeError
from data.ingest.app.brokers.rate_budget import RateBudget


# What the vendor's client raises that means the vendor could not be served, and nothing else. A
# connection error or a timeout comes out of alpaca-py's HTTP session unwrapped; an error status comes out
# as APIError, after the SDK has already retried a 429 and a 504 itself.
VENDOR_FAILURES: Final = (APIError, requests_exceptions.ConnectionError, requests_exceptions.Timeout)

# Unix seconds, the instant the rate-limit window resets.
RATE_LIMIT_RESET_HEADER: Final = 'X-RateLimit-Reset'
# Seconds to wait, or an HTTP-date (RFC 9110 s10.2.3).
RETRY_AFTER_HEADER: Final = 'Retry-After'

_VENDOR: Final = {'vendor': DataSource.ALPACA_API.value}


def classify_vendor_error(
    error: BaseException, *, rate_budget: RateBudget, clock: Callable[[], datetime]
) -> TraderJoeError | None:
    """Turn a failure of the vendor's client into the typed error it stands for.

    By status: 400 is VENDOR_INVALID_REQUEST (refused: Alpaca names a parameter invalid, such as an end
    before its start), 401 and 403 are VENDOR_AUTH, 429 is VENDOR_RATE_LIMITED with a reset_at, any other 4xx
    is VENDOR_REJECTED, and 5xx is VENDOR_UNAVAILABLE, as is a connection error or a timeout.

    Args:
        error (BaseException): What the vendor's client raised.
        rate_budget (RateBudget): This vendor's budget, whose refill time stands in for a 429's reset when
            the response names none.
        clock (Callable[[], datetime]): The reader's clock: now for a reset_at, and the clock the error
            derives retry_after from.

    Returns:
        TraderJoeError | None: The error, with `error` as its __cause__, or None when this cannot say what
            the failure was: not a vendor failure, or an APIError that carries no HTTP status. The caller
            re-raises it, because a failure nobody can classify is a bug (D5), loud and never a false
            answer.
    """
    classified: TraderJoeError | None
    if isinstance(error, APIError):
        classified = _classify_status(error, rate_budget, clock)
    elif isinstance(error, requests_exceptions.ConnectionError | requests_exceptions.Timeout):
        classified = ExogenousError(
            Reason.VENDOR_UNAVAILABLE, 'Alpaca could not be reached, or did not answer in time.', metadata=_VENDOR
        )
    else:
        classified = None
    if classified is not None:
        classified.__cause__ = error
    return classified


def _classify_status(error: APIError, rate_budget: RateBudget, clock: Callable[[], datetime]) -> TraderJoeError | None:
    status = error.status_code
    if status is None:
        return None
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return _rate_limited(error, rate_budget, clock)
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return ExogenousError(
            Reason.VENDOR_AUTH,
            f'Alpaca refused the credentials or entitlement this deployment holds: HTTP {status}.',
            metadata=_VENDOR,
        )
    if status == HTTPStatus.BAD_REQUEST:
        return InvalidRequestError(Reason.VENDOR_INVALID_REQUEST, _vendor_message(error, status), metadata=_VENDOR)
    if 400 <= status < 500:
        return ExogenousError(Reason.VENDOR_REJECTED, _vendor_message(error, status), metadata=_VENDOR)
    if 500 <= status < 600:
        return ExogenousError(Reason.VENDOR_UNAVAILABLE, f'Alpaca failed on its side: HTTP {status}.', metadata=_VENDOR)
    return None


def _vendor_message(error: APIError, status: int) -> str:
    """Give the message Alpaca sent as the detail, or a fixed sentence naming the status.

    APIError.message json-loads the body, so it raises when the body is not JSON or has no 'message'.
    Exactly those three failures fall back; anything else is a bug and propagates.
    """
    try:
        message = error.message
    except (KeyError, TypeError, ValueError):
        message = None
    if isinstance(message, str) and message.strip():
        return message
    return f'Alpaca answered HTTP {status} without a readable message.'


def _rate_limited(error: APIError, rate_budget: RateBudget, clock: Callable[[], datetime]) -> ExogenousError:
    """Build VENDOR_RATE_LIMITED, which always says when the window resets (the error refuses to build without).

    The reset comes from the first source that gives one: the response's X-RateLimit-Reset (Unix seconds),
    else its Retry-After, else the time this vendor's own RateBudget needs to refill completely.
    """
    now = clock()
    headers = getattr(getattr(error, 'response', None), 'headers', None) or {}
    reset_at = (
        _reset_from_header(headers.get(RATE_LIMIT_RESET_HEADER))
        or _reset_from_retry_after(headers.get(RETRY_AFTER_HEADER), now)
        or now + timedelta(seconds=rate_budget.full_refill_seconds)
    )
    return ExogenousError(
        Reason.VENDOR_RATE_LIMITED,
        'Alpaca rate-limited the request, even after retrying it.',
        metadata=_VENDOR,
        reset_at=reset_at,
        clock=clock,
    )


def _reset_from_header(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _reset_from_retry_after(value: str | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        # RFC 9110 dates are GMT; a zone-less parse is read as that.
        return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        return None
