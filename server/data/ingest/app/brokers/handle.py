"""A broker handle: the one place a broker's requests are held, scheduled, attempted and reported.

Ingest has one handle per broker, routed by the request's source (tj-26jt9w addendum 1 item 1). A handle owns the
broker's Scheduler (limits, FIFO queue, credential provider), its reader and its AckGatedExecutor (workers, retry
gate, bounded outgoing buffer). Submit holds a request and answers AT ONCE with the ack: the request is queued, not
fetched, and the store's POST stays synchronous on its own side of the hop. Everything after that -- the vendor
call, the retries, the report to the store -- happens behind the queue.

IDEMPOTENT ON request_id. The store re-sends a request it has not heard back about (its restart, tj-7zjov1 N4
item 5), so a request id already queued, running, waiting out a backoff or awaiting its acknowledgement is
answered again with the same ack and is not queued twice.

RESTART INTAKE (P36, tj-7zjov1.21; tj-26jt9w addendum 5). The store re-requests, flagged recovered, every request the
old instance held whose result never reached it, each with its durable attempt count. For one that is not already
held here the handle charges its registered cost estimate to its scope's window (Scheduler.charge_restart) BEFORE it
is queued, never started requests included, so the new instance starts with the right count. The count it arrives
with is kept: numbering continues from it. A recovered re-submit of an id this instance holds is a duplicate, like
any other: answered again, neither charged nor queued twice.

A request that arrives at or past max_attempts (its budget spent in an earlier instance) is bounded here: it gets no
vendor attempt and is never queued. It is reported to the store as EXHAUSTED, so the store ends it. The check does not
depend on the recovered flag; the charge does: a recovered one is charged its estimate before the report (the old
instance may have had an attempt in flight), one not flagged recovered is not.

What a handle does NOT do: announce the instance or answer the probe (app/instance.py).
"""

from collections.abc import Callable
from dataclasses import dataclass

from common.enums.data_stock import DataSource, Feed
from common.errors.vocabulary import ERROR_DOMAIN, InvalidRequestError, Reason
from common.logging import get_logger
from common.rpc.clients.store_delivery import StoreDeliveryClient
from data.ingest.app.brokers.bars_attempt import BarsWork
from data.ingest.app.brokers.interface import BrokerRead
from data.ingest.app.brokers.rate_budget import priority_for_update_type
from data.ingest.app.scheduler.executor import AckGatedExecutor
from data.ingest.app.scheduler.queue import ExpiredRequest, ScheduledRequest
from data.ingest.app.scheduler.scheduler import Scheduler
from schemas.data_ingest.fetch_dataset import FetchRefused
from schemas.data_ingest.submit import RequestKind, SubmitAccepted, SubmitAck, SubmitRequest


log = get_logger(__name__)

# The fixed prefix of a handle's gRPC health service name. A registered servicer is named by its protobuf package and
# service (trader_joe.proto.internal.ingest.v1.IngestService), so a name under this prefix cannot collide with one.
INGEST_DELIVERY_HEALTH_PREFIX = 'trader_joe.ingest.delivery.'


@dataclass(frozen=True)
class Intake:
    """What a request kind's intake decides for the shared tail: where it is charged, what it runs, which tape."""

    scope: str
    payload: object
    feed: Feed | None = None


def _not_served(kind: RequestKind) -> InvalidRequestError:
    """Build INVALID_REQUEST for a kind this handle has no intake for."""
    return InvalidRequestError(Reason.INVALID_REQUEST, f'{kind.value} requests are not served yet')


def no_body(kind: RequestKind) -> InvalidRequestError:
    """Build INVALID_REQUEST for a request that carries no body for its kind."""
    return InvalidRequestError(Reason.INVALID_REQUEST, f'invalid {kind.value} request: no body for its kind')


