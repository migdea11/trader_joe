"""THE EXPIRY REAPER: a periodic pass over the datasets whose expiry has passed (tj-grna9p.23).

THE PENDING SET is `expiry <= now`, whatever the update type, read from the database on every pass by ONE query
(crud list_pending / pending_ids). A retired DAILY or STREAM dataset is pending only once its expiry has passed.
Catch-up after a restart is simply the next pass: nothing is stored and nothing is replayed (common/scheduling.py).

DRY-RUN BY DEFAULT. Deletions need REAPER_DELETES_ENABLED to be exactly `true`; anything else, unset included, is
dry-run. Dry-run logs a COUNT and deletes nothing. The default exists because STATIC rows made before the expiry
default was changed carry an expiry that has long passed, and the operator is to see that list
(GET /ui/v1/reaper/pending) before anything is deleted.

ENABLED, each pending dataset is deleted in its OWN transaction on its own session, through the same cascade as the
operator's delete, with a deletion-record row (cause REAPER, actor 'reaper') and NO override-log row: a system
action. DatasetChanged DELETED is published after each commit. A failure on one dataset is logged by exception type
and the pass carries on.

THE AUDIT RULE. A deletion is never left unrecorded: when the audit tables are missing an enabled pass deletes
nothing (one warning, from audit_tables_exist). A dry-run pass is unaffected.

A DATASET WITH A RUNNING INGEST IS SKIPPED: deleting under a live fetch would fail the run. It is still pending, and
the next pass retries it. The check is made twice: by the pass, and again inside each dataset's delete transaction
under the row lock (reaper_delete_entry), which also re-reads the expiry, so a PATCH that extends it cannot be
overtaken by the delete.

ACCEPTED GAP: a fetch not yet acknowledged has no dataset id recorded (tj-jc635p), so neither check can see it. If the
reaper deletes that dataset, the fetch recreates its entry when it finishes, with its own expiry; nothing it fetched
is lost.

A DATASET THE OPERATOR DELETED FIRST is "already gone": logged at INFO with no owner, and counted as neither a
deletion nor a failure.

ReaperPendingChanged is published when the set of pending ids differs from the previous pass's, after enabled-mode
deletions, and once on the first pass after startup, in dry-run too. It is a notification that the set changed;
whether deletions are enabled is ReaperPendingPage.deletes_enabled.

NOTHING IN A LOG NAMES AN OWNER OR A SYMBOL: counts and exception types only (D8).

ONE PROCESS: the previous pass's ids live in this process's memory, like the events hub, which is correct only
while data_store runs one worker (see the store CLAUDE.md).
"""

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from common.environment import get_env_var
from common.logging import get_logger
from common.scheduling import Every, run_periodic
from data.store.app.database.crud.dataset_override import audit_tables_exist
from data.store.app.database.crud.ingest_run import running_dataset_ids
from data.store.app.database.crud.stock import dataset_catalog as repo
from data.store.app.database.crud.stock.store_dataset_entry import EntryNotFound, reaper_delete_entry
from data.store.app.database.database import own_session
from data.store.app.events import DatasetChange, DatasetChanged, ReaperPendingChanged, publish


log = get_logger(__name__)

INTERVAL_ENV: Final = 'REAPER_INTERVAL_SECONDS'
DELETES_ENABLED_ENV: Final = 'REAPER_DELETES_ENABLED'

DEFAULT_INTERVAL_SECONDS: Final = 3600


def deletes_enabled() -> bool:
    """Whether the reaper deletes: only an explicit `true` (any case) enables it.

    Read from the environment each time, so the pass and GET /ui/v1/reaper/pending always agree.
    """
    return os.environ.get(DELETES_ENABLED_ENV, '').strip().lower() == 'true'


