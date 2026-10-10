"""The seed scenario: a fixed list of dataset requests, and the patterns that define "synthetic".

Decision tj-vhboky.55 (SEED PRODUCTION) as amended by tj-j4wknb: the requests are POSTed to
data_store's /store route through the real ingest running the FAKE (the fake-mode stack), so the
bars are fake_bar()'s pure function of (symbol, timestamp) and the rows are the ones the
production write path produces.

WHAT THE SCENARIO COVERS, and why each is there:
  * two owners over the SAME range (ZZSEEDAA, 2024-01-01 to 2024-02-01): the head seed carries the
    old six-column ENTRY key collision and, because both datasets hold a bar at every instant, the
    old four-column BAR key collision (E4 in tj-vhboky.55);
  * two owners over OVERLAPPING, different ranges (ZZSEEDAA, mid-February onward), so ranges
    overlap without being equal. No owner overlaps itself: the store refuses that with a 409. Its
    check is half-open, so two ADJACENT ranges of one owner do not collide (none are seeded);
  * a differing expiry_type (ROLLING against BULK) on ZZSEEDBB;
  * one EMPTY_ range (an entry that holds no bars) and one GAPS_ range (every other bar missing);
  * one 1hour dataset, so more than one granularity is present; everything else is default.
A few hundred bars in all (see SEED_REQUESTS).

DETERMINISTIC: every value is a literal. In particular expiry is always sent, because the body's
default is now() plus a day, which would differ between runs.

THE RANGES ARE HALF-OPEN, [start, end) (decision tj-j4wknb addendum 5).

trade_count: the fake emits an int for every bar and today's schema requires int (addendum 6 may
make it nullable later, epic tj-wjs8z0). Nothing here depends on that.

SYNTHETIC ONLY (the repo is public). SYMBOL_PATTERN and OWNER_PATTERN define what a row may look
like; is_synthetic() is what the producer refuses on. The symbol pattern is built from the fake's
own prefixes, imported rather than retyped.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from tests.fakes.market_data import EMPTY_PREFIX, GAPS_PREFIX


OWNER_A = 'seed-owner-a'
OWNER_B = 'seed-owner-b'

# Synthetic symbols are ZZSEED plus one to three letters, optionally behind one scenario prefix.
# The system suite's run identity uses ZZSYS; no real ticker starts with ZZ.
SYMBOL_PATTERN = re.compile(rf'^(?:{re.escape(EMPTY_PREFIX)}|{re.escape(GAPS_PREFIX)})?ZZSEED[A-Z]{{1,3}}$')
OWNER_PATTERN = re.compile(r'^seed-owner-[a-z]$')

# Sent explicitly on every request: the body's own default would be "now plus a day".
SEED_EXPIRY = datetime(2030, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SeedRequest:
    """One dataset request of the scenario, as POSTed to /store/{asset_type}/{data_type}/{symbol}.

    Args:
        owner (str): The declared principal.
        symbol (str): Requested symbol, scenario prefix included.
        granularity (Granularity): Bar size.
        start (datetime): Inclusive start, aware UTC.
        end (datetime): Exclusive end, aware UTC.
        expiry_type (ExpiryType): Expiry policy; part of the entry identity.
    """

    owner: str
    symbol: str
    granularity: Granularity
    start: datetime
    end: datetime
    expiry_type: ExpiryType = ExpiryType.BULK

    @property
    def path(self) -> str:
        """The route this request is POSTed to."""
        return f'/store/{AssetType.STOCK.value}/{DataType.MARKET_ACTIVITY.value}/{self.symbol}'

    def body(self) -> dict[str, str]:
        """The JSON body: every field a string, in the wire spelling (aware ISO instants, enum names).

        Returns:
            dict[str, str]: The body; identical on every call.
        """
        return {
            'owner': self.owner,
            'source': DataSource.ALPACA_API.value,
            'granularity': self.granularity.value,
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'expiry': SEED_EXPIRY.isoformat(),
            'expiry_type': self.expiry_type.name,
            'update_type': UpdateType.STATIC.name,
        }


def _day(month: int, day: int) -> datetime:
    return datetime(2024, month, day, tzinfo=UTC)


SEED_REQUESTS: tuple[SeedRequest, ...] = (
    # Same range, two owners: the old six-column entry key and the old four-column bar key collide.
    SeedRequest(OWNER_A, 'ZZSEEDAA', Granularity.ONE_DAY, _day(1, 1), _day(2, 1)),
    SeedRequest(OWNER_B, 'ZZSEEDAA', Granularity.ONE_DAY, _day(1, 1), _day(2, 1)),
    # Different owners, overlapping but different ranges (B's runs 5 Feb - 1 Mar, A's 15 Feb - 15 Mar).
    SeedRequest(OWNER_B, 'ZZSEEDAA', Granularity.ONE_DAY, _day(2, 5), _day(3, 1)),
    SeedRequest(OWNER_A, 'ZZSEEDAA', Granularity.ONE_DAY, _day(2, 15), _day(3, 15)),
    # Two entries that differ only in expiry_type.
    SeedRequest(OWNER_A, 'ZZSEEDBB', Granularity.ONE_DAY, _day(1, 1), _day(3, 1)),
    SeedRequest(OWNER_A, 'ZZSEEDBB', Granularity.ONE_DAY, _day(1, 1), _day(3, 1), expiry_type=ExpiryType.ROLLING),
    SeedRequest(OWNER_B, 'ZZSEEDBB', Granularity.ONE_DAY, _day(3, 10), _day(4, 10)),
    SeedRequest(OWNER_A, f'{EMPTY_PREFIX}ZZSEEDEE', Granularity.ONE_DAY, _day(1, 1), _day(2, 1)),
    SeedRequest(OWNER_A, f'{GAPS_PREFIX}ZZSEEDGG', Granularity.ONE_DAY, _day(1, 1), _day(2, 1)),
    SeedRequest(OWNER_A, 'ZZSEEDCC', Granularity.ONE_HOUR, _day(1, 2), _day(1, 4)),
)


def is_synthetic(symbol: str, owner: str | None = None) -> bool:
    """Whether a row's symbol, and its owner when it has one, fall inside the scenario's patterns.

    Args:
        symbol (str): The row's asset_symbol.
        owner (str | None): The row's owner, or None for a table that has no owner column.

    Returns:
        bool: True only if the symbol matches SYMBOL_PATTERN and the owner, if given, OWNER_PATTERN.
    """
    if not SYMBOL_PATTERN.fullmatch(symbol):
        return False
    return owner is None or bool(OWNER_PATTERN.fullmatch(owner))
