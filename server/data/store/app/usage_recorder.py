"""THE USAGE RECORDER: one dataset_read row per bar read, written in batches (tj-grna9p.46).

CONSUMPTION telemetry (db -> client), distinct from the run recorder's COLLECTION record (app/ingest/
run_recording.py). The model is app/database/models/dataset_read.py.

THE READ PATH NEVER WRITES. record_read appends to an in-process buffer and returns; a periodic flush inserts
the buffer as one batch on its own session and its own transaction (never a request's). The flush runs on an
Every schedule from common/scheduling.py, is woken early when the buffer reaches the batch size, and runs once
more at shutdown.

BEST EFFORT, BOTH WAYS. A failed flush is logged by exception type and its rows go back in the buffer for the
next one; the buffer is bounded, and past the bound the oldest rows are dropped (and counted) rather than grow
without limit. Nothing here ever raises into a read, and nothing logs a client id (D8).

AN UNMIGRATED DATABASE is met the way the startup sweep meets it: the tables are probed before the first
write, one warning names `make migrate`, and the rows are dropped. The probe repeats each flush while the
tables are absent, so usage begins on its own once the database is migrated, and the warning is not repeated.

THE PER-DATASET-PER-DAY CAP. At most MAX_IDS_PER_DATASET_DAY distinct client ids are kept per dataset per UTC
day; a later id is recorded as OVERFLOW. It is applied at flush against the ids already stored for that day, so
it holds across restarts. The reserved values and the operator principal never count toward the cap and are
never replaced.

ONE PROCESS: the buffer lives in this process's memory, which is correct only while data_store runs one worker
(see the store CLAUDE.md). The rollup is idempotent, so a second process would not corrupt it, but it would
split the per-day cap.

THE ROLLUP LOOP folds rows past ROLLUP_AFTER_DAYS into dataset_read_daily (crud.fold_reads_before) on its own
Every schedule. It works out what is outstanding from the database, as common/scheduling.py requires.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Final

from common.environment import get_env_var
from common.logging import get_logger
from common.scheduling import Every, run_periodic
from data.store.app.client_label import OPERATOR_PRINCIPAL, OVERFLOW, RESERVED_PREFIX, ClientLabel
from data.store.app.database.crud.dataset_read import (
    NewRead,
    close_session,
    fold_reads_before,
    insert_reads,
    known_client_ids,
    usage_tables_exist,
)
from data.store.app.database.database import own_session
from data.store.app.database.models.dataset_read import DatasetRead, DatasetReadKind


log = get_logger(__name__)

FLUSH_INTERVAL_ENV: Final = 'USAGE_FLUSH_INTERVAL_SECONDS'
FLUSH_BATCH_SIZE_ENV: Final = 'USAGE_FLUSH_BATCH_SIZE'
BUFFER_MAX_ENV: Final = 'USAGE_BUFFER_MAX'
ROLLUP_AFTER_DAYS_ENV: Final = 'USAGE_ROLLUP_AFTER_DAYS'
ROLLUP_INTERVAL_ENV: Final = 'USAGE_ROLLUP_INTERVAL_SECONDS'

DEFAULT_FLUSH_INTERVAL_SECONDS: Final = 5
DEFAULT_FLUSH_BATCH_SIZE: Final = 500
DEFAULT_BUFFER_MAX: Final = 10_000
DEFAULT_ROLLUP_AFTER_DAYS: Final = 90
DEFAULT_ROLLUP_INTERVAL_SECONDS: Final = 3600

MAX_IDS_PER_DATASET_DAY: Final = 32

_ONE_DAY = timedelta(days=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _positive_int(name: str, default: int) -> int:
    value = get_env_var(name, default=default, cast_type=int)
    return value if value > 0 else default


class UsageRecorder:
    """Buffers read events in process and writes them in batches. See the module docstring."""

    def __init__(
        self, *, flush_batch_size: int = DEFAULT_FLUSH_BATCH_SIZE, buffer_max: int = DEFAULT_BUFFER_MAX
    ) -> None:
        self.flush_batch_size = flush_batch_size
        self.buffer_max = max(buffer_max, flush_batch_size)
        # Set when the buffer reaches the batch size; the flush loop waits on it (run_periodic's wake).
        self.wake = asyncio.Event()
        self._buffer: list[NewRead] = []
        self._dropped = 0
        self._flush_lock = asyncio.Lock()
        self._tables_ready = False
        self._warned_missing = False

    @classmethod
    def from_env(cls) -> 'UsageRecorder':
        return cls(
            flush_batch_size=_positive_int(FLUSH_BATCH_SIZE_ENV, DEFAULT_FLUSH_BATCH_SIZE),
            buffer_max=_positive_int(BUFFER_MAX_ENV, DEFAULT_BUFFER_MAX),
        )

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def record_read(
        self,
        dataset_id: uuid.UUID,
        label: ClientLabel,
        route: str,
        bars_returned: int,
        *,
        read_at: datetime | None = None,
    ) -> None:
        """Buffer one bar read. Never raises, never awaits, never touches the database.

        Args:
            dataset_id: The dataset read.
            label: The reader's label and kind (app/client_label.py).
            route: The route template that served the read, never a path with values in it.
            bars_returned: The bars the read returned.
            read_at: When it was served; now when omitted.
        """
        try:
            self._append(
                NewRead(
                    dataset_id=dataset_id,
                    client_id=label.client_id,
                    client_kind=label.client_kind,
                    route=route,
                    bars_returned=bars_returned,
                    read_at=read_at or _utc_now(),
                )
            )
        except Exception as error:
            log.error(f'Could not buffer a dataset read: {type(error).__name__}')

    def _append(self, read: NewRead) -> None:
        self._buffer.append(read)
        self._trim()
        if len(self._buffer) >= self.flush_batch_size:
            self.wake.set()

    def _trim(self) -> None:
        excess = len(self._buffer) - self.buffer_max
        if excess > 0:
            del self._buffer[:excess]
            self._dropped += excess

    async def flush(self) -> int:
        """Write the buffer as one batch. Never raises; a failure puts the rows back for the next flush.

        Returns:
            int: How many rows were written.
        """
        async with self._flush_lock:
            if self._dropped:
                log.warning(f'Dropped {self._dropped} dataset read(s): the usage buffer was full')
                self._dropped = 0
            if not self._buffer:
                return 0
            batch, self._buffer = self._buffer, []
            try:
                async with own_session() as session:
                    if not self._tables_ready:
                        if not await usage_tables_exist(session):
                            self._warn_missing()
                            return 0
                        self._tables_ready = True
                    written = await insert_reads(session, await self._apply_cap(session, batch))
                return written
            except asyncio.CancelledError:
                # Cancelled mid-flush (shutdown): the rows go back, so the final flush still writes them.
                self._buffer[:0] = batch
                self._trim()
                raise
            except Exception as error:
                # Type only (D8): a statement's text carries bound values.
                self._tables_ready = False
                log.error(f'Could not flush {len(batch)} dataset read(s): {type(error).__name__}; will retry')
                self._buffer[:0] = batch
                self._trim()
                return 0

    def _warn_missing(self) -> None:
        if not self._warned_missing:
            self._warned_missing = True
            log.warning(
                f'Usage recording skipped: table {DatasetRead.__tablename__} does not exist; '
                'the database has not been migrated, run make migrate'
            )

    async def _apply_cap(self, session, batch: list[NewRead]) -> list[NewRead]:
        """Replace ids past the per-dataset-per-day cap with OVERFLOW, in arrival order."""
        capped = [read for read in batch if _counts_toward_cap(read.client_id)]
        if not capped:
            return batch
        days = [read.read_at.astimezone(UTC).date() for read in capped]
        lower = datetime.combine(min(days), datetime.min.time(), tzinfo=UTC)
        upper = datetime.combine(max(days), datetime.min.time(), tzinfo=UTC) + _ONE_DAY
        known = await known_client_ids(session, {read.dataset_id for read in capped}, lower, upper)
        seen: dict[tuple[uuid.UUID, date], set[str]] = {
            key: {client_id for client_id in ids if _counts_toward_cap(client_id)} for key, ids in known.items()
        }
        result: list[NewRead] = []
        for read in batch:
            if not _counts_toward_cap(read.client_id):
                result.append(read)
                continue
            ids = seen.setdefault((read.dataset_id, read.read_at.astimezone(UTC).date()), set())
            if read.client_id in ids or len(ids) < MAX_IDS_PER_DATASET_DAY:
                ids.add(read.client_id)
                result.append(read)
            else:
                result.append(_with_client_id(read, OVERFLOW))
        return result

    async def record_session_open(
        self, dataset_id: uuid.UUID, label: ClientLabel, route: str, *, opened_at: datetime | None = None
    ) -> uuid.UUID | None:
        """Write a STREAM_SESSION row for a session that has just opened, at once, and return its id.

        Schema and API only: no stream route exists yet to call it (tj-grna9p.46). Best effort: None when the
        row could not be written, and then record_session_close is a no-op for it.

        Args:
            dataset_id: The dataset streamed.
            label: The reader's label and kind.
            route: The route template that opened the session.
            opened_at: When it opened; now when omitted.

        Returns:
            uuid.UUID | None: The row's id, to hand to record_session_close.
        """
        opened = opened_at or _utc_now()
        row = NewRead(
            dataset_id=dataset_id,
            client_id=label.client_id,
            client_kind=label.client_kind,
            route=route,
            bars_returned=0,
            read_at=opened,
            kind=DatasetReadKind.STREAM_SESSION,
            session_opened_at=opened,
        )
        try:
            async with own_session() as session:
                if not await usage_tables_exist(session):
                    self._warn_missing()
                    return None
                (row,) = await self._apply_cap(session, [row])
                await insert_reads(session, [row])
        except Exception as error:
            log.error(f'Could not record a stream session open: {type(error).__name__}')
            return None
        return row.id

    async def record_session_close(
        self, session_id: uuid.UUID | None, bars_returned: int, *, closed_at: datetime | None = None
    ) -> None:
        """Close a STREAM_SESSION row. Best effort; a None id (the open failed) is a no-op."""
        if session_id is None:
            return
        try:
            async with own_session() as session:
                await close_session(session, session_id, closed_at or _utc_now(), bars_returned)
        except Exception as error:
            log.error(f'Could not record a stream session close: {type(error).__name__}')

    async def roll_up(self, *, now: datetime | None = None, after_days: int | None = None) -> int:
        """Fold per-read rows older than the window into daily summaries. Idempotent; never raises.

        Args:
            now: The instant the window is counted back from; now when omitted.
            after_days: The window in days; USAGE_ROLLUP_AFTER_DAYS (default 90) when omitted.

        Returns:
            int: How many daily summary rows were written or updated.
        """
        days = after_days if after_days is not None else _positive_int(ROLLUP_AFTER_DAYS_ENV, DEFAULT_ROLLUP_AFTER_DAYS)
        cutoff = (now or _utc_now()) - timedelta(days=days)
        try:
            async with own_session() as session:
                if not await usage_tables_exist(session):
                    self._warn_missing()
                    return 0
                folded = await fold_reads_before(session, cutoff)
        except Exception as error:
            log.error(f'Could not roll up dataset reads: {type(error).__name__}')
            return 0
        if folded:
            log.info(f'Rolled up dataset reads into {folded} daily summary row(s)')
        return folded


def _counts_toward_cap(client_id: str) -> bool:
    """Reserved values and the operator principal are exempt from the cap and never replaced."""
    return client_id != OPERATOR_PRINCIPAL and not client_id.startswith(RESERVED_PREFIX)


def _with_client_id(read: NewRead, client_id: str) -> NewRead:
    values = read.as_values()
    values['client_id'] = client_id
    return NewRead(**values)


# ONE RECORDER PER PROCESS, like the events hub. Routers call usage_recorder.record_read; the lifespan runs its
# loops.
usage_recorder: Final = UsageRecorder.from_env()


def start_usage_loops() -> list[asyncio.Task[None]]:
    """Start the flush and the rollup loops. Call from the running loop (the lifespan).

    Returns:
        list[asyncio.Task[None]]: The two tasks, for stop_usage_loops.
    """
    flush_every = timedelta(seconds=_positive_int(FLUSH_INTERVAL_ENV, DEFAULT_FLUSH_INTERVAL_SECONDS))
    rollup_every = timedelta(seconds=_positive_int(ROLLUP_INTERVAL_ENV, DEFAULT_ROLLUP_INTERVAL_SECONDS))

    async def flush_job() -> None:
        await usage_recorder.flush()

    async def rollup_job() -> None:
        await usage_recorder.roll_up()

    return [
        run_periodic('usage-flush', Every(flush_every), flush_job, wake=usage_recorder.wake, log=log),
        run_periodic('usage-rollup', Every(rollup_every), rollup_job, log=log),
    ]


async def stop_usage_loops(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel the loops, await them, and flush what is buffered. Call before the database shuts down."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await usage_recorder.flush()
