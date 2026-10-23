"""The read repository behind the /ui/v1 dataset catalog and bar pages (tj-grna9p.20).

READ-ONLY. Nothing here writes, and nothing here commits: every function takes the request's session and only
selects. The route layer composes them (data/store/app/dataset_catalog.py) and never writes SQL of its own.

A CONSTANT NUMBER OF QUERIES, NEVER ONE PER DATASET (tj-grna9p.8 item 2). Each function below is one statement
for however many datasets it is asked about:

    list_catalog        every matching entry with its newest bar, in one statement
    bar_extents         first bar, last bar and count for a set of entries, in one statement
    covered_dates       the session dates holding a bar, for a set of STATIC entries, in one statement per time zone
    siblings            the other granularities of a set of entries' series, in one statement
    list_pending        the datasets whose expiry has passed (the reaper's pending set), keyset paged, in one statement
    pending_ids         the same set as ids, or those among a page's ids, in one statement
    read_bar_page       one dataset's bars in a window, in one statement
    read_bar_page_starts  the page index of a window (count and the start of every page), in one statement

THE BAR LOOKUPS KEY ON THE ENTRY'S OWN IDENTITY COLUMNS, NOT ON dataset_id ALONE. The bar table's only index that
leads with dataset_id is the natural key (dataset_id, asset_symbol, source, feed, granularity, timestamp), so a
lookup naming dataset_id and nothing else cannot be answered in timestamp order from it. The four columns after
dataset_id are denormalised copies of the entry's own (the writer copies them from the dataset, see
data/store/app/ingest/data_action_request.py), so equating them to the entry's costs no rows and gives the planner
a five-column equality prefix with timestamp next: newest bar, oldest bar and an ordered window are index range
scans. That is why this task adds no migration.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
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
        last_bar_written_at: When that newest bar was last written (its updated_at), or None when there are no bars.
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
    last_bar_written_at: datetime | None = None


@dataclass(frozen=True)
class BarPageOffset:
    """Where one page of a bar window starts.

    Attributes:
        offset: The 0-based rank, ascending by timestamp, of the page's first bar within the window.
        start: That bar's timestamp.
    """

    offset: int
    start: datetime


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


def _last_bar_written_at() -> ColumnElement[datetime]:
    """The newest bar's updated_at, a correlated scalar subquery: one more index range scan per entry."""
    return (
        select(Bar.updated_at)
        .where(*_bar_matches_entry())
        .order_by(Bar.timestamp.desc())
        .limit(1)
        .correlate(Entry)
        .scalar_subquery()
    )


def _row(entry: Entry, last_bar: datetime | None, last_bar_written_at: datetime | None = None) -> CatalogRow:
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
        last_bar_written_at=last_bar_written_at,
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
    stmt = select(Entry, last_bar.label('last_bar'), _last_bar_written_at().label('last_bar_written_at')).order_by(
        Entry.asset_symbol, Entry.id
    )
    if catalog_filter.asset_symbol_prefix is not None:
        stmt = stmt.where(Entry.asset_symbol.istartswith(catalog_filter.asset_symbol_prefix, autoescape=True))
    if catalog_filter.source is not None:
        stmt = stmt.where(Entry.source == catalog_filter.source)
    if catalog_filter.update_type is not None:
        stmt = stmt.where(Entry.update_type == catalog_filter.update_type)
    result = await db.execute(stmt)
    return [_row(entry, newest, written) for entry, newest, written in result.all()]


async def get_catalog_row(db: AsyncSession, dataset_id: uuid.UUID) -> CatalogRow | None:
    """One entry with its newest bar, or None when no such entry exists.

    Args:
        db: The request's session.
        dataset_id: The entry's id.

    Returns:
        CatalogRow | None: The entry, or None.
    """
    last_bar = select(func.max(Bar.timestamp)).where(*_bar_matches_entry()).correlate(Entry).scalar_subquery()
    result = await db.execute(
        select(Entry, last_bar.label('last_bar'), _last_bar_written_at().label('last_bar_written_at')).where(
            Entry.id == dataset_id
        )
    )
    found = result.first()
    return None if found is None else _row(found[0], found[1], found[2])


