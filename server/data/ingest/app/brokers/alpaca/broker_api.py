import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

from alpaca.common.constants import DEFAULT_RETRY_ATTEMPTS, DEFAULT_RETRY_WAIT_SECONDS
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.models.bars import BarSet
from alpaca.data.models.quotes import Quote
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockQuotesRequest,
    StockTradesRequest,
)
from alpaca.trading.client import TradingClient
from requests.exceptions import HTTPError

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.environment import get_env_var
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger, register_secret
from common.sensitive import OpaqueStr
from data.ingest.app.brokers.alpaca.classify import reset_from_rate_limit_header
from data.ingest.app.brokers.alpaca.corporate_actions import parse_corporate_actions_page
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError, MissingCredentialsError
from data.ingest.app.brokers.interface import BarsQuery, CallTally, CorporateAction
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.brokers.request_key import VendorRequestKey
from data.ingest.app.brokers.single_flight import SingleFlight
from data.ingest.app.scheduler import Scheduler
from data.ingest.app.scheduler.credentials import Credential, CredentialShapeError, check_credential_shape


log = get_logger(__name__)

# The bars request never sets an adjustment, so every bar we hold is the vendor's raw one.
# Named here because it is part of the request key: a raw bar is not a split-adjusted one.
# This is the design, not an accident of how the request happens to be built: ADR tj-vhboky.1
# section 6 rules that bars are stored raw and immutable, with adjustment applied on read
# against an events table that is not built yet. Do not "fix" this by passing an adjustment.
ALPACA_ADJUSTMENT = 'raw'

ALPACA_CREDENTIAL_VARS = ('ALPACA_API_KEY', 'ALPACA_API_SECRET')

# Where the trading API is, read when the client is built. No default: see get_trading_client.
ALPACA_TRADING_HOST_VAR = 'ALPACA_TRADING_BASE_URL'

BARS_PATH = '/stocks/bars'
ASSETS_PATH = '/assets'
CORPORATE_ACTIONS_PATH = '/corporate-actions'

# The most rows one page of a bars call returns. The SDK's own bars call asks for the same (get_stock_bars,
# page_size=10_000), so a read costs the pages it cost before.
BARS_PAGE_SIZE = 10_000

# The page count past which a bars read is refused as a vendor that never ends. The longest legitimate read is one
# liquid symbol at 1Min over the full SIP history since 2016, extended hours included (up to 960 bars a day, about
# 2.6 million rows, roughly 260 pages at 10,000 rows per page), growing about 24 pages a year; 1000 leaves nearly
# fourfold headroom.
BARS_MAX_PAGES = 1000

# The most records one page of a corporate-actions call returns (the vendor's limit parameter caps at 1000), and the page
# count past which a read is refused as a vendor that never ends: a million records for one symbol is not a window.
CORPORATE_ACTIONS_PAGE_SIZE = 1000
CORPORATE_ACTIONS_MAX_PAGES = 1000

# How many times one page that answered 504 is asked again, and how long to wait before each, taken from the
# SDK's own defaults so a 504 is retried as it always was. Only now every retry acquires from the scheduler.
# 429 is deliberately absent: it ends the attempt at once and the executor waits for the reset.
BARS_PAGE_RETRIES = DEFAULT_RETRY_ATTEMPTS
BARS_PAGE_RETRY_WAIT_S = float(DEFAULT_RETRY_WAIT_SECONDS)
_RETRIED_STATUS = 504

__CLIENT: StockHistoricalDataClient | None = None
__TRADING_CLIENT: TradingClient | None = None
__CORPORATE_ACTIONS_CLIENT: CorporateActionsClient | None = None
__SINGLE_FLIGHT = SingleFlight()
# The instant the vendor's rate-limit window resets, per quota scope, read from the headers of the last response of
# that scope that named one. Touched only on the event loop.
__LAST_RESET: dict[str, datetime] = {}


@dataclass(frozen=True)
class Page:
    """One good page of a vendor read.

    Attributes:
        body (dict): The decoded JSON body.
        headers (Mapping[str, str]): The response headers.
    """

    body: dict
    headers: Mapping[str, str]


