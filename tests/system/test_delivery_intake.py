"""DeliverAttempt and RegisterInstance through the StoreService gRPC bind, on the stack's Postgres (P34s, tj-7zjov1.127).

THE DESIGN: this bead's cases (1) to (5) and the architect's cases (6) to (11) added at the P34 gate (tj-7zjov1.27,
2026-10-25), ADR tj-26jt9w addenda (4) (5) (6) (9), P34c tj-7zjov1.123 (served range, request trigger, unknown spend)
and P49 tj-7zjov1.143 (the header's vendor asset id on the attempt row). test_delivery_intake_pg.py drives the
intake's Python entry points; THIS file drives the same intake ONLY through the wire: every report is split into
messages by the real GrpcStoreDeliveryClient, crosses a real TCP connection, is reassembled by the real StoreServicer
behind the real error boundary, written by DeliveryIntake on the stack's Postgres, and its answer or its typed refusal
travels back the same way.

WHAT IS REAL, AND THE ONE THING THAT IS NOT. The host is data_store's own: grpc_host.build_grpc_host() reading
APP_INTERNAL_GRPC_HOST / APP_INTERNAL_GRPC_PORT exactly as the lifespan does, around registered_services(DeliveryIntake())
with the intake's MARKET_ACTIVITY_BATCH_SIZE read from the environment as in production. It is hosted INSIDE this
client container on 127.0.0.1, not reached at data-store-grpc:50052 in the running data_store container. That is not
a choice made here, it is the network design: data-store-grpc is an alias on ingest_store only, the server binds that
address only, test_client joins store_api and store_db only (docker-compose.test-client.yaml), and P34b's CI Check
Network Lockdown fails the build if test_client can resolve data-store-grpc or connect to any store address (ADR
tj-q9ae5u addendum 3). The precedent is test_daily_refresh_pass.py, which hosts data_ingest's servicer the same way
for the same reason. What the deployed bind adds beyond this file (the alias, the port expression, the compose
healthcheck, the peer reach from data_ingest) is P34b's, checked by CI's System Testing job. The intake's own_session
is pointed at this test's asyncpg engine, because an engine's pooled connections belong to the event loop that opened
them and every test here has its own loop (as in test_delivery_intake_pg.py).

THE CASES:
  (1)  the host built from the environment serves the health service, SERVING for StoreService's own name and for '';
  (2)  a bars SUCCEEDED delivery commits the bars, ONE attempt row (quota key, verdict, trigger, spend columns, the
       vendor asset id and the served range as sent), the request's count 1, spend and state, and is answered FINAL;
  (3)  the same delivery again is answered identically and changes no row (bars' updated_at included);
  (4)  a report a cross-field rule refuses reaches the client as the typed INVALID_REQUEST with nothing written; a
       request the store does not hold reaches it as NOT_FOUND; rule f both ways (tj-7zjov1.31): corporate-action
       records on a bars request and a bar page on a corporate-actions request;
  (5)  RegisterInstance with a new id supersedes the older instance and marks its QUEUED request NOT_DELIVERED, a
       repeat moves nothing, and the superseded instance's later reports are still written and answered FINAL;
  (6)  a transient FAILED from the live instance is answered CONTINUE and moves a PENDING or NOT_DELIVERED request to
       QUEUED under that instance;
  (7)  last_attempt and PERMANENT finish the request FAILED with their causes; a FAILED report with no stats counts
       one unknown spend and leaves the vendor columns NULL;
  (8)  a late counted report on a finished request is written and counted, keeps the verdict, and is answered FINAL;
  (9)  the two instance-collision branches write nothing and log ERROR with ids only: the live instance is refused,
       the superseded one answered FINAL;
  (10) two concurrent first sightings of one new id: exactly one supersedes, the other takes the later-sighting
       branch, no error (the interleaving is forced: the first holds its commit until the second is blocked on it);
  (11) with MARKET_ACTIVITY_BATCH_SIZE small, one delivery's bars go out in several upsert statements in its one
       transaction; and EXPIRED and EXHAUSTED through the wire: FINAL, no attempt row, the count unchanged.

A first sighting is GLOBAL by design (it supersedes every older instance and marks their QUEUED requests
NOT_DELIVERED): safe on the disposable stack this suite requires, as test_delivery_intake_pg.py and
test_request_record_schema.py already accept; every assertion reads only this run's rows.

Run by make test-system and the agent-stack MCP's run_system_tests. FAIL, NEVER SKIP.
"""

