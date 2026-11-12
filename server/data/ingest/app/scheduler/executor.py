"""The acknowledgement-gated executor: a broker handle's workers, its retries and its outgoing buffer.

Ruled in tj-26jt9w (addendum 1 item 4, addenda 4 and 6). What it does, in order:

- A worker takes a request off the scheduler (which checks ready-by, so an expired request never reaches the
  vendor: it goes to report_expired and out on the same buffer as FAILED-for-EXPIRED, with no vendor call and
  no attempt), runs ONE attempt through the handle's AttemptRunner, and puts the attempt's report in the buffer.
- Every report, success or failure, is delivered to the store by its own task. The store's answer IS the
  acknowledgement. NO RETRY of a request runs until its report is acknowledged: the retry is scheduled by the
  acknowledgement, never by the failure. CONTINUE re-enqueues the request (after the backoff, or after the
  vendor's reset for a rate limit) through Scheduler.submit, so the ready-by is checked again at the next take.
  FINAL drops it. A permanent refusal, and an attempt that spent the budget, are never retried whatever the
  store answers.
- A report that does not reach the store is redelivered with the SAME AttemptDelivery, so the same
  (request_id, attempt_no), after a bounded backoff. That is not a broker retry and makes no vendor call.
- The buffer is bounded (ExecutorSettings.buffer_size). While it is full the workers take nothing from the queue.
  Nothing is dropped.
- If the oldest unacknowledged report has waited longer than ExecutorSettings.stall_timeout_s the executor
  raises its alarm: an ERROR log naming the handle, the oldest request, attempt and age, and `stalled` is True
  until that report is acknowledged. One stuck report therefore cannot stall a broker unnoticed.
- A caller that passes on_stalled_change to start() is told the stalled state when the executor starts (False) and at
  every transition after it (stall start, stall cleared), never by polling. That is how the handle reports the state
  on a named gRPC health service without the executor importing any server code.

STATED CRASH BOUND. An ingest crash loses at most the IN-FLIGHT attempts: the reports in the buffer plus the
attempt each worker is making. Requests still in the queue, and retries waiting out a backoff, never started
or spent nothing at the vendor since their last acknowledged report; the restart charges each its registered
cost estimate (P36) and the store re-requests them. The bound is buffer_size + workers attempts.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from common.enums.data_stock import Feed
from common.errors.vocabulary import TraderJoeError
from common.logging import get_logger
from common.rpc.clients.store_delivery import StoreDeliveryClient
from common.rpc.mapping.values import ProtoMappingError
from common.scheduling import Every, run_periodic
from data.ingest.app.scheduler.outbox import OutgoingBuffer, PendingReport
from data.ingest.app.scheduler.queue import ExpiredRequest, ScheduledRequest
from data.ingest.app.scheduler.retry import RetryPolicy
from data.ingest.app.scheduler.scheduler import Scheduler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryPayload,
    DeliveryTrailer,
    DeliveryVerdict,
)


log = get_logger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AttemptResult:
    """What one attempt came to, as the handle's runner reports it. The executor adds the header's identity.

    Attributes:
        trailer (DeliveryTrailer): How the attempt ended. The runner leaves last_attempt False; the executor sets
            it when this attempt spends the retry budget.
        parts (list[DeliveryPayload]): The payload messages, empty for a failure.
        quota_key (str): The quota scope the attempt was charged to, a non-secret label.
        feed (Feed | None): The resolved tape for a bars attempt, None for other kinds.
        vendor_asset_id (str): The vendor's opaque key, when the attempt had a single instrument.
        reset_at (datetime | None): When a rate limit the attempt met resets, when the vendor named one.
    """

    trailer: DeliveryTrailer
    parts: list[DeliveryPayload] = field(default_factory=list)
    quota_key: str = ''
    feed: Feed | None = None
    vendor_asset_id: str = ''
    reset_at: datetime | None = None


class AttemptRunner(Protocol):
    """Runs ONE attempt of a request at the vendor, in the handle's own terms, and reports it."""

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        """Run one attempt.

        Args:
            request (ScheduledRequest): The request, as the scheduler handed it over.
            attempt_no (int): The attempt's number, the store's count plus one.

        Returns:
            AttemptResult: The report, whether the attempt succeeded or failed. An expected failure is a FAILED
            report and never a raise; a raise is a bug and the executor reports it as a permanent failure.
        """
        ...