class BrokerHandle:
    """One broker's scheduler, reader and executor, and the Submit that feeds them."""

    def __init__(
        self,
        *,
        source: DataSource,
        scope: str,
        scheduler: Scheduler,
        executor: AckGatedExecutor,
        reader: BrokerRead,
        feed: Feed,
    ) -> None:
        """Bind a handle to the parts its broker built.

        Args:
            source (DataSource): The data source this handle serves; the router's key.
            scope (str): The quota scope its requests are charged to. Starts with the source's value.
            scheduler (Scheduler): The handle's scheduler.
            executor (AckGatedExecutor): The handle's executor, over the same scheduler.
            reader (BrokerRead): What the executor's runner reads.
            feed (Feed): The default tape of the account this handle is built for, a value of the account passed at
                construction (owner rulings, tj-7zjov1.91). The handle is the one place a feed is resolved: every
                bars request is served on this feed and a request naming another is refused. One handle, one account,
                one feed (ADR tj-xn3qa6 addendum 1): never resolved per request or per granularity.
        """
        self.__source = source
        self.__scope = scope
        self.__scheduler = scheduler
        self.__executor = executor
        self.__reader = reader
        self.__feed = feed
        self.__active: set[str] = set()
        self.__intakes: dict[RequestKind, Callable[[SubmitRequest], Intake | SubmitAck]] = {}
        self.register_intake(RequestKind.BARS, self.__bars_intake)

    @property
    def source(self) -> DataSource:
        """DataSource: The source this handle serves."""
        return self.__source

    @property
    def feed(self) -> Feed:
        """Feed: The default tape of the account this handle serves; the one feed every bars request is served on."""
        return self.__feed

    @property
    def reader(self) -> BrokerRead:
        """BrokerRead: The handle's reader, the one its runners read through."""
        return self.__reader

    @property
    def scheduler(self) -> Scheduler:
        """Scheduler: The handle's scheduler."""
        return self.__scheduler

    @property
    def executor(self) -> AckGatedExecutor:
        """AckGatedExecutor: The handle's executor; its `stalled` is the handle's delivery health."""
        return self.__executor

    @property
    def health_service(self) -> str:
        """str: The handle's named gRPC health service: the delivery prefix plus its quota scope.

        SERVING while its executor is not stalled, NOT_SERVING while it is. The scope never carries an account
        identifier or a credential (ADR tj-fa1rpu D8).
        """
        return f'{INGEST_DELIVERY_HEALTH_PREFIX}{self.__scope}'

    @property
    def active(self) -> int:
        """int: Requests held that have not finished: queued, running, backing off or awaiting acknowledgement."""
        return len(self.__active)

    def start(
        self, deliver: StoreDeliveryClient, instance_id: str, report_health: Callable[[str, bool], None] | None = None
    ) -> None:
        """Start the handle's workers.

        Args:
            deliver (StoreDeliveryClient): Where its attempt reports go.
            instance_id (str): The id this process minted at start.
            report_health (Callable[[str, bool], None] | None): Told (health_service, serving) when the handle
                starts (True) and at each change of the executor's stalled state (serving is not stalled).
        """
        if report_health is None:
            self.__executor.start(deliver, instance_id)
            return
        service = self.health_service
        self.__executor.start(deliver, instance_id, lambda stalled: report_health(service, not stalled))

    async def stop(self) -> None:
        """Stop the handle's workers and its waiting retries."""
        await self.__executor.stop()

    def register_intake(self, kind: RequestKind, intake: Callable[[SubmitRequest], Intake | SubmitAck]) -> None:
        """Register the intake of one request kind: the only part of Submit a kind supplies.

        The shared tail (submit) is not overridden to add a kind. An intake is given the SubmitRequest and returns
        either a refusal ack (nothing is queued) or the Intake it is queued with; it raises for a request this kind
        cannot serve. A second registration of a kind replaces the first.

        Args:
            kind (RequestKind): The request kind the intake serves.
            intake (Callable[[SubmitRequest], Intake | SubmitAck]): Its intake.
        """
        self.__intakes[kind] = intake

    async def submit(self, request: SubmitRequest) -> SubmitAck:
        """Hold one request and answer at once.

        The kind's own intake decides the scope, the work and the refusals; this tail is the same for every kind and
        its order matters: a duplicate id answered again, an exhausted request reported and never queued, a recovered
        one charged before it is queued, and no tape put on a kind whose intake gives none. The caller has validated
        what is knowable before the vendor (routers/data_ingest/bars_validation.py).

        Args:
            request (SubmitRequest): A request of any kind this handle has an intake for.

        Returns:
            SubmitAck: Accepted with the feed (None for a no-tape kind) and the quota scope, or the refusal its
            intake gave. A refused request is not queued.

        Raises:
            InvalidRequestError: INVALID_REQUEST for a kind this handle does not serve yet, and whatever the kind's
                intake raises for a request it cannot serve.
        """
        intake_for = self.__intakes.get(request.kind)
        if intake_for is None:
            raise _not_served(request.kind)
        intake = intake_for(request)
        if isinstance(intake, SubmitAck):
            return intake
        accepted = SubmitAck(accepted=SubmitAccepted(feed=intake.feed, quota_key=intake.scope))
        if request.request_id in self.__active:
            return accepted
        scheduled = ScheduledRequest(
            request_id=request.request_id,
            quota_scope=intake.scope,
            payload=intake.payload,
            ready_by=request.ready_by,
            client_label=request.client,
            attempt_count=request.attempt_count,
            feed=intake.feed,
        )
        self.__active.add(request.request_id)
        if self.__executor.settings.retry.is_last(request.attempt_count):
            # The budget was spent before this instance: no vendor attempt, never queued. A recovered one is still
            # charged (the old instance may have had an attempt in flight), before it is reported.
            if request.recovered:
                self.__charge_recovered(request, scheduled)
            self.__executor.report_exhausted(scheduled)
            return accepted
        if request.recovered:
            # Before the queue, so the window already holds this request's cost when a worker can take it.
            self.__charge_recovered(request, scheduled)
        await self.__scheduler.submit(scheduled)
        return accepted

    def __charge_recovered(self, request: SubmitRequest, scheduled: ScheduledRequest) -> None:
        """Charge a recovered request its registered estimate and log it once (tj-26jt9w addendum 5)."""
        estimate = self.__scheduler.charge_restart(scheduled)
        log.info(
            f'Recovered request {request.request_id} ({request.kind.value}) charged its estimate of {estimate} '
            f'calls to quota scope {scheduled.quota_scope} after an ingest restart'
        )

    def __bars_intake(self, request: SubmitRequest) -> Intake | SubmitAck:
        """The bars kind's intake: serve the account's feed, refuse a named one it cannot serve, charge the data scope.

        The feed is resolved once, when AlpacaHandle is built (broker_api.resolve_feed in AlpacaHandle.__init__); this
        intake serves the stored value and refuses a differing named feed (tj-7zjov1.97, owner rulings of
        tj-7zjov1.91). The feed is sent to the vendor explicitly, so the feed requested is known and recorded; no
        request goes out without one, and a feed is never guessed for one that did.
        """
        if request.bars is None:
            raise no_body(request.kind)
        bars = request.bars
        feed = self.__feed
        if bars.feed is not None and bars.feed != feed:
            return SubmitAck(
                refused=FetchRefused(
                    reason=Reason.FEED_NOT_AVAILABLE.value,
                    domain=ERROR_DOMAIN,
                    detail=f'This account cannot serve the requested feed: feed={bars.feed.value}',
                    metadata={'feed': bars.feed.value},
                )
            )
        return Intake(
            scope=self.__scope,
            payload=BarsWork(request=bars, priority=priority_for_update_type(bars.update_type), feed=feed),
            feed=feed,
        )

    async def report_expired(self, expired: ExpiredRequest) -> None:
        """Send an expired request out through the executor. This is the scheduler's expired sink.

        Args:
            expired (ExpiredRequest): The request the dequeue check refused.
        """
        await self.__executor.report_expired(expired)

    def finished(self, request: ScheduledRequest) -> None:
        """Forget a request that will not run again, so the store can submit its id afresh.

        Args:
            request (ScheduledRequest): The request the executor is done with.
        """
        self.__active.discard(request.request_id)
