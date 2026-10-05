"""The read side of a broker handle: what data_ingest asks of any broker, whichever one it is.

Decision tj-j4wknb (addenda 2 to 4). Read and write are separate interfaces and separate
services: this Protocol lives with data_ingest, the write Protocol will live with trade_exec.
Nothing here names a vendor -- broker-native ids (IBKR conid, Questrade symbolId, Alpaca asset
id), sessions, paging, row caps, rate-limit backoff, retry and single-flight stay INSIDE the
adapter and never appear in a signature.

The Protocol is the enforcement, not an inheritance requirement: a class conforms by having the
member, and the contract suite checks that against every implementation. There are no
capability flags; an operation a broker cannot serve comes back as a BarsFailure carrying a typed
error (BrokerUnsupportedError here), and callers branch on it.

THE TYPED RESULT (ADR tj-fa1rpu D1(a), D2, D3, D6): get_bars is boundary (a), so it RETURNS either
a BarsResponse, the SERVED outcome, or a BarsFailure, and never raises for a failure that is
expected. Which of REFUSED and NOT_READY a failure is comes from REASONS[error.reason] and is
never stated a second time. A bug still raises (D5).
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, NamedTuple, Protocol

from common.enums.data_select import AssetType
from common.enums.data_stock import Feed, Granularity
from common.errors.vocabulary import InvalidRequestError, Reason, TraderJoeError
from data.ingest.app.brokers.rate_budget import RequestPriority


# The reasons a broker can give for not serving what was asked of it (TE-4).
_UNSUPPORTED_REASONS: Final = frozenset(
    {Reason.UNSUPPORTED_INSTRUMENT, Reason.UNSUPPORTED_ASSET_TYPE, Reason.FEED_NOT_AVAILABLE}
)


class BrokerUnsupportedError(InvalidRequestError):
    """Why a broker cannot serve what was asked of it, found before any vendor call is made.

    Re-parented onto InvalidRequestError by PR 2's typed errors (ADR tj-fa1rpu D5). It stays here, with
    the interface it belongs to, because shared broker code moves to common/ only at the first trade_exec
    PR (decision tj-j4wknb addendum 4 item 8). A reader RETURNS it inside a BarsFailure.

    It carries UNSUPPORTED_INSTRUMENT (a currency, exchange or adjustment the broker cannot serve),
    UNSUPPORTED_ASSET_TYPE (not a stock) or FEED_NOT_AVAILABLE (a feed this deployment cannot serve). The
    detail names the field and the value that could not be served.

    Args:
        reason (Reason): One of the three reasons above.
        detail (str): One sentence for a human, never parsed. Never a credential or a raw vendor body.
        metadata (Mapping[str, str | Sequence[str]] | None): Allowlisted machine-readable context.

    Raises:
        ValueError: If reason is not one of the three above.
    """

    def __init__(
        self, reason: Reason, detail: str, *, metadata: Mapping[str, str | Sequence[str]] | None = None
    ) -> None:
        if reason not in _UNSUPPORTED_REASONS:
            raise ValueError(f'BrokerUnsupportedError carries {sorted(_UNSUPPORTED_REASONS)}, not {reason!r}')
        super().__init__(reason, detail, metadata=metadata)


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
        feed (Feed | None): The tape the caller names, or None to leave it to the deployment. A reader
            serves a named feed only if its deployment can, and otherwise refuses before any vendor
            call (FEED_NOT_AVAILABLE).
        deadline (datetime | None): When the caller stops waiting, timezone aware, or None for no
            bound. A rate budget that cannot admit the call before it fails fast (RATE_BUDGET)
            instead of sleeping past it (ADR tj-fa1rpu U4).
    """

    instrument: Instrument
    granularity: Granularity
    start: datetime
    end: datetime | None = None
    adjustment: str = 'raw'
    # Required, no default: a silent INTERACTIVE would outrank BACKFILL for a caller that forgot it.
    priority: RequestPriority = field(kw_only=True)
    # Keyword only: a feed or a deadline passed positionally would be a silent mistake.
    feed: Feed | None = field(default=None, kw_only=True)
    deadline: datetime | None = field(default=None, kw_only=True)


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


class ServedRange(NamedTuple):
    """The range a vendor answered for, which is never simply the range asked for (ADR tj-fa1rpu D3 note a).

    Attributes:
        start (datetime): Inclusive start, timezone aware.
        end (datetime): Exclusive end, timezone aware. Never open: an open request end is served as as_of.
    """

    start: datetime
    end: datetime


@dataclass(frozen=True)
class BarsResponse:
    """The SERVED outcome: a range the vendor answered for, whose bars may be none (ADR tj-fa1rpu D2).

    served_range present with no bars IS the positive statement that nothing happened in it.

    Attributes:
        feed (Feed): The entitlement the adapter requested, resolved once per fetch. Known
            BEFORE iteration.
        bars (AsyncIterator[Bar]): The bars, ascending by timestamp, unique, within [start, end):
            a bar stamped exactly at end is never included, for every broker.
        served_range (ServedRange): The range the vendor ANSWERED for. Its end is the query's end
            clamped to as_of, because a vendor cannot have answered for time after it answered, and
            an open end is as_of.
        as_of (datetime): When the vendor answered, timezone aware, UTC.
    """

    feed: Feed
    bars: AsyncIterator[Bar]
    served_range: ServedRange
    as_of: datetime


@dataclass(frozen=True)
class BarsFailure:
    """A failure get_bars RETURNS rather than raises: REFUSED or NOT_READY, never SERVED (ADR tj-fa1rpu D3, D6).

    Which of the two it is, and whether it names a delay, is read from REASONS[error.reason] and
    error.reset_at and never stated a second time here.

    Where the failure converts an exception the vendor's client raised, that exception is the
    error's __cause__: the returned form of 'raise ... from e', so the edge that renders it can
    log the chain under the error_id it mints (D8). The error's detail and metadata never hold it.
    A refusal found before any vendor call, and the rate budget's own refusal, have no cause.

    Attributes:
        error (TraderJoeError): The typed error, with its reason, detail and where known reset_at.
    """

    error: TraderJoeError


class BrokerRead(Protocol):
    """What data_ingest reads from a broker."""

    async def get_bars(self, query: BarsQuery) -> BarsResponse | BarsFailure:
        """Fetch historical bars for one instrument.

        Contract: the feed is known before iteration; the bars ascend, are unique and lie
        within [start, end), half-open, the same for every broker (tj-irhy0a.15). This is a
        contract every implementation meets: an adapter whose vendor treats end as inclusive
        normalises the result itself. Paging, backoff and single-flight are the adapter's
        business and never part of the signature.

        THIS RETURNS A BarsFailure, NEVER RAISES, for a failure that is expected: a request the
        broker refuses before any vendor call (an instrument, a feed or a range it will not
        serve), a vendor that refuses or cannot answer, a rate limit or a missing credential.
        That is D6's first permitted form, catch, convert and return. A bug still raises (D5).
        It yields nothing of a failed fetch, never a partial range. If a future adapter can fail
        while its bars are iterated, its iterator raises a TraderJoeError and the servicer
        converts it (D1(b)).

        An answer with no bars is SERVED, never a failure: served_range says what the vendor
        answered for.

        Args:
            query (BarsQuery): What to fetch.

        Returns:
            BarsResponse | BarsFailure: The served range, the resolved feed and the bars, or the
                typed failure.
        """
        ...