def last_reset(scope: str, now: datetime) -> datetime | None:
    """Give the rate-limit reset the last response of a scope named, if it is still ahead.

    The first fallback of a 429 that names no reset of its own (tj-7zjov1.56). A reset already behind now
    describes a window that has closed, so it is not returned: waiting for it would wait for nothing.

    Args:
        scope (str): The quota scope.
        now (datetime): The instant to measure against, timezone aware.

    Returns:
        datetime | None: The remembered reset, or None when none was seen or it has passed.
    """
    reset = __LAST_RESET.get(scope)
    return reset if reset is not None and reset > now else None


def forget_resets() -> None:
    """Drop every remembered reset. For tests and a restart of the scope's state."""
    __LAST_RESET.clear()


def _remember_reset(scope: str, headers: Mapping[str, str] | None) -> None:
    """Remember the reset a response's headers name, whether the response was good or bad."""
    if not headers:
        return
    reset = reset_from_rate_limit_header(headers)
    if reset is not None:
        __LAST_RESET[scope] = reset


def sip_enabled() -> bool:
    """Report whether the account's setting asks for the SIP feed rather than IEX.

    Reads ALPACA_SIP_ENABLED. It is called once, when the Alpaca handle is built, and never per call or per request
    (tj-7zjov1.97). Cast, because an unset variable and the string 'false' are both truthy without it, and the feed
    is part of the request key rather than a log line.

    Returns:
        bool: True when ALPACA_SIP_ENABLED is set to a true value; false when it is unset.
    """
    return get_env_var('ALPACA_SIP_ENABLED', default=False, cast_type=bool)


def resolve_feed() -> Feed:
    """Give the default feed of the account the Alpaca handle is built for.

    Called only by AlpacaHandle when it is built; its result is the handle's feed, and the handle is the single
    resolution site (owner rulings, tj-7zjov1.91; tj-7zjov1.97). The handle decides per broker and per account; the
    deployment-global flag is only today's input for the one Alpaca account. Neither AlpacaRead nor any caller
    resolves a feed again, so the single-flight key, the vendor parameter and the stamped batch cannot drift apart.

    WHAT THIS RECORDS, SAID PLAINLY: an assertion about the ACCOUNT ("which tape is it entitled to"), not an
    observation of the response. Alpaca's bars response carries no feed field to read the served tape back off
    (researcher-broker), so the feed is the one REQUESTED, sent to the vendor explicitly on every call. A lapsed or
    misconfigured entitlement would make it wrong in a way nothing here can detect. Where a handle cannot know which
    feed the vendor used, the feed requested is recorded and unspecified stays its own value: a feed is never guessed.

    Returns:
        Feed: Feed.SIP when the account's setting asks for the consolidated tape, else Feed.IEX.
    """
    return Feed.SIP if sip_enabled() else Feed.IEX


def _checked_credentials() -> tuple[str, str]:
    """Read the configured key and secret once, register them with the log scrub, and check them.

    Each variable is read exactly once into a local; the log-scrub registration, the missing check and the shape
    check all use those locals. Each non-empty plain value is registered straight after the read, before the
    missing and shape checks, so it is registered even when the other variable is unset or empty and even when it is
    malformed; a blank one registers nothing (tj-7zjov1.76). Registered as the plain strings: an OpaqueStr would
    register only the marker word.

    Returns:
        tuple[str, str]: The key id and the secret, checked and plain. The caller wraps them in OpaqueStr.

    Raises:
        MissingCredentialsError: If either credential variable is unset or empty.
        MalformedCredentialsError: If either value is not non-empty visible ASCII. No chained cause, no value.
    """
    key_id, secret = (get_env_var(name) for name in ALPACA_CREDENTIAL_VARS)
    if key_id:
        register_secret(key_id)
    if secret:
        register_secret(secret)
    missing = [name for name, value in zip(ALPACA_CREDENTIAL_VARS, (key_id, secret), strict=True) if not value]
    if missing or key_id is None or secret is None:
        raise MissingCredentialsError(f'Alpaca credentials are not configured: {", ".join(missing)} unset')
    try:
        check_credential_shape(Credential(key_id=key_id, secret=secret))
    except CredentialShapeError:
        raise MalformedCredentialsError(ALPACA_CREDENTIAL_VARS) from None
    return key_id, secret


