"""The dataset_read and dataset_read_daily repository: the consumption record's writes and reads (tj-grna9p.46).

EVERY WRITE COMMITS ON ITS OWN, through write_transaction, on the session the caller hands in -- the usage
recorder's own session (app/usage_recorder.py), never a request's. Usage is bookkeeping: a failure here must
never reach a read, so the callers catch.

THE ROLLUP IS ONE STATEMENT. fold_reads_before deletes the old rows and upserts their aggregate in a single
data-modifying CTE, so a crash can neither fold without deleting (a double count on re-run) nor delete without
folding (a lost count), and a row a late flush inserts mid-statement is either in the fold or still there.
Re-running it finds nothing past the cutoff and changes nothing.

client_id IS A SensitiveString: no statement binding it renders the value, and nothing here logs it.
"""

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import BigInteger, Date, cast, delete, func, select, union, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from data.store.app.database.models.dataset_read import DatasetRead, DatasetReadKind, ReadClientKind
from data.store.app.database.models.dataset_read_daily import DatasetReadDaily
from data.store.app.database.transaction import write_transaction


_READ = DatasetRead.__table__
_DAILY = DatasetReadDaily.__table__


def _window_start_day(now: datetime, days: int) -> date:
    """The first UTC day of a window of `days` ending at `now`; a naive `now` is refused.

    The rollup keys its days in UTC, so the window's day is taken in UTC whatever zone `now` carries.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    return (now.astimezone(UTC) - timedelta(days=days)).date()


def _utc_day(column):
    """The UTC calendar day of a timestamptz column."""
    return cast(func.timezone('UTC', column), Date)


@dataclass(frozen=True)
class NewRead:
    """One dataset_read row to insert. client_id is repr=False: it is never logged."""

    dataset_id: uuid.UUID
    client_id: str = field(repr=False)
    client_kind: ReadClientKind
    route: str
    bars_returned: int
    read_at: datetime
    kind: DatasetReadKind = DatasetReadKind.READ
    session_opened_at: datetime | None = None
    session_closed_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def as_values(self) -> dict:
        return {
            'id': self.id,
            'dataset_id': self.dataset_id,
            'client_id': self.client_id,
            'client_kind': self.client_kind,
            'kind': self.kind,
            'route': self.route,
            'bars_returned': self.bars_returned,
            'read_at': self.read_at,
            'session_opened_at': self.session_opened_at,
            'session_closed_at': self.session_closed_at,
        }


@dataclass(frozen=True)
class DatasetUsage:
    """What the usage helper says about one dataset: its latest read and its reads in the window."""

    dataset_id: uuid.UUID
    last_read_at: datetime | None
    reads_in_window: int


@dataclass(frozen=True)
class DailyReads:
    """Reads of one dataset on one UTC day by one client kind, over the per-read rows and the rollup."""

    dataset_id: uuid.UUID
    day: date
    client_kind: ReadClientKind
    reads: int


async def usage_tables_exist(db: AsyncSession) -> bool:
    """Whether both usage tables exist, by one catalogue read that does not open a write transaction.

    The recorder asks before its first write, as the startup sweep does for ingest_run: on a never-migrated
    database the INSERT would fail with UndefinedTable, which write_transaction logs as a bug with its full
    traceback.

    Args:
        db: The recorder's own session.

    Returns:
        bool: True when both tables resolve on the session's search path.
    """
    result = await db.execute(
        select(
            func.to_regclass(DatasetRead.__tablename__).is_not(None),
            func.to_regclass(DatasetReadDaily.__tablename__).is_not(None),
        )
    )
    first, second = result.one()
    return bool(first and second)


async def insert_reads(db: AsyncSession, reads: Iterable[NewRead]) -> int:
    """Insert rows in one statement and commit them.

    Args:
        db: The recorder's own session.
        reads: The rows. An empty batch writes nothing.

    Returns:
        int: How many rows were inserted.
    """
    values = [read.as_values() for read in reads]
    if not values:
        return 0
    async with write_transaction(db, 'record dataset reads'):
        await db.execute(_READ.insert(), values)
    return len(values)


async def known_client_ids(
    db: AsyncSession, dataset_ids: Collection[uuid.UUID], since: datetime, until: datetime
) -> dict[tuple[uuid.UUID, date], set[str]]:
    """The distinct client ids already recorded per (dataset, UTC day) in [since, until).

    One statement, for the recorder's per-dataset-per-day cap. Rolled-up days are not read: a day is rolled up
    only once it is past the window, long after any cap decision for it.

    Args:
        db: The recorder's own session.
        dataset_ids: The datasets of the batch.
        since: Inclusive lower bound on read_at.
        until: Exclusive upper bound on read_at.

    Returns:
        dict: The ids per (dataset_id, day); a pair with no rows is absent.
    """
    stmt = (
        select(_READ.c.dataset_id, _utc_day(_READ.c.read_at), _READ.c.client_id)
        .where(_READ.c.dataset_id.in_(list(dataset_ids)), _READ.c.read_at >= since, _READ.c.read_at < until)
        .distinct()
    )
    known: dict[tuple[uuid.UUID, date], set[str]] = {}
    for dataset_id, day, client_id in (await db.execute(stmt)).all():
        known.setdefault((dataset_id, day), set()).add(client_id)
    return known


async def close_session(db: AsyncSession, session_id: uuid.UUID, closed_at: datetime, bars_returned: int) -> None:
    """Set a STREAM_SESSION row's close and its bar count, and commit.

    Args:
        db: The recorder's own session.
        session_id: The row the open returned.
        closed_at: When the session closed.
        bars_returned: The bars the session delivered.
    """
    async with write_transaction(db, 'record dataset stream session close'):
        await db.execute(
            update(_READ)
            .where(_READ.c.id == session_id, _READ.c.kind == DatasetReadKind.STREAM_SESSION)
            .values(session_closed_at=closed_at, bars_returned=bars_returned)
        )


async def fold_reads_before(db: AsyncSession, cutoff: datetime) -> int:
    """Fold every dataset_read row with read_at before `cutoff` into dataset_read_daily, delete them, and commit.

    Grouped per (dataset, client id, UTC day) and ADDED to the day's existing summary, so a day split across two
    runs sums correctly. One statement and one transaction (see the module docstring), so it is idempotent and a
    crash mid-way leaves the rows unfolded or folded-and-deleted, never both.

    Args:
        db: The recorder's own session.
        cutoff: Rows strictly older than this are folded.

    Returns:
        int: How many daily summary rows were inserted or updated; 0 when nothing was past the cutoff.
    """
    moved = (
        delete(_READ)
        .where(_READ.c.read_at < cutoff)
        .returning(
            _READ.c.dataset_id,
            _READ.c.client_id,
            _READ.c.client_kind,
            _READ.c.kind,
            _READ.c.bars_returned,
            _READ.c.read_at,
        )
        .cte('moved')
    )
    day = _utc_day(moved.c.read_at)
    aggregate = select(
        moved.c.dataset_id,
        moved.c.client_id,
        moved.c.client_kind,
        day.label('day'),
        func.count().filter(moved.c.kind == DatasetReadKind.READ).label('reads'),
        func.count().filter(moved.c.kind == DatasetReadKind.STREAM_SESSION).label('sessions'),
        cast(func.coalesce(func.sum(moved.c.bars_returned), 0), BigInteger).label('bars_returned'),
        func.max(moved.c.read_at).label('last_read_at'),
    ).group_by(moved.c.dataset_id, moved.c.client_id, moved.c.client_kind, day)
    stmt = pg_insert(_DAILY).from_select(
        ['dataset_id', 'client_id', 'client_kind', 'day', 'reads', 'sessions', 'bars_returned', 'last_read_at'],
        aggregate,
    )
    stmt = stmt.on_conflict_do_update(
        constraint=DatasetReadDaily.UNIQUE_CONSTRAINT,
        set_={
            'reads': _DAILY.c.reads + stmt.excluded.reads,
            'sessions': _DAILY.c.sessions + stmt.excluded.sessions,
            'bars_returned': _DAILY.c.bars_returned + stmt.excluded.bars_returned,
            'last_read_at': func.greatest(_DAILY.c.last_read_at, stmt.excluded.last_read_at),
        },
    )
    async with write_transaction(db, 'roll up dataset reads'):
        result = await db.execute(stmt)
        return result.rowcount


def _per_day(
    dataset_ids: Collection[uuid.UUID] | None,
    client_kinds: Collection[ReadClientKind] | None,
    client_ids: Collection[str] | None,
):
    """The union of per-read rows and daily summaries, as (dataset_id, day, client_kind, reads, last_read_at).

    Filtered by dataset and by client ids when given, else by kinds (default SDK). Not filtered by date.
    """
    per_read_day = _utc_day(_READ.c.read_at)
    per_read = select(
        _READ.c.dataset_id,
        per_read_day.label('day'),
        _READ.c.client_kind,
        func.count().filter(_READ.c.kind == DatasetReadKind.READ).label('reads'),
        func.max(_READ.c.read_at).label('last_read_at'),
    ).group_by(_READ.c.dataset_id, per_read_day, _READ.c.client_kind)
    daily = select(
        _DAILY.c.dataset_id,
        _DAILY.c.day,
        _DAILY.c.client_kind,
        func.sum(_DAILY.c.reads).label('reads'),
        func.max(_DAILY.c.last_read_at).label('last_read_at'),
    ).group_by(_DAILY.c.dataset_id, _DAILY.c.day, _DAILY.c.client_kind)
    if dataset_ids is not None:
        per_read = per_read.where(_READ.c.dataset_id.in_(list(dataset_ids)))
        daily = daily.where(_DAILY.c.dataset_id.in_(list(dataset_ids)))
    if client_ids is not None:
        per_read = per_read.where(_READ.c.client_id.in_(list(client_ids)))
        daily = daily.where(_DAILY.c.client_id.in_(list(client_ids)))
    else:
        kinds = list(client_kinds) if client_kinds is not None else [ReadClientKind.SDK]
        per_read = per_read.where(_READ.c.client_kind.in_(kinds))
        daily = daily.where(_DAILY.c.client_kind.in_(kinds))
    return union_all(per_read, daily).subquery('usage_by_day')


async def read_dataset_usage(
    db: AsyncSession,
    dataset_ids: Collection[uuid.UUID] | None,
    *,
    now: datetime,
    window_days: int,
    client_kinds: Collection[ReadClientKind] | None = None,
    client_ids: Collection[str] | None = None,
) -> dict[uuid.UUID, DatasetUsage]:
    """Each dataset's last_read_at and its reads in the last `window_days`, over per-read rows and the rollup.

    Only reads count: a stream session sets last_read_at but is not in reads_in_window. A dataset with no
    matching read is absent from the result. The window is whole UTC days, from the day `window_days` before
    `now`. Used by tj-grna9p.20, .64 and .85.

    Args:
        db: A session; the read is one statement.
        dataset_ids: The datasets to report, or None for every dataset with a read.
        now: The instant the window ends at; aware, else ValueError. Converted to UTC for the window's start day.
        window_days: The window's length in days.
        client_kinds: Count only these kinds. Default {SDK}. Ignored when client_ids is given.
        client_ids: Count only these client ids instead of filtering by kind.

    Returns:
        dict[uuid.UUID, DatasetUsage]: Keyed by dataset id.
    """
    since_day = _window_start_day(now, window_days)
    usage = _per_day(dataset_ids, client_kinds, client_ids)
    stmt = select(
        usage.c.dataset_id,
        func.max(usage.c.last_read_at),
        cast(func.coalesce(func.sum(usage.c.reads).filter(usage.c.day >= since_day), 0), BigInteger),
    ).group_by(usage.c.dataset_id)
    return {
        dataset_id: DatasetUsage(dataset_id, last_read_at, int(reads))
        for dataset_id, last_read_at, reads in (await db.execute(stmt)).all()
    }


async def read_daily_reads(
    db: AsyncSession,
    dataset_ids: Collection[uuid.UUID] | None,
    *,
    now: datetime,
    days: int,
    client_kinds: Collection[ReadClientKind] | None = None,
    client_ids: Collection[str] | None = None,
) -> list[DailyReads]:
    """Reads per day per client kind over the last `days` UTC days, over per-read rows and the rollup.

    Args:
        db: A session; the read is one statement.
        dataset_ids: The datasets to report, or None for every dataset with a read.
        now: The instant the window ends at; aware, else ValueError. Converted to UTC for the window's start day.
        days: The window's length in days.
        client_kinds: Report only these kinds. Default {SDK}. Ignored when client_ids is given.
        client_ids: Report only these client ids instead of filtering by kind.

    Returns:
        list[DailyReads]: Ordered by day, then dataset id, then kind.
    """
    since_day = _window_start_day(now, days)
    usage = _per_day(dataset_ids, client_kinds, client_ids)
    stmt = (
        select(usage.c.dataset_id, usage.c.day, usage.c.client_kind, cast(func.sum(usage.c.reads), BigInteger))
        .where(usage.c.day >= since_day)
        .group_by(usage.c.dataset_id, usage.c.day, usage.c.client_kind)
        .order_by(usage.c.day, usage.c.dataset_id, usage.c.client_kind)
    )
    return [
        DailyReads(dataset_id, day, ReadClientKind(kind), int(reads))
        for dataset_id, day, kind, reads in (await db.execute(stmt)).all()
    ]


async def read_readers(
    db: AsyncSession, dataset_ids: Collection[uuid.UUID], *, now: datetime, window_days: int
) -> dict[uuid.UUID, tuple[frozenset[ReadClientKind], frozenset[str]]]:
    """Which client kinds and which client ids read each dataset in the last `window_days`, whatever the filter.

    Only reads count, as in read_dataset_usage. The window is whole UTC days. A dataset nobody read is absent.

    Args:
        db: A session; the read is one statement, and none for an empty `dataset_ids`.
        dataset_ids: The datasets to report.
        now: The instant the window ends at (aware, else ValueError).
        window_days: The window's length in days.

    Returns:
        dict: (kinds, client ids) per dataset id.
    """
    if not dataset_ids:
        return {}
    since_day = _window_start_day(now, window_days)
    ids = list(dataset_ids)
    per_read = select(_READ.c.dataset_id, _READ.c.client_id, _READ.c.client_kind).where(
        _READ.c.dataset_id.in_(ids), _READ.c.kind == DatasetReadKind.READ, _utc_day(_READ.c.read_at) >= since_day
    )
    daily = select(_DAILY.c.dataset_id, _DAILY.c.client_id, _DAILY.c.client_kind).where(
        _DAILY.c.dataset_id.in_(ids), _DAILY.c.reads > 0, _DAILY.c.day >= since_day
    )
    held: dict[uuid.UUID, tuple[set[ReadClientKind], set[str]]] = {}
    for dataset_id, client_id, kind in (await db.execute(union(per_read, daily))).all():
        kinds, clients = held.setdefault(dataset_id, (set(), set()))
        kinds.add(ReadClientKind(kind))
        clients.add(client_id)
    return {dataset_id: (frozenset(kinds), frozenset(clients)) for dataset_id, (kinds, clients) in held.items()}


@dataclass(frozen=True)
class ClientDatasets:
    """One client seen reading and how many datasets it read in the window. client_id is repr=False."""

    client_id: str = field(repr=False)
    client_kind: ReadClientKind
    datasets: int


async def read_client_datasets(
    db: AsyncSession, dataset_ids: Collection[uuid.UUID], *, now: datetime, window_days: int
) -> list[ClientDatasets]:
    """Each client that read any of `dataset_ids` in the last `window_days`, with the datasets it read.

    Not filtered by client: it feeds the client picker. Only reads count.

    Args:
        db: A session; the read is one statement, and none for an empty `dataset_ids`.
        dataset_ids: The datasets to count over.
        now: The instant the window ends at (aware, else ValueError).
        window_days: The window's length in days.

    Returns:
        list[ClientDatasets]: Most datasets first, then by client id.
    """
    if not dataset_ids:
        return []
    since_day = _window_start_day(now, window_days)
    ids = list(dataset_ids)
    per_read = select(_READ.c.dataset_id, _READ.c.client_id, _READ.c.client_kind).where(
        _READ.c.dataset_id.in_(ids), _READ.c.kind == DatasetReadKind.READ, _utc_day(_READ.c.read_at) >= since_day
    )
    daily = select(_DAILY.c.dataset_id, _DAILY.c.client_id, _DAILY.c.client_kind).where(
        _DAILY.c.dataset_id.in_(ids), _DAILY.c.reads > 0, _DAILY.c.day >= since_day
    )
    seen = union(per_read, daily).subquery('readers')
    stmt = (
        select(seen.c.client_id, seen.c.client_kind, func.count(seen.c.dataset_id.distinct()))
        .group_by(seen.c.client_id, seen.c.client_kind)
        .order_by(func.count(seen.c.dataset_id.distinct()).desc(), seen.c.client_id)
    )
    return [
        ClientDatasets(client_id, ReadClientKind(kind), int(count))
        for client_id, kind, count in (await db.execute(stmt)).all()
    ]


@dataclass(frozen=True)
class ClientDayReads:
    """Reads on one UTC day by one client. client_id is repr=False: it is never logged."""

    day: date
    client_id: str = field(repr=False)
    client_kind: ReadClientKind
    reads: int


async def read_client_day_reads(
    db: AsyncSession,
    dataset_ids: Collection[uuid.UUID] | None,
    *,
    now: datetime,
    days: int,
    client_kinds: Collection[ReadClientKind] | None = None,
    client_ids: Collection[str] | None = None,
) -> list[ClientDayReads]:
    """Reads per UTC day per client over the last `days` UTC days INCLUDING today, over per-read rows and the rollup.

    The union of dataset_read (reads only: a stream session is not a read) and dataset_read_daily is summed per
    (day, client id, kind), so a day split across the rollup boundary is one entry and the series is continuous.
    Only days with a read appear. Used by tj-grna9p.64.

    Args:
        db: A session; the read is one statement.
        dataset_ids: Count only these datasets, or None for every dataset with a read.
        now: The instant the window ends at; aware, else ValueError.
        days: The window's length in UTC days, today included (1 is today only).
        client_kinds: Count only these kinds. Default {SDK}. Ignored when client_ids is given.
        client_ids: Count only these client ids instead of filtering by kind.

    Returns:
        list[ClientDayReads]: Ascending by day, then client id, then kind.
    """
    since_day = _window_start_day(now, days - 1)
    per_read_day = _utc_day(_READ.c.read_at)
    per_read = (
        select(per_read_day.label('day'), _READ.c.client_id, _READ.c.client_kind, func.count().label('reads'))
        .where(_READ.c.kind == DatasetReadKind.READ, per_read_day >= since_day)
        .group_by(per_read_day, _READ.c.client_id, _READ.c.client_kind)
    )
    daily = (
        select(_DAILY.c.day, _DAILY.c.client_id, _DAILY.c.client_kind, func.sum(_DAILY.c.reads).label('reads'))
        .where(_DAILY.c.day >= since_day)
        .group_by(_DAILY.c.day, _DAILY.c.client_id, _DAILY.c.client_kind)
    )
    if dataset_ids is not None:
        per_read = per_read.where(_READ.c.dataset_id.in_(list(dataset_ids)))
        daily = daily.where(_DAILY.c.dataset_id.in_(list(dataset_ids)))
    if client_ids is not None:
        per_read = per_read.where(_READ.c.client_id.in_(list(client_ids)))
        daily = daily.where(_DAILY.c.client_id.in_(list(client_ids)))
    else:
        kinds = list(client_kinds) if client_kinds is not None else [ReadClientKind.SDK]
        per_read = per_read.where(_READ.c.client_kind.in_(kinds))
        daily = daily.where(_DAILY.c.client_kind.in_(kinds))
    both = union_all(per_read, daily).subquery('client_reads_by_day')
    total = cast(func.sum(both.c.reads), BigInteger)
    stmt = (
        select(both.c.day, both.c.client_id, both.c.client_kind, total)
        .group_by(both.c.day, both.c.client_id, both.c.client_kind)
        .having(func.sum(both.c.reads) > 0)
        .order_by(both.c.day, both.c.client_id, both.c.client_kind)
    )
    return [
        ClientDayReads(day, client_id, ReadClientKind(kind), int(reads))
        for day, client_id, kind, reads in (await db.execute(stmt)).all()
    ]


@dataclass(frozen=True)
class ClientLastReadAt:
    """When one client last read one dataset. client_id is repr=False."""

    dataset_id: uuid.UUID
    client_id: str = field(repr=False)
    client_kind: ReadClientKind
    last_read_at: datetime


async def read_client_last_reads(db: AsyncSession, dataset_ids: Collection[uuid.UUID]) -> list[ClientLastReadAt]:
    """The latest read of each of `dataset_ids` by every client that ever read it, whatever the client filter.

    Over per-read rows and the rollup (`last_read_at` of a folded day), a stream session counting as a read, as in
    read_dataset_usage.

    Args:
        db: A session; the read is one statement, and none for an empty `dataset_ids`.
        dataset_ids: The datasets to report.

    Returns:
        list[ClientLastReadAt]: Ordered by dataset id, then client id.
    """
    if not dataset_ids:
        return []
    ids = list(dataset_ids)
    per_read = (
        select(
            _READ.c.dataset_id, _READ.c.client_id, _READ.c.client_kind, func.max(_READ.c.read_at).label('last_read_at')
        )
        .where(_READ.c.dataset_id.in_(ids))
        .group_by(_READ.c.dataset_id, _READ.c.client_id, _READ.c.client_kind)
    )
    daily = (
        select(
            _DAILY.c.dataset_id,
            _DAILY.c.client_id,
            _DAILY.c.client_kind,
            func.max(_DAILY.c.last_read_at).label('last_read_at'),
        )
        .where(_DAILY.c.dataset_id.in_(ids))
        .group_by(_DAILY.c.dataset_id, _DAILY.c.client_id, _DAILY.c.client_kind)
    )
    both = union_all(per_read, daily).subquery('client_last_reads')
    stmt = (
        select(both.c.dataset_id, both.c.client_id, both.c.client_kind, func.max(both.c.last_read_at))
        .group_by(both.c.dataset_id, both.c.client_id, both.c.client_kind)
        .order_by(both.c.dataset_id, both.c.client_id)
    )
    return [
        ClientLastReadAt(dataset_id, client_id, ReadClientKind(kind), last_read_at)
        for dataset_id, client_id, kind, last_read_at in (await db.execute(stmt)).all()
    ]
