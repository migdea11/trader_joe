"""THE RUN RECORDER: one ingest_run row around every fetch the store drives (tj-grna9p.18).

WHY IT IS NOT PART OF THE FETCH'S TRANSACTION. data_action_request.py commits the entry and every page
of bars at FetchDone and rolls everything back otherwise. The collection record has the opposite
needs: the RUNNING row must be durable BEFORE the stream opens, so a second session can see the run
while it is in flight, and a fetch that rolls back must still leave its FAILED row, since that row is
the only trace the attempt happened. So every write here is its own short transaction on its own
session (database.own_session), committed through write_transaction, and none of them ever touches the
request's session.

EVENTS: each committed start, ack and finish is published to the events hub (app/events.py) as
IngestRunChanged, strictly after its own commit.

ORDER, as the route sees it:

    1. start      a RUNNING row, committed, before the stream opens.
    2. ack        the dataset id and resolved feed, as a separate committed UPDATE (the worker's
                  on_ack). A new dataset's entry is written on the ack, so its id does not exist
                  before then: a run that fails before its ack keeps a NULL dataset_id.
    3. finish     SUCCEEDED (bars > 0), EMPTY (0 bars) or FAILED, after the fetch's transaction has
                  committed or rolled back.

THE EXCEPTION STILL PROPAGATES, UNCHANGED (D6). This context manager records and re-raises: the route
catches nothing, and the edge renders the error as problem+json exactly as it did before the record
existed. A cancellation finishes the row inside asyncio.shield; whatever that misses is caught by the
startup sweep (app_depends.lifespan).

THE RECORD IS BEST EFFORT. A failure to write the record is logged and the fetch carries on unrecorded:
the data is the product and the history is bookkeeping, so a database blip in the bookkeeping must not
turn a servable request into an error. The cost is a missing or stuck row, which the startup sweep
bounds.

THE ACTOR IS NEVER LOGGED (D8). It is the request body's owner; nothing in this module formats it.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import Feed
from common.errors.vocabulary import ERROR_DOMAIN, TraderJoeError, own_error_id
from common.logging import get_logger
from data.store.app.database.crud.ingest_run import (
    IngestRunRecord,
    NewRun,
    RunFailure,
    acknowledge_run,
    fail_interrupted_runs,
    finish_run,
    ingest_run_table_exists,
    start_run,
)
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from data.store.app.events import IngestRunChanged, publish
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


log = get_logger(__name__)

# Fixed sentences for the failures nobody typed. Never str() of an exception (D8): that text can carry
# a statement and its bound values.
UNEXPECTED_MESSAGE = 'The run failed with an unexpected error.'
CANCELLED_MESSAGE = 'The run was cancelled before it finished.'


def failure_for(error: BaseException) -> RunFailure:
    """The record of a failure: the typed (domain, reason, detail, error_id), or a fixed sentence.

    Args:
        error: What the fetch raised.

    Returns:
        RunFailure: A TraderJoeError keeps its reason and detail. Anything else has NULL domain and
        reason and a fixed message, because an untyped error is a bug and its text may be sensitive.
    """
    if isinstance(error, TraderJoeError):
        return RunFailure(
            domain=ERROR_DOMAIN, reason=str(error.reason), message=error.detail, error_id=own_error_id(error)
        )
    if isinstance(error, asyncio.CancelledError):
        return RunFailure(domain=None, reason=None, message=CANCELLED_MESSAGE)
    return RunFailure(domain=None, reason=None, message=UNEXPECTED_MESSAGE)


class RunHandle:
    """One run's row, as the route and the worker's ack hook see it.

    Every method swallows its own recording failures (see the module docstring), so none can mask the
    fetch's own outcome. record is None when the RUNNING row could not be written, and then every
    method is a no-op.
    """

    def __init__(self, record: IngestRunRecord | None):
        self.record = record
        # The dataset the ack named, kept here even when recording the ack fails, so the route can
        # announce the dataset's change.
        self.dataset_id: uuid.UUID | None = None

    async def acknowledge(self, dataset_id: uuid.UUID, feed: Feed) -> None:
        """Record the dataset the run is collecting, once ingest has accepted the fetch."""
        self.dataset_id = dataset_id
        if self.record is None:
            return
        try:
            async with own_session() as session:
                self.record = await acknowledge_run(session, self.record.id, dataset_id, feed)
            # After the commit above, as every publish is.
            publish(IngestRunChanged(self.record))
        except Exception as error:
            log.error(f'Could not record the acknowledgement of run {self.record.id}: {type(error).__name__}')

    async def succeed(self, bars_written: int) -> None:
        """Finish the run: SUCCEEDED when it wrote bars, EMPTY when the vendor held none."""
        state = IngestRunState.SUCCEEDED if bars_written > 0 else IngestRunState.EMPTY
        await self._finish(state, bars_written, None)

    async def fail(self, error: BaseException) -> None:
        """Finish the run FAILED with the error's typed record."""
        await self._finish(IngestRunState.FAILED, 0, failure_for(error))

    async def _finish(self, state: IngestRunState, bars_written: int, failure: RunFailure | None) -> None:
        if self.record is None:
            return
        try:
            async with own_session() as session:
                self.record = await finish_run(
                    session, self.record.id, state=state, bars_written=bars_written, failure=failure
                )
            publish(IngestRunChanged(self.record))
        except Exception as error:
            log.error(f'Could not record the finish of run {self.record.id}: {type(error).__name__}')