def get_client() -> StockHistoricalDataClient:
    """Get this process's Alpaca client, building it on first use.

    BUILT LAZILY, NOT AT IMPORT (tj-84jfb9). get_env_var() returns None for an unset
    variable, so building the client at module scope made alpaca-py raise at import time
    and left the whole ingest package unimportable without credentials -- no tests, no
    app start, and a secret rotation that needed a process restart rather than a fresh
    client. Callers that want to supply their own client inject it instead; see
    set_client() and the client argument on AlpacaRead.

    Each variable is read exactly once into a local; the log-scrub registration, the missing check, the shape check
    and the wrap all use those locals. Each non-empty plain value is registered with the log scrub straight after the
    read, before the missing and shape checks, so it is registered even when the other variable is unset or empty
    (MissingCredentialsError) and even when it is malformed; a blank one registers nothing. The scrub covers
    formatted log output only, see common.logging.

    The values are checked, and only then wrapped in OpaqueStr for the client. The wrapper is defense
    in depth behind the check, which it replaces nothing of: it keeps the key out of any repr, str or format of
    the client. Header mode only, and it fails closed: a library change that formats the key into a string sends
    the marker and gets a 401, never a leak.

    Returns:
        StockHistoricalDataClient: Shared client for every Alpaca call this process makes.

    Raises:
        MissingCredentialsError: If either credential variable is unset or empty.
        MalformedCredentialsError: If either value is not non-empty visible ASCII (0x21 to 0x7e). Raised before the
            client is built, with no chained cause and no value in the message.
    """
    global __CLIENT
    if __CLIENT is None:
        key_id, secret = _checked_credentials()
        # Wrapped AFTER the check, from the same two checked values (never a second read), so any later rendering of
        # the client's stored key shows the marker. Header mode only: never pass use_basic_auth, which formats the
        # key into a string and would send the marker. No reveal() here: the header carries the object and requests
        # encodes its raw characters (tj-7zjov1.72).
        __CLIENT = StockHistoricalDataClient(OpaqueStr(key_id), OpaqueStr(secret))
    return __CLIENT


def set_client(client: StockHistoricalDataClient | None) -> None:
    """Install a client for this process, or clear the one already built.

    The injection seam: tests pass a stub, and passing None drops the cached client so
    the next call rebuilds it -- which is what makes a rotated secret take effect without
    a restart.

    Args:
        client (StockHistoricalDataClient | None): Client to use, or None to clear.
    """
    global __CLIENT
    __CLIENT = client


def get_trading_client() -> TradingClient:
    """Get this process's Alpaca trading-API client, building it on first use.

    The trading API serves the asset reads. It is a different host from the market data API, and the host is
    CONFIGURATION (ALPACA_TRADING_BASE_URL), never a default: a paper key and a live key are not interchangeable
    and nothing here may guess which one this deployment holds. Built lazily like get_client, from the same checked
    credentials, wrapped the same way.

    Returns:
        TradingClient: Shared client for every trading-API call this process makes.

    Raises:
        MissingCredentialsError: If either credential variable, or the trading host, is unset or empty.
        MalformedCredentialsError: If either credential value is not non-empty visible ASCII.
    """
    global __TRADING_CLIENT
    if __TRADING_CLIENT is None:
        key_id, secret = _checked_credentials()
        host = get_env_var(ALPACA_TRADING_HOST_VAR)
        if not host:
            raise MissingCredentialsError(f'Alpaca trading host is not configured: {ALPACA_TRADING_HOST_VAR} unset')
        __TRADING_CLIENT = TradingClient(OpaqueStr(key_id), OpaqueStr(secret), url_override=host.rstrip('/'))
    return __TRADING_CLIENT


def set_trading_client(client: TradingClient | None) -> None:
    """Install a trading client for this process, or clear the one already built. The injection seam.

    Args:
        client (TradingClient | None): Client to use, or None to clear.
    """
    global __TRADING_CLIENT
    __TRADING_CLIENT = client