import asyncio
import logging
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import grpc
import pytest
import pytest_asyncio
import sqlalchemy as sa
from grpc_health.v1 import health_pb2, health_pb2_grpc
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason, TraderJoeError
from common.rpc.channel import create_channel
from common.rpc.clients.store_delivery import GrpcStoreDeliveryClient
from common.rpc.server import GRPC_HOST_ENV, GRPC_PORT_ENV
from common.rpc.store import SERVICE_NAME
from data.store.app import grpc_host
from data.store.app.database.crud import ingest_instance as instance_crud
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.app.ingest import delivery_intake
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecord,
    CorporateActionRecords,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange
from tests.system import test_delivery_intake_pg as pg
from tests.system.test_delivery_intake_pg import (
    AS_OF,
    BAR_START,
    FIRST_CALL,
    INSTANCES,
    QUOTA_KEY,
    REQUESTS,
    START,
    VENDOR_ASSET_ID,
    Created,
    bar,
    bar_page,
    report,
)


# A first sighting supersedes the stack's live ingest too; the fixture makes it live again (tj-7zjov1.233).
pytestmark = [pytest.mark.data_store, pytest.mark.asyncio, pytest.mark.usefixtures('live_instances_kept')]

# The cleanup fixture of the sibling file, reused: it deletes exactly the requests, their attempt rows and the
# instances one test wrote.
created = pg.created

FINAL, CONTINUE = DeliveryVerdict.FINAL, DeliveryVerdict.CONTINUE
LOOPBACK = '127.0.0.1'
# Bounds every call: a delivery that waits on a row lock is waited for, never forever.
CALL_DEADLINE_S = 30.0
GUARD_S = 40.0
DETAIL = 'the vendor answered 503'
SERVED = ServedRange(start=BAR_START, end=BAR_START + timedelta(days=10))
BAR_UPSERT = 'insert into stock_market_activity'
APP_LOGGER = 'data.store'


# --------------------------------------------------------------------------------------------- the bind


def _free_port() -> int:
    """A port nothing listens on now, for a bind address that, like production's, names a real port (never 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


@dataclass
class BoundStore:
    """The store's gRPC host, serving, and a client connected to it over TCP."""

    engine: AsyncEngine
    channel: grpc.aio.Channel
    client: GrpcStoreDeliveryClient
    port: int

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        return await asyncio.wait_for(self.client.deliver(attempt), GUARD_S)

    async def register(self, instance_id: str) -> None:
        await asyncio.wait_for(
            self.client.register_instance(RegisterInstanceRequest(instance_id=instance_id, started_at=START)), GUARD_S
        )

    async def refused(self, attempt: AttemptDelivery) -> TraderJoeError:
        """Deliver, expecting the store's typed refusal; return it as the client raised it."""
        with pytest.raises(TraderJoeError) as caught:
            await self.deliver(attempt)
        return caught.value


@asynccontextmanager
async def serving(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, *, batch_size: int | None = None
) -> AsyncIterator[BoundStore]:
    """Build data_store's gRPC host from the environment, as the lifespan does, start it and connect a client."""

    @asynccontextmanager
    async def own_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(delivery_intake, 'own_session', own_session)
    if batch_size is None:
        monkeypatch.delenv('MARKET_ACTIVITY_BATCH_SIZE', raising=False)
    else:
        monkeypatch.setenv('MARKET_ACTIVITY_BATCH_SIZE', str(batch_size))
    port = _free_port()
    monkeypatch.setenv(GRPC_HOST_ENV, LOOPBACK)
    monkeypatch.setenv(GRPC_PORT_ENV, str(port))

    # The production builder, with no intake handed in: it builds DeliveryIntake() itself, which reads the batch size.
    host = grpc_host.build_grpc_host()
    async with host:
        channel = create_channel(f'{LOOPBACK}:{port}')
        try:
            client = GrpcStoreDeliveryClient(channel, deadline_s=CALL_DEADLINE_S, register_deadline_s=CALL_DEADLINE_S)
            yield BoundStore(engine=engine, channel=channel, client=client, port=host.port)
        finally:
            await channel.close()


