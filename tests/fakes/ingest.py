"""FakeIngest: one ingest process's broker handle, built from the production parts around a FakeRead, that restarts.

Bead tj-7zjov1.24 (P14). Design: ADR tj-26jt9w addendum 1 item 1 (one handle per broker, its scheduler built through
the shared SchedulerBuilder), addenda 4 and 6 (the acknowledgement-gated executor) and addendum 5 (the restart: a new
instance id, announced to the store; the store re-submits what the old instance held, flagged recovered).

WHAT IS PRODUCTION AND WHAT IS NOT. The handle is the production BrokerHandle, its Submit tail included; the scheduler
is the production Scheduler, built through the production SchedulerBuilder with Alpaca's two quota scopes, its 200 a
minute ceiling and its cost function (estimate_request_calls); the executor is the production AckGatedExecutor; the
runners are the production bars, asset-list and corporate-actions runners; the identity is minted by the production
mint_instance. Only the reader is a fake: a FakeRead PACED on the handle's own scheduler, as AlpacaRead is. The intakes
of the asset-list and corporate-actions kinds are written here in AlpacaHandle's terms (trading scope for the list,
data scope for the actions, no tape, BACKFILL). AlpacaHandle now takes a reader factory (P53, tj-7zjov1.158), and the
fake-mode launcher uses it, but this class still builds its own parts: AlpacaHandle offers no seam for the injectable
clocks, budget clock and sleep below, and a fake never patches production code (tests/fakes/__init__.py: production
holds no fake and no swap). So the two intakes stay re-stated here, and test_fake_ingest_intakes_pinned.py runs them
beside AlpacaHandle's on the same requests so they cannot drift (P53v, tj-7zjov1.159).

A RESTART is a new process: restart() stops the handle and builds every part again, so the queue, the outgoing
buffer, the rate budgets, the reader's per-instance memory (FAILONCE_, REUSED_) and the instance id are all new, and
the new id is announced to the store. What survives is the store's.

CLOCKS. Every clock is injectable: the wall clock of the scheduler, the executor and the reader; the monotonic clock
the rate budgets refill on; and the executor's and the reader's sleep. Scheduler.acquire sleeps through its own
module's asyncio, which a test replaces as test_scheduler.py does; nothing here can.

IMPORT DISCIPLINE: this package runs in the production image (tests/fakes/__init__.py), so this module imports only
the standard library and first-party code.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from common.rpc.clients.store_delivery import StoreDeliveryClient
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.asset_attempt import AssetListAttemptRunner, AssetListWork
from data.ingest.app.brokers.alpaca.corporate_actions_attempt import CorporateActionsAttemptRunner, CorporateActionsWork
from data.ingest.app.brokers.alpaca.handle import (
    ALPACA_BARS_PAGE_SIZE,
    ALPACA_CALLS_PER_MINUTE_CEILING,
    ALPACA_DATA_SCOPE,
    ALPACA_TRADING_SCOPE,
    estimate_request_calls,
)
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner
from data.ingest.app.brokers.handle import BrokerHandle, Intake, no_body
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.instance import InstanceIdentity, mint_instance
from data.ingest.app.scheduler import (
    AckGatedExecutor,
    AttemptResult,
    Credential,
    ExecutorSettings,
    Limit,
    ScheduledRequest,
    Scheduler,
    SchedulerBuilder,
    StaticCredentialProvider,
)
from schemas.data_ingest.delivery import RegisterInstanceRequest
from schemas.data_ingest.submit import RequestKind, SubmitAck, SubmitRequest
from tests.fakes.market_data import FakeRead, FakeWorld


# A label pair the scheduler's credential provider hands out. Not a vendor key: nothing here calls a vendor.
FAKE_CREDENTIAL = Credential(key_id='fake-ingest-key-id', secret='not-a-vendor-secret')

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _KindRunner:
    """Hands each request to the runner of its kind, by its payload, as AlpacaHandle's runner does."""

    def __init__(self, reader: FakeRead) -> None:
        self.__bars = BarsAttemptRunner(reader)
        self.__assets = AssetListAttemptRunner(reader)
        self.__actions = CorporateActionsAttemptRunner(reader)

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        if isinstance(request.payload, AssetListWork):
            return await self.__assets(request, attempt_no)
        if isinstance(request.payload, CorporateActionsWork):
            return await self.__actions(request, attempt_no)
        return await self.__bars(request, attempt_no)