def get_corporate_actions_client() -> CorporateActionsClient:
    """Get this process's Alpaca corporate-actions client, building it on first use.

    The corporate-actions endpoint is on the market-data host under /v1 (the stock bars client is /v2 on the same
    host), so it takes its own SDK client. Built lazily like get_client, from the same checked credentials, wrapped the
    same way. The host is the SDK's data host: nothing here is configuration.

    Returns:
        CorporateActionsClient: Shared client for every corporate-actions call this process makes.

    Raises:
        MissingCredentialsError: If either credential variable is unset or empty.
        MalformedCredentialsError: If either credential value is not non-empty visible ASCII.
    """
    global __CORPORATE_ACTIONS_CLIENT
    if __CORPORATE_ACTIONS_CLIENT is None:
        key_id, secret = _checked_credentials()
        __CORPORATE_ACTIONS_CLIENT = CorporateActionsClient(OpaqueStr(key_id), OpaqueStr(secret))
    return __CORPORATE_ACTIONS_CLIENT


def set_corporate_actions_client(client: CorporateActionsClient | None) -> None:
    """Install a corporate-actions client for this process, or clear the one already built. The injection seam.

    Args:
        client (CorporateActionsClient | None): Client to use, or None to clear.
    """
    global __CORPORATE_ACTIONS_CLIENT
    __CORPORATE_ACTIONS_CLIENT = client


def create_stock_quote(data: Quote, symbol: str, granularity: Granularity, source: DataSource) -> None:
    return None


def match_client_request(
    client: StockHistoricalDataClient, asset_type: AssetType, data_type: DataType, request_latest: bool
) -> tuple[Callable, type]:
    match (asset_type, data_type, request_latest):
        ### STOCK ###
        ## MARKET ACTIVITY ##
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY, False):
            return client.get_stock_bars, StockBarsRequest
        case (AssetType.STOCK, DataType.MARKET_ACTIVITY, True):
            return client.get_stock_latest_bar, StockLatestBarRequest
        ## QUOTE ##
        case (AssetType.STOCK, DataType.QUOTE, False):
            return client.get_stock_quotes, StockQuotesRequest
        case (AssetType.STOCK, DataType.QUOTE, True):
            return client.get_stock_latest_quote, StockLatestQuoteRequest
        ## TRADE ##
        case (AssetType.STOCK, DataType.TRADE, False):
            return client.get_stock_trades, StockTradesRequest
        case (AssetType.STOCK, DataType.TRADE, True):
            return client.get_stock_latest_trade, StockLatestTradeRequest
        ### CRYPTO ###
        ### OPTION ###
        case _:
            raise InvalidRequestError(
                Reason.UNSUPPORTED_ASSET_TYPE,
                f'Alpaca does not serve this yet: asset_type={asset_type.value}, data_type={data_type.value}',
            )


def request_page(client: StockHistoricalDataClient, path: str, params: dict) -> Page:
    """Make exactly one HTTP GET to the vendor, with no retry and no sleep, and keep its headers.

    WHY THIS EXISTS (tj-7zjov1.56). alpaca-py's get call loops over pages and sleeps and retries a 429 and a 504 by
    itself, all inside one call, so none of those requests could be paced or counted, and the headers of a good
    response were thrown away. This is the SDK's own _request/_one_request for ONE request, minus its retry loop:
    same URL, same headers (the client's own, built from the credential get_client checked), same
    allow_redirects=False, same APIError for an error status. Switching the SDK's retry off by configuration is
    not possible (retry_attempts 0 and an empty retry_exception_codes both fall back to the defaults,
    alpaca/common/rest.py RESTClient init), and doing it here also covers a client somebody injected.

    COUPLING, RECORDED: reads the SDK client's _session, _base_url, _api_version and _get_default_headers, pinned
    by the alpaca-py version in the lock file (0.44.0). Blocking; run it on the worker pool.

    Args:
        client (StockHistoricalDataClient): The client whose session, base URL and auth headers are used.
        path (str): The endpoint path, such as BARS_PATH.
        params (dict): The query parameters, page_token included.

    Returns:
        Page: The decoded body and the response headers.

    Raises:
        alpaca.common.exceptions.APIError: If the vendor answers an error status. Its response carries the headers.
        requests.exceptions.RequestException: As the HTTP session raises it, unwrapped.
        ValueError: If the body is empty or not an object.
    """
    # Concatenated, as the SDK does: base_url is a str-valued enum, which an f-string would render by member name.
    url = client._base_url + '/' + client._api_version + path
    response = client._session.request(
        'GET', url, headers=client._get_default_headers(), allow_redirects=False, params=params
    )
    try:
        response.raise_for_status()
    except HTTPError as http_error:
        raise APIError(response.text, http_error) from http_error
    body = response.json() if response.text != '' else None
    if not isinstance(body, dict):
        raise ValueError(f'The vendor answered a page that is not a JSON object: HTTP {response.status_code}')
    return Page(body=body, headers=response.headers)