@pytest_asyncio.fixture
async def store(pg_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[BoundStore]:
    async with serving(pg_async_engine, monkeypatch) as bound:
        yield bound


@pytest.fixture
def instance_id(run_identity, created: Created) -> Callable[[str], str]:
    def _mint(name: str) -> str:
        # Not prefixed with the run's owner (system-test-<run>): the owner is the request's label, which no log line
        # may carry, and case (9) checks exactly that against log lines that name instance ids.
        minted = f'bind-{run_identity.run_id}-{name}-{uuid.uuid4().hex[:6]}'
        created.instances.append(minted)
        return minted

    return _mint


@pytest.fixture
def bars_entry(insert_entry, own_symbol: str) -> sa.Row:
    """The dataset entry a bars request writes under: this test's own symbol, 1day, IEX."""
    return insert_entry(asset_symbol=own_symbol, granularity=Granularity.ONE_DAY, feed=Feed.IEX)


# --------------------------------------------------------------------------------------------- rows


async def _request(
    engine: AsyncEngine,
    created: Created,
    run_identity,
    symbol: str,
    *,
    holder: str | None,
    dataset_id: uuid.UUID | None,
    state: IngestRequestState = IngestRequestState.QUEUED,
    trigger: IngestRunTrigger | None = None,
) -> uuid.UUID:
    """A bars request written through the store's own create_request (the seed cast's way), then moved to `state`.

    There is no submit path yet (P35), so the row is created here, as the request record the submit path will write.
    """
    request_id = uuid.uuid4()
    created.requests.append(request_id)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                id=request_id,
                kind=IngestRequestKind.BARS,
                source=DataSource.ALPACA_API,
                client_label=run_identity.owner,
                dataset_id=dataset_id,
                asset_symbol=symbol,
                asset_type=AssetType.STOCK,
                data_types=(DataType.MARKET_ACTIVITY.value,),
                granularity=Granularity.ONE_DAY,
                update_type=UpdateType.DAILY,
                feed=Feed.IEX,
                requested_start=START,
                requested_end=START + timedelta(days=30),
                trigger=trigger,
            ),
        )
        await db.execute(sa.update(REQUESTS).where(REQUESTS.c.id == request_id).values(state=state, instance_id=holder))
        await db.commit()
    return request_id


async def _instance(engine: AsyncEngine, instance_id: str) -> sa.Row:
    async with engine.connect() as conn:
        return (await conn.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == instance_id))).one()


def failed(
    request_id: uuid.UUID,
    *,
    instance_id: str,
    attempt_no: int = 1,
    error_class: DeliveryErrorClass = DeliveryErrorClass.TRANSIENT,
    last_attempt: bool = False,
    with_stats: bool = True,
) -> AttemptDelivery:
    attempt = report(
        request_id,
        DeliveryOutcome.FAILED,
        instance_id=instance_id,
        attempt_no=attempt_no,
        error_class=error_class,
        last_attempt=last_attempt,
    )
    if with_stats:
        return attempt
    return attempt.model_copy(update={'trailer': attempt.trailer.model_copy(update={'vendor_stats': None})})


def _with(attempt: AttemptDelivery, *, header: dict | None = None, trailer: dict | None = None, **fields):
    """A copy of a report with header, trailer or top-level fields replaced: a crafted report the wire still carries."""
    update = dict(fields)
    if header:
        update['header'] = attempt.header.model_copy(update=header)
    if trailer:
        update['trailer'] = attempt.trailer.model_copy(update=trailer)
    return attempt.model_copy(update=update)


def _app_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith(APP_LOGGER) and r.levelno == level]


def _first_sighting_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The intake's INFO line for a first sighting of an instance id (DeliveryIntake.register_instance)."""
    return [r for r in _app_records(caplog, logging.INFO) if 'Ingest instance registered' in r.getMessage()]


# --------------------------------------------------------------------------------------------- (1)


async def test_1_the_host_built_from_the_environment_reports_store_service_serving(store):
    health = health_pb2_grpc.HealthStub(store.channel)

    for name in (SERVICE_NAME, ''):
        response = await asyncio.wait_for(
            health.Check(health_pb2.HealthCheckRequest(service=name), timeout=CALL_DEADLINE_S), GUARD_S
        )
        assert response.status == health_pb2.HealthCheckResponse.SERVING, name
    assert SERVICE_NAME == 'trader_joe.proto.internal.store.v1.StoreService'


# --------------------------------------------------------------------------------------------- (2) and (3)


