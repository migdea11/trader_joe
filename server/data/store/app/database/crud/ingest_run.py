"""The ingest_run repository: the collection record's writes and reads (tj-grna9p.18).

EVERY WRITE COMMITS ON ITS OWN, through write_transaction, on the session the caller hands in -- which
is the recorder's own session and never the fetch's (app/ingest/run_recording.py). That is the point of
the record: the RUNNING row is durable before the stream opens, and a fetch that rolls back still
leaves its FAILED row. So unlike store_dataset_entry's *_in_transaction functions, nothing here joins
a transaction the caller owns.

RESULTS ARE DETACHED SNAPSHOTS (IngestRunRecord), never ORM rows: a row read after its commit would be
expired, and the hub carries a run to subscribers long after the session that wrote it has closed.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import Row, case, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.logging import get_logger
from data.store.app.database.models.ingest_run import INTERRUPTED_REASON, IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.database.transaction import write_transaction


log = get_logger(__name__)

# The sentence a swept run carries. Fixed: nothing about why the process died is known here.
INTERRUPTED_MESSAGE: Final = 'The run was still in progress when the service stopped.'


@dataclass(frozen=True)
class IngestRunRecord:
    """One ingest_run row, detached.

    actor is repr=False: it is the request body's owner for a POST /store run and is never logged (D8).
    """

    id: uuid.UUID
    dataset_id: uuid.UUID | None
    request_id: uuid.UUID | None
    trigger: IngestRunTrigger
    actor: str = field(repr=False)
    asset_symbol: str
    asset_type: AssetType
    data_type: DataType
    source: DataSource
    granularity: Granularity
    update_type: UpdateType
    feed: Feed | None
    requested_start: datetime | None
    requested_end: datetime | None
    state: IngestRunState
    bars_written: int
    error_domain: str | None
    error_reason: str | None
    error_message: str | None
    error_id: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_row(cls, row: IngestRun | Row) -> 'IngestRunRecord':
        """Snapshot an ORM row, or a RETURNING row of the table's columns (same attribute names).

        Call it before the transaction that loaded the row commits.
        """
        return cls(
            id=row.id,
            dataset_id=row.dataset_id,
            request_id=row.request_id,
            trigger=IngestRunTrigger(row.trigger),
            actor=row.actor,
            asset_symbol=row.asset_symbol,
            asset_type=row.asset_type,
            data_type=row.data_type,
            source=row.source,
            granularity=row.granularity,
            update_type=UpdateType(row.update_type),
            feed=row.feed,
            requested_start=row.requested_start,
            requested_end=row.requested_end,
            state=IngestRunState(row.state),
            bars_written=row.bars_written,
            error_domain=row.error_domain,
            error_reason=row.error_reason,
            error_message=row.error_message,
            error_id=row.error_id,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )


@dataclass(frozen=True)
class RunFailure:
    """The typed error a FAILED run records. domain and reason are NULL for an error nobody typed."""

    domain: str | None
    reason: str | None
    message: str
    error_id: str | None = None


@dataclass(frozen=True)
class NewRun:
    """What a run is created from: its label, its trigger and the window it asks for."""

    trigger: IngestRunTrigger
    actor: str = field(repr=False)
    asset_symbol: str
    asset_type: AssetType
    data_type: DataType
    source: DataSource
    granularity: Granularity
    update_type: UpdateType
    requested_start: datetime | None
    requested_end: datetime | None
    request_id: uuid.UUID | None = None


async def start_run(db: AsyncSession, run: NewRun) -> IngestRunRecord:
    """Insert a RUNNING row and commit it.

    Args:
        db: The recorder's own session.
        run: The run's label, trigger and requested window.

    Returns:
        IngestRunRecord: The committed row, with its id and started_at.
    """
    async with write_transaction(db, 'record ingest run start'):
        row = (
            await db.execute(
                IngestRun.__table__.insert()
                .values(
                    # Minted here rather than by gen_random_uuid(): the id is needed at once for the
                    # ack and finish, and a client-side id keeps the statement portable.
                    id=uuid.uuid4(),
                    trigger=run.trigger,
                    actor=run.actor,
                    asset_symbol=run.asset_symbol,
                    asset_type=run.asset_type,
                    data_type=run.data_type,
                    source=run.source,
                    granularity=run.granularity,
                    update_type=run.update_type.value,
                    requested_start=run.requested_start,
                    requested_end=run.requested_end,
                    request_id=run.request_id,
                    state=IngestRunState.RUNNING,
                    bars_written=0,
                )
                .returning(*IngestRun.__table__.c)
            )
        ).one()
        return IngestRunRecord.from_row(row)


async def acknowledge_run(db: AsyncSession, run_id: uuid.UUID, dataset_id: uuid.UUID, feed: Feed) -> IngestRunRecord:
    """Set the dataset the run is collecting, and the tape ingest resolved, once the fetch is acknowledged.

    A separate committed UPDATE on the run's own session, so a dataset shows Running during its page
    writes. The entry it names may still be uncommitted: dataset_id carries no foreign key.

    Args:
        db: The recorder's own session.
        run_id: The run.
        dataset_id: The entry the ack's upsert returned.
        feed: The resolved tape off FetchAccepted.

    Returns:
        IngestRunRecord: The row after the update.
    """
    async with write_transaction(db, 'record ingest run acknowledgement'):
        stmt = (
            update(IngestRun.__table__)
            .where(IngestRun.__table__.c.id == run_id)
            .values(dataset_id=dataset_id, feed=feed)
            .returning(*IngestRun.__table__.c)
        )
        return IngestRunRecord.from_row((await db.execute(stmt)).one())


async def finish_run(
    db: AsyncSession, run_id: uuid.UUID, *, state: IngestRunState, bars_written: int, failure: RunFailure | None = None
) -> IngestRunRecord:
    """Move a run to a terminal state and commit it.

    A FAILED run's dataset_id is KEPT ONLY IF THE ENTRY EXISTS: the ack's upsert may have written a
    new entry inside a fetch transaction that then rolled back, which leaves a dataset id naming
    nothing. In that case the run reverts to the NULL a run that failed before its ack carries, so a
    run never points at a dataset that was never committed. One statement, so there is no window.

    Args:
        db: The recorder's own session.
        run_id: The run.
        state: SUCCEEDED, EMPTY or FAILED.
        bars_written: Bars the fetch wrote.
        failure: The typed error, for FAILED only.

    Returns:
        IngestRunRecord: The row after the update.
    """
    table = IngestRun.__table__
    values: dict = {'state': state, 'bars_written': bars_written, 'finished_at': func.now()}
    # Always written, so a finish never leaves a stale error from an earlier outcome behind.
    values.update(
        error_domain=failure.domain if failure else None,
        error_reason=failure.reason if failure else None,
        error_message=failure.message if failure else None,
        error_id=failure.error_id if failure else None,
    )
    if state is IngestRunState.FAILED:
        entry_exists = exists().where(StoreDatasetEntry.__table__.c.id == table.c.dataset_id)
        values['dataset_id'] = case((entry_exists, table.c.dataset_id), else_=None)
    async with write_transaction(db, 'record ingest run finish'):
        stmt = update(table).where(table.c.id == run_id).values(**values).returning(*table.c)
        return IngestRunRecord.from_row((await db.execute(stmt)).one())


async def ingest_run_table_exists(db: AsyncSession) -> bool:
    """Whether the ingest_run table exists, by one catalogue read that does not open a write transaction.

    The startup sweep asks before its UPDATE: on a never-migrated database the UPDATE would fail with
    UndefinedTable, which write_transaction (rightly, for every write) logs as a bug with its full
    traceback. A read here keeps that out of the sweep without softening write_transaction.

    Args:
        db: The recorder's own session.

    Returns:
        bool: True when the table resolves on the session's search path.
    """
    result = await db.execute(select(func.to_regclass(IngestRun.__tablename__).is_not(None)))
    return bool(result.scalar_one())


async def fail_interrupted_runs(db: AsyncSession) -> int:
    """Turn every RUNNING row FAILED / INTERRUPTED. The startup sweep.

    SAFE ONLY BECAUSE data_store RUNS ONE PROCESS (the store CLAUDE.md says so): with two workers one
    worker's sweep would fail the other's live run.

    Args:
        db: The recorder's own session.

    Returns:
        int: How many rows were swept.
    """
    table = IngestRun.__table__
    async with write_transaction(db, 'sweep interrupted ingest runs'):
        result = await db.execute(
            update(table)
            .where(table.c.state == IngestRunState.RUNNING)
            .values(
                state=IngestRunState.FAILED,
                error_domain=None,
                error_reason=INTERRUPTED_REASON,
                error_message=INTERRUPTED_MESSAGE,
                finished_at=func.now(),
            )
        )
        return result.rowcount


async def list_runs_for_dataset(
    db: AsyncSession, dataset_id: uuid.UUID, *, limit: int, after: tuple[datetime, uuid.UUID] | None = None
) -> list[IngestRunRecord]:
    """A dataset's runs, newest first, by keyset on (started_at DESC, id DESC).

    Args:
        db: A session.
        dataset_id: The dataset.
        limit: At most this many rows. A caller that needs to know whether more exist asks for one more.
        after: The (started_at, id) of the last row of the previous page; rows strictly after it in the
            sort order come back.

    Returns:
        list[IngestRunRecord]: The page, newest first.
    """
    stmt = select(IngestRun).where(IngestRun.dataset_id == dataset_id)
    if after is not None:
        stmt = stmt.where(
            (IngestRun.started_at < after[0]) | ((IngestRun.started_at == after[0]) & (IngestRun.id < after[1]))
        )
    stmt = stmt.order_by(IngestRun.started_at.desc(), IngestRun.id.desc()).limit(limit)
    return [IngestRunRecord.from_row(row) for row in (await db.execute(stmt)).scalars()]


async def latest_runs_for_datasets(db: AsyncSession, dataset_ids: list[uuid.UUID]) -> dict[uuid.UUID, IngestRunRecord]:
    """The newest run of each given dataset, in ONE statement (a window function), whatever its state.

    A dataset that has never run is absent from the result.

    Args:
        db: A session.
        dataset_ids: The datasets of one page.

    Returns:
        dict[uuid.UUID, IngestRunRecord]: Dataset id to its latest run.
    """
    if not dataset_ids:
        return {}
    # A window function rather than DISTINCT ON, which is Postgres-only: the same one statement, and
    # the (dataset_id, started_at DESC, id DESC) index serves the partition.
    ranked = (
        select(
            IngestRun,
            func.row_number()
            .over(partition_by=IngestRun.dataset_id, order_by=(IngestRun.started_at.desc(), IngestRun.id.desc()))
            .label('rank'),
        )
        .where(IngestRun.dataset_id.in_(dataset_ids))
        .subquery()
    )
    newest = aliased(IngestRun, ranked)
    stmt = select(newest).where(ranked.c.rank == 1)
    return {row.dataset_id: IngestRunRecord.from_row(row) for row in (await db.execute(stmt)).scalars()}


async def running_dataset_ids(db: AsyncSession) -> frozenset[uuid.UUID]:
    """The datasets that have a run in state RUNNING right now, in one statement.

    Served by the partial index on RUNNING rows, so it reads only the runs in flight. A RUNNING run
    that has not been acknowledged has no dataset yet and names none.

    Args:
        db: A session.

    Returns:
        frozenset[uuid.UUID]: The dataset ids.
    """
    stmt = (
        select(IngestRun.dataset_id)
        .where(IngestRun.state == IngestRunState.RUNNING, IngestRun.dataset_id.is_not(None))
        .distinct()
    )
    return frozenset((await db.execute(stmt)).scalars())