@dataclass(frozen=True)
class RawPage:
    """One good answer of a vendor read, undecoded.

    Attributes:
        content (bytes): The response body exactly as served.
        headers (Mapping[str, str]): The response headers.
        status_code (int): The HTTP status. raise_for_status passes any 2xx, so a caller wanting exactly 200 checks it.
    """

    content: bytes
    headers: Mapping[str, str]
    status_code: int


def request_raw(client: TradingClient | CorporateActionsClient, path: str, params: dict | None = None) -> RawPage:
    """Make exactly one HTTP GET, with no retry and no sleep, and keep the body as served.

    Serves the trading API and the data API's v1 corporate-actions endpoint. The sibling of request_page for a body
    that is not a JSON object (the asset list is an array) or that the caller slices byte for byte (the
    corporate-actions page). It charges and classifies nothing: read_raw does, on the
    scope its caller names. Same URL construction, same headers (the client's own, from the checked credential),
    same allow_redirects=False, same APIError for an error status. Blocking; run it on the worker pool.

    Args:
        client (TradingClient | CorporateActionsClient): The client whose session, base URL, API version and auth
            headers are used.
        path (str): The endpoint path, such as ASSETS_PATH.
        params (dict | None): The query parameters.

    Returns:
        RawPage: The body bytes and the response headers.

    Raises:
        alpaca.common.exceptions.APIError: If the vendor answers an error status. Its response carries the headers.
        requests.exceptions.RequestException: As the HTTP session raises it, unwrapped.
    """
    url = client._base_url + '/' + client._api_version + path
    response = client._session.request(
        'GET', url, headers=client._get_default_headers(), allow_redirects=False, params=params
    )
    try:
        response.raise_for_status()
    except HTTPError as http_error:
        raise APIError(response.text, http_error) from http_error
    return RawPage(content=response.content, headers=response.headers, status_code=response.status_code)


async def read_raw(
    executor: ThreadPoolExecutor,
    client: TradingClient | CorporateActionsClient,
    path: str,
    params: dict | None,
    *,
    scheduler: Scheduler,
    scope: str,
    priority: RequestPriority,
    deadline: datetime | None,
    tally: CallTally | None = None,
) -> RawPage:
    """Make one paced raw read: one scheduler token on the scope given, one HTTP request.

    Serves the trading API and the data API's v1 corporate-actions endpoint alike. Never retries (the executor
    retries the whole attempt). Charges only the scope the caller names, and the reset the response names, good or
    bad, is remembered on that same scope.

    Args:
        executor (ThreadPoolExecutor): Pool the blocking request runs on.
        client (TradingClient | CorporateActionsClient): The checked client.
        path (str): The endpoint path.
        params (dict | None): The query parameters.
        scheduler (Scheduler): The handle's scheduler.
        scope (str): The quota scope to charge: the trading API's or the market-data API's, as the caller's endpoint.
        priority (RequestPriority): The priority class of the acquire.
        deadline (datetime | None): When the caller stops waiting, or None.
        tally (CallTally | None): Counts the call, a good page and a 429, or None when nobody reads them.

    Returns:
        RawPage: The answer.

    Raises:
        ExogenousError: With the reason RATE_BUDGET, when the acquire cannot be admitted before the deadline.
        alpaca.common.exceptions.APIError: If the vendor answers an error status.
        requests.exceptions.RequestException: If the vendor cannot be reached or the connection is cut.
    """
    await scheduler.acquire(scope, priority, deadline=deadline)
    if tally is not None:
        tally.record(datetime.now(UTC))
    loop = asyncio.get_running_loop()
    try:
        page = await loop.run_in_executor(executor, request_raw, client, path, params)
    except APIError as error:
        _remember_reset(scope, getattr(getattr(error, 'response', None), 'headers', None))
        if error.status_code == HTTPStatus.TOO_MANY_REQUESTS and tally is not None:
            tally.record_throttled()
        raise
    _remember_reset(scope, page.headers)
    if tally is not None:
        tally.record_page()
    return page