async def test_2_a_succeeded_bars_delivery_commits_everything_and_3_a_redelivery_changes_nothing(
    store, created, run_identity, bars_entry, instance_id
):
    holder = instance_id('ok')
    await store.register(holder)
    request_id = await _request(
        store.engine,
        created,
        run_identity,
        bars_entry.asset_symbol,
        holder=holder,
        dataset_id=bars_entry.id,
        trigger=IngestRunTrigger.SCHEDULE,
    )
    attempt = report(request_id, parts=[bar_page(0, 1, 2), bar_page(3)], instance_id=holder)
    assert attempt.trailer.served_range == SERVED

    answer = await store.deliver(attempt)

    # (2) The answer, read back through the wire.
    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    # The bars, under the request's dataset, on the header's tape.
    bars = await pg._bars(store.engine, bars_entry.id)
    assert [row.timestamp for row in bars] == [BAR_START + timedelta(days=d) for d in range(4)]
    assert {(row.feed, row.asset_symbol, row.granularity, row.trade_count) for row in bars} == {
        (Feed.IEX, bars_entry.asset_symbol, Granularity.ONE_DAY, 42)
    }
    # ONE attempt row, carrying what the store's own intake writes.
    (run,) = await pg._runs(store.engine, request_id)
    assert (run.attempt_no, run.instance_id, run.quota_key, run.verdict) == (1, holder, QUOTA_KEY, 'FINAL')
    assert (run.state, run.trigger, run.bars_written, run.dataset_id, run.feed) == (
        IngestRunState.SUCCEEDED,
        IngestRunTrigger.SCHEDULE,
        4,
        bars_entry.id,
        Feed.IEX,
    )
    assert (run.vendor_calls, run.vendor_pages, run.vendor_throttled_429, run.vendor_retries) == (3, 2, 1, 1)
    assert (run.vendor_reported_limit, run.vendor_reported_remaining) == (200, 150)
    assert run.vendor_first_call_at == FIRST_CALL
    assert run.vendor_asset_id == VENDOR_ASSET_ID, 'the header vendor asset id, as sent, on the attempt row'
    assert (run.served_start, run.served_end) == (SERVED.start, SERVED.end), 'the served range, as sent'
    assert (run.started_at, run.finished_at, run.error_message) == (FIRST_CALL, AS_OF, None)
    # The request: count, spend, state.
    row = await pg._request_row(store.engine, request_id)
    assert (row.attempts, row.spent_calls, row.spent_pages, row.spent_throttled_429, row.spent_retries) == (
        1,
        3,
        2,
        1,
        1,
    )
    assert row.spend_unknown_attempts == 0
    assert (row.state, row.failure_cause, row.instance_id) == (IngestRequestState.SUCCEEDED, None, holder)
    assert row.finished_at is not None
    # As Postgres holds them: enum labels, the integer update_type, the timestamptz served range.
    async with store.engine.connect() as conn:
        raw = (
            await conn.execute(
                sa.text(
                    'SELECT r.state::text AS state, run.trigger::text AS trigger, run.state::text AS run_state, '
                    'run.feed::text AS feed, run.update_type, run.vendor_asset_id, '
                    'run.served_start = :start AND run.served_end = :end AS served_matches '
                    'FROM ingest_request r JOIN ingest_run run ON run.request_id = r.id WHERE r.id = :id'
                ),
                {'id': request_id, 'start': SERVED.start, 'end': SERVED.end},
            )
        ).one()
    assert (raw.state, raw.trigger, raw.run_state, raw.feed) == ('SUCCEEDED', 'SCHEDULE', 'SUCCEEDED', 'IEX')
    assert (raw.update_type, raw.vendor_asset_id, raw.served_matches) == (UpdateType.DAILY.value, VENDOR_ASSET_ID, True)

    # (3) The same report again, through the wire: the same answer, and not one row changed.
    before = await pg._snapshot(store.engine, request_id, bars_entry.id)

    again = await store.deliver(attempt)

    assert again == answer
    assert await pg._snapshot(store.engine, request_id, bars_entry.id) == before, (
        'a redelivery changed the request, the attempt rows or a bar (updated_at included)'
    )


# --------------------------------------------------------------------------------------------- (4)


def _refusals() -> list:
    """Crafted reports, each breaking one cross-field rule, keyed by the rule. request_id and instance filled in later."""
    return [
        ('a-expired-names-a-quota-key', DeliveryOutcome.EXPIRED, {'header': {'quota_key': QUOTA_KEY}}),
        (
            'b-error-class-on-succeeded',
            DeliveryOutcome.SUCCEEDED,
            {'trailer': {'error_class': DeliveryErrorClass.TRANSIENT}},
        ),
        ('c-last-attempt-on-succeeded', DeliveryOutcome.SUCCEEDED, {'trailer': {'last_attempt': True}}),
        ('d-served-range-on-failed', DeliveryOutcome.FAILED, {'trailer': {'served_range': SERVED}}),
        ('e-bars-report-with-no-feed', DeliveryOutcome.SUCCEEDED, {'header': {'feed': None}}),
        ('g-counted-report-with-no-quota-key', DeliveryOutcome.SUCCEEDED, {'header': {'quota_key': ''}}),
        ('h-succeeded-with-no-vendor-stats', DeliveryOutcome.SUCCEEDED, {'trailer': {'vendor_stats': None}}),
        ('bar-on-another-tape', DeliveryOutcome.SUCCEEDED, {'parts': [BarPage(bars=[bar(0), bar(1, feed=Feed.SIP)])]}),
    ]


