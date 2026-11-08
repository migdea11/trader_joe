"""THE INSTANCE WATCHER: re-sends the requests no ingest process is working on (tj-7zjov1.28, P35).

Design: ADR tj-26jt9w addenda 5 and 9.

A request can be held by nobody in two ways, and the watcher treats both on a periodic pass (the shared run_periodic):

  PENDING     written, and the Submit never reached an answer (the store or the call died between the two). Re-sent,
              as it is, once it is older than PENDING_RESEND_AFTER (a POST still submitting is not touched). Submit is
              idempotent on the request id.
  ABANDONED   NOT_DELIVERED (its ingest instance was replaced before its result arrived), and QUEUED under an instance
              that has been superseded. THE TWO ARE ONE CASE: the instance sighting marks only the rows QUEUED in its
              own statement snapshot, so a request that was moved to QUEUED under the old instance just after the new
              one was first seen is never marked, and nothing works on it. Re-submitted flagged `recovered`, carrying
              the durable attempt count, to the live instance.

THE RE-SUBMISSION WAITS (architect ruling (C), ADR addendum 9 (d)). The idempotency key is (request_id,
attempt_no), and the new instance starts counting from the number it is handed. A request is re-submitted only once its
old instance was superseded at least DEFAULT_DELIVERY_DEADLINE_S (the longest the old instance's delivery client keeps
trying) plus RESUBMIT_MARGIN_SECONDS ago, and the count is read under the request lock (claim_abandoned), so the old
instance's last in-flight report has committed (and is in the count) or is refused for ever. Otherwise the new
instance would reuse that attempt number and its report would be refused for ever.

THE PROBE. Each pass also asks ingest which instance is answering (Instance) and notes it, so a restart is seen even if
RegisterInstance was lost and no report has arrived since; the sighting is the same idempotent one the intake makes.

KILL SWITCH AND DRY RUN. INSTANCE_WATCH_ENABLED=false (also 0, no, off) stops the loop from starting; read at start.
INSTANCE_WATCH_DRY_RUN=true (also 1, yes, on) probes nothing and re-sends nothing: a pass logs what it would have
re-sent. Neither is set in any committed file.

BARS AND ASSET-LIST ROWS are both re-sent and re-submitted (an asset-list row left open would keep
open_asset_list_request_exists true for its list for good); a corporate-actions row is skipped.

ONE PROCESS: like the reaper and the refresh, correct only while data_store runs one worker.

NOTHING IN A LOG NAMES AN OWNER OR A SYMBOL: counts, ids and exception types only (D8).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from common.environment import get_env_var
from common.errors.vocabulary import TraderJoeError
from common.logging import get_logger
from common.rpc.clients.ingest_submit import IngestSubmitClient
from common.rpc.clients.store_delivery import DEFAULT_DELIVERY_DEADLINE_S
from common.scheduling import Every, run_periodic
from data.store.app.database.crud.ingest_instance import note_instance_seen
from data.store.app.database.crud.ingest_request import claim_abandoned, list_abandoned, list_pending_before
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_request import IngestRequestKind
from data.store.app.ingest.data_action_request import submit_record


log = get_logger(__name__)

ENABLED_ENV: Final = 'INSTANCE_WATCH_ENABLED'
DRY_RUN_ENV: Final = 'INSTANCE_WATCH_DRY_RUN'
INTERVAL_ENV: Final = 'INSTANCE_WATCH_INTERVAL_SECONDS'
PENDING_RESEND_AFTER_ENV: Final = 'INSTANCE_WATCH_PENDING_AFTER_SECONDS'
RESUBMIT_MARGIN_ENV: Final = 'INSTANCE_WATCH_RESUBMIT_MARGIN_SECONDS'

DEFAULT_INTERVAL_S: Final = 15
# Longer than a Submit call's deadline plus its answer's commit, so a POST still submitting is not re-sent under itself.
DEFAULT_PENDING_RESEND_AFTER_S: Final = 30
# Added to the delivery deadline before a superseded instance's requests are re-submitted.
DEFAULT_RESUBMIT_MARGIN_S: Final = 15

_FALSE: Final = ('false', '0', 'no', 'off')
_TRUE: Final = ('true', '1', 'yes', 'on')

# The kinds submit_request_of can build; a corporate-actions row is skipped (no store code writes one yet).
_SENDABLE_KINDS: Final = frozenset({IngestRequestKind.BARS, IngestRequestKind.ASSET_LIST})


def is_enabled() -> bool:
    """Whether the watcher runs. Anything but an explicit false (`false`, `0`, `no`, `off`) leaves it on."""
    return get_env_var(ENABLED_ENV, default='true', cast_type=str).strip().lower() not in _FALSE


def is_dry_run() -> bool:
    """Whether a pass only logs. Only an explicit true (`true`, `1`, `yes`, `on`) makes it so; unset is live."""
    return get_env_var(DRY_RUN_ENV, default='false', cast_type=str).strip().lower() in _TRUE


def _non_negative(name: str, default: int) -> int:
    value = get_env_var(name, default=default, cast_type=int)
    return value if value >= 0 else default


def interval_seconds() -> int:
    """Seconds between passes; a non-positive or malformed value is the default."""
    value = get_env_var(INTERVAL_ENV, default=DEFAULT_INTERVAL_S, cast_type=int)
    return value if value > 0 else DEFAULT_INTERVAL_S


def pending_resend_after() -> timedelta:
    """How old a PENDING request must be before the watcher re-sends it."""
    return timedelta(seconds=_non_negative(PENDING_RESEND_AFTER_ENV, DEFAULT_PENDING_RESEND_AFTER_S))


def resubmit_delay() -> timedelta:
    """How long after a supersession its instance's requests are re-submitted: the delivery deadline plus a margin."""
    return timedelta(
        seconds=DEFAULT_DELIVERY_DEADLINE_S + _non_negative(RESUBMIT_MARGIN_ENV, DEFAULT_RESUBMIT_MARGIN_S)
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WatchResult:
    """What one pass did. Counts only."""

    # PENDING requests re-sent, and ABANDONED ones re-submitted recovered.
    resent: int = 0
    resubmitted: int = 0
    # Requests the pass found and could not send: a call that failed (left for the next pass), or a row this release
    # cannot build a request from (a corporate-actions row, or a row missing a field).
    failed: int = 0
    skipped: int = 0


class InstanceWatcher:
    """One periodic pass over the requests nobody is working on, over the Submit client the lifespan built.

    Args:
        client: The Submit seam, as the interface.
        clock: The current aware instant; injected so a test does not wait out the delay.
        on_instance: Called with the instance id after each probe noted it (the asset-list snapshot schedule's
            ingest-started hook, P47); None for none.
    """

    def __init__(
        self, client: IngestSubmitClient, *, clock=_utc_now, on_instance: Callable[[str], Awaitable[None]] | None = None
    ) -> None:
        self._client = client
        self._clock = clock
        self._on_instance = on_instance

    async def run_pass(self) -> WatchResult:
        """Probe the live instance, then re-send what is PENDING and re-submit what is abandoned.

        Returns:
            WatchResult: The counts. All zero when nothing needs doing.
        """
        now = self._clock()
        dry_run = is_dry_run()
        if not dry_run:
            await self._probe()
        async with own_session() as db:
            pending = await list_pending_before(db, now - pending_resend_after())
            abandoned = await list_abandoned(db, now - resubmit_delay())
        if dry_run:
            log.info(
                f'Instance watch (dry run): would re-send {len(pending)} pending and re-submit {len(abandoned)} '
                'abandoned request(s)'
            )
            return WatchResult(skipped=len(pending) + len(abandoned))
        resent = resubmitted = failed = skipped = 0
        for record in pending:
            if record.kind not in _SENDABLE_KINDS:
                skipped += 1
                continue
            try:
                await submit_record(record, self._client)
            except ValueError:
                # A row that cannot be built into a request (a field it needs is missing): never sent.
                skipped += 1
                continue
            except TraderJoeError as error:
                # The type only, and the reason: the request stays PENDING for the next pass.
                log.warning(f'Instance watch could not re-send a request: {type(error).__name__} {error.reason}')
                failed += 1
                continue
            resent += 1
        for request_id in abandoned:
            async with own_session() as db:
                # The count is read under the request lock, after the delay (module docstring).
                record = await claim_abandoned(db, request_id, now - resubmit_delay())
            if record is None:
                continue
            if record.kind not in _SENDABLE_KINDS:
                skipped += 1
                continue
            try:
                await submit_record(record, self._client, recovered=True)
            except ValueError:
                skipped += 1
                continue
            except TraderJoeError as error:
                log.warning(f'Instance watch could not re-submit a request: {type(error).__name__} {error.reason}')
                failed += 1
                continue
            resubmitted += 1
        if resent or resubmitted or failed or skipped:
            log.info(
                f'Instance watch: {resent} pending re-sent, {resubmitted} recovered re-submitted, '
                f'{failed} failed, {skipped} skipped'
            )
        return WatchResult(resent=resent, resubmitted=resubmitted, failed=failed, skipped=skipped)

    async def _probe(self) -> None:
        """Ask ingest which instance is answering and note it; a peer that is down is the next pass's problem."""
        try:
            answer = await self._client.instance()
            async with own_session() as db:
                await note_instance_seen(db, answer.instance_id)
            if self._on_instance is not None:
                await self._on_instance(answer.instance_id)
        except TraderJoeError as error:
            log.warning(f'Instance watch could not probe ingest: {type(error).__name__} {error.reason}')


def start_instance_watch_loop(
    client: IngestSubmitClient, *, on_instance: Callable[[str], Awaitable[None]] | None = None
) -> asyncio.Task[None] | None:
    """Start the periodic pass. Call from the running loop, after the Submit client exists.

    Args:
        client: The Submit seam the watcher re-sends through.
        on_instance: Called with each probed instance id, after it was noted; None for none.

    Returns:
        asyncio.Task[None] | None: The task, for stop_instance_watch_loop; None when INSTANCE_WATCH_ENABLED is off.
    """
    if not is_enabled():
        log.info(f'Instance watch is off ({ENABLED_ENV}); abandoned and pending requests are not re-sent')
        return None
    watcher = InstanceWatcher(client, on_instance=on_instance)

    async def job() -> None:
        await watcher.run_pass()

    return run_periodic('instance-watch', Every(timedelta(seconds=interval_seconds())), job, log=log)


async def stop_instance_watch_loop(task: asyncio.Task[None] | None) -> None:
    """Cancel the loop and await it. Call before the Submit channel and the database go."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