def interval_seconds() -> int:
    """The pass interval from REAPER_INTERVAL_SECONDS; a missing, malformed or non-positive value is the default."""
    value = get_env_var(INTERVAL_ENV, default=DEFAULT_INTERVAL_SECONDS, cast_type=int)
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Outcomes:
    """What one enabled pass did with each pending dataset. Counts only: no owner, no dataset name."""

    deleted: set[uuid.UUID] = field(default_factory=set)
    skipped: int = 0  # a RUNNING ingest when the pass listed
    left_alone: int = 0  # refused inside the transaction: expiry moved, or an ingest started since listing
    already_gone: int = 0
    failed: int = 0


class Reaper:
    """One reaper pass and the state it carries between passes (the previous pass's pending ids)."""

    def __init__(self) -> None:
        # None until the first pass has run: the first pass always publishes ReaperPendingChanged.
        self._previous: frozenset[uuid.UUID] | None = None

    async def run_pass(self, now: datetime | None = None) -> int:
        """Run one pass. Idempotent: it works out what is outstanding from the database.

        Args:
            now: The instant expiry is judged at; aware; now when omitted.

        Returns:
            int: How many datasets were deleted (always 0 in dry-run).
        """
        instant = now or _utc_now()
        enabled = deletes_enabled()
        async with own_session() as session:
            pending = await repo.pending_ids(session, instant)
            running = await running_dataset_ids(session) if enabled and pending else frozenset()
            recording = await audit_tables_exist(session) if enabled and pending else False

        outcomes = _Outcomes()
        if not enabled:
            if pending:
                log.info(f'Expiry reaper dry-run: {len(pending)} dataset(s) are past expiry; nothing is deleted')
        elif pending and not recording:
            # audit_tables_exist has warned once; a deletion is never left unrecorded.
            log.info(f'Expiry reaper: {len(pending)} dataset(s) are past expiry; not deleted, audit tables missing')
        elif pending:
            outcomes = await self._delete(sorted(pending), running, instant)
            log.info(
                f'Expiry reaper: {len(outcomes.deleted)} of {len(pending)} dataset(s) past expiry deleted, '
                f'{outcomes.skipped} skipped for a running ingest, '
                f'{outcomes.left_alone} left alone (expiry moved or ingest started since listing), '
                f'{outcomes.already_gone} gone before their turn, {outcomes.failed} failed'
            )

        deleted = outcomes.deleted
        remaining = pending - deleted
        changed = self._previous is None or remaining != self._previous or bool(deleted)
        self._previous = remaining
        if changed:
            # Strictly after every commit above.
            publish(ReaperPendingChanged())
        return len(deleted)

    async def _delete(self, ids: list[uuid.UUID], running: frozenset[uuid.UUID], now: datetime) -> _Outcomes:
        outcomes = _Outcomes()
        for dataset_id in ids:
            if dataset_id in running:
                outcomes.skipped += 1
                continue
            try:
                async with own_session() as session:
                    done = await reaper_delete_entry(session, dataset_id, now)
            except EntryNotFound:
                log.info('Expiry reaper: a dataset was already gone')
                outcomes.already_gone += 1
                continue
            except Exception as error:
                # The type only: a message can carry a bound parameter, and this record's owner is sensitive.
                log.error(f'Expiry reaper could not delete a dataset: {type(error).__name__}')
                outcomes.failed += 1
                continue
            if done:
                outcomes.deleted.add(dataset_id)
                publish(DatasetChanged(dataset_id, DatasetChange.DELETED))
            else:
                outcomes.left_alone += 1
        return outcomes


# ONE REAPER PER PROCESS, like the usage recorder and the events hub.
reaper: Final = Reaper()


def start_reaper_loop() -> asyncio.Task[None]:
    """Start the periodic pass. Call from the running loop (the lifespan).

    Returns:
        asyncio.Task[None]: The task, for stop_reaper_loop. The first pass runs at once.
    """

    async def job() -> None:
        await reaper.run_pass()

    return run_periodic('expiry-reaper', Every(timedelta(seconds=interval_seconds())), job, log=log)


async def stop_reaper_loop(task: asyncio.Task[None]) -> None:
    """Cancel the loop and await it. Call before the database shuts down."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