async def read_corporate_action_pages(
    executor: ThreadPoolExecutor,
    client: CorporateActionsClient,
    params: dict,
    *,
    scheduler: Scheduler,
    scope: str,
    priority: RequestPriority,
    deadline: datetime | None,
    tally: CallTally | None,
) -> list[CorporateAction]:
    """Read every page of a corporate-actions window, one scheduler token per HTTP request, bodies kept as served.

    Follows next_page_token until the vendor gives none. Every page acquires first, under the same deadline, and is
    counted on the tally. Never retries a page (the executor retries the whole attempt), so a failure on any page
    drops the pages already read with it: a partial list is never a served window.

    PROGRESS GUARD. A token the vendor already gave in this read, or a read past CORPORATE_ACTIONS_MAX_PAGES pages,
    is a vendor that does not end; it raises rather than looping on the quota.

    Args:
        executor (ThreadPoolExecutor): Pool the blocking request runs on.
        client (CorporateActionsClient): The checked client.
        params (dict): The request fields without paging parameters.
        scheduler (Scheduler): The handle's scheduler.
        scope (str): The quota scope charged: the market-data API's.
        priority (RequestPriority): The priority class of every acquire.
        deadline (datetime | None): When the caller stops waiting, or None.
        tally (CallTally | None): Counts calls, pages and 429s, or None when nobody reads them.

    Returns:
        list[CorporateAction]: Every record of every page, in the order served.

    Raises:
        ExogenousError: With the reason RATE_BUDGET, when any acquire cannot be admitted before the deadline.
        alpaca.common.exceptions.APIError: If the vendor answers an error status.
        requests.exceptions.RequestException: If the vendor cannot be reached or the connection is cut.
        ValueError: If a page is not a corporate-actions page, or the vendor repeats a token or never ends.
    """
    actions: list[CorporateAction] = []
    seen_tokens: set[str] = set()
    page_token: str | None = None
    for _ in range(CORPORATE_ACTIONS_MAX_PAGES):
        page_params = {**params, 'limit': CORPORATE_ACTIONS_PAGE_SIZE}
        if page_token is not None:
            page_params['page_token'] = page_token
        page = await read_raw(
            executor,
            client,
            CORPORATE_ACTIONS_PATH,
            page_params,
            scheduler=scheduler,
            scope=scope,
            priority=priority,
            deadline=deadline,
            tally=tally,
        )
        parsed = parse_corporate_actions_page(page.content)
        actions.extend(parsed.actions)
        page_token = parsed.next_page_token
        if page_token is None:
            return actions
        if page_token in seen_tokens:
            raise ValueError('The vendor repeated a corporate-actions page token, so the read does not end')
        seen_tokens.add(page_token)
    raise ValueError(f'The vendor served more than {CORPORATE_ACTIONS_MAX_PAGES} corporate-actions pages')


