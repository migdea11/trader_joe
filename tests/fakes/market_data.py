"""FakeRead: a deterministic BrokerRead for tests, seeds and the fake-mode stack.

Decision tj-j4wknb (R3, R4, addenda 2-5) and tj-vhboky.54 DECISION 3 as amended. FakeRead
conforms to data/ingest/app/brokers/interface.py's BrokerRead Protocol STRUCTURALLY: it inherits
nothing, not even from AlpacaRead, and carries no capability flags (none exist, addendum 4 item 5).
The launcher installs it in the DataSource.ALPACA_API slot, so its bars are labelled ALPACA; a
DataSource.FAKE member would be test instrumentation in production code (R4).

THE BARS. Every bar is a pure function of (symbol, timestamp) -- fake_bar() below -- so a test or
the seed producer computes what must come back instead of recording it. Timestamps lie on a grid
ANCHORED AT THE UNIX EPOCH: GRID_EPOCH + k * granularity.offset for integer k. Anchoring at the
epoch rather than at the query's start means two overlapping queries agree on every shared
timestamp, and which bars GAPS drops is itself a function of the timestamp, never of where the
query began. Granularity.ONE_MONTH steps 28 days, because that is its offset.

THE RANGE is half-open, [start, end), the same for every broker (addendum 5, tj-irhy0a.15): a bar
at t is served when start <= t < end, compared as instants, so a grid point exactly at end is
NEVER served. Bars are aware UTC whatever offset the query's bounds carry, ascending and unique.

AN OPEN-ENDED QUERY (end None) is bounded by a COUNT, not by the clock: it serves the first
OPEN_ENDED_STEPS grid points at or after start, exactly as a query whose end were that many steps
on. A wall-clock bound would make two identical queries disagree across a step boundary; an
unbounded one would never finish. Scenarios then apply to that range as to any other, so no
scenario runs forever.

THE TYPED RESULT (ADR tj-fa1rpu D1(a), D2, D3; TE-5 tj-3mk3u5.37.6). get_bars RETURNS a
BarsResponse (SERVED) or a BarsFailure, exactly as AlpacaRead does, and never raises for either.

* SERVED carries served_range and as_of. as_of is when the fake "answered", read from its clock
  after any SLOW_ delay. served_range starts at the query's start and ends at the range's end
  (end, or the OPEN_ENDED_STEPS bound for end None) clamped to as_of: a vendor cannot have answered
  for time after it answered, and a window this fake clamps by count is reported as clamped (D3
  note a), never as the whole open range. Every bar served lies inside served_range.
* Three REFUSALS come before any "vendor" work -- before a scenario is looked at, so no SLOW_
  sleep is slept and no FAILONCE_ memory is spent -- mirroring AlpacaRead and the Q-EMPTY ruling
  (tj-3mk3u5.37.1), so fake mode can show each end to end:
    - a query naming a feed other than this fake's deployment feed: FEED_NOT_AVAILABLE
      (BrokerUnsupportedError). The deployment feed is the `feed` argument, FEED unless set, so
      in the fake-mode stack a request naming SIP is the rejection case (tj-3mk3u5.9, 16:36 UTC
      2026-09-28: the fake's feed-entitlement setting);
    - a start at or after the fake's clock: RANGE_IN_FUTURE;
    - an end before the start: VENDOR_INVALID_REQUEST, as Alpaca's recorded 400 answers it.
* A vendor failure (FAIL_, FAILONCE_, RATELIMIT_) carries a FakeReadError as its error's
  __cause__, standing in for the exception a real adapter catches and keeps (D8, the 16:22 UTC
  2026-10-02 addendum item 5).

SCENARIOS are chosen by the requested symbol's PREFIX (no control channel, no restart, parallel
tests cannot interfere). Import the constants rather than retyping them. data_store upper-cases
symbols, so every prefix is upper case. No prefix starts with another (FAILONCE_ is not FAIL_
followed by more), so at most one ever matches and the order they are tried in cannot matter.

* default (no prefix matches) -- every grid bar of the range.
* EMPTY_ -- SERVED with zero bars, served_range as for any range (Q-EMPTY: never a failure).
* GAPS_ -- every other bar missing: only grid points with an EVEN index k are served.
* FAIL_ -- a BarsFailure, VENDOR_UNAVAILABLE, on every call, and nothing served. It reaches the
  caller as the typed failure it is: the ingest_control edge that used to turn any BarsFailure
  into a bare {} was deleted on tj-3mk3u5.11, so nothing swallows it any more.
* SLOW_ -- sleeps slow_delay_seconds (default DEFAULT_SLOW_DELAY_SECONDS, refused above
  MAX_SLOW_DELAY_SECONDS), then serves as default. Slow but SERVED: see the constant's own note
  for why it no longer outlives its caller's deadline.
* FAILONCE_ -- the first call for a given (symbol, granularity, start, end) fails as FAIL_ does;
  every later call serves as default. The memory is this INSTANCE's, in process: a service that
  runs more than one worker process fails once PER WORKER, so the fake-mode stack must run
  data_ingest with SERVICE_WORKERS=1.
* RATELIMIT_ -- a BarsFailure, VENDOR_RATE_LIMITED, on every call, whose reset_at is the fake's
  clock plus RATELIMIT_RESET_SECONDS: deterministic under an injected clock.
* NOTFOUND_ -- bars as default, but get_asset answers AssetNotFound: the vendor serves bars under a symbol it no
  longer lists (SQ after its rename to XYZ, P27a). A bars attempt for it fails PERMANENT after its bars (P12h).
* REUSED_ -- bars as default, but the vendor id behind the symbol CHANGES: the first get_asset of the symbol on this
  instance answers fake_asset(symbol), every later one fake_asset(symbol, holder=2), a different id. This is a reused
  ticker seen live (a new asset id behind an old symbol), the case the store's vendor-id stop (P50, tj-7zjov1.144)
  catches. The memory is this INSTANCE's, like FAILONCE_'s.

A true hang is not a scenario: nothing here blocks forever.

THE ASSET READS (tj-7zjov1.22, BrokerRead.get_assets and get_asset). Deterministic too, and nothing is judged:

* get_assets serves FAKE_LISTED_SYMBOLS for the ACTIVE list and FAKE_DELISTED_SYMBOLS for the INACTIVE one, each
  record fake_asset(symbol), in that order. Stocks only: another asset_type is refused UNSUPPORTED_ASSET_TYPE
  (BrokerUnsupportedError), because a record carries no asset class and would read as a stock listing.
* get_asset serves fake_asset(symbol) for ANY non-empty symbol without a NOTFOUND_ or REUSED_ prefix, so a fake-mode
  bars attempt that looks its symbol up after the bars (P12h) finds one. An empty symbol is INVALID_REQUEST, as
  AlpacaRead answers it.
* fake_asset is a pure function of the symbol (and the holder): its vendor id is a UUID5 of it, so two reads agree.

THE CORPORATE-ACTIONS READ (tj-7zjov1.23, BrokerRead.get_corporate_actions). Every query, whatever its symbol and
window, is SERVED with no actions, stamped with the fake's clock: never a failure, and no prefix scenario applies.
Empty because the fake stands for the interface: a window with no action is the commonest real answer, and an
invented action would be delivered and judged as if a vendor had published it. Actions are served only by a fake
built with a world (below), which a caller asks for by name.

THE WORLD (tj-7zjov1.24, P14): an OPT-IN scenario a FakeRead is built with (world=WORLD); without one every answer
above is unchanged. It replays what the vendor spikes saw (P27a, tj-7zjov1.7: s1, s3, s4, s10) on a few consecutive
fake days, chosen by the fake's clock:

* ASSET DAYS. WORLD_DAYS[n] is the asset list on WORLD_DAY_0 + n days; a clock before the first day reads the first
  and one after the last reads the last. Across the days: a rename keeps the asset id under a new symbol (FB to META,
  SQ to XYZ); a delisting is a DISAPPEARANCE from both lists (WPRT, and BUUU's old id, as s3 saw them: two of the six
  ids that left the lists, each still found by id as inactive); a ticker is reused by a new id (FB by ProShares, BUUU
  relisted under a new id, as s3 saw it); the inactive list drops an asset that still exists (WCCP, as s3 saw it).
  INVENTED, not replayed: a STATUS FLIP, one id moving from the active list to the inactive one (FLIPX). s3 recorded
  no flip between the lists in its one pair of sittings, but the store's snapshot diff (P17) must still handle one.
  ILLUSTRATIVE, not taken from a capture: the asset names and exchanges, and WCCP's empty name (s1 counted 535 empty
  names; WCCP is not known to be one). The records are built as vendor JSON and read by the production parser.
* ASSET BY SYMBOL in the world: the day's active record, else its inactive one; else the most recent earlier record
  that held the symbol, as INACTIVE, unless that asset is listed today under another symbol (renamed away), which is
  AssetNotFound, as is a world symbol no day so far has listed. So SQ is not found once it is XYZ, WPRT stays found
  as inactive after its delisting, and FB answers ProShares' id once ProShares holds it. A symbol the world does not
  list is answered as without a world. A DELISTED symbol found inactive BY SYMBOL is one of the two answers the
  vendor was recorded giving (ABMD and TWTR answered 200 inactive; SIVB answered 404): the fake serves the first.
* LINEAGE BARS. A world symbol's bars follow the lineage the vendor serves under it, queried by the latest symbol:
  META serves Facebook's series throughout, so META's bars before 2022-06-09 equal FB's; FB serves Facebook's series
  up to 2022-06-08, nothing until 2025-06-26, then ProShares'; SQ serves Block's to 2025-01-17 and XYZ Block's
  throughout. PARA's series holds a run of PLACEHOLDER bars inside real ones, shaped as the one recorded placeholder
  (s4, META with asof: open, high, low and close one flat price, volume 0, trade count 0, vwap 0). ILLUSTRATIVE: the
  run's dates (the recorded PARA pads lie between 2019-12 and 2020-12) and PARA's single lineage (the vendor serves
  the current holder's lineage under PARA; the fake does not model which).
* CORPORATE ACTIONS, as served: the name_change records FB to META (2022-06-09), META to METV (2022-01-31) and SQ to
  XYZ (2025-01-21) with a process_date only, and the stock_merger of XLNX into AMD (2022-02-14, both dates, acquirer
  rate 1.7234), each byte for byte as recorded; plus one record the vendor never served, AAPL's cash dividend, whose
  REVISION is served from WORLD_DAY_0 + 1 under the same id. A query matches a record by any of its symbol fields
  (old and new symbols alike) and keeps it when its process_date lies in [start, end).

PACING (optional): a FakeRead built with a scheduler acquires one call from it before each "vendor" call, as AlpacaRead
does -- bars and corporate actions on `scope`, the asset reads on `trading_scope` -- and then counts it in the query's
tally. A refusal before the vendor acquires nothing. Without a scheduler nothing is paced and nothing is counted.

THE CALL LOG: FakeRead.calls lists every call that reached the "vendor" (after the refusals), in order, as
(read, argument) pairs, so a test can tell whether a vendor call happened.
"""