@pytest.mark.parametrize(('outcome', 'change'), [r[1:] for r in _refusals()], ids=[r[0] for r in _refusals()])
async def test_4_a_report_a_rule_refuses_reaches_the_client_as_invalid_request_with_nothing_written(
    store, created, run_identity, bars_entry, instance_id, outcome, change
):
    holder = instance_id('refused')
    await store.register(holder)
    request_id = await _request(
        store.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )
    attempt = _with(report(request_id, outcome, instance_id=holder), **change)
    before = await pg._snapshot(store.engine, request_id, bars_entry.id)

    error = await store.refused(attempt)

    assert isinstance(error, InvalidRequestError), type(error)
    assert error.reason is Reason.INVALID_REQUEST, error.reason
    assert await pg._snapshot(store.engine, request_id, bars_entry.id) == before, 'a refused report wrote something'


async def test_4_a_request_the_store_does_not_hold_reaches_the_client_as_not_found(store, instance_id):
    holder = instance_id('unknown')
    await store.register(holder)

    error = await store.refused(report(uuid.uuid4(), instance_id=holder))

    assert error.reason is Reason.NOT_FOUND, error.reason


async def _observation_count(engine: AsyncEngine, request_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count()).select_from(pg.ACTIONS).where(pg.ACTIONS.c.request_id == request_id)
            )
        ).scalar_one()


async def test_4f_a_corporate_actions_payload_on_a_bars_request_reaches_the_client_as_invalid_request(
    store, created, run_identity, bars_entry, instance_id
):
    """Rule f through the wire (tj-7zjov1.31, the architect's 16:16 UTC note from the P34s gate), bars side."""
    holder = instance_id('rule-f-bars')
    await store.register(holder)
    request_id = await _request(
        store.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )
    records = CorporateActionRecords(
        records=[
            CorporateActionRecord(vendor_action_id=f'ca-{uuid.uuid4().hex}', action_type='split', raw_payload=b'{}')
        ]
    )
    attempt = _with(report(request_id, instance_id=holder), parts=[records])
    before = await pg._snapshot(store.engine, request_id, bars_entry.id)

    error = await store.refused(attempt)

    assert isinstance(error, InvalidRequestError) and error.reason is Reason.INVALID_REQUEST, error
    assert await pg._snapshot(store.engine, request_id, bars_entry.id) == before, 'a refused report wrote something'
    assert await _observation_count(store.engine, request_id) == 0


async def test_4f_a_bar_page_on_a_corporate_actions_request_reaches_the_client_as_invalid_request(
    store, created, run_identity, instance_id
):
    """Rule f through the wire, corporate-actions side: a well-formed corporate-actions report carrying a BarPage."""
    holder = instance_id('rule-f-actions')
    await store.register(holder)
    request_id = await pg._ca_request(store.engine, created, run_identity, holder=holder)
    attempt = _with(pg.ca_report(request_id, [], instance_id=holder), parts=[bar_page(0, 1)])
    before = await pg._snapshot(store.engine, request_id, None)

    error = await store.refused(attempt)

    assert isinstance(error, InvalidRequestError) and error.reason is Reason.INVALID_REQUEST, error
    assert await pg._snapshot(store.engine, request_id, None) == before, 'a refused report wrote something'
    assert await _observation_count(store.engine, request_id) == 0


# --------------------------------------------------------------------------------------------- (5)


async def test_5_register_instance_supersedes_the_older_instance_and_its_later_reports_are_still_written(
    store, created, run_identity, bars_entry, own_symbol, instance_id
):
    old, new = instance_id('old'), instance_id('new')
    await store.register(old)
    queued = await _request(
        store.engine, created, run_identity, bars_entry.asset_symbol, holder=old, dataset_id=bars_entry.id
    )
    pending = await _request(
        store.engine, created, run_identity, own_symbol, holder=None, dataset_id=None, state=IngestRequestState.PENDING
    )

    await store.register(new)

    old_row, new_row = await _instance(store.engine, old), await _instance(store.engine, new)
    assert old_row.superseded_at is not None and new_row.superseded_at is None
    assert (await pg._request_row(store.engine, queued)).state is IngestRequestState.NOT_DELIVERED
    assert (await pg._request_row(store.engine, pending)).state is IngestRequestState.PENDING, (
        'only a QUEUED request held by an older instance is marked NOT_DELIVERED'
    )

    # A repeat of the same id is a later sighting: it supersedes nothing more and moves only last_seen_at.
    await store.register(new)

    assert (await _instance(store.engine, old)).superseded_at == old_row.superseded_at
    again = await _instance(store.engine, new)
    assert again.superseded_at is None and again.last_seen_at >= new_row.last_seen_at

    # The superseded instance's transient failure happened and cost what it cost: written, counted, FINAL, and the
    # request left for the live instance (last_attempt on it is ignored).
    answer = await store.deliver(failed(queued, instance_id=old, last_attempt=True))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    row = await pg._request_row(store.engine, queued)
    assert (row.state, row.instance_id, row.finished_at, row.attempts, row.spent_calls) == (
        IngestRequestState.NOT_DELIVERED,
        old,
        None,
        1,
        3,
    )
    (run,) = await pg._runs(store.engine, queued)
    assert (run.instance_id, run.verdict, run.state) == (old, 'FINAL', IngestRunState.FAILED)

    # Its success is conclusive by itself: it finishes the request.
    succeeded = await store.deliver(report(queued, attempt_no=2, instance_id=old))

    assert succeeded == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    row = await pg._request_row(store.engine, queued)
    assert row.state is IngestRequestState.SUCCEEDED and row.finished_at is not None
    assert len(await pg._bars(store.engine, bars_entry.id)) == 2
    assert [r.trigger for r in await pg._runs(store.engine, queued)] == [
        IngestRunTrigger.REQUEST,
        IngestRunTrigger.RETRY,
    ]


