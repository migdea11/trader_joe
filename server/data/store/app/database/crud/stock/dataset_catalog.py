"""The read repository behind the /ui/v1 dataset catalog and bar pages (tj-grna9p.20).

READ-ONLY. Nothing here writes, and nothing here commits: every function takes the request's session and only
selects. The route layer composes them (data/store/app/dataset_catalog.py) and never writes SQL of its own.

A CONSTANT NUMBER OF QUERIES, NEVER ONE PER DATASET (tj-grna9p.8 item 2). Each function below is one statement
for however many datasets it is asked about:

    list_catalog        every matching entry with its newest bar, in one statement
    bar_extents         first bar, last bar and count for a set of entries, in one statement
    covered_dates       the session dates holding a bar, for a set of STATIC entries, in one statement per time zone
    siblings            the other granularities of a set of entries' series, in one statement
    read_bar_page       one dataset's bars in a window, in one statement

THE BAR LOOKUPS KEY ON THE ENTRY'S OWN IDENTITY COLUMNS, NOT ON dataset_id ALONE. The bar table's only index that
leads with dataset_id is the natural key (dataset_id, asset_symbol, source, feed, granularity, timestamp), so a
lookup naming dataset_id and nothing else cannot be answered in timestamp order from it. The four columns after
dataset_id are denormalised copies of the entry's own (the writer copies them from the dataset, see
data/store/app/ingest/data_action_request.py), so equating them to the entry's costs no rows and gives the planner
a five-column equality prefix with timestamp next: newest bar, oldest bar and an ordered window are index range
scans. That is why this task adds no migration.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.logging import get_logger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


log = get_logger(__name__)

Bar = StockMarketActivity
Entry = StoreDatasetEntry


@dataclass(frozen=True)
class CatalogFilter:
    """The filters the catalog can apply in SQL. Status filters need the computed freshness, so they are not here.

    Attributes:
        asset_symbol_prefix: Keep entries whose symbol starts with this, case-insensitively. LIKE wildcards in it
            are matched literally.
        source: Keep entries from this source.
        update_type: Keep entries of this update type.
    """

    asset_symbol_prefix: str | None = None
    source: DataSource | None = None
    update_type: UpdateType | None = None


@dataclass(frozen=True)
class CatalogRow:
    """One dataset entry as the catalog reads it, plus the newest bar timestamp.

    Attributes:
        id: The entry's id.
        asset_symbol: The symbol, upper-case.
        asset_type: The asset type.
        data_type: The data type.
        source: The vendor the data came from.
        feed: The resolved tape.
        granularity: The bar width.
        start: The requested range's start, inclusive.
        end: The requested range's end, exclusive, or None for an open-ended request.
        update_type: How the dataset is kept current.
        expiry_type: How the dataset's rows expire.
        expiry: When the dataset is deleted, or None when nothing is scheduled.
        owner: The free-text label of who asked for it.
        last_bar: The newest stored bar's timestamp, or None when there are no bars.
    """

    id: uuid.UUID
    asset_symbol: str
    asset_type: AssetType
    data_type: DataType
    source: DataSource
    feed: Feed
    granularity: Granularity
    start: datetime
    end: datetime | None
    update_type: UpdateType
    expiry_type: ExpiryType
    expiry: datetime | None
    owner: str
    last_bar: datetime | None


@dataclass(frozen=True)
class BarExtent:
    """What is stored for one dataset.

    Attributes:
        first_bar: The oldest bar's timestamp, or None when there are none.
        last_bar: The newest bar's timestamp, or None when there are none.
        count: How many bars are stored.
    """

    first_bar: datetime | None
    last_bar: datetime | None
    count: int


def _bar_matches_entry() -> list[ColumnElement[bool]]:
    """The predicates that tie a bar to its entry through the natural-key columns, for a correlated subquery."""
    return [
        Bar.dataset_id == Entry.id,
        Bar.asset_symbol == Entry.asset_symbol,
        Bar.source == Entry.source,
        Bar.feed == Entry.feed,
        Bar.granularity == Entry.granularity,
    ]


def _row(entry: Entry, last_bar: datetime | None) -> CatalogRow:
    """Build a CatalogRow from an ORM entry, mapping the open-end sentinel back to None."""
    end = None if entry.end == NullableDateTime.EPOCH else entry.end
    return CatalogRow(
        id=entry.id,
        asset_symbol=entry.asset_symbol,
        asset_type=entry.asset_type,
        data_type=entry.data_type,
        source=entry.source,
        feed=entry.feed,
        granularity=entry.granularity,
        start=entry.start,
        end=end,
        # The OrderedEnum column is a plain Integer and the ORM hands the raw int back: only the schema layer
        # (to_validated_schema) converts, and this read path bypasses it. Every consumer compares members.
        update_type=UpdateType(entry.update_type),
        expiry_type=ExpiryType(entry.expiry_type),
        expiry=entry.expiry,
        owner=entry.owner,
        last_bar=last_bar,
    )


async def list_catalog(db: AsyncSession, catalog_filter: CatalogFilter) -> list[CatalogRow]:
    """Every entry matching the SQL-level filters, with its newest bar, ordered by symbol then id.

    One statement: the newest bar is a correlated scalar subquery per entry (an index range scan each, see the
    module docstring), so the cost is the number of entries and never the number of bars.

    Args:
        db: The request's session.
        catalog_filter: The filters to apply.

    Returns:
        list[CatalogRow]: All matching entries, ascending by (asset_symbol, id). Empty for an empty store.
    """
    last_bar = select(func.max(Bar.timestamp)).where(*_bar_matches_entry()).correlate(Entry).scalar_subquery()
    stmt = select(Entry, last_bar.label('last_bar')).order_by(Entry.asset_symbol, Entry.id)
    if catalog_filter.asset_symbol_prefix is not None:
        stmt = stmt.where(Entry.asset_symbol.istartswith(catalog_filter.asset_symbol_prefix, autoescape=True))
    if catalog_filter.source is not None:
        stmt = stmt.where(Entry.source == catalog_filter.source)
    if catalog_filter.update_type is not None:
        stmt = stmt.where(Entry.update_type == catalog_filter.update_type)
    result = await db.execute(stmt)
    return [_row(entry, newest) for entry, newest in result.all()]


async def get_catalog_row(db: AsyncSession, dataset_id: uuid.UUID) -> CatalogRow | None:
    """One entry with its newest bar, or None when no such entry exists.

    Args:
        db: The request's session.
        dataset_id: The entry's id.

    Returns:
        CatalogRow | None: The entry, or None.
    """
    last_bar = select(func.max(Bar.timestamp)).where(*_bar_matches_entry()).correlate(Entry).scalar_subquery()
    result = await db.execute(select(Entry, last_bar.label('last_bar')).where(Entry.id == dataset_id))
    found = result.first()
    return None if found is None else _row(found[0], found[1])


async def bar_extents(db: AsyncSession, rows: list[CatalogRow]) -> dict[uuid.UUID, BarExtent]:
    """First bar, last bar and count for each of these entries, in one statement.

    Args:
        db: The request's session.
        rows: The entries to measure. May be empty, which runs no statement.

    Returns:
        dict[uuid.UUID, BarExtent]: One extent per entry id, a zero count and no bounds where an entry holds no
        bars.
    """
    if not rows:
        return {}
    extents = {row.id: BarExtent(first_bar=None, last_bar=None, count=0) for row in rows}
    stmt = (
        select(
            Entry.id,
            select(func.min(Bar.timestamp)).where(*_bar_matches_entry()).correlate(Entry).scalar_subquery(),
            select(func.max(Bar.timestamp)).where(*_bar_matches_entry()).correlate(Entry).scalar_subquery(),
            select(func.count(Bar.id)).where(*_bar_matches_entry()).correlate(Entry).scalar_subquery(),
        )
        .where(Entry.id.in_([row.id for row in rows]))
        .order_by(Entry.id)
    )
    result = await db.execute(stmt)
    for dataset_id, first_bar, newest, count in result.all():
        extents[dataset_id] = BarExtent(first_bar=first_bar, last_bar=newest, count=count)
    return extents


async def covered_dates(db: AsyncSession, rows: list[CatalogRow], tz_name: str) -> dict[uuid.UUID, frozenset[date]]:
    """For each entry, the set of calendar dates (in tz_name) on which it holds at least one bar.

    This is the `covered` input of freshness.evaluate_dataset: a bar belongs to the session whose local date its
    timestamp falls on (freshness.session_of), and Postgres computes that date in the calendar's own zone, so the
    two agree without shipping every timestamp to Python. One statement for however many entries.

    Args:
        db: The request's session.
        rows: The STATIC entries to cover. May be empty, which runs no statement.
        tz_name: The IANA zone of the entries' trading calendar.

    Returns:
        dict[uuid.UUID, frozenset[date]]: One set per entry id; empty for an entry with no bars.
    """
    if not rows:
        return {}
    covered: dict[uuid.UUID, set[date]] = {row.id: set() for row in rows}
    local_date = func.date(func.timezone(tz_name, Bar.timestamp))
    stmt = (
        select(Entry.id, local_date)
        .join(Bar, and_(*_bar_matches_entry()))
        .where(Entry.id.in_([row.id for row in rows]))
        .distinct()
    )
    result = await db.execute(stmt)
    for dataset_id, session_date in result.all():
        covered[dataset_id].add(session_date)
    return {dataset_id: frozenset(dates) for dataset_id, dates in covered.items()}


async def siblings(db: AsyncSession, rows: list[CatalogRow]) -> dict[uuid.UUID, list[tuple[uuid.UUID, Granularity]]]:
    """For each entry, the other entries of the same series at a different bar width.

    A series is the same symbol, source, feed and data type (tj-grna9p.14). An entry at the same granularity is
    not a sibling: the viewer offers a switch between widths, not between two ranges of one width. Siblings are
    ordered by granularity (narrowest first), then id.

    Args:
        db: The request's session.
        rows: The entries to find siblings for. May be empty, which runs no statement.

    Returns:
        dict[uuid.UUID, list[tuple[uuid.UUID, Granularity]]]: One list per entry id, possibly empty.
    """
    if not rows:
        return {}
    result_by_id: dict[uuid.UUID, list[tuple[uuid.UUID, Granularity]]] = {row.id: [] for row in rows}
    # Narrowed by symbol in SQL and by the rest of the series key in Python: a tuple IN over enum columns is
    # not worth the risk for the handful of extra rows a symbol's other vendors and tapes add.
    stmt = select(Entry.id, Entry.asset_symbol, Entry.source, Entry.feed, Entry.data_type, Entry.granularity).where(
        Entry.asset_symbol.in_({row.asset_symbol for row in rows})
    )
    held = (await db.execute(stmt)).all()
    order = {granularity: position for position, granularity in enumerate(Granularity)}
    for row in rows:
        mine = (row.asset_symbol, row.source, row.feed, row.data_type)
        others = [
            (other_id, other_granularity)
            for other_id, symbol, source, feed, data_type, other_granularity in held
            if (symbol, source, feed, data_type) == mine and other_granularity != row.granularity
        ]
        result_by_id[row.id] = sorted(others, key=lambda sibling: (order[sibling[1]], str(sibling[0])))
    return result_by_id


async def read_bar_page(
    db: AsyncSession,
    row: CatalogRow,
    *,
    start: datetime | None,
    end: datetime | None,
    after: datetime | None,
    limit: int,
) -> list[StockMarketActivity]:
    """One dataset's bars in the half-open window [start, end), ascending by timestamp, after a cursor.

    Keyset paging on the timestamp, which is unique within a dataset (it is the last column of the bar's natural
    key), so a bar inserted while a client pages is neither shown twice nor does it make the client skip one
    that was already ahead of the cursor. Offset paging would do both.

    Args:
        db: The request's session.
        row: The dataset whose bars to read.
        start: Include bars at or after this instant, or None for no lower bound.
        end: Exclude bars at or after this instant, or None for no upper bound.
        after: Exclude bars at or before this instant (the cursor), or None for the first page.
        limit: How many bars to return at most.

    Returns:
        list[StockMarketActivity]: At most `limit` bars, ascending.
    """
    conditions: list[ColumnElement[bool]] = [
        Bar.dataset_id == row.id,
        Bar.asset_symbol == row.asset_symbol,
        Bar.source == row.source,
        Bar.feed == row.feed,
        Bar.granularity == row.granularity,
    ]
    if start is not None:
        conditions.append(Bar.timestamp >= start)
    if end is not None:
        # Half-open [start, end): a bar AT end belongs to the next range (tj-86g751).
        conditions.append(Bar.timestamp < end)
    if after is not None:
        conditions.append(Bar.timestamp > after)
    stmt = select(Bar).where(*conditions).order_by(Bar.timestamp).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