import asyncio
import hashlib
import itertools
import json
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from common.enums.data_select import AssetType
from common.enums.data_stock import Feed, Granularity
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason, TraderJoeError
from data.ingest.app.brokers.alpaca.assets import asset_from_record
from data.ingest.app.brokers.alpaca.corporate_actions import action_from_record
from data.ingest.app.brokers.interface import (
    Asset,
    AssetListFailure,
    AssetListQuery,
    AssetListResponse,
    AssetLookupFailure,
    AssetLookupQuery,
    AssetNotFound,
    AssetResponse,
    Bar,
    BarsFailure,
    BarsQuery,
    BarsResponse,
    BrokerUnsupportedError,
    CallTally,
    CorporateAction,
    CorporateActionsFailure,
    CorporateActionsQuery,
    CorporateActionsResponse,
    ServedRange,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from schemas.data_ingest.submit import AssetListStatus


# The deployment feed a FakeRead serves unless built with another: the entitlement every fake
# response reports, known before iteration as the interface requires.
FEED = Feed.IEX

# Every bar timestamp is GRID_EPOCH + k * granularity.offset for an integer k.
GRID_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# How many grid points an open-ended query (end None) serves, counted from the first at or after start.
OPEN_ENDED_STEPS = 100

# A vendor that is SLOW BUT STILL SERVED. This was chosen to be longer than data_store's 5 s Kafka
# RPC deadline, so that SLOW_ outlived its caller; tj-3mk3u5.10 replaced that transport and set
# DEFAULT_FETCH_DEADLINE_S to 300 s deliberately, because 5 s abandoned requests the vendor was
# still serving. So 8 s outlives nothing now, and nothing permitted here would: the ceiling below
# is 60 s. tests/system/test_ingest_e2e.py asserts the served outcome and reversed its own
# expectation on tj-xhcoyc item 2 for this reason.
DEFAULT_SLOW_DELAY_SECONDS = 8.0
# SLOW_ is bounded: a delay above this is refused when the fake is built, never slept.
MAX_SLOW_DELAY_SECONDS = 60.0

# How far past the fake's clock a RATELIMIT_ failure says its window resets.
RATELIMIT_RESET_SECONDS = 60.0

# The detail of the end-before-start refusal: Alpaca's own recorded 400 message (error_400.json).
END_BEFORE_START_DETAIL = 'end should not be before start'

EMPTY_PREFIX = 'EMPTY_'
GAPS_PREFIX = 'GAPS_'
FAIL_PREFIX = 'FAIL_'
SLOW_PREFIX = 'SLOW_'
FAILONCE_PREFIX = 'FAILONCE_'
RATELIMIT_PREFIX = 'RATELIMIT_'
NOTFOUND_PREFIX = 'NOTFOUND_'
REUSED_PREFIX = 'REUSED_'

# The fake's asset lists, by status. Small and fixed: the fake stands for the interface, not for a market.
FAKE_LISTED_SYMBOLS = ('VFV', 'XEQT', 'AAPL')
FAKE_DELISTED_SYMBOLS = ('GONE',)
# The venue every fake asset names, as a vendor string carried, never judged.
FAKE_EXCHANGE = 'FAKE'
# Fixed, so a fake vendor id is the same in every process (uuid5 of the symbol in this namespace).
FAKE_ASSET_NAMESPACE = uuid.UUID('6f1c1b8e-2b4f-4c55-9a1e-6b8f5b2d7a10')


class Scenario(StrEnum):
    """What FakeRead does for a symbol, chosen by the symbol's prefix."""

    DEFAULT = 'default'
    EMPTY = 'empty'
    GAPS = 'gaps'
    FAIL = 'fail'
    SLOW = 'slow'
    FAILONCE = 'failonce'
    RATELIMIT = 'ratelimit'
    NOTFOUND = 'notfound'
    REUSED = 'reused'


# Every scenario but DEFAULT, by the prefix that selects it.
SCENARIO_PREFIXES: dict[str, Scenario] = {
    EMPTY_PREFIX: Scenario.EMPTY,
    GAPS_PREFIX: Scenario.GAPS,
    FAIL_PREFIX: Scenario.FAIL,
    SLOW_PREFIX: Scenario.SLOW,
    FAILONCE_PREFIX: Scenario.FAILONCE,
    RATELIMIT_PREFIX: Scenario.RATELIMIT,
    NOTFOUND_PREFIX: Scenario.NOTFOUND,
    REUSED_PREFIX: Scenario.REUSED,
}


class FakeReadError(Exception):
    """The vendor-side exception a FAIL_, FAILONCE_ or RATELIMIT_ failure carries as its error's __cause__.

    Never raised out of get_bars: it stands for what a real adapter catches from its vendor and keeps
    as the cause of the typed error it returns (ADR tj-fa1rpu D8, the 16:22 UTC 2026-10-02 addendum).
    """


def _utc_now() -> datetime:
    return datetime.now(UTC)


def scenario_for(symbol: str) -> Scenario:
    """Choose the scenario a symbol selects.

    Args:
        symbol (str): Requested symbol.

    Returns:
        Scenario: The scenario of the one prefix the symbol starts with, or DEFAULT.
    """
    for prefix in SCENARIO_PREFIXES:
        if symbol.startswith(prefix):
            return SCENARIO_PREFIXES[prefix]
    return Scenario.DEFAULT


def grid_index(granularity: Granularity, timestamp: datetime) -> int:
    """Index k of the grid point at or before a timestamp: GRID_EPOCH + k * granularity.offset.

    Args:
        granularity (Granularity): Grid step.
        timestamp (datetime): Timezone-aware instant.

    Returns:
        int: The grid index, floor-rounded.
    """
    return (timestamp - GRID_EPOCH) // granularity.offset


def grid_timestamps(granularity: Granularity, start: datetime, end: datetime | None) -> Iterator[datetime]:
    """Every grid point of [start, end), ascending, aware UTC.

    Args:
        granularity (Granularity): Grid step.
        start (datetime): Inclusive start, timezone aware.
        end (datetime | None): Exclusive end, timezone aware, or None for OPEN_ENDED_STEPS points.

    Yields:
        datetime: Each grid point t with start <= t < end.
    """
    step = granularity.offset
    stop = range_end(granularity, start, end)
    timestamp = GRID_EPOCH + _first_index(granularity, start) * step
    while timestamp < stop:
        yield timestamp
        timestamp += step


def _first_index(granularity: Granularity, start: datetime) -> int:
    # Ceiling division: the index of the first grid point at or after start.
    return -((GRID_EPOCH - start) // granularity.offset)


def range_end(granularity: Granularity, start: datetime, end: datetime | None) -> datetime:
    """The exclusive end of the range FakeRead serves for [start, end), before any clamp to its clock.

    Args:
        granularity (Granularity): Grid step.
        start (datetime): Inclusive start, timezone aware.
        end (datetime | None): Exclusive end, timezone aware, or None for an open-ended query.

    Returns:
        datetime: end itself, or for end None the grid point OPEN_ENDED_STEPS steps on from the first
            grid point at or after start: the count bound.
    """
    if end is not None:
        return end
    return GRID_EPOCH + (_first_index(granularity, start) + OPEN_ENDED_STEPS) * granularity.offset


def _unit(digest: bytes, slot: int) -> float:
    """Read four bytes of a digest as a number in [0, 1)."""
    return int.from_bytes(digest[slot * 4 : slot * 4 + 4], 'big') / 2**32


def fake_bar(symbol: str, timestamp: datetime) -> Bar:
    """The bar FakeRead serves for a symbol at a timestamp: a pure function of the two.

    Built from SHA-256 rather than hash(), which is salted per process: the seed producer and the
    stack must compute the same bars in different interpreters. Prices sit near a per-symbol level
    between 10 and 500 and satisfy low <= open, close, vwap <= high. volume and trade_count are
    always ints and never None.

    Args:
        symbol (str): Requested symbol, prefix included.
        timestamp (datetime): Bar start, timezone aware.

    Returns:
        Bar: The bar, stamped in UTC.
    """
    utc = timestamp.astimezone(UTC)
    level = 10.0 + 490.0 * _unit(hashlib.sha256(symbol.encode()).digest(), 0)
    digest = hashlib.sha256(f'{symbol}|{utc.isoformat()}'.encode()).digest()
    open_ = round(level * (0.98 + 0.04 * _unit(digest, 0)), 4)
    close = round(level * (0.98 + 0.04 * _unit(digest, 1)), 4)
    high = round(max(open_, close) * (1 + 0.01 * _unit(digest, 2)), 4)
    low = round(min(open_, close) * (1 - 0.01 * _unit(digest, 3)), 4)
    return Bar(
        timestamp=utc,
        open=open_,
        high=high,
        low=low,
        close=close,
        # Both ints, never None: the store's batch schema requires int volume and trade_count, and
        # a None there is an open question on tj-irhy0a.7, not something the fake should provoke.
        volume=1_000 + int(99_000 * _unit(digest, 4)),
        trade_count=10 + int(990 * _unit(digest, 5)),
        vwap=round(low + (high - low) * _unit(digest, 6), 4),
    )


def fake_asset(symbol: str, *, holder: int = 1) -> Asset:
    """The asset record FakeRead serves for a symbol: a pure function of it and of which holder it names.

    Active unless the symbol is one of FAKE_DELISTED_SYMBOLS; tradable exactly when active. The raw payload is
    the record's own JSON, as a vendor would serve it. holder 1 is the symbol's first holder, the record every
    scenario but REUSED_'s later lookups serves; another holder is a different asset behind the same symbol, with an
    id of its own.

    Args:
        symbol (str): The symbol, non-empty.
        holder (int): Which holder of the symbol, from 1.

    Returns:
        Asset: The record.

    Raises:
        ValueError: If holder is below 1.
    """
    if holder < 1:
        raise ValueError(f'a holder is counted from 1, got {holder!r}')
    active = symbol not in FAKE_DELISTED_SYMBOLS
    # Holder 1 keeps the id every earlier release derived, so existing seeds and pins do not move.
    seed = symbol if holder == 1 else f'{symbol}#holder{holder}'
    vendor_asset_id = str(uuid.uuid5(FAKE_ASSET_NAMESPACE, seed))
    name = f'{symbol} fake listing' if holder == 1 else f'{symbol} fake listing, holder {holder}'
    record = {
        'id': vendor_asset_id,
        'class': 'us_equity',
        'exchange': FAKE_EXCHANGE,
        'symbol': symbol,
        'name': name,
        'status': 'active' if active else 'inactive',
        'tradable': active,
    }
    return Asset(
        vendor_asset_id=vendor_asset_id,
        symbol=symbol,
        raw_payload=json.dumps(record).encode('utf-8'),
        active=active,
        tradable=active,
        exchange=FAKE_EXCHANGE,
        name=record['name'],
    )


# ---------------------------------------------------------------------------------------------
# The world: an opt-in scenario of asset days, lineage bars and corporate actions (P14)
# ---------------------------------------------------------------------------------------------

# Fixed, so a world asset's id is the same in every process (uuid5 of its key in this namespace).
WORLD_ASSET_NAMESPACE = uuid.UUID('0b7e5f1c-9a8d-4f3e-b2c1-5d6e7f8a9b0c')
# The seed every lineage's bars are computed from: fake_bar(LINEAGE_SEED + series, t), so two symbols serving one
# lineage serve the same bars and no plain symbol's bars can collide with a lineage's.
LINEAGE_SEED = 'lineage:'


@dataclass(frozen=True)
class WorldAsset:
    """One asset of the world: an id that never changes, under whatever symbol a day lists it.

    Attributes:
        key (str): The asset's own name in the world, from which its vendor id is derived; never a symbol.
        symbol (str): The ticker it is listed under on the days that hold this entry.
        name (str): The vendor's name for it; may be empty.
        exchange (str): The vendor's venue wording.
    """

    key: str
    symbol: str
    name: str
    exchange: str = 'NASDAQ'

    @property
    def vendor_asset_id(self) -> str:
        """str: The asset's vendor id, the same on every day and under every symbol."""
        return str(uuid.uuid5(WORLD_ASSET_NAMESPACE, self.key))

    def record(self, active: bool) -> Asset:
        """The vendor record of this asset as listed active or inactive, read by the production parser.

        Args:
            active (bool): Whether the record is the active list's (status active, tradable) or the inactive one's.

        Returns:
            Asset: The record, its raw payload the JSON it was built from.
        """
        record = {
            'id': self.vendor_asset_id,
            'class': 'us_equity',
            'exchange': self.exchange,
            'symbol': self.symbol,
            'name': self.name,
            'status': 'active' if active else 'inactive',
            'tradable': active,
        }
        return asset_from_record(record, json.dumps(record).encode('utf-8'))


@dataclass(frozen=True)
class AssetDay:
    """The vendor's two asset lists on one day.

    Attributes:
        day (date): The day, UTC.
        active (tuple[WorldAsset, ...]): The active list, in the order served.
        inactive (tuple[WorldAsset, ...]): The inactive list, in the order served.
    """

    day: date
    active: tuple[WorldAsset, ...]
    inactive: tuple[WorldAsset, ...]

    def holding(self, symbol: str) -> tuple[WorldAsset, bool] | None:
        """The asset listed under a symbol on this day and whether it is on the active list, or None."""
        for listing, active in ((self.active, True), (self.inactive, False)):
            for asset in listing:
                if asset.symbol == symbol:
                    return asset, active
        return None

    def lists_key(self, key: str) -> bool:
        """Whether either list holds the asset with this key, under any symbol."""
        return any(asset.key == key for asset in (*self.active, *self.inactive))


@dataclass(frozen=True)
class Segment:
    """A stretch of a symbol's bars served from one lineage: [start, end), either end open when None.

    Attributes:
        series (str): The lineage the bars are computed from.
        start (datetime | None): Inclusive start, aware, or None for no bound.
        end (datetime | None): Exclusive end, aware, or None for no bound.
        placeholder (bool): Whether the stretch is the vendor's padding: volume 0, trade count 0, one flat price.
    """

    series: str
    start: datetime | None = None
    end: datetime | None = None
    placeholder: bool = False

    def covers(self, timestamp: datetime) -> bool:
        """Whether the stretch holds a timestamp."""
        return (self.start is None or self.start <= timestamp) and (self.end is None or timestamp < self.end)


@dataclass(frozen=True)
class WorldAction:
    """One corporate-action record as the vendor serves it, on the days it is served.

    Attributes:
        group (str): The vendor's group name, served as the action's type.
        record (Mapping[str, Any]): The record as the vendor's JSON object.
        raw (bytes): Its bytes as served.
        first_day (date | None): The first day it is served, or None for every day before.
        last_day (date | None): The last day it is served, or None for every day after: a revised record ends here.
    """

    group: str
    record: Mapping[str, Any]
    raw: bytes
    first_day: date | None = None
    last_day: date | None = None

    def served_on(self, day: date) -> bool:
        """Whether the vendor serves this version of the record on a day."""
        return (self.first_day is None or self.first_day <= day) and (self.last_day is None or day <= self.last_day)

    def matches(self, symbol: str, start: datetime, end: datetime) -> bool:
        """Whether a query for a symbol over [start, end) returns the record.

        It does when a symbol field of the record names the symbol and its process_date lies in the window.
        """
        named = any(
            isinstance(value, str) and value == symbol and (name == 'symbol' or name.endswith('_symbol'))
            for name, value in self.record.items()
        )
        process_date = self.record.get('process_date')
        if not named or not isinstance(process_date, str):
            return False
        day = date.fromisoformat(process_date)
        return start <= datetime(day.year, day.month, day.day, tzinfo=UTC) < end

    def action(self) -> CorporateAction:
        """The record as the production parser reads it."""
        return action_from_record(self.group, dict(self.record), self.raw)


def world_action(group: str, raw: str, first_day: date | None = None, last_day: date | None = None) -> WorldAction:
    """Build a WorldAction from a record's bytes as served.

    Args:
        group (str): The vendor's group name.
        raw (str): The record's JSON as served, byte for byte.
        first_day (date | None): The first day it is served.
        last_day (date | None): The last day it is served.

    Returns:
        WorldAction: The record.
    """
    return WorldAction(group, json.loads(raw), raw.encode('utf-8'), first_day, last_day)


class FakeWorld:
    """Asset days, lineage bars and corporate actions, chosen by the fake's clock. See the module docstring.

    Args:
        days (Sequence[AssetDay]): The asset lists, one per consecutive day, ascending; at least one.
        lineages (Mapping[str, Sequence[Segment]]): Each lineage symbol's stretches, not overlapping.
        actions (Sequence[WorldAction]): Every corporate-action record version, in the order served.

    Raises:
        ValueError: If there is no day, or the days are not consecutive and ascending.
    """

    def __init__(
        self, days: Sequence[AssetDay], lineages: Mapping[str, Sequence[Segment]], actions: Sequence[WorldAction]
    ) -> None:
        if not days:
            raise ValueError('a world has at least one asset day')
        for before, after in itertools.pairwise(days):
            if after.day - before.day != timedelta(days=1):
                raise ValueError(f'asset days are consecutive: {before.day} is followed by {after.day}')
        self.__days = tuple(days)
        self.__lineages = {symbol: tuple(segments) for symbol, segments in lineages.items()}
        self.__actions = tuple(actions)
        self.__symbols = frozenset(asset.symbol for day in days for asset in (*day.active, *day.inactive))

    @property
    def days(self) -> tuple[AssetDay, ...]:
        """tuple[AssetDay, ...]: The asset days, ascending."""
        return self.__days

    @property
    def symbols(self) -> frozenset[str]:
        """frozenset[str]: Every symbol any day lists: the symbols whose lookups the world answers."""
        return self.__symbols

    def day_index(self, now: datetime) -> int:
        """The index of the day a clock reading falls on, clamped to the first and the last day.

        Args:
            now (datetime): The fake's clock, aware.

        Returns:
            int: The index into days.
        """
        offset = (now.astimezone(UTC).date() - self.__days[0].day).days
        return min(max(offset, 0), len(self.__days) - 1)

    def assets(self, status: AssetListStatus, now: datetime) -> list[Asset]:
        """The day's list of one status, as vendor records."""
        day = self.__days[self.day_index(now)]
        if status is AssetListStatus.ACTIVE:
            return [asset.record(True) for asset in day.active]
        return [asset.record(False) for asset in day.inactive]

    def lookup(self, symbol: str, now: datetime) -> Asset | None:
        """The record a lookup by symbol answers on the day, or None for not found. See the module docstring.

        Args:
            symbol (str): A world symbol.
            now (datetime): The fake's clock, aware.

        Returns:
            Asset | None: The record, or None when the vendor has no asset under the symbol.
        """
        index = self.day_index(now)
        today = self.__days[index]
        held = today.holding(symbol)
        if held is not None:
            asset, active = held
            return asset.record(active)
        for earlier in reversed(self.__days[:index]):
            held = earlier.holding(symbol)
            if held is None:
                continue
            asset, _active = held
            if today.lists_key(asset.key):
                # Listed today under another symbol: the symbol was renamed away from it.
                return None
            return asset.record(False)
        return None

    def lineage(self, symbol: str) -> tuple[Segment, ...] | None:
        """The stretches a lineage symbol's bars are served from, or None for a symbol with no lineage."""
        return self.__lineages.get(symbol)

    def actions(self, symbol: str, start: datetime, end: datetime, now: datetime) -> list[CorporateAction]:
        """Every record version served on the day that a query for the symbol over [start, end) returns, in order."""
        day = self.__days[self.day_index(now)].day
        return [
            action.action() for action in self.__actions if action.served_on(day) and action.matches(symbol, start, end)
        ]


def lineage_bar(symbol_segment: Segment, timestamp: datetime) -> Bar:
    """The bar a lineage stretch serves at a timestamp: the lineage's own bar, or its placeholder.

    A placeholder is flat at the close of the lineage's bar at the stretch's start, with volume 0, trade count 0 and
    vwap 0: the shape of the one recorded placeholder (s4, META with asof, tj-7zjov1.90 fact 4). The flat price itself
    is illustrative.

    Args:
        symbol_segment (Segment): The stretch holding the timestamp.
        timestamp (datetime): Bar start, aware.

    Returns:
        Bar: The bar, stamped in UTC.
    """
    seed = f'{LINEAGE_SEED}{symbol_segment.series}'
    if not symbol_segment.placeholder:
        return fake_bar(seed, timestamp)
    anchor = symbol_segment.start if symbol_segment.start is not None else GRID_EPOCH
    price = fake_bar(seed, anchor).close
    return Bar(
        timestamp=timestamp.astimezone(UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=0,
        trade_count=0,
        vwap=0.0,
    )


# The first asset day. The world's days are consecutive fake days: they replay the spikes' facts in order, so a
# rename and a reuse that were years apart at the vendor are a day apart here.
WORLD_DAY_0 = date(2026, 10, 26)

# Names and exchanges are ILLUSTRATIVE, not taken from a capture; the symbols, the renames and the reuses are replayed.
_AAPL = WorldAsset('apple', 'AAPL', 'Apple Inc. Common Stock')
_FACEBOOK_FB = WorldAsset('facebook', 'FB', 'Meta Platforms, Inc. Class A Common Stock')
_FACEBOOK_META = WorldAsset('facebook', 'META', 'Meta Platforms, Inc. Class A Common Stock')
_ROUNDHILL = WorldAsset('roundhill', 'METV', 'Roundhill Ball Metaverse ETF', 'ARCA')
_BLOCK_SQ = WorldAsset('block', 'SQ', 'Block, Inc. Class A Common Stock', 'NYSE')
_BLOCK_XYZ = WorldAsset('block', 'XYZ', 'Block, Inc. Class A Common Stock', 'NYSE')
_AMD = WorldAsset('amd', 'AMD', 'Advanced Micro Devices, Inc. Common Stock')
_XILINX = WorldAsset('xilinx', 'XLNX', 'Xilinx, Inc. Common Stock')
_PROSHARES_FB = WorldAsset('proshares', 'FB', 'ProShares S&P 500 Dynamic Buffer ETF', 'ARCA')
_WESTPORT = WorldAsset('westport', 'WPRT', 'Westport Fuel Systems Inc. Common Shares')
_BUUU_OLD = WorldAsset('buuu-old', 'BUUU', 'BUUU Trust Units')
_BUUU_NEW = WorldAsset('buuu-new', 'BUUU', 'BUUU Trust Units')
# The one asset whose vendor name is empty. ILLUSTRATIVE: s1 counted 535 empty names, and WCCP is not known to be one.
_WCCP = WorldAsset('wccp', 'WCCP', '', 'OTC')
# INVENTED, not replayed: the status flip (s3 recorded none between the lists), kept because P17 must handle one.
_INVENTED_FLIP = WorldAsset('invented-flip', 'FLIPX', 'Invented status-flip asset')

WORLD_DAYS = (
    # Day 0: the baseline.
    AssetDay(
        WORLD_DAY_0,
        active=(_AAPL, _FACEBOOK_FB, _ROUNDHILL, _BLOCK_SQ, _AMD, _WESTPORT, _BUUU_OLD, _INVENTED_FLIP),
        inactive=(_XILINX, _WCCP),
    ),
    # Day 1: two renames (FB to META, SQ to XYZ: same ids); WPRT and BUUU's old id vanish from both lists (delistings,
    # as s3 saw them); INVENTED: FLIPX's id moves from the active list to the inactive one (a status flip).
    AssetDay(
        WORLD_DAY_0 + timedelta(days=1),
        active=(_AAPL, _FACEBOOK_META, _ROUNDHILL, _BLOCK_XYZ, _AMD),
        inactive=(_XILINX, _WCCP, _INVENTED_FLIP),
    ),
    # Day 2: two reused tickers appear (FB under ProShares' id, BUUU relisted under a new id, as s3 saw it); the
    # inactive list drops WCCP, though the asset still exists (as s3 saw it).
    AssetDay(
        WORLD_DAY_0 + timedelta(days=2),
        active=(_AAPL, _FACEBOOK_META, _ROUNDHILL, _BLOCK_XYZ, _AMD, _PROSHARES_FB, _BUUU_NEW),
        inactive=(_XILINX, _INVENTED_FLIP),
    ),
)

# The day Facebook's FB became META (the name_change process_date), and the first FB bar ProShares' lineage serves.
FB_RENAMED_ON = datetime(2022, 6, 9, tzinfo=UTC)
PROSHARES_FB_FROM = datetime(2025, 6, 26, tzinfo=UTC)
# The day after the last SQ bar the vendor serves (SQ bars to 2025-01-17).
SQ_SERVED_UNTIL = datetime(2025, 1, 18, tzinfo=UTC)
# PARA's run of placeholder bars, [start, end), inside its real series. ILLUSTRATIVE dates: the recorded PARA pads lie
# between 2019-12 and 2020-12. PARA's single 'paramount' lineage is illustrative too.
PARA_PLACEHOLDERS = (datetime(2025, 8, 4, tzinfo=UTC), datetime(2025, 8, 9, tzinfo=UTC))

WORLD_LINEAGES: dict[str, tuple[Segment, ...]] = {
    'META': (Segment('facebook'),),
    'FB': (Segment('facebook', end=FB_RENAMED_ON), Segment('proshares', start=PROSHARES_FB_FROM)),
    'SQ': (Segment('block', end=SQ_SERVED_UNTIL),),
    'XYZ': (Segment('block'),),
    'PARA': (
        Segment('paramount', end=PARA_PLACEHOLDERS[0]),
        Segment('paramount', start=PARA_PLACEHOLDERS[0], end=PARA_PLACEHOLDERS[1], placeholder=True),
        Segment('paramount', start=PARA_PLACEHOLDERS[1]),
    ),
}

# The recorded records, byte for byte as served (fixtures corporate_actions_name_changes.json and
# corporate_actions_xlnx_stock_merger.json, P27a s10).
FB_TO_META = (
    '{"id":"ed6f16b1-fc46-443e-8dba-898ed9007a7a","new_cusip":"30303M102","new_symbol":"META",'
    '"old_cusip":"30303M102","old_symbol":"FB","process_date":"2022-06-09"}'
)
META_TO_METV = (
    '{"id":"19a51822-eda9-40e2-9301-b8958c74a003","new_cusip":"53656F417","new_symbol":"METV",'
    '"old_cusip":"53656F417","old_symbol":"META","process_date":"2022-01-31"}'
)
SQ_TO_XYZ = (
    '{"id":"959d6941-3cb9-4b2d-a282-543a0a3f05d1","new_cusip":"852234103","new_symbol":"XYZ",'
    '"old_cusip":"852234103","old_symbol":"SQ","process_date":"2025-01-21"}'
)
XLNX_INTO_AMD = (
    '{"acquiree_cusip":"983919101","acquiree_rate":1,"acquiree_symbol":"XLNX","acquirer_cusip":"007903107",'
    '"acquirer_rate":1.7234,"acquirer_symbol":"AMD","effective_date":"2022-02-14",'
    '"id":"15459aa5-b297-4e33-9624-c9d59b75ba46","process_date":"2022-02-14"}'
)
# Invented (the vendor never served it): one record the vendor REVISES under the same id, served as first published
# on day 0 and as revised from day 1.
AAPL_DIVIDEND_ID = 'f4a3c0de-0000-4000-8000-00000000a001'
AAPL_DIVIDEND = (
    f'{{"cusip":"037833100","ex_date":"2026-08-11","foreign":false,"id":"{AAPL_DIVIDEND_ID}",'
    '"payable_date":"2026-08-14","process_date":"2026-08-11","rate":0.25,"record_date":"2026-08-11",'
    '"special":false,"symbol":"AAPL"}'
)
AAPL_DIVIDEND_REVISED = (
    f'{{"cusip":"037833100","ex_date":"2026-08-11","foreign":false,"id":"{AAPL_DIVIDEND_ID}",'
    '"payable_date":"2026-08-15","process_date":"2026-08-11","rate":0.26,"record_date":"2026-08-11",'
    '"special":false,"symbol":"AAPL"}'
)

WORLD_ACTIONS = (
    world_action('name_changes', FB_TO_META),
    world_action('name_changes', META_TO_METV),
    world_action('name_changes', SQ_TO_XYZ),
    world_action('stock_mergers', XLNX_INTO_AMD),
    world_action('cash_dividends', AAPL_DIVIDEND, last_day=WORLD_DAY_0),
    world_action('cash_dividends', AAPL_DIVIDEND_REVISED, first_day=WORLD_DAY_0 + timedelta(days=1)),
)

WORLD = FakeWorld(WORLD_DAYS, WORLD_LINEAGES, WORLD_ACTIONS)


class Pacer(Protocol):
    """What a paced FakeRead acquires its calls from: the one member of Scheduler it uses."""

    async def acquire(
        self, scope: str, priority: RequestPriority = RequestPriority.INTERACTIVE, deadline: datetime | None = None
    ) -> None:
        """Wait for one call of the scope, or raise RATE_BUDGET when the wait would pass the deadline."""
        ...


class FakeRead:
    """A deterministic BrokerRead: bars from fake_bar(), behaviour chosen by the symbol's prefix.

    See the module docstring for the grid, the half-open range, the open-ended bound, the typed
    result, the refusals and each scenario.

    Args:
        slow_delay_seconds (float): How long SLOW_ sleeps before serving. Finite, 0 to
            MAX_SLOW_DELAY_SECONDS inclusive.
        sleep (Callable[[float], Awaitable[None]]): What SLOW_ awaits; asyncio.sleep unless a
            test injects its own.
        clock (Callable[[], datetime]): Yields now, timezone aware: the future-range refusal, the
            as_of a response is stamped with, the end served_range is clamped to and RATELIMIT_'s
            reset_at all read it. Injectable, as AlpacaRead's is, so tests need not sleep.
        feed (Feed): The deployment feed: the tape this fake serves, and the only one a query may
            name. FEED unless set.
        world (FakeWorld | None): The world to serve (asset days, lineage bars, corporate actions), or None for
            the default answers.
        scheduler (Pacer | None): What each "vendor" call acquires one call from, or None for no pacing.
        scope (str | None): The quota scope bars and corporate actions are paced on; required with a scheduler.
        trading_scope (str | None): The quota scope the asset reads are paced on; scope when None.

    Raises:
        ValueError: If slow_delay_seconds is not finite or lies outside 0 to MAX_SLOW_DELAY_SECONDS, or a
            scheduler is given without a scope.
    """

    def __init__(
        self,
        slow_delay_seconds: float = DEFAULT_SLOW_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
        feed: Feed = FEED,
        *,
        world: FakeWorld | None = None,
        scheduler: Pacer | None = None,
        scope: str | None = None,
        trading_scope: str | None = None,
    ) -> None:
        if not (math.isfinite(slow_delay_seconds) and 0 <= slow_delay_seconds <= MAX_SLOW_DELAY_SECONDS):
            raise ValueError(
                f'slow_delay_seconds must be finite and within 0 to {MAX_SLOW_DELAY_SECONDS}: '
                f'slow_delay_seconds={slow_delay_seconds!r}'
            )
        if scheduler is not None and not scope:
            raise ValueError('a paced FakeRead needs the quota scope its calls are charged to')
        self.__slow_delay_seconds = slow_delay_seconds
        self.__sleep = sleep
        self.__clock = clock
        self.__feed = feed
        self.__world = world
        self.__scheduler = scheduler
        self.__scope = scope
        self.__trading_scope = trading_scope or scope
        self.__failed_once: set[tuple[str, Granularity, datetime, datetime | None]] = set()
        self.__looked_up: set[str] = set()
        self.__calls: list[tuple[str, str]] = []

    @property
    def calls(self) -> tuple[tuple[str, str], ...]:
        """tuple[tuple[str, str], ...]: Every call that reached the "vendor", in order.

        Each is ('bars', symbol), ('assets', status), ('asset', symbol) or ('corporate_actions', symbol).
        """
        return tuple(self.__calls)

    @property
    def world(self) -> FakeWorld | None:
        """FakeWorld | None: The world this fake serves, or None for the default answers."""
        return self.__world

    @property
    def slow_delay_seconds(self) -> float:
        """How long SLOW_ sleeps before serving, in seconds."""
        return self.__slow_delay_seconds

    @property
    def feed(self) -> Feed:
        """The deployment feed this fake serves."""
        return self.__feed

    async def get_bars(self, query: BarsQuery) -> BarsResponse | BarsFailure:
        """Serve fake bars for one instrument, per the scenario its symbol selects.

        Args:
            query (BarsQuery): What to fetch.

        Returns:
            BarsResponse | BarsFailure: The deployment feed, the bars of [start, end) (or of
                OPEN_ENDED_STEPS grid points when end is None) up to as_of, served_range and as_of;
                or the failure: FEED_NOT_AVAILABLE, RANGE_IN_FUTURE or VENDOR_INVALID_REQUEST before
                any scenario, VENDOR_UNAVAILABLE for FAIL_ and a first FAILONCE_, VENDOR_RATE_LIMITED
                for RATELIMIT_.
        """
        refusal = self.__refusal(query)
        if refusal is not None:
            return BarsFailure(refusal)

        symbol = query.instrument.symbol
        paced = await self.__pace('bars', symbol, self.__scope, query.priority, query.deadline, query.tally)
        if paced is not None:
            return BarsFailure(paced)
        scenario = scenario_for(symbol)
        if scenario is Scenario.FAIL:
            return self.__unavailable(f'FAIL scenario: symbol={symbol!r}')
        if scenario is Scenario.FAILONCE:
            key = (symbol, query.granularity, query.start, query.end)
            if key not in self.__failed_once:
                self.__failed_once.add(key)
                return self.__unavailable(f'FAILONCE scenario, first call: symbol={symbol!r}')
        if scenario is Scenario.RATELIMIT:
            return self.__rate_limited(symbol)
        if scenario is Scenario.SLOW:
            await self.__sleep(self.__slow_delay_seconds)

        # When the "vendor" answered: after any delay, so a slow answer is stamped late, as a real one is.
        as_of = self.__clock().astimezone(UTC)
        served_end = min(range_end(query.granularity, query.start, query.end), as_of)
        if query.tally is not None and self.__scheduler is not None:
            query.tally.record_page()
        lineage = self.__world.lineage(symbol) if self.__world is not None else None
        return BarsResponse(
            feed=self.__feed,
            bars=self.__iterate(query, scenario, served_end, lineage),
            served_range=ServedRange(query.start, served_end),
            as_of=as_of,
        )

    async def get_assets(self, query: AssetListQuery) -> AssetListResponse | AssetListFailure:
        """Serve the fake asset list the query's status names.

        Args:
            query (AssetListQuery): Which list.

        Returns:
            AssetListResponse | AssetListFailure: FAKE_LISTED_SYMBOLS or FAKE_DELISTED_SYMBOLS as fake_asset
                records (or the world's list of the day), stamped with the fake's clock; UNSUPPORTED_ASSET_TYPE for
                a kind that is not a stock; or RATE_BUDGET from a paced fake whose scheduler cannot admit the call.
        """
        if query.asset_type is not AssetType.STOCK:
            return AssetListFailure(
                BrokerUnsupportedError(
                    Reason.UNSUPPORTED_ASSET_TYPE, f'The fake lists stocks only: asset_type={query.asset_type!r}'
                )
            )
        paced = await self.__pace(
            'assets', query.status.value, self.__trading_scope, query.priority, query.deadline, query.tally
        )
        if paced is not None:
            return AssetListFailure(paced)
        as_of = self.__clock().astimezone(UTC)
        if query.tally is not None and self.__scheduler is not None:
            query.tally.record_page()
        if self.__world is not None:
            return AssetListResponse(assets=self.__world.assets(query.status, as_of), as_of=as_of)
        symbols = FAKE_LISTED_SYMBOLS if query.status is AssetListStatus.ACTIVE else FAKE_DELISTED_SYMBOLS
        return AssetListResponse(assets=[fake_asset(symbol) for symbol in symbols], as_of=as_of)

    async def get_asset(self, query: AssetLookupQuery) -> AssetResponse | AssetNotFound | AssetLookupFailure:
        """Serve the fake record of one symbol: every non-empty symbol has one but NOTFOUND_'s and some world ones.

        Args:
            query (AssetLookupQuery): The symbol.

        Returns:
            AssetResponse | AssetNotFound | AssetLookupFailure: The record stamped with the fake's clock --
                fake_asset(symbol), a REUSED_ symbol's later holder after its first lookup, or the world's answer
                for a world symbol; AssetNotFound for NOTFOUND_ and a world symbol with no asset under it today;
                INVALID_REQUEST for an empty symbol; or RATE_BUDGET from a paced fake.
        """
        symbol = query.symbol
        if not symbol.strip():
            return AssetLookupFailure(InvalidRequestError(Reason.INVALID_REQUEST, 'An asset lookup needs a symbol'))
        paced = await self.__pace('asset', symbol, self.__trading_scope, query.priority, query.deadline, None)
        if paced is not None:
            return AssetLookupFailure(paced)
        as_of = self.__clock().astimezone(UTC)
        if self.__world is not None and symbol in self.__world.symbols:
            found = self.__world.lookup(symbol, as_of)
            return AssetNotFound(symbol, as_of) if found is None else AssetResponse(asset=found, as_of=as_of)
        scenario = scenario_for(symbol)
        if scenario is Scenario.NOTFOUND:
            return AssetNotFound(symbol, as_of)
        if scenario is Scenario.REUSED:
            holder = 2 if symbol in self.__looked_up else 1
            self.__looked_up.add(symbol)
            return AssetResponse(asset=fake_asset(symbol, holder=holder), as_of=as_of)
        return AssetResponse(asset=fake_asset(symbol), as_of=as_of)

    async def get_corporate_actions(
        self, query: CorporateActionsQuery
    ) -> CorporateActionsResponse | CorporateActionsFailure:
        """Serve the world's actions for the symbol and window, or none without a world.

        Args:
            query (CorporateActionsQuery): The symbol and the window.

        Returns:
            CorporateActionsResponse | CorporateActionsFailure: A response stamped with the fake's clock: no actions
                without a world, the world's records of the day that match with one. RATE_BUDGET only from a paced
                fake; otherwise never a failure.
        """
        paced = await self.__pace(
            'corporate_actions', query.symbol, self.__scope, query.priority, query.deadline, query.tally
        )
        if paced is not None:
            return CorporateActionsFailure(paced)
        as_of = self.__clock().astimezone(UTC)
        if query.tally is not None and self.__scheduler is not None:
            query.tally.record_page()
        if self.__world is None:
            return CorporateActionsResponse(actions=[], as_of=as_of)
        return CorporateActionsResponse(
            actions=self.__world.actions(query.symbol, query.start, query.end, as_of), as_of=as_of
        )

    async def __pace(
        self,
        read: str,
        argument: str,
        scope: str | None,
        priority: RequestPriority,
        deadline: datetime | None,
        tally: CallTally | None,
    ) -> TraderJoeError | None:
        """Log a call that reached the "vendor" and, when paced, acquire it and count it.

        Returns:
            TraderJoeError | None: The scheduler's refusal (RATE_BUDGET), or None to go on with the call.
        """
        if self.__scheduler is not None and scope is not None:
            try:
                await self.__scheduler.acquire(scope, priority, deadline)
            except TraderJoeError as error:
                return error
            if tally is not None:
                tally.record(self.__clock())
        self.__calls.append((read, argument))
        return None

    def __refusal(self, query: BarsQuery) -> BrokerUnsupportedError | InvalidRequestError | None:
        """The refusal a query earns before any "vendor" work, or None to go on to the scenario."""
        if query.feed is not None and query.feed != self.__feed:
            return BrokerUnsupportedError(
                Reason.FEED_NOT_AVAILABLE,
                f'This account cannot serve the requested feed: feed={query.feed.value}',
                metadata={'feed': query.feed.value},
            )
        now = self.__clock()
        if query.start >= now:
            return InvalidRequestError(
                Reason.RANGE_IN_FUTURE,
                f'The range starts at or after now: start={query.start.isoformat()}, now={now.isoformat()}',
                metadata={'range_start': query.start.isoformat()},
            )
        if query.end is not None and query.end < query.start:
            return InvalidRequestError(Reason.VENDOR_INVALID_REQUEST, END_BEFORE_START_DETAIL)
        return None

    @staticmethod
    def __unavailable(cause: str) -> BarsFailure:
        error = ExogenousError(Reason.VENDOR_UNAVAILABLE, 'The fake vendor failed on its side.')
        error.__cause__ = FakeReadError(cause)
        return BarsFailure(error)

    def __rate_limited(self, symbol: str) -> BarsFailure:
        error = ExogenousError(
            Reason.VENDOR_RATE_LIMITED,
            'The fake vendor rate-limited the request.',
            reset_at=self.__clock() + timedelta(seconds=RATELIMIT_RESET_SECONDS),
            clock=self.__clock,
        )
        error.__cause__ = FakeReadError(f'RATELIMIT scenario: symbol={symbol!r}')
        return BarsFailure(error)

    @staticmethod
    async def __iterate(
        query: BarsQuery, scenario: Scenario, served_end: datetime, lineage: tuple[Segment, ...] | None
    ) -> AsyncIterator[Bar]:
        if scenario is Scenario.EMPTY:
            return
        for timestamp in grid_timestamps(query.granularity, query.start, served_end):
            if scenario is Scenario.GAPS and grid_index(query.granularity, timestamp) % 2:
                continue
            if lineage is None:
                yield fake_bar(query.instrument.symbol, timestamp)
                continue
            segment = next((segment for segment in lineage if segment.covers(timestamp)), None)
            if segment is not None:
                yield lineage_bar(segment, timestamp)