# --------------------------------------------------------------------------------------------- (6)


@pytest.mark.parametrize('start_state', [IngestRequestState.PENDING, IngestRequestState.NOT_DELIVERED])
async def test_6_a_transient_failure_from_the_live_instance_continues_and_queues_the_request_under_it(
    store, created, run_identity, own_symbol, instance_id, start_state
):
    old, live = instance_id('was'), instance_id('live')
    await store.register(old)
    if start_state is IngestRequestState.PENDING:
        request_id = await _request(
            store.engine, created, run_identity, own_symbol, holder=None, dataset_id=None, state=start_state
        )
        await store.register(live)
    else:
        request_id = await _request(store.engine, created, run_identity, own_symbol, holder=old, dataset_id=None)
        await store.register(live)
    assert (await pg._request_row(store.engine, request_id)).state is start_state

    answer = await store.deliver(failed(request_id, instance_id=live))

    assert answer == DeliverAttemptResponse(verdict=CONTINUE, attempt_count=1)
    row = await pg._request_row(store.engine, request_id)
    assert (row.state, row.instance_id, row.finished_at, row.failure_cause) == (
        IngestRequestState.QUEUED,
        live,
        None,
        None,
    )
    assert (row.attempts, row.spent_calls, row.spend_unknown_attempts) == (1, 3, 0)
    (run,) = await pg._runs(store.engine, request_id)
    assert (run.verdict, run.state, run.error_message, run.instance_id) == (
        'CONTINUE',
        IngestRunState.FAILED,
        DETAIL,
        live,
    )
    assert (run.served_start, run.served_end, run.vendor_asset_id) == (None, None, VENDOR_ASSET_ID)


# --------------------------------------------------------------------------------------------- (7)


@pytest.mark.parametrize(
    ('error_class', 'last_attempt', 'with_stats', 'cause'),
    [
        (DeliveryErrorClass.TRANSIENT, True, True, 'ATTEMPTS_EXHAUSTED'),
        (DeliveryErrorClass.PERMANENT, False, True, 'PERMANENT'),
        # The attempt runner's crash path: a permanent failure with no stats. Accepted; its spend is unknown.
        (DeliveryErrorClass.PERMANENT, False, False, 'PERMANENT'),
    ],
    ids=['last-attempt', 'permanent', 'permanent-without-stats'],
)
async def test_7_last_attempt_and_permanent_finish_the_request_failed_with_their_causes(
    store, created, run_identity, own_symbol, instance_id, error_class, last_attempt, with_stats, cause
):
    holder = instance_id('final')
    await store.register(holder)
    request_id = await _request(store.engine, created, run_identity, own_symbol, holder=holder, dataset_id=None)

    answer = await store.deliver(
        failed(
            request_id, instance_id=holder, error_class=error_class, last_attempt=last_attempt, with_stats=with_stats
        )
    )

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    row = await pg._request_row(store.engine, request_id)
    assert (row.state, row.failure_cause, row.failure_detail) == (IngestRequestState.FAILED, cause, DETAIL)
    assert row.finished_at is not None and row.attempts == 1
    (run,) = await pg._runs(store.engine, request_id)
    assert (run.verdict, run.state) == ('FINAL', IngestRunState.FAILED)
    if with_stats:
        assert (row.spent_calls, row.spend_unknown_attempts, run.vendor_calls) == (3, 0, 3)
    else:
        assert (row.spent_calls, row.spent_pages, row.spend_unknown_attempts) == (0, 0, 1)
        assert (run.vendor_calls, run.vendor_pages, run.vendor_first_call_at) == (None, None, None), (
            'unknown spend is NULL on the attempt row, never 0'
        )


