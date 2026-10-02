import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models.quotes import Quote
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockQuotesRequest,
    StockTradesRequest,
)

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.environment import get_env_var
from common.logging import get_logger
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import BarsQuery
from data.ingest.app.brokers.rate_budget import RateBudget
from data.ingest.app.brokers.request_key import VendorRequestKey
from data.ingest.app.brokers.single_flight import SingleFlight


log = get_logger(__name__)

# The bars request never sets an adjustment, so every bar we hold is the vendor's raw one.
# Named here because it is part of the request key: a raw bar is not a split-adjusted one.
# This is the design, not an accident of how the request happens to be built: ADR tj-vhboky.1
# section 6 rules that bars are stored raw and immutable, with adjustment applied on read
# against an events table that is not built yet. Do not "fix" this by passing an adjustment.
ALPACA_ADJUSTMENT = 'raw'

ALPACA_CREDENTIAL_VARS = ('ALPACA_API_KEY', 'ALPACA_API_SECRET')

__CLIENT: StockHistoricalDataClient | None = None
__SINGLE_FLIGHT = SingleFlight()
__RATE_BUDGET: RateBudget | None = None


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
    ("which tape actually served this call"). NO CALLER CAN SELECT A FEED today (the ruling
    above defers that to the gRPC transport work), and Alpaca's bars response carries no feed
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
    """
    global __CLIENT
    if __CLIENT is None:
        missing = [name for name in ALPACA_CREDENTIAL_VARS if not get_env_var(name)]
        if missing:
            raise MissingCredentialsError(f'Alpaca credentials are not configured: {", ".join(missing)} unset')
        __CLIENT = StockHistoricalDataClient(*(get_env_var(name) for name in ALPACA_CREDENTIAL_VARS))
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


def get_rate_budget() -> RateBudget:
    """Get this vendor's rate budget, building it on first use.

    Built lazily so that importing the module does not read the environment.

    Returns:
        RateBudget: Shared budget for every Alpaca call this process makes.
    """
    global __RATE_BUDGET
    if __RATE_BUDGET is None:
        __RATE_BUDGET = RateBudget.from_env(DataSource.ALPACA_API)
    return __RATE_BUDGET


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
        case (_, _):
            raise NotImplementedError(f'{asset_type} - {data_type} not implemented')


async def fetch_data_type(
    executor: ThreadPoolExecutor,
    query: BarsQuery,
    data_type: DataType,
    latest: bool,
    params: dict,
    feed: Feed,
    client: StockHistoricalDataClient | None = None,
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

    Returns:
        The vendor's response, shared read-only with any caller that attached to this call.
    """
    client_request, client_request_type = match_client_request(
        client if client is not None else get_client(), AssetType.STOCK, data_type, latest
    )
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

    async def call_vendor():
        # One token per call that actually reaches the vendor. Calls collapsed by the guard
        # cost nothing, which is the point of doing this inside it rather than outside.
        await get_rate_budget().acquire(priority)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, client_request, client_request_type(**params))

    return await __SINGLE_FLIGHT.run(key, call_vendor)
