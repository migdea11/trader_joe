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
from schemas.data_ingest.submit import AssetListStatus


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


@dataclass
class CallTally:
    """The vendor calls one attempt actually made, counted by the adapter as it spends its quota.

    A call that was collapsed onto another caller's in flight, or refused before the vendor, is not counted: it
    spent nothing. The attempt's report to the store carries these numbers.

    Attributes:
        calls (int): HTTP requests that reached the vendor: every page and every retry, whatever the answer.
        pages (int): Good pages the vendor answered.
        retries (int): Requests made again after a failed one inside the attempt.
        throttled_429 (int): Requests the vendor answered 429.
        first_call_at (datetime | None): When the first was made, timezone aware.
        last_call_at (datetime | None): When the last was made, timezone aware.
    """

    calls: int = 0
    pages: int = 0
    retries: int = 0
    throttled_429: int = 0
    first_call_at: datetime | None = None
    last_call_at: datetime | None = None

    def record(self, at: datetime) -> None:
        """Count one vendor call made at the given instant.

        Args:
            at (datetime): When the call was made, timezone aware.
        """
        self.calls += 1
        if self.first_call_at is None:
            self.first_call_at = at
        self.last_call_at = at

    def record_page(self) -> None:
        """Count one good page the vendor answered."""
        self.pages += 1

    def record_retry(self) -> None:
        """Count one request made again after a failed one."""
        self.retries += 1

    def record_throttled(self) -> None:
        """Count one request the vendor answered 429."""
        self.throttled_429 += 1


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
        tally (CallTally | None): Counts the vendor calls this query makes, for the attempt's report.
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
    # Where the adapter counts the vendor calls this query makes, or None when nobody reads them.
    tally: CallTally | None = field(default=None, kw_only=True)


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


@dataclass(frozen=True)
class AssetListQuery:
    """One request for a broker's asset list, one list per request.

    Attributes:
        asset_type (AssetType): What kind of instrument to list. A broker that lists only some kinds refuses the rest.
        status (AssetListStatus): Which list: the active one or the inactive one. Never both, never "all".
        priority (RequestPriority): The platform's priority class for the vendor call.
        deadline (datetime | None): When the caller stops waiting, timezone aware, or None for no bound. A rate
            budget that cannot admit the call before it fails fast (RATE_BUDGET) instead of sleeping past it.
        tally (CallTally | None): Counts the vendor calls this query makes, for the attempt's report.
    """

    asset_type: AssetType
    status: AssetListStatus
    priority: RequestPriority = field(kw_only=True)
    deadline: datetime | None = field(default=None, kw_only=True)
    tally: CallTally | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class AssetLookupQuery:
    """One request for the broker's record of one symbol.

    The lookup is by SYMBOL only. The broker's own asset id is data a lookup returns and never an argument.

    Attributes:
        symbol (str): The ticker to look up, as the caller holds it.
        priority (RequestPriority): The platform's priority class for the vendor call.
        deadline (datetime | None): When the caller stops waiting, timezone aware, or None for no bound.
    """

    symbol: str
    priority: RequestPriority = field(kw_only=True)
    deadline: datetime | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class Asset:
    """One broker asset, carried RAW: a few neutral fields beside the broker's own record, untouched.

    Nothing here is a judgement. active and tradable are None when the broker said nothing, which is never
    read as inactive or untradable; the store judges what an asset is worth.

    Attributes:
        vendor_asset_id (str): The broker's id for the asset, OPAQUE: nothing parses or compares it.
        symbol (str): The ticker the broker lists it under now.
        raw_payload (bytes): The broker's record as served, byte for byte.
        active (bool | None): Whether the broker's status says active, or None when it gave no status.
        tradable (bool | None): The broker's tradable flag, or None when it gave none.
        exchange (str): The broker's venue wording as given, empty when it gave none.
        name (str): The broker's name for the asset as given, empty when it gave none.
    """

    vendor_asset_id: str
    symbol: str
    raw_payload: bytes
    active: bool | None = None
    tradable: bool | None = None
    exchange: str = ''
    name: str = ''


@dataclass(frozen=True)
class AssetListResponse:
    """The SERVED outcome of an asset list: every asset the broker listed, which may be none.

    Attributes:
        assets (Sequence[Asset]): The assets in the order the broker served them.
        as_of (datetime): When the broker answered, timezone aware, UTC.
    """

    assets: Sequence[Asset]
    as_of: datetime


@dataclass(frozen=True)
class AssetListFailure:
    """A failure get_assets RETURNS rather than raises.

    Attributes:
        error (TraderJoeError): The typed error; the caught vendor exception, where there is one, is its __cause__.
    """

    error: TraderJoeError


@dataclass(frozen=True)
class AssetResponse:
    """The SERVED outcome of a lookup: the broker's record for the symbol.

    Attributes:
        asset (Asset): The record. Its symbol is the broker's own and may differ from the one asked for.
        as_of (datetime): When the broker answered, timezone aware, UTC.
    """

    asset: Asset
    as_of: datetime


