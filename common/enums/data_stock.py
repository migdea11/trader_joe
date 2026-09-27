from datetime import timedelta
from enum import StrEnum
from typing import Generic, Self, TypeVar

from common.enums.composed_enum import StrSupersetEnum, SubsetStrEnum
from common.enums.pydantic_enums import NamedIntEnum


class Granularity(StrEnum):
    ONE_MINUTE = ('1min', timedelta(minutes=1))
    FIVE_MINUTES = ('5min', timedelta(minutes=5))
    THIRTY_MINUTES = ('30min', timedelta(minutes=30))
    ONE_HOUR = ('1hour', timedelta(hours=1))
    ONE_DAY = ('1day', timedelta(days=1))
    ONE_WEEK = ('1week', timedelta(weeks=1))
    ONE_MONTH = ('1month', timedelta(weeks=4))

    def __new__(cls, value: str, offset: timedelta):
        obj = str.__new__(cls, value)  # Ensure the Enum behaves like a str
        obj._value_ = value
        obj._offset = offset
        return obj

    @property
    def offset(self) -> timedelta:
        return self._offset

    def __str__(self) -> str:
        return self._value_


T = TypeVar('T')


class BrokerGranularityBase(Generic[T]):
    """Base class for mapping broker-specific granularities to standardized ones."""

    def __init__(self, broker_code: T, granularity: Granularity):
        self._broker_code = broker_code
        self._granularity = granularity

    @property
    def broker_code(self) -> T:
        """Get the broker-specific granularity code.

        Returns:
            T: Broker-specific granularity code.
        """
        return self._broker_code

    @property
    def granularity(self) -> Granularity:
        """Get the standardized granularity.

        Returns:
            Granularity: Standardized granularity.
        """
        return self._granularity

    @classmethod
    def from_broker_code(cls: type['BrokerGranularityBase'], broker_code: T) -> Self:
        """Find and return the granularity mapping for a given broker-specific.

        Args:
            cls (Type[BrokerGranularityBase&quot])
            broker_code (T): Broker-specific granularity code.

        Raises:
            ValueError: If the broker code is not found.

        Returns:
            Self: Granularity mapping.
        """
        granularity_map: Self
        for granularity_map in cls:
            if granularity_map.broker_code == broker_code:
                return granularity_map
        raise ValueError(f"Broker code '{broker_code}' not found in {cls.__name__}")

    @classmethod
    def from_granularity(cls: type['BrokerGranularityBase'], granularity: Granularity) -> Self:
        """Find and return the granularity mapping for a given standardized granularity.

        Args:
            cls (Type[BrokerGranularityBase])
            granularity (Granularity): Standardized granularity.

        Raises:
            ValueError: If the standardized granularity is not found.

        Returns:
            Self: Granularity mapping.
        """
        granularity_map: Self
        for granularity_map in cls:
            if granularity_map.granularity == granularity:
                return granularity_map
        raise ValueError(f"Granularity '{granularity}' not found in {cls.__name__}")


class DataSource(StrEnum):
    IB_API = 'IB'
    ALPACA_API = 'ALPACA'
    MANUAL_ENTRY = 'MANUAL'


class UsEquityFeed(SubsetStrEnum):
    """The tapes a US equity bar can have come from.

    This is the NARROW vocabulary a US equity strategy reasons in (tj-vhboky.1, the user's
    per-market ruling). It is one of the subsets composed into Feed below; nothing stores or
    transports this type.

    ONLY THE TWO TAPES THIS DEPLOYMENT CAN ACTUALLY RECEIVE. Alpaca's GET /v2/stocks/bars accepts
    four feed values -- iex, sip, otc and boats -- and the other two are deliberately absent
    (researcher-broker, tj-vhboky.1). otc is gated behind a broker-partner subscription this
    account structurally cannot hold and boats is used nowhere in the tree, so both are values
    nothing here can produce. A member nothing can produce still lands inside the dataset entry's
    identity constraint, where it is an unreachable branch that every conflict query and every
    reader has to carry. Adding one later costs a line here and an ALTER TYPE ... ADD VALUE; that
    is cheaper than carrying two dead ones now.

    NOT_APPLICABLE IS DELIBERATELY NOT HERE. It is not a US equity tape, it is the absence of a
    tape distinction, so it lives in NoTapeFeed. The consequence is intended:
    UsEquityFeed.from_superset(Feed.NOT_APPLICABLE) RAISES, which is the correct answer to "which
    US tape served this row" for a row that has none.
    """

    IEX = 'IEX'
    SIP = 'SIP'