async def read_bars_pages(
    executor: ThreadPoolExecutor,
    client: StockHistoricalDataClient,
    params: dict,
    *,
    scheduler: Scheduler,
    scope: str,
    priority: RequestPriority,
    deadline: datetime | None,
    tally: CallTally | None,
) -> BarSet:
    """Read every page of a bars range, one scheduler token per HTTP request that reaches the vendor.

    The page loop of alpaca-py's _get_marketdata, owned here so each request can be paced (tj-7zjov1.56): every
    page and every retry acquires first, under the same deadline, and is counted on the tally. A page whose
    acquire cannot be admitted before the deadline raises the scheduler's RATE_BUDGET error, and the bars already
    read are dropped with it: a partial read is never a served range. The models stay the SDK's (BarSet).

    A 429 is not retried: it surfaces at once, its headers remembered, for classify_vendor_error. A 504 is asked
    again up to BARS_PAGE_RETRIES times after BARS_PAGE_RETRY_WAIT_S, each time through the scheduler, and only
    while the wait still fits before the deadline. Any other failure surfaces at once.

    PROGRESS GUARD. An empty token ends the read like a missing one. A token the vendor already gave in this read, or
    a read past BARS_MAX_PAGES pages, is a vendor that does not end; it raises rather than looping on the quota. The
    retry of one page resends that page's token and is not a repeat.

    Args:
        executor (ThreadPoolExecutor): Pool the blocking request runs on.
        client (StockHistoricalDataClient): The checked client; its credentials were refused or accepted before this.
        params (dict): The bars request fields as the SDK sends them (StockBarsRequest.to_request_fields()), without
            paging parameters.
        scheduler (Scheduler): The handle's scheduler.
        scope (str): The quota scope charged.
        priority (RequestPriority): The priority class of every acquire.
        deadline (datetime | None): When the caller stops waiting, or None.
        tally (CallTally | None): Counts calls, pages, retries and 429s, or None when nobody reads them.

    Returns:
        BarSet: All pages merged, as the SDK would have returned them.

    Raises:
        ExogenousError: With the reason RATE_BUDGET, when any acquire cannot be admitted before the deadline.
        alpaca.common.exceptions.APIError: If the vendor answers an error status (the 504 retries spent included).
        requests.exceptions.RequestException: If the vendor cannot be reached or the connection is cut.
        AttributeError: If a page answers a JSON null 'bars'. Deliberately not guarded, loud (ADR tj-fa1rpu).
        ValueError: If a page is not a bars page, or the vendor repeats a token or never ends.
    """
    loop = asyncio.get_running_loop()
    merged: dict[str, list] = {}
    seen_tokens: set[str] = set()
    page_token = None
    for _ in range(BARS_MAX_PAGES):
        page_params = {**params, 'limit': BARS_PAGE_SIZE, 'page_token': page_token}
        retries_left = BARS_PAGE_RETRIES
        while True:
            await scheduler.acquire(scope, priority, deadline=deadline)
            if tally is not None:
                tally.record(datetime.now(UTC))
            try:
                page = await loop.run_in_executor(executor, request_page, client, BARS_PATH, page_params)
            except APIError as error:
                response = getattr(error, 'response', None)
                _remember_reset(scope, getattr(response, 'headers', None))
                status = error.status_code
                if status == HTTPStatus.TOO_MANY_REQUESTS and tally is not None:
                    tally.record_throttled()
                if status == _RETRIED_STATUS and retries_left > 0 and _wait_fits(deadline, BARS_PAGE_RETRY_WAIT_S):
                    retries_left -= 1
                    if tally is not None:
                        tally.record_retry()
                    await asyncio.sleep(BARS_PAGE_RETRY_WAIT_S)
                    continue
                raise
            break
        _remember_reset(scope, page.headers)
        if tally is not None:
            tally.record_page()
        # A JSON null 'bars' fails here with an AttributeError, as it did inside the SDK.
        if 'bars' not in page.body:
            raise ValueError('The data in the page does not match any known keys.')
        for symbol, bars in page.body['bars'].items():
            merged.setdefault(symbol, []).extend(bars)
        page_token = page.body.get('next_page_token')
        if not page_token:
            return BarSet(merged)
        if page_token in seen_tokens:
            raise ValueError('The vendor repeated a bars page token, so the read does not end')
        seen_tokens.add(page_token)
    raise ValueError(f'The vendor served more than {BARS_MAX_PAGES} bars pages')


def _wait_fits(deadline: datetime | None, wait_s: float) -> bool:
    """Report whether a wait of wait_s seconds ends before the deadline (always, with no deadline)."""
    return deadline is None or datetime.now(UTC) + timedelta(seconds=wait_s) < deadline