@dataclass(frozen=True)
class AssetNotFound:
    """The broker answered that it has no asset for the symbol. An outcome, not a failure.

    A caller that holds a vendor id for the symbol reads this as the id having gone, not as an error.

    Attributes:
        symbol (str): The symbol that was asked for.
        as_of (datetime): When the broker answered, timezone aware, UTC.
    """

    symbol: str
    as_of: datetime


@dataclass(frozen=True)
class AssetLookupFailure:
    """A failure get_asset RETURNS rather than raises: anything but an answer, found or not found.

    Attributes:
        error (TraderJoeError): The typed error, with reset_at where a rate limit names one.
    """

    error: TraderJoeError


@dataclass(frozen=True)
class CorporateActionsQuery:
    """One request for a broker's corporate actions for one symbol over an explicit window.

    Attributes:
        symbol (str): The ticker to read, as the caller holds it. The broker may match it on either side of an action
            (a name change is found by its old and its new symbol).
        start (datetime): Inclusive start of the window, timezone aware.
        end (datetime): Exclusive end of the window, timezone aware.
        priority (RequestPriority): The platform's priority class for the vendor calls.
        deadline (datetime | None): When the caller stops waiting, timezone aware, or None for no bound.
        tally (CallTally | None): Counts the vendor calls this query makes, for the attempt's report.
    """

    symbol: str
    start: datetime
    end: datetime
    priority: RequestPriority = field(kw_only=True)
    deadline: datetime | None = field(default=None, kw_only=True)
    tally: CallTally | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class CorporateAction:
    """One broker corporate action, carried RAW: a few neutral fields beside the broker's own record, untouched.

    Nothing here is a judgement and no type is filtered: the store decides what an action is worth. A date the broker
    did not give is None.

    Attributes:
        vendor_action_id (str): The broker's id for the action, OPAQUE.
        action_type (str): The broker's own wording of the type (its group name), uninterpreted.
        raw_payload (bytes): The broker's record as served, byte for byte.
        ex_date (datetime | None): The broker's ex date, midnight UTC of the day it gave.
        record_date (datetime | None): The broker's record date, likewise.
        payable_date (datetime | None): The broker's payable date, likewise.
        process_date (datetime | None): The broker's process date, likewise.
        effective_date (datetime | None): The broker's effective date, likewise.
        identifiers (Mapping[str, str]): Every instrument identifier the broker gave (symbols, CUSIPs, ISINs, role-named
            ones included), keyed by the broker's own field name.
    """

    vendor_action_id: str
    action_type: str
    raw_payload: bytes
    ex_date: datetime | None = None
    record_date: datetime | None = None
    payable_date: datetime | None = None
    process_date: datetime | None = None
    effective_date: datetime | None = None
    identifiers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CorporateActionsResponse:
    """The SERVED outcome of a corporate-actions read: every action the broker returned for the window, maybe none.

    Attributes:
        actions (Sequence[CorporateAction]): The actions in the order the broker served them, every page followed.
        as_of (datetime): When the last page was answered, timezone aware, UTC.
    """

    actions: Sequence[CorporateAction]
    as_of: datetime


@dataclass(frozen=True)
class CorporateActionsFailure:
    """A failure get_corporate_actions RETURNS rather than raises.

    Attributes:
        error (TraderJoeError): The typed error; the caught vendor exception, where there is one, is its __cause__.
    """

    error: TraderJoeError


class BrokerRead(Protocol):
    """What data_ingest reads from a broker."""

    async def get_corporate_actions(
        self, query: CorporateActionsQuery
    ) -> CorporateActionsResponse | CorporateActionsFailure:
        """Fetch a broker's corporate actions for one symbol over a window, every page, nothing filtered.

        Returns a CorporateActionsFailure, and never raises, for a failure that is expected: a missing credential or
        configuration, a rate budget that cannot admit a call before query.deadline, and a vendor that refuses or
        cannot answer. A failure on any page fails the whole read: never a partial list. A bug still raises.

        Args:
            query (CorporateActionsQuery): The symbol and the window.

        Returns:
            CorporateActionsResponse | CorporateActionsFailure: Every action served, or the typed failure.
        """
        ...

    async def get_assets(self, query: AssetListQuery) -> AssetListResponse | AssetListFailure:
        """Fetch one of the broker's asset lists, whole.

        Returns an AssetListFailure, and never raises, for a failure that is expected: a kind of asset the broker
        does not list (BrokerUnsupportedError), a missing credential or configuration, a rate budget that cannot admit
        the call before query.deadline, and a vendor that refuses or cannot answer. A bug still raises.

        Args:
            query (AssetListQuery): Which list to fetch.

        Returns:
            AssetListResponse | AssetListFailure: Every asset of the list, or the typed failure.
        """
        ...

    async def get_asset(self, query: AssetLookupQuery) -> AssetResponse | AssetNotFound | AssetLookupFailure:
        """Look up the broker's record for one symbol.

        The broker saying it has no such asset is AssetNotFound, an answer. Every other expected failure is an
        AssetLookupFailure: a rate limit carries its reset_at. This paces on the broker's asset-API scope and is
        not counted in any bars attempt's spend.

        Args:
            query (AssetLookupQuery): The symbol to look up.

        Returns:
            AssetResponse | AssetNotFound | AssetLookupFailure: The record, the not-found answer, or the failure.
        """
        ...

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