def _asset_list_intake(request: SubmitRequest) -> Intake:
    """AlpacaHandle's asset-list intake: stocks only, the trading scope, BACKFILL, no tape."""
    listing = request.asset_list
    if listing is None:
        raise no_body(request.kind)
    if listing.asset_type is not AssetType.STOCK:
        raise InvalidRequestError(
            Reason.UNSUPPORTED_ASSET_TYPE, f'The fake lists stocks only: asset_type={listing.asset_type!r}'
        )
    return Intake(scope=ALPACA_TRADING_SCOPE, payload=AssetListWork(request=listing, priority=RequestPriority.BACKFILL))


def _corporate_actions_intake(request: SubmitRequest) -> Intake:
    """AlpacaHandle's corporate-actions intake: the data scope, BACKFILL, no tape."""
    actions = request.corporate_actions
    if actions is None:
        raise no_body(request.kind)
    return Intake(
        scope=ALPACA_DATA_SCOPE, payload=CorporateActionsWork(request=actions, priority=RequestPriority.BACKFILL)
    )


class FakeIngest:
    """One ingest process: a production broker handle around a paced FakeRead, delivering to a store.

    Args:
        store (StoreDeliveryClient): Where the reports and the announcement go: a FakeStore in process, or a
            GrpcStoreDeliveryClient dialling a FakeStoreServer.
        settings (ExecutorSettings | None): The executor's settings, or the defaults.
        calls_per_minute (int): The per-minute limit of both scopes, at most Alpaca's ceiling of 200.
        page_size (int): The page size the cost estimate assumes; Alpaca's unless set.
        world (FakeWorld | None): The world the reader serves, or None for its default answers.
        clock (Clock): The wall clock of the scheduler, the executor, the reader and the minted identity.
        budget_clock (Callable[[], float] | None): The monotonic clock the rate budgets refill on, or the default.
        sleep (Sleep): The executor's and the reader's sleep.
    """

    def __init__(
        self,
        store: StoreDeliveryClient,
        *,
        settings: ExecutorSettings | None = None,
        calls_per_minute: int = ALPACA_CALLS_PER_MINUTE_CEILING,
        page_size: int = ALPACA_BARS_PAGE_SIZE,
        world: FakeWorld | None = None,
        clock: Clock = _utc_now,
        budget_clock: Callable[[], float] | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.__store = store
        self.__settings = settings if settings is not None else ExecutorSettings()
        self.__calls_per_minute = calls_per_minute
        self.__page_size = page_size
        self.__world = world
        self.__clock = clock
        self.__budget_clock = budget_clock
        self.__sleep = sleep
        self.__instances: list[InstanceIdentity] = []
        self.__finished: list[str] = []
        self.__running = False
        self.__build()

    # -- what a test reads --------------------------------------------------------------------

    @property
    def handle(self) -> BrokerHandle:
        """BrokerHandle: The current process's handle."""
        return self.__handle

    @property
    def scheduler(self) -> Scheduler:
        """Scheduler: The current process's scheduler."""
        return self.__scheduler

    @property
    def executor(self) -> AckGatedExecutor:
        """AckGatedExecutor: The current process's executor."""
        return self.__executor

    @property
    def reader(self) -> FakeRead:
        """FakeRead: The current process's reader; its call log is this process's vendor calls."""
        return self.__reader

    @property
    def identity(self) -> InstanceIdentity:
        """InstanceIdentity: The current process's identity."""
        return self.__instances[-1]

    @property
    def instances(self) -> tuple[InstanceIdentity, ...]:
        """tuple[InstanceIdentity, ...]: Every identity minted, oldest first: one per process."""
        return tuple(self.__instances)

    @property
    def finished(self) -> tuple[str, ...]:
        """tuple[str, ...]: Every request id the executors were done with, in order, across processes."""
        return tuple(self.__finished)

    # -- what a test drives -------------------------------------------------------------------

    async def submit(self, request: SubmitRequest) -> SubmitAck:
        """Ingest's Submit, as the gRPC handler hands it to the handle.

        Args:
            request (SubmitRequest): The request.

        Returns:
            SubmitAck: The handle's answer.
        """
        return await self.__handle.submit(request)

    def start(self) -> None:
        """Start the current process's workers, delivering to the store under its instance id.

        Raises:
            RuntimeError: If it is already running.
        """
        if self.__running:
            raise RuntimeError('the fake ingest is already running')
        self.__handle.start(self.__store, self.identity.instance_id)
        self.__running = True

    async def announce(self) -> None:
        """Announce the current instance to the store once, as the lifespan's announcement does.

        Raises:
            TraderJoeError: If the store does not acknowledge it.
        """
        identity = self.identity
        await self.__store.register_instance(
            RegisterInstanceRequest(instance_id=identity.instance_id, started_at=identity.started_at)
        )

    async def stop(self) -> None:
        """Stop the current process's workers, its deliveries in flight and its waiting retries."""
        if self.__running:
            self.__running = False
            await self.__handle.stop()

    async def restart(self) -> None:
        """Stop this process and start a new one: every in-memory part rebuilt, a new id minted and announced."""
        await self.stop()
        self.__build()
        self.start()
        await self.announce()

    async def __aenter__(self) -> 'FakeIngest':
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -- the build ----------------------------------------------------------------------------

    def __build(self) -> None:
        minute = timedelta(minutes=1)
        builder = SchedulerBuilder(clock=self.__clock)
        for scope in (ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE):
            builder.declare_scope(scope, [Limit(minute, ALPACA_CALLS_PER_MINUTE_CEILING)]).set_limit(
                minute, self.__calls_per_minute, scope=scope
            )
        builder.cost_function(estimate_request_calls, page_size=self.__page_size)
        builder.credential_provider(StaticCredentialProvider(FAKE_CREDENTIAL))
        builder.on_expired(self.__report_expired)
        if self.__budget_clock is not None:
            builder.budget_clock(self.__budget_clock)
        scheduler = builder.build()
        reader = FakeRead(
            sleep=self.__sleep,
            clock=self.__clock,
            world=self.__world,
            scheduler=scheduler,
            scope=ALPACA_DATA_SCOPE,
            trading_scope=ALPACA_TRADING_SCOPE,
        )
        executor = AckGatedExecutor(
            name=ALPACA_DATA_SCOPE,
            scheduler=scheduler,
            run_attempt=_KindRunner(reader),
            settings=self.__settings,
            on_finished=self.__on_finished,
            clock=self.__clock,
            sleep=self.__sleep,
        )
        handle = BrokerHandle(
            source=DataSource.ALPACA_API,
            scope=ALPACA_DATA_SCOPE,
            scheduler=scheduler,
            executor=executor,
            reader=reader,
            # Alpaca's own per-request rule, as AlpacaHandle passes it (ADR tj-xn3qa6 addendum 3).
            feed_policy=broker_api.resolve_feed,
        )
        handle.register_intake(RequestKind.ASSET_LIST, _asset_list_intake)
        handle.register_intake(RequestKind.CORPORATE_ACTIONS, _corporate_actions_intake)
        self.__scheduler, self.__reader, self.__executor, self.__handle = scheduler, reader, executor, handle
        self.__instances.append(mint_instance(self.__clock))

    async def __report_expired(self, expired) -> None:
        # The scheduler's expired sink, bound to whichever handle is current when the scheduler hands one over.
        await self.__handle.report_expired(expired)

    def __on_finished(self, request: ScheduledRequest) -> None:
        self.__finished.append(request.request_id)
        self.__handle.finished(request)