# --------------------------------------------------------------------------------------------- (8)


async def test_8_a_late_counted_report_on_a_finished_request_is_written_and_counted_and_the_verdict_kept(
    store, created, run_identity, bars_entry, instance_id
):
    holder = instance_id('late')
    await store.register(holder)
    request_id = await _request(
        store.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
    )
    assert await store.deliver(report(request_id, instance_id=holder)) == DeliverAttemptResponse(
        verdict=FINAL, attempt_count=1
    )
    finished = await pg._request_row(store.engine, request_id)

    # A transient failure would be answered CONTINUE on an open request; on a finished one it is FINAL.
    answer = await store.deliver(failed(request_id, instance_id=holder, attempt_no=2))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    row = await pg._request_row(store.engine, request_id)
    assert (row.state, row.finished_at, row.failure_cause) == (IngestRequestState.SUCCEEDED, finished.finished_at, None)
    assert (row.attempts, row.spent_calls, row.spent_pages) == (2, 6, 4), (
        'the late report is counted and its spend added'
    )
    first, late = await pg._runs(store.engine, request_id)
    assert (first.verdict, first.state) == ('FINAL', IngestRunState.SUCCEEDED)
    assert (late.attempt_no, late.verdict, late.state, late.trigger) == (
        2,
        'FINAL',
        IngestRunState.FAILED,
        IngestRunTrigger.RETRY,
    )


# --------------------------------------------------------------------------------------------- (9)


async def test_9_the_instance_collision_branches_write_nothing_and_log_error_with_ids_only(
    store, created, run_identity, bars_entry, instance_id, caplog
):
    caplog.set_level(logging.INFO, logger=APP_LOGGER)
    old, new = instance_id('a'), instance_id('b')
    await store.register(old)
    request_id = await _request(
        store.engine, created, run_identity, bars_entry.asset_symbol, holder=old, dataset_id=bars_entry.id
    )
    assert await store.deliver(failed(request_id, instance_id=old)) == DeliverAttemptResponse(
        verdict=CONTINUE, attempt_count=1
    )
    await store.register(new)

    # THE LIVE BRANCH: the live instance reports a number the old one already recorded. Refused, loudly.
    before = await pg._snapshot(store.engine, request_id, bars_entry.id)
    caplog.clear()

    error = await store.refused(failed(request_id, instance_id=new))

    assert isinstance(error, InvalidRequestError) and error.reason is Reason.INVALID_REQUEST, error
    assert await pg._snapshot(store.engine, request_id, bars_entry.id) == before
    (logged,) = _app_records(caplog, logging.ERROR)
    message = logged.getMessage()
    assert all(part in message for part in (str(request_id), 'Attempt 1 ', old, new)), message

    # The live instance carries on with the next number.
    assert await store.deliver(failed(request_id, instance_id=new, attempt_no=2)) == DeliverAttemptResponse(
        verdict=CONTINUE, attempt_count=2
    )

    # THE SUPERSEDED BRANCH: the old instance reports the number the live one recorded. FINAL, nothing written.
    before = await pg._snapshot(store.engine, request_id, bars_entry.id)
    caplog.clear()

    answer = await store.deliver(report(request_id, attempt_no=2, instance_id=old))

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=2)
    assert await pg._snapshot(store.engine, request_id, bars_entry.id) == before
    assert await pg._bars(store.engine, bars_entry.id) == [], 'the dropped success wrote no bar'
    (logged,) = _app_records(caplog, logging.ERROR)
    message = logged.getMessage()
    assert all(part in message for part in (str(request_id), 'Attempt 2 ', old, new)), message

    # Ids only: never the label, the quota key or the vendor asset id, on any record of the intake.
    for record in caplog.records:
        if record.name.startswith(APP_LOGGER):
            text = record.getMessage()
            assert QUOTA_KEY not in text and VENDOR_ASSET_ID not in text and run_identity.owner not in text, text


# --------------------------------------------------------------------------------------------- (10)