@dataclass(frozen=True)
class ExecutorSettings:
    """A handle's builder settings for its executor; configuration may set them.

    Attributes:
        buffer_size (int): The outgoing buffer's capacity, in unacknowledged attempts. Sized so the store's
            acknowledgement adds no latency to broker requests while the store answers in seconds, and so a store
            that is down jams a bounded amount of memory.
        workers (int): Attempts the handle runs at once.
        stall_timeout_s (float): How long the oldest unacknowledged report may wait before the alarm.
        stall_check_interval_s (float): How often the alarm looks.
        redelivery_base_s (float): The wait after a failed delivery.
        redelivery_max_s (float): The most a redelivery backoff waits.
        retry (RetryPolicy): The request retry budget and its waits.
    """

    buffer_size: int = 64
    workers: int = 4
    stall_timeout_s: float = 300.0
    stall_check_interval_s: float = 5.0
    redelivery_base_s: float = 1.0
    redelivery_max_s: float = 30.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        if self.buffer_size < 1 or self.workers < 1:
            raise ValueError('buffer_size and workers must each be at least 1')
        for name in ('stall_timeout_s', 'stall_check_interval_s', 'redelivery_base_s', 'redelivery_max_s'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive, got {getattr(self, name)!r}')


class AckGatedExecutor:
    """One broker handle's workers, retry gate, outgoing buffer and stall alarm."""

    def __init__(
        self,
        *,
        name: str,
        scheduler: Scheduler,
        run_attempt: AttemptRunner,
        settings: ExecutorSettings | None = None,
        on_finished: Callable[[ScheduledRequest], None] | None = None,
        clock: Clock = _utc_now,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Bind the executor to a handle's scheduler and runner. Nothing runs until start().

        Args:
            name (str): The handle's name, for the log and the alarm.
            scheduler (Scheduler): The handle's scheduler, whose expired sink is report_expired.
            run_attempt (AttemptRunner): Runs one attempt at the vendor.
            settings (ExecutorSettings | None): The handle's settings, or the defaults.
            on_finished (Callable[[ScheduledRequest], None] | None): Called once a request will not run again.
            clock (Clock): Now, timezone aware. Injectable for tests.
            sleep (Sleep): Waits, in seconds. Injectable for tests.
        """
        self.__name = name
        self.__scheduler = scheduler
        self.__run_attempt = run_attempt
        self.__settings = settings if settings is not None else ExecutorSettings()
        self.__on_finished = on_finished
        self.__clock = clock
        self.__sleep = sleep
        self.__buffer = OutgoingBuffer(self.__settings.buffer_size)
        self.__deliver: StoreDeliveryClient | None = None
        self.__instance_id: str | None = None
        self.__loops: list[asyncio.Task[None]] = []
        self.__tasks: set[asyncio.Task[None]] = set()
        self.__stalled = False
        self.__last_alarm: datetime | None = None
        self.__on_stalled_change: Callable[[bool], None] | None = None

    @property
    def settings(self) -> ExecutorSettings:
        """ExecutorSettings: The settings the executor was built with."""
        return self.__settings

    @property
    def unacknowledged(self) -> int:
        """int: Reports delivered to nobody yet, or delivered and not yet acknowledged."""
        return len(self.__buffer)

    @property
    def stalled(self) -> bool:
        """bool: True while the oldest unacknowledged report has waited past the stall timeout."""
        return self.__stalled

    def start(
        self, deliver: StoreDeliveryClient, instance_id: str, on_stalled_change: Callable[[bool], None] | None = None
    ) -> None:
        """Start the workers and the alarm.

        Args:
            deliver (StoreDeliveryClient): Where the reports go. One attempt is one call; its answer is the ack.
            instance_id (str): The id this ingest process minted at start, carried in every report header.
            on_stalled_change (Callable[[bool], None] | None): Called with the stalled state at start (False) and
                whenever it changes. It must not block; a raise is logged and does not stop the alarm.

        Raises:
            RuntimeError: If the executor is already running.
        """
        if self.__loops:
            raise RuntimeError(f'the {self.__name} executor is already running')
        self.__deliver = deliver
        self.__instance_id = instance_id
        self.__on_stalled_change = on_stalled_change
        self.__stalled = False
        self.__notify_stalled_change()
        self.__loops = [
            asyncio.create_task(self.__work(), name=f'{self.__name}-worker-{n}') for n in range(self.__settings.workers)
        ]
        # Sleep first, then look: the first fire is one interval out, as the hand-rolled loop's was.
        interval = timedelta(seconds=self.__settings.stall_check_interval_s)
        alarm = run_periodic(
            f'{self.__name}-stall-alarm',
            Every(interval, first=self.__clock() + interval),
            self.__watch,
            clock=self.__clock,
            sleep=self.__sleep,
            log=log,
        )
        alarm.set_name(f'{self.__name}-stall-alarm')
        self.__loops.append(alarm)

    async def stop(self) -> None:
        """Cancel the workers, the deliveries in flight and the waiting retries, and wait for them to end."""
        tasks = [*self.__loops, *self.__tasks]
        self.__loops = []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.__tasks.clear()

    async def report_expired(self, expired: ExpiredRequest) -> None:
        """Send an expired request out on the buffer as FAILED with cause expired. It made no vendor call.

        This is the scheduler's expired sink. It waits for a free slot, like every report. The header feed follows
        the request kind's rule: the tape resolved at intake for a bars request (request.feed), None for a kind
        with no tape.

        Args:
            expired (ExpiredRequest): The request the dequeue check refused, with the instant it was checked.
        """
        request = expired.request
        attempt_no = request.attempt_count + 1
        delivery = AttemptDelivery(
            header=DeliveryHeader(
                request_id=request.request_id,
                instance_id=self.__require_instance(),
                attempt_no=attempt_no,
                feed=request.feed,
            ),
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.EXPIRED,
                detail=f'ready_by {request.ready_by.isoformat() if request.ready_by else None} had passed at '
                f'{expired.checked_at.isoformat()}; no vendor call was made',
            ),
        )
        await self.__enqueue(request, PendingReport(request, delivery, attempt_no, None, self.__clock()))

    def report_exhausted(self, request: ScheduledRequest) -> None:
        """Report a request that entered with its attempt budget already spent, making no vendor call.

        A request the store re-requested at or past max_attempts (its count, or the budget, changed across the
        restart) is not attempted: it goes out on the buffer as an EXHAUSTED report, shaped like EXPIRED (no stats, no
        as_of, no served range, empty quota key and vendor asset id, no error class, last_attempt false) with the
        next attempt number as its idempotency key only, so the store ends it without writing an attempt. It returns
        at once: the report waits for a buffer slot in its own task, so the Submit that called this still answers
        immediately. The header feed follows the request kind's rule, as for EXPIRED: the tape resolved at intake
        (request.feed) for a bars request, None for a kind with no tape. Not called before start().

        Args:
            request (ScheduledRequest): A request whose attempt_count is at or past the retry budget.
        """
        attempt_no = request.attempt_count + 1
        delivery = AttemptDelivery(
            header=DeliveryHeader(
                request_id=request.request_id,
                instance_id=self.__require_instance(),
                attempt_no=attempt_no,
                feed=request.feed,
            ),
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.EXHAUSTED,
                detail=f'the request arrived with {request.attempt_count} attempts counted, at or past the budget of '
                f'{self.__settings.retry.max_attempts}; no vendor call was made',
            ),
        )
        report = PendingReport(request, delivery, attempt_no, None, self.__clock())
        self.__spawn(self.__enqueue(request, report), f'{self.__name}-exhausted-{request.request_id}')

    def check_stall(self) -> bool:
        """Look at the oldest unacknowledged report and raise or clear the alarm.

        Called by the alarm's loop; public so a test can drive it without waiting.

        Returns:
            bool: Whether the executor is stalled after the check.
        """
        oldest = self.__buffer.oldest()
        age = (self.__clock() - oldest.enqueued_at).total_seconds() if oldest is not None else 0.0
        stalled = oldest is not None and age > self.__settings.stall_timeout_s
        now = self.__clock()
        repeat = (
            self.__last_alarm is None or (now - self.__last_alarm).total_seconds() >= self.__settings.stall_timeout_s
        )
        if oldest is not None and stalled and repeat:
            self.__last_alarm = now
            log.error(
                f'Store delivery stalled on handle {self.__name}: the oldest unacknowledged attempt, '
                f'request_id={oldest.request.request_id} attempt_no={oldest.attempt_no}, has waited {age:.0f}s '
                f'(timeout {self.__settings.stall_timeout_s:.0f}s, {oldest.redeliveries} redeliveries, '
                f'{len(self.__buffer)} of {self.__buffer.capacity} buffer slots held)'
            )
        elif self.__stalled and not stalled:
            self.__last_alarm = None
            log.info(f'Store delivery on handle {self.__name} is no longer stalled')
        changed = stalled != self.__stalled
        self.__stalled = stalled
        if changed:
            self.__notify_stalled_change()
        return stalled

    def __notify_stalled_change(self) -> None:
        if self.__on_stalled_change is None:
            return
        try:
            self.__on_stalled_change(self.__stalled)
        except Exception:
            # The alarm's ERROR log and the flag are the record; a failing observer must not take the alarm down.
            log.exception(f'Handle {self.__name}: reporting the stalled state {self.__stalled} failed')

    async def __work(self) -> None:
        while True:
            await self.__buffer.wait_for_space()
            request = await self.__scheduler.take()
            attempt_no = request.attempt_count + 1
            try:
                result = await self.__run_attempt(request, attempt_no)
            except Exception:
                # A bug in the runner. Reported, never inferred: the store must hear that the attempt failed, and a
                # worker that died here would leave the request, and then the broker, silently stuck.
                log.exception(f'Handle {self.__name}: the attempt runner raised for request {request.request_id}')
                result = AttemptResult(
                    DeliveryTrailer(
                        outcome=DeliveryOutcome.FAILED,
                        error_class=DeliveryErrorClass.PERMANENT,
                        detail='ingest hit an internal error running the attempt; see the ingest log',
                    ),
                    quota_key=request.quota_scope,
                )
            delivery = self.__delivery(request, attempt_no, result)
            await self.__enqueue(request, PendingReport(request, delivery, attempt_no, result.reset_at, self.__clock()))

    async def __watch(self) -> None:
        self.check_stall()

    async def __enqueue(self, request: ScheduledRequest, report: PendingReport) -> None:
        await self.__buffer.put(report)
        self.__spawn(self.__deliver_until_acknowledged(report), f'{self.__name}-deliver-{request.request_id}')

    def __spawn(self, coroutine: Awaitable[None], name: str) -> None:
        task = asyncio.ensure_future(coroutine)
        task.set_name(name)
        self.__tasks.add(task)
        task.add_done_callback(self.__tasks.discard)

    async def __deliver_until_acknowledged(self, report: PendingReport) -> None:
        deliver = self.__deliver
        if deliver is None:
            raise RuntimeError(f'the {self.__name} executor delivered before start()')
        delay = self.__settings.redelivery_base_s
        while True:
            try:
                ack = await deliver.deliver(report.delivery)
            except (TraderJoeError, ProtoMappingError) as error:
                report.redeliveries += 1
                log.warning(
                    f'Handle {self.__name}: delivery of request {report.request.request_id} attempt '
                    f'{report.attempt_no} failed ({type(error).__name__}); redelivering the same attempt in '
                    f'{delay:.1f}s (redelivery {report.redeliveries})'
                )
                await self.__sleep(delay)
                delay = min(delay * 2, self.__settings.redelivery_max_s)
                continue
            break
        await self.__buffer.remove(report)
        self.check_stall()
        self.__after_acknowledgement(report, ack)

    def __after_acknowledgement(self, report: PendingReport, ack: DeliverAttemptResponse) -> None:
        trailer = report.delivery.trailer
        request = report.request
        retry_wait = self.__retry_wait(report, ack)
        if retry_wait is None:
            if self.__on_finished is not None:
                self.__on_finished(request)
            return
        # The count the store acknowledged, never one ingest kept: a recovered request resumes from it.
        again = replace(request, attempt_count=max(ack.attempt_count, report.attempt_no))
        log.info(
            f'Handle {self.__name}: request {request.request_id} attempt {report.attempt_no} '
            f'{trailer.error_class}; retrying in {retry_wait:.1f}s'
        )
        self.__spawn(self.__retry_later(again, retry_wait), f'{self.__name}-retry-{request.request_id}')

    def __retry_wait(self, report: PendingReport, ack: DeliverAttemptResponse) -> float | None:
        """Give the wait before the next attempt, or None when the request will not run again."""
        trailer = report.delivery.trailer
        if trailer.outcome is not DeliveryOutcome.FAILED or ack.verdict is not DeliveryVerdict.CONTINUE:
            return None
        if self.__settings.retry.is_last(report.attempt_no):
            # Ingest holds the budget: last_attempt went out on this report, so no further attempt is made, whatever
            # the store answered.
            return None
        if trailer.error_class is DeliveryErrorClass.TRANSIENT:
            return self.__settings.retry.backoff(report.attempt_no)
        if trailer.error_class is DeliveryErrorClass.RATE_LIMITED:
            if report.reset_at is None:
                return self.__settings.retry.backoff(report.attempt_no)
            return self.__settings.retry.reset_wait(report.reset_at, self.__clock())
        # PERMANENT, or a failure that names no class: never retried, whatever the store answered.
        log.warning(
            f'Handle {self.__name}: the store answered CONTINUE for request {report.request.request_id}, whose '
            f'attempt {report.attempt_no} failed {trailer.error_class}; it is not retried'
        )
        return None

    async def __retry_later(self, request: ScheduledRequest, wait: float) -> None:
        await self.__sleep(wait)
        await self.__scheduler.submit(request)

    def __delivery(self, request: ScheduledRequest, attempt_no: int, result: AttemptResult) -> AttemptDelivery:
        """Put the identity on a runner's result and set last_attempt exactly when this FAILED attempt spends the budget."""
        trailer = result.trailer
        last = trailer.outcome is DeliveryOutcome.FAILED and self.__settings.retry.is_last(attempt_no)
        return AttemptDelivery(
            header=DeliveryHeader(
                request_id=request.request_id,
                instance_id=self.__require_instance(),
                attempt_no=attempt_no,
                quota_key=result.quota_key,
                feed=result.feed,
                vendor_asset_id=result.vendor_asset_id,
            ),
            parts=result.parts,
            trailer=trailer.model_copy(update={'last_attempt': last}),
        )

    def __require_instance(self) -> str:
        if self.__instance_id is None:
            raise RuntimeError(f'the {self.__name} executor has no instance id before start()')
        return self.__instance_id