class NoTapeFeed(SubsetStrEnum):
    """The answer for a source with no tape distinction at all -- IB_API, MANUAL_ENTRY.

    NOT A GAP AND NOT A DEFAULT. This is the final, correct value for those sources, which is
    why it is a member rather than a null. It is its own subset rather than a member of every
    market's enum because it is market-independent: a tape distinction that does not exist does
    not belong to US equities any more than to anything else.
    """

    NOT_APPLICABLE = 'NOT_APPLICABLE'


class Feed(StrSupersetEnum):
    """Which tape a bar's data came from, independent of who supplied it (tj-u12tjo.11).

    DataSource names the VENDOR -- IB, ALPACA, MANUAL -- and answers "who did we ask". Feed
    answers a different question: "which tape did the answer come from". Alpaca alone resells
    both the free IEX partial tape and the paid SIP consolidated tape under the single
    DataSource.ALPACA_API value, so an IEX bar and a SIP bar for the same symbol/granularity/
    minute are different numbers that collided on the bar's old natural key and silently
    overwrote each other.

    The user's ruling on tj-u12tjo.11 rejected folding feed into DataSource as a compound value
    (ALPACA_IEX, ALPACA_SIP, ...): IEX and SIP exist independently of whoever resells them, and a
    compound value multiplies with every broker that resells more than one tape, turning "what
    did IEX say, across brokers" into a pattern match on enum names. So feed is its own column.

    THIS CLASS DECLARES NO MEMBERS. It is the SUPERSET of the per-market subsets above, composed
    from them by the call directly below its body (tj-vhboky.1: "one enum per market, with the DB
    and API type a SUPERSET of all of them, so strategies use the narrow vocabulary that applies
    to them"). Feed is the only one of these types that is stored, transported or validated
    against; the subsets exist so a caller can say which vocabulary it is entitled to and get a
    ValueError rather than a surprise when it is handed something outside it.

    ADDING A MARKET IS PURELY ADDITIVE: declare a subset enum, add it to the compose() call. No
    list is hand-maintained, so no two lists can drift apart.

    WHERE THE MIGRATION GETS THE MEMBER LIST, which is the one thing that must not be guessed:
    from THIS CLASS, by passing it to sa.Enum(Feed, name='feed'). Composition runs at import
    time, so by the time a model or a migration module body executes, Feed is complete and
    SQLAlchemy reads the full list off it. A migration that writes the members as string literals
    instead gets no error and no warning the day a market is added -- it simply stops matching,
    and the first insert of the new value fails at runtime. Adding a member after the type exists
    is not autogenerated either: Alembic does not diff enum labels, so it needs a hand-written
    ALTER TYPE feed ADD VALUE.

    NO SENTINEL FOR "WE DO NOT KNOW" (tj-vhboky.1, ruling of 2026-09-25). An earlier version of
    this enum carried UNKNOWN as the column's default. It is gone, because the adapter is now
    required to resolve a feed for every row and a value that should never be written is better
    expressed as an ERROR than as an enum member -- left in the vocabulary it invites use as a
    default in exactly the place a decision was wanted. An adapter that cannot determine the feed
    has FAILED and must say so.

    HOW THE ADAPTER RESOLVES IT, in order:
      1. the caller's selection, if the request carried one;
      2. otherwise the constant for a vendor with a single tape.
    The ruling's middle branch -- "else what the vendor response reports" -- is unimplementable
    for the one vendor there is: Alpaca's bars response carries bars, currency and a page token,
    and each bar carries timestamp, OHLC, volume, count and vwap. There is no feed field at any
    level (researcher-broker, confirmed on two independent reads). Nothing here may assume a feed
    can be recovered from a vendor payload. A vendor that DOES report its feed would use that
    branch, between the two above.
    """


Feed.compose(UsEquityFeed, NoTapeFeed)


class ExpiryType(NamedIntEnum):
    # All items from request expire at the same time
    BULK = 1
    # A max number of items are stored, when the limit is reached the oldest item is removed
    BUFFER_1K = 2
    BUFFER_10K = 3
    BUFFER_100K = 4
    # Each item from request expires at an offset from the first item (1day bars will expire 1 day after the previous)
    ROLLING = 5


class UpdateType(NamedIntEnum):
    # Data is pulled once and never updated
    STATIC = 1
    # Data is pulled at the end of the day
    DAILY = 2
    # Data is streamed in real-time
    STREAM = 3