async def test_10_two_concurrent_first_sightings_of_one_new_id_supersede_exactly_once(
    store, created, run_identity, own_symbol, instance_id, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=APP_LOGGER)
    old, new = instance_id('older'), instance_id('twice')
    await store.register(old)
    queued = await _request(store.engine, created, run_identity, own_symbol, holder=old, dataset_id=None)

    # FORCE THE INTERLEAVING. The first sighting to finish its statements holds its commit until another session is
    # blocked behind its backend: the second sighting's INSERT ... ON CONFLICT DO NOTHING on the same primary key
    # waits for the first transaction's outcome. Without that wait the two calls could simply run one after the other.
    real_transaction = instance_crud.write_transaction
    held: list[int] = []

    @asynccontextmanager
    async def commit_after_the_other_blocks(db, description):
        async with real_transaction(db, description):
            yield
            if not held:
                pid = (await db.execute(sa.text('SELECT pg_backend_pid()'))).scalar_one()
                held.append(pid)
                await pg._wait_until_blocked_by(store.engine, pid)

    real_seen = delivery_intake.note_instance_seen
    sightings: list[instance_crud.InstanceSighting] = []

    async def recording_seen(db, seen_id):
        sighting = await real_seen(db, seen_id)
        sightings.append(sighting)
        return sighting

    monkeypatch.setattr(instance_crud, 'write_transaction', commit_after_the_other_blocks)
    monkeypatch.setattr(delivery_intake, 'note_instance_seen', recording_seen)
    # Only the race's own log lines are counted below: the setup's registration of the old instance is a first
    # sighting too, and logs the same line.
    caplog.clear()

    results = await asyncio.wait_for(
        asyncio.gather(store.register(new), store.register(new), return_exceptions=True), GUARD_S
    )

    assert results == [None, None], f'a concurrent sighting raised: {results}'
    assert len(held) == 1, 'the first sighting never held its commit'
    assert sorted(s.is_new for s in sightings) == [False, True], sightings
    (first,) = [s for s in sightings if s.is_new]
    assert old in first.superseded and queued in first.not_delivered
    async with store.engine.connect() as conn:
        rows = (await conn.execute(sa.select(INSTANCES).where(INSTANCES.c.instance_id == new))).all()
    assert len(rows) == 1 and rows[0].superseded_at is None
    assert (await _instance(store.engine, old)).superseded_at is not None
    assert (await pg._request_row(store.engine, queued)).state is IngestRequestState.NOT_DELIVERED
    assert len(_first_sighting_lines(caplog)) == 1, 'exactly one racing sighting took the first-sighting branch'
    assert _app_records(caplog, logging.ERROR) == []


# --------------------------------------------------------------------------------------------- (11)


async def test_11_a_small_batch_size_splits_one_deliverys_bars_into_several_statements_in_one_transaction(
    pg_async_engine, monkeypatch, created, run_identity, bars_entry, instance_id
):
    statements: list[str] = []

    def count_bar_upserts(_conn, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith(BAR_UPSERT):
            statements.append(statement)

    sa.event.listen(pg_async_engine.sync_engine, 'before_cursor_execute', count_bar_upserts)
    try:
        async with serving(pg_async_engine, monkeypatch, batch_size=2) as bound:
            holder = instance_id('chunks')
            await bound.register(holder)
            request_id = await _request(
                bound.engine, created, run_identity, bars_entry.asset_symbol, holder=holder, dataset_id=bars_entry.id
            )

            answer = await bound.deliver(
                report(request_id, parts=[bar_page(0, 1, 2, 3, 4), bar_page(5)], instance_id=holder)
            )
    finally:
        sa.event.remove(pg_async_engine.sync_engine, 'before_cursor_execute', count_bar_upserts)

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=1)
    # 5 bars at 2 per statement is 3 statements, and the second page's one bar a fourth.
    assert len(statements) == 4, f'{len(statements)} bar upserts'
    bars = await pg._bars(pg_async_engine, bars_entry.id)
    assert [row.timestamp for row in bars] == [BAR_START + timedelta(days=d) for d in range(6)]
    (run,) = await pg._runs(pg_async_engine, request_id)
    assert run.bars_written == 6


@pytest.mark.parametrize(
    ('outcome', 'cause'), [(DeliveryOutcome.EXPIRED, 'EXPIRED'), (DeliveryOutcome.EXHAUSTED, 'ATTEMPTS_EXHAUSTED')]
)
async def test_11_expired_and_exhausted_through_the_wire_are_final_with_no_attempt_row(
    store, created, run_identity, own_symbol, instance_id, outcome, cause
):
    holder = instance_id('nocall')
    await store.register(holder)
    request_id = await _request(store.engine, created, run_identity, own_symbol, holder=holder, dataset_id=None)
    attempt = report(request_id, outcome, attempt_no=1, instance_id=holder)

    answer = await store.deliver(attempt)

    assert answer == DeliverAttemptResponse(verdict=FINAL, attempt_count=0)
    assert await pg._runs(store.engine, request_id) == []
    row = await pg._request_row(store.engine, request_id)
    assert (row.state, row.failure_cause, row.attempts, row.spent_calls) == (IngestRequestState.FAILED, cause, 0, 0)
    assert row.finished_at is not None

    before = await pg._snapshot(store.engine, request_id, None)
    assert await store.deliver(attempt) == answer
    assert await pg._snapshot(store.engine, request_id, None) == before
