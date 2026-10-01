"""The read side of a broker handle: what data_ingest asks of any broker, whichever one it is.

Decision tj-j4wknb (addenda 2 to 4). Read and write are separate interfaces and separate
services: this Protocol lives with data_ingest, the write Protocol will live with trade_exec.
Nothing here names a vendor -- broker-native ids (IBKR conid, Questrade symbolId, Alpaca asset
id), sessions, paging, row caps, rate-limit backoff, retry and single-flight stay INSIDE the
adapter and never appear in a signature.

The Protocol is the enforcement, not an inheritance requirement: a class conforms by having the
member, and the contract suite checks that against every implementation. There are no
capability flags; an operation a broker cannot serve raises a typed error (BrokerUnsupportedError
here), and tests catch it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from common.enums.data_select import AssetType
from common.enums.data_stock import Feed, Granularity
from data.ingest.app.brokers.rate_budget import RequestPriority


class BrokerUnsupportedError(Exception):
    """Raised when a broker cannot serve what was asked of it, before any vendor call is made.

    The one typed error PR 1 adds (decision tj-j4wknb addendum 4 item 5). PR 2's typed-errors
    work re-homes it under the common hierarchy (tj-fa1rpu). The message names the field and
    the value that could not be served.
    """


@dataclass(frozen=True)
class Instrument:
    """A generic instrument symbol, the same whichever broker serves it.

    Broker-native identifiers never appear here; the handle resolves them internally. exchange
    and currency stay optional, so a caller that does not care gets the broker's own listing. A
    handle that cannot serve the combination refuses with BrokerUnsupportedError rather than
    guessing a listing.

    Attributes:
        symbol (str): Generic instrument symbol.
        asset_type (AssetType): Kind of instrument.
        exchange (str | None): LISTING exchange as an ISO 10383 MIC, or None for the broker's default.
        currency (str | None): ISO 4217 currency of the listing, or None for the broker's default.
    """

    symbol: str
    asset_type: AssetType
    exchange: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class BarsQuery:
    """One request for historical bars.

    Attributes:
        instrument (Instrument): What to fetch.
        granularity (Granularity): Bar size, the platform's own vocabulary.
        start (datetime): Inclusive start, timezone aware.
        end (datetime | None): Exclusive end of the range, timezone aware, or None for open-ended.
        adjustment (str): Price adjustment. Only 'raw' today: bars are stored raw and adjusted
            on read (tj-vhboky.1 section 6).
        priority (RequestPriority): The platform's fetch priority; the adapter's rate budget uses it.
    """

    instrument: Instrument
    granularity: Granularity
    start: datetime
    end: datetime | None = None
    adjustment: str = 'raw'
    # Required, no default: a silent INTERACTIVE would outrank BACKFILL for a caller that forgot it.
    priority: RequestPriority = field(kw_only=True)


@dataclass(frozen=True)
class Bar:
    """One broker-neutral bar.

    Attributes:
        timestamp (datetime): Bar start, timezone aware, UTC.
        open (float): Opening price.
        high (float): Highest price.
        low (float): Lowest price.
        close (float): Closing price.
        volume (float): Traded volume.
        trade_count (int | None): Number of trades, or None where the broker gives none.
        vwap (float | None): Volume-weighted average price, or None where the broker gives none.
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None


@dataclass(frozen=True)
class BarsResponse:
    """The answer to a BarsQuery.

    Attributes:
        feed (Feed): The entitlement the adapter requested, resolved once per fetch. Known
            BEFORE iteration.
        bars (AsyncIterator[Bar]): The bars, ascending by timestamp, unique, within [start, end):
            a bar stamped exactly at end is never included, for every broker.
    """

    feed: Feed
    bars: AsyncIterator[Bar]


class BrokerRead(Protocol):
    """What data_ingest reads from a broker."""

    async def get_bars(self, query: BarsQuery) -> BarsResponse:
        """Fetch historical bars for one instrument.

        Contract: the feed is known before iteration; the bars ascend, are unique and lie
        within [start, end), half-open, the same for every broker (tj-irhy0a.15). This is a
        contract every implementation meets: an adapter whose vendor treats end as inclusive
        normalises the result itself. Paging, backoff and single-flight are the adapter's
        business and never part of the signature.

        On a vendor failure this RAISES and yields nothing of that fetch, never a partial
        range. PR 2 (gRPC plus typed errors) replaces the raise with typed errors.

        Args:
            query (BarsQuery): What to fetch.

        Returns:
            BarsResponse: The resolved feed and the bars.

        Raises:
            BrokerUnsupportedError: If the broker cannot serve the instrument or the query.
        """
        ...