def _past_expiry(now: datetime) -> ColumnElement[bool]:
    """THE PENDING-SET PREDICATE, written once: an expiry is set and has passed (`expiry <= now`).

    Whatever the update type: a retired DAILY or STREAM dataset is pending only once its expiry has passed, the same
    as a STATIC one. Both pending reads below use it, so the reaper's pass, GET /ui/v1/reaper/pending and the
    DatasetSummary.reaper_pending flag can never disagree about which datasets are pending.
    """
    return and_(Entry.expiry.is_not(None), Entry.expiry <= now)


async def list_pending(
    db: AsyncSession, now: datetime, *, after: tuple[datetime, uuid.UUID] | None = None, limit: int | None = None
) -> list[CatalogRow]:
    """The datasets whose expiry has passed, oldest expiry first, with their newest bar. One statement.

    Reads record no usage. Keyset paged over (expiry, id): `after` is the last row's pair.

    Args:
        db: A session.
        now: The instant expiry is judged at; aware.
        after: Keep only rows strictly after this (expiry, id), or None for the first page.
        limit: The most rows to return, or None for all of them.

    Returns:
        list[CatalogRow]: The pending datasets, ascending by (expiry, id).
    """
    last_bar = select(func.max(Bar.timestamp)).where(*_bar_matches_entry()).correlate(Entry).scalar_subquery()
    stmt = (
        select(Entry, last_bar.label('last_bar'), _last_bar_written_at().label('last_bar_written_at'))
        .where(_past_expiry(now))
        .order_by(Entry.expiry, Entry.id)
    )
    if after is not None:
        after_expiry, after_id = after
        stmt = stmt.where(or_(Entry.expiry > after_expiry, and_(Entry.expiry == after_expiry, Entry.id > after_id)))
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return [_row(entry, newest, written) for entry, newest, written in result.all()]


async def pending_ids(
    db: AsyncSession, now: datetime, dataset_ids: Collection[uuid.UUID] | None = None
) -> frozenset[uuid.UUID]:
    """The ids of the pending datasets, or those among `dataset_ids` that are pending. One statement.

    This is what DatasetSummary.reaper_pending reads on list and detail reads: pass the page's ids and test
    membership. The predicate is the one list_pending uses.

    Args:
        db: A session.
        now: The instant expiry is judged at; aware.
        dataset_ids: Restrict the answer to these ids; None means every pending dataset. An empty collection
            answers an empty set without a statement.

    Returns:
        frozenset[uuid.UUID]: The pending ids.
    """
    if dataset_ids is not None and not dataset_ids:
        return frozenset()
    stmt = select(Entry.id).where(_past_expiry(now))
    if dataset_ids is not None:
        stmt = stmt.where(Entry.id.in_(list(dataset_ids)))
    return frozenset((await db.execute(stmt)).scalars())


async def dataset_exists(db: AsyncSession, dataset_id: uuid.UUID) -> bool:
    """Whether an entry with this id exists, without reading its bars.

    Args:
        db: The request's session.
        dataset_id: The entry's id.

    Returns:
        bool: True when the entry exists.
    """
    return (await db.execute(select(Entry.id).where(Entry.id == dataset_id))).first() is not None


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
    conditions = _window_conditions(row, start, end)
    if after is not None:
        conditions.append(Bar.timestamp > after)
    stmt = select(Bar).where(*conditions).order_by(Bar.timestamp).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _window_conditions(row: CatalogRow, start: datetime | None, end: datetime | None) -> list[ColumnElement[bool]]:
    """The five-column identity predicate plus the half-open window [start, end), shared by every bar read."""
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
    return conditions