@asynccontextmanager
async def recorded_run(
    request_path: StoreAssetDatasetPath,
    request_body: StoreAssetDatasetBody,
    *,
    trigger: IngestRunTrigger = IngestRunTrigger.REQUEST,
    request_id: uuid.UUID | None = None,
) -> AsyncIterator[RunHandle]:
    """Record one fetch: a RUNNING row before the body runs, a terminal row after it.

    The body calls handle.acknowledge from the worker's on_ack and handle.succeed when the fetch has
    committed. An exception out of the body finishes the row FAILED and propagates unchanged.

    Args:
        request_path: The asset and data type being stored.
        request_body: What the caller asked for. Its owner becomes the run's actor.
        trigger: What started the run. POST /store is a REQUEST.
        request_id: The request-log entry that caused the run. NULL until the scheduler's
            request log exists (tj-pd41mt), which adds the foreign key.

    Yields:
        RunHandle: The run's handle.
    """
    record = await _begin(request_path, request_body, trigger, request_id)
    handle = RunHandle(record)
    if record is not None:
        publish(IngestRunChanged(record))
    try:
        yield handle
    except asyncio.CancelledError as error:
        # Shielded: the task is being cancelled, so an unprotected await here would be cancelled too
        # and the row would stay RUNNING until the sweep.
        await asyncio.shield(handle.fail(error))
        raise
    except Exception as error:
        await handle.fail(error)
        raise


async def _begin(
    request_path: StoreAssetDatasetPath,
    request_body: StoreAssetDatasetBody,
    trigger: IngestRunTrigger,
    request_id: uuid.UUID | None,
) -> IngestRunRecord | None:
    """Commit the RUNNING row, or return None when it cannot be written."""
    run = NewRun(
        trigger=trigger,
        actor=request_body.owner,
        asset_symbol=request_path.asset_symbol,
        asset_type=request_path.asset_type,
        data_type=request_path.data_type,
        source=request_body.source,
        granularity=request_body.granularity,
        update_type=request_body.update_type,
        requested_start=request_body.start,
        requested_end=request_body.end,
        request_id=request_id,
    )
    try:
        async with own_session() as session:
            return await start_run(session, run)
    except Exception as error:
        log.error(f'Could not record the start of a run: {type(error).__name__}; the fetch goes ahead unrecorded')
        return None


async def _table_is_missing(session: AsyncSession) -> bool:
    """True only when the probe ran and the table is absent; a probe that fails answers False.

    A failed probe must fall through to the sweep's own path, which logs and returns as before. The
    session is rolled back first, since a failed statement leaves the connection's transaction aborted.
    """
    try:
        return not await ingest_run_table_exists(session)
    except Exception:
        await session.rollback()
        return False


async def sweep_interrupted_runs() -> int:
    """Fail every run a previous process left RUNNING, as FAILED / INTERRUPTED. Called once at startup.

    Best effort like the rest of the record: a sweep that cannot run is logged and startup goes on.

    Returns:
        int: How many runs were swept.
    """
    try:
        async with own_session() as session:
            if await _table_is_missing(session):
                log.warning(
                    f'Startup sweep skipped: table {IngestRun.__tablename__} does not exist; '
                    'the database has not been migrated, run make migrate'
                )
                return 0
            swept = await fail_interrupted_runs(session)
    except Exception as error:
        log.error(f'Could not sweep interrupted runs: {type(error).__name__}')
        return 0
    if swept:
        log.warning(f'Marked {swept} run(s) left RUNNING by a previous process as FAILED (INTERRUPTED)')
    return swept
