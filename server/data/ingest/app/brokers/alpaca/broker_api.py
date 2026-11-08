import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

from alpaca.common.constants import DEFAULT_RETRY_ATTEMPTS, DEFAULT_RETRY_WAIT_SECONDS
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
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
from requests.exceptions import HTTPError

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.environment import get_env_var
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from data.ingest.app.brokers.alpaca.classify import reset_from_rate_limit_header
from data.ingest.app.brokers.broker_errors import MalformedCredentialsError, MissingCredentialsError
from data.ingest.app.brokers.interface import BarsQuery, CallTally
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

BARS_PATH = '/stocks/bars'

# The most rows one page of a bars call returns. The SDK's own bars call asks for the same (get_stock_bars,
# page_size=10_000), so a read costs the pages it cost before.
BARS_PAGE_SIZE = 10_000

# How many times one page that answered 504 is asked again, and how long to wait before each, taken from the
# SDK's own defaults so a 504 is retried as it always was. Only now every retry acquires from the scheduler.
# 429 is deliberately absent: it ends the attempt at once and the executor waits for the reset.
BARS_PAGE_RETRIES = DEFAULT_RETRY_ATTEMPTS
BARS_PAGE_RETRY_WAIT_S = float(DEFAULT_RETRY_WAIT_SECONDS)
_RETRIED_STATUS = 504

__CLIENT: StockHistoricalDataClient | None = None
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
    """Report whether this process is configured for the SIP feed rather than IEX.

    Read on each call rather than at import, for the same reason the client is built
    lazily. Cast, because an unset variable and the string 'false' are both truthy
    without it, and the feed is part of the request key rather than a log line.

    Returns:
        bool: True when ALPACA_SIP_ENABLED is set to a true value.
    """
    return get_env_var('ALPACA_SIP_ENABLED', default=False, cast_type=bool)


def resolve_feed() -> Feed:
    """Resolve the tape this deployment is entitled to, once, for a whole fetch.

    THE SINGLE RESOLUTION SITE (tj-rh4b7f, 2026-09-25 ruling; tj-vhboky.9 part B). Both the
    single-flight request key and the batch stamped onto every bar must agree, so this is the
    only place ALPACA_SIP_ENABLED is read for that purpose -- callers take the return value
    rather than re-deriving it, which is what stops the key and the batch silently drifting
    apart.

    WHAT THIS RECORDS, SAID PLAINLY RATHER THAN PAPERED OVER: this is an assertion about the
    ACCOUNT ("which tape is this deployment entitled to"), not an observation of the response
    ("which tape actually served this call"). A CALLER MAY NAME A FEED (BarsQuery.feed, live at
    the adapter since PR 2's typed errors), but AlpacaRead serves a named feed only if it IS
    the tape this returns, and otherwise answers FEED_NOT_AVAILABLE before any vendor call. So
    the deployment, never the caller, decides the tape. Alpaca's bars response carries no feed
    field at any level to read the served tape back off of (researcher-broker, confirmed on two
    independent reads) -- so there is no vendor confirmation to fall back to even if there were
    a caller selection to try first. The value below is passed to the vendor call as its own
    `feed` parameter (AlpacaRead.get_bars), which is the best available assurance that the
    entitled tape and the requested tape are the same one; it is still not proof that the tape
    which answered matches, since a lapsed or misconfigured entitlement would make this value
    wrong in a way nothing here can detect.

    Returns:
        Feed: Feed.SIP when this deployment is entitled to the consolidated tape,
            else Feed.IEX.
    """
    return Feed.SIP if sip_enabled() else Feed.IEX


def get_client() -> StockHistoricalDataClient:
    """Get this process's Alpaca client, building it on first use.

    BUILT LAZILY, NOT AT IMPORT (tj-84jfb9). get_env_var() returns None for an unset
    variable, so building the client at module scope made alpaca-py raise at import time
    and left the whole ingest package unimportable without credentials -- no tests, no
    app start, and a secret rotation that needed a process restart rather than a fresh
    client. Callers that want to supply their own client inject it instead; see
    set_client() and the client argument on AlpacaRead.

    Returns:
        StockHistoricalDataClient: Shared client for every Alpaca call this process makes.

    Raises:
        MissingCredentialsError: If either credential variable is unset or empty.
        MalformedCredentialsError: If either value is not non-empty visible ASCII (0x21 to 0x7e). Raised before the
            client is built, with no chained cause and no value in the message.
    """
    global __CLIENT
    if __CLIENT is None:
        missing = [name for name in ALPACA_CREDENTIAL_VARS if not get_env_var(name)]
        if missing:
            raise MissingCredentialsError(f'Alpaca credentials are not configured: {", ".join(missing)} unset')
        key_id, secret = (get_env_var(name) for name in ALPACA_CREDENTIAL_VARS)
        try:
            check_credential_shape(Credential(key_id=key_id, secret=secret))
        except CredentialShapeError:
            raise MalformedCredentialsError(ALPACA_CREDENTIAL_VARS) from None
        __CLIENT = StockHistoricalDataClient(key_id, secret)
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
        ValueError: If a page is not a bars page.
    """
    loop = asyncio.get_running_loop()
    merged: dict[str, list] = {}
    page_token = None
    while True:
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
        if page_token is None:
            return BarSet(merged)


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
        feed (Feed): This call's resolved tape (resolve_feed()), taken as a parameter
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