async def fetch_data_type(
    executor: ThreadPoolExecutor,
    query: BarsQuery,
    data_type: DataType,
    latest: bool,
    params: dict,
    feed: Feed,
    client: StockHistoricalDataClient | None = None,
    *,
    scheduler: Scheduler,
    scope: str,
):
    """Fetch one data type from the vendor, collapsed and rate-limited.

    Concurrent callers asking for the same content share one vendor call; the extra ones
    attach to it and nothing is retained afterwards (tj-84ty47 section 6). The shared value
    is the vendor's own response, which every caller then converts on its own -- the platform
    conversion (dataset_id and the store's batch schema) happens above the broker, in
    ingest_control, so nothing caller-specific is ever shared.

    Args:
        executor (ThreadPoolExecutor): Pool the blocking SDK call runs on.
        query (BarsQuery): Query being served.
        data_type (DataType): Data type to fetch.
        latest (bool): Whether the latest-value endpoint is being used.
        params (dict): Vendor request parameters. Already carries this call's resolved
            `feed`, put there by AlpacaRead.get_bars so every vendor call asks for the same
            tape the key and the response record.
        feed (Feed): This call's resolved tape (the handle's feed, handed to AlpacaRead at build), taken as a parameter
            rather than re-read here so the key can never disagree with the response that
            AlpacaRead stamps from the same value.
        client (StockHistoricalDataClient | None): Client to call, or None for this
            process's own (see get_client()).
        scheduler (Scheduler): The handle's scheduler, whose quota limits pace every vendor HTTP request: for
            bars, each page and each retry (read_bars_pages). Nothing else paces a call: the budget this module
            used to build from the environment is gone.
        scope (str): The quota scope of the call.

    Returns:
        The vendor's response, shared read-only with any caller that attached to this call.

    Raises:
        MissingCredentialsError: If no client was passed and a credential variable is unset or empty.
        MalformedCredentialsError: If no client was passed and a credential value is not non-empty visible
            ASCII.
        ExogenousError: With the reason RATE_BUDGET, if query.deadline is set and the scope's limits
            cannot admit the call before it. The call that reaches the vendor is the leader's, so
            the leader's deadline is the one the budget holds it to, and an attached caller shares
            that outcome whatever its own deadline.
        InvalidRequestError: With the reason UNSUPPORTED_ASSET_TYPE, if the vendor has no
            endpoint for the data type.
        alpaca.common.exceptions.APIError: If the vendor answers an error status: a 429 at once, a 504
            once its retries (BARS_PAGE_RETRIES, each through the scheduler) are spent.
        requests.exceptions.ConnectionError: If the vendor cannot be reached.
        requests.exceptions.Timeout: If the vendor does not answer in time.
        requests.exceptions.ChunkedEncodingError: If the connection is cut while the body is read.
    """
    # Resolved first, so a missing or malformed credential refuses before any token is spent or request is made.
    client = client if client is not None else get_client()
    client_request, client_request_type = match_client_request(client, AssetType.STOCK, data_type, latest)
    paged_bars = (data_type, latest) == (DataType.MARKET_ACTIVITY, False)
    key = VendorRequestKey(
        broker=DataSource.ALPACA_API,
        # Lowercase, matching the vendor's own wire values ('iex'/'sip', see DataFeed) rather
        # than the contract's uppercase Feed members -- this key is never stored or compared
        # against the schema, only against itself, and the vendor call above the key was
        # already stamped from this same lowercase form.
        feed=feed.value.lower(),
        asset_type=AssetType.STOCK,
        asset_symbol=query.instrument.symbol,
        data_type=data_type,
        granularity=query.granularity,
        range_start=query.start,
        range_end=query.end,
        adjustment=ALPACA_ADJUSTMENT,
    )
    priority = query.priority
    deadline = query.deadline

    async def call_vendor():
        # One token per HTTP request that actually reaches the vendor. Calls collapsed by the guard
        # cost nothing, which is the point of doing this inside it rather than outside. The
        # deadline makes a wait the caller cannot afford a refusal at once (ADR tj-fa1rpu U4).
        if paged_bars:
            # Every page and every retry of a bars read acquires on its own (tj-7zjov1.56).
            return await read_bars_pages(
                executor,
                client,
                client_request_type(**params).to_request_fields(),
                scheduler=scheduler,
                scope=scope,
                priority=priority,
                deadline=deadline,
                tally=query.tally,
            )
        # The other data types are not reachable from the reader; they stay one call, one token.
        await scheduler.acquire(scope, priority, deadline=deadline)
        if query.tally is not None:
            query.tally.record(datetime.now(UTC))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, client_request, client_request_type(**params))

    return await __SINGLE_FLIGHT.run(key, call_vendor)