async def read_bar_page_starts(
    db: AsyncSession, row: CatalogRow, *, start: datetime | None, end: datetime | None, size: int, overlap: int
) -> tuple[int, list[BarPageOffset]]:
    """The page index of a dataset's bar window [start, end): its bar count and where every page starts.

    N is the number of bars in the half-open window. Pages are FIXED blocks of `size` bars counted back from the
    NEWEST bar (owner ruling, tj-eozqhi 2026-10-13): with q = N div size and r = N mod size the pages are
    offsets r, r + size, ..., N - size (the full blocks), plus offset 0 first when r > 0 (the earliest page, a
    display exception that holds `size` bars and so overlaps the next block). When r = 0 the first full block is
    already at 0 and is listed once. N <= size is the single page [0]; N = 0 has no pages. An offset is a 0-based
    rank ascending by timestamp, the order of read_bar_page, so page k's bars are read_bar_page with
    start=pages[k].start, end=<the window's end>, limit=size (plus the client's look-ahead).

    ONE STATEMENT, however many bars: this shape:
        SELECT timestamp, rank, n FROM (
            SELECT timestamp, row_number() OVER (ORDER BY timestamp) - 1 AS rank,
                   (SELECT count(*) FROM <window>) AS n
            FROM <window>) AS ranked
        WHERE (rank >= n % size AND (rank - n % size) % size = 0) OR rank = 0
    The count is an uncorrelated scalar subquery (an index-only range scan, evaluated once) and the ranks stream
    through a WindowAgg over the same index range in timestamp order, so nothing is buffered and only the wanted
    rows, about N / size of them, leave the database. Both read one snapshot because it is one statement. The
    window is the five-column equality prefix with timestamp next, an index range scan of the natural key.
    `rank >= r` is spelled out so the modulo never sees a negative operand, which SQLite and Postgres would
    otherwise sign differently.

    Args:
        db: The request's session.
        row: The dataset whose bars to index.
        start: Include bars at or after this instant, or None for no lower bound.
        end: Exclude bars at or after this instant, or None for no upper bound.
        size: The page width in bars, at least 1.
        overlap: Unused by the page rule: the look-ahead the client adds from the next block, validated by the
            caller and carried here only so the signature mirrors the route.

    Returns:
        tuple[int, list[BarPageOffset]]: N, and the pages ascending by offset; (0, []) for an empty window.
    """
    conditions = _window_conditions(row, start, end)
    count = select(func.count()).select_from(Bar).where(*conditions).correlate(None).scalar_subquery()
    ranked = (
        select(
            Bar.timestamp.label('ts'),
            (func.row_number().over(order_by=Bar.timestamp) - 1).label('rk'),
            count.label('n'),
        )
        .where(*conditions)
        .subquery()
    )
    # r = n mod size; the full blocks start at r, r + size, ... and the earliest page at 0 is always listed.
    remainder = ranked.c.n % size
    stmt = (
        select(ranked.c.ts, ranked.c.rk, ranked.c.n)
        .where(or_(and_(ranked.c.rk >= remainder, (ranked.c.rk - remainder) % size == 0), ranked.c.rk == 0))
        .order_by(ranked.c.rk)
    )
    result = await db.execute(stmt)
    rows = result.all()
    if not rows:
        return 0, []
    return int(rows[0].n), [BarPageOffset(offset=int(r.rk), start=r.ts) for r in rows]


@dataclass(frozen=True)
class BarStorage:
    """What the bar table holds now, measured once.

    Attributes:
        total_bytes: pg_total_relation_size of the bar table: heap, indexes and TOAST.
        by_source: The bar count of each (source, feed) holding bars, ordered by source then feed.
    """

    total_bytes: int
    by_source: list[tuple[DataSource, Feed, int]]


async def read_bar_storage(db: AsyncSession) -> BarStorage:
    """The bar table's total size and its bar count per (source, feed). Two statements, whatever the table holds.

    The size is the measured whole; a per-source split of it can only be an estimate (the caller multiplies a count
    by the average row size), because Postgres does not account storage per value of a column.

    Args:
        db: The request's session.

    Returns:
        BarStorage: The size, and a count per (source, feed) that holds at least one bar.
    """
    size = await db.execute(select(func.pg_total_relation_size(func.to_regclass(Bar.__tablename__))))
    counts = await db.execute(
        select(Bar.source, Bar.feed, func.count()).group_by(Bar.source, Bar.feed).order_by(Bar.source, Bar.feed)
    )
    return BarStorage(
        total_bytes=int(size.scalar() or 0),
        by_source=[(DataSource(source), Feed(feed), int(count)) for source, feed, count in counts.all()],
    )
