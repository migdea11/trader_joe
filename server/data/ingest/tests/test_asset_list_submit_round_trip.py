"""An asset-list Submit end to end into the REAL delivery intake over SQLite: the store writes, ingest serves (P12s).

Bead tj-7zjov1.125. THE DESIGN: the bead's acceptance (an asset_list Submit is accepted and queued) with the architect's
additions on it: the 21:56 UTC served-range seam ruling (an asset list has no served range; rule (i) of validate_report
excludes it by name; the runner sends none), the 23:33 UTC note (send the request through the store's own
submit_request_of for BOTH lists and assert the snapshot lands under the list asked for, an EMPTY inactive list
included), and the 00:46 UTC note (a recovered asset_list Submit is accepted exactly as a recovered bars one); ADR
tj-26jt9w addenda 17 and 18 (the request row names its list; the arm files every delivery under it).

WHAT IS REAL. The store's half: the request row is written by the store's own request writer, new_asset_list_request,
through create_request and mark_queued, and turned into the Submit by submit_request_of, exactly as the store sends it;
every report lands in the production DeliveryIntake, its validate_report and its asset-list arm, on SQLite through the
harness of data/store/tests/test_delivery_intake.py (its engine and Store, imported). Ingest's half: the production
IngestSubmitHandler routes the Submit to FakeIngest's handle (tests/fakes/ingest.py), which is the production
BrokerHandle, Scheduler, AckGatedExecutor and AssetListAttemptRunner around a paced FakeRead serving a one-day world.
Nothing between the two is a double but the in-process client that hands each report to the intake.

WHAT SQLITE CANNOT SHOW (the enum types, the CHECK tying the list to the kind, the advisory lock, Postgres's own
statements) is tests/system/test_asset_list_submit_pg.py's.

THE CASES:
  (1) per list: accepted, read once as THAT list, delivered and written FINAL: the attempt row SUCCEEDED with no served
      range on the trading quota key, the snapshot under the list asked for with every row, the request SUCCEEDED;
  (2) an EMPTY inactive list: a success with no parts, recorded as an inactive zero-row snapshot, the request EMPTY;
  (3) THE CONTROL: the report the runner delivered passes validate_report as an asset list, and the same report judged
      as a corporate-actions report is refused by rule (i), so (1) and (2) guard that seam rather than pass around it;
      a replay of it writes nothing and repeats its answer;
  (4) a FAILED attempt (the last one), an EXPIRED request and a RECOVERED request already past the budget (EXHAUSTED)
      are each written as the store expects: no snapshot, the request FAILED with its own cause, never refused;
  (5) a recovered asset_list Submit (count 1) is accepted, read and written as attempt 2.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason, TraderJoeError
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.interface import AssetListFailure, AssetListQuery
from data.ingest.app.scheduler import ExecutorSettings, RetryPolicy
from data.ingest.tests.alpaca_recorded import raw_answer, stub_trading_vendor
from data.ingest.tests.scheduler_doubles import RecordingScheduler
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.asset_list import AssetListRow, AssetListSnapshot
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.app.ingest.data_action_request import new_asset_list_request, submit_request_of
from data.store.app.ingest.delivery_intake import validate_report
from data.store.tests.test_delivery_intake import Store, aware_sqlite_datetimes, engine  # noqa: F401 (fixtures)
from routers.data_ingest.submit_handler import IngestSubmitHandler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryOutcome,
    DeliveryVerdict,
    RegisterInstanceRequest,
    RegisterInstanceResponse,
)
from schemas.data_ingest.submit import AssetListStatus, SubmitResponse
from tests.fakes.ingest import FakeIngest
from tests.fakes.market_data import AssetDay, FakeWorld, WorldAsset


pytestmark = pytest.mark.data_ingest

ACTIVE, INACTIVE = AssetListStatus.ACTIVE, AssetListStatus.INACTIVE
SNAPSHOTS, ROWS = AssetListSnapshot.__table__, AssetListRow.__table__
TRADING_SCOPE = f'{DataSource.ALPACA_API.value}_TRADING'
OWNER = 'world-manager'
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 10.0
MAX_ATTEMPTS = 3

_LISTED = (WorldAsset('apple', 'AAPL', 'Apple'), WorldAsset('amd', 'AMD', 'AMD'), WorldAsset('obk', 'OBK', ''))
_DELISTED = (WorldAsset('xilinx', 'XLNX', 'Xilinx'), WorldAsset('wccp', 'WCCP', '', 'OTC'))
# One day, so the fake's clock always serves it (the world clamps to its days).
BOTH_LISTS = FakeWorld((AssetDay(date(2026, 10, 26), active=_LISTED, inactive=_DELISTED),), {}, ())
NO_INACTIVE = FakeWorld((AssetDay(date(2026, 10, 26), active=_LISTED, inactive=()),), {}, ())


def _settings(max_attempts: int = MAX_ATTEMPTS, retry_delay_s: float = 60.0) -> ExecutorSettings:
    return ExecutorSettings(
        buffer_size=8,
        workers=1,
        stall_timeout_s=300.0,
        stall_check_interval_s=3600.0,
        redelivery_base_s=5.0,
        redelivery_max_s=5.0,
        retry=RetryPolicy(max_attempts=max_attempts, base_delay_s=retry_delay_s, max_delay_s=60.0, factor=2.0),
    )


class IntakeClient:
    """The real DeliveryIntake as ingest's StoreDeliveryClient: every report, answer and refusal recorded and signalled.

    A refusal is recorded and re-raised, so a test reports it rather than the executor redelivering it silently.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self.delivered: list[AttemptDelivery] = []
        self.answers: list[DeliverAttemptResponse] = []
        self.refusals: list[TraderJoeError] = []
        self.signal = asyncio.Event()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.delivered.append(attempt)
        try:
            answer = await self.store.intake.deliver(attempt)
        except TraderJoeError as refused:
            self.refusals.append(refused)
            self.signal.set()
            raise
        self.answers.append(answer)
        self.signal.set()
        return answer

    async def register_instance(self, request: RegisterInstanceRequest) -> RegisterInstanceResponse:
        return await self.store.intake.register_instance(request)


@pytest.fixture
def store(engine, monkeypatch: pytest.MonkeyPatch) -> Store:  # noqa: F811 (the imported fixture, requested)
    return Store(engine, monkeypatch)


class RoundTrip:
    """One store, one fake ingest: the request written by the store, sent as the store sends it, reported back."""

    def __init__(
        self, store: Store, world: FakeWorld, max_attempts: int = MAX_ATTEMPTS, retry_delay_s: float = 60.0
    ) -> None:
        self.store = store
        self.client = IntakeClient(store)
        self.ingest = FakeIngest(self.client, world=world, settings=_settings(max_attempts, retry_delay_s))
        self.handler = IngestSubmitHandler(
            {DataSource.ALPACA_API: self.ingest.handle}, self.ingest.identity.instance_id
        )

    async def run(
        self,
        status: AssetListStatus,
        *,
        attempts: int = 0,
        recovered: bool = False,
        ready_by: datetime | None = None,
        answers: int = 1,
    ) -> tuple[uuid.UUID, SubmitResponse]:
        """Write the request as the store does, mark it held by this ingest, submit it, wait for the store's answers.

        It waits for `answers` acknowledgements (or the first refusal) before the ingest stops.
        """
        if ready_by is None:
            ready_by = datetime.now(UTC) + timedelta(minutes=5)
        async with self.ingest:
            await self.ingest.announce()
            async with AsyncSession(self.store.engine, expire_on_commit=False) as db:
                written = await request_crud.create_request(
                    db,
                    new_asset_list_request(
                        DataSource.ALPACA_API,
                        AssetType.STOCK,
                        status,
                        trigger=IngestRunTrigger.SCHEDULE,
                        client_label=OWNER,
                        ready_by=ready_by,
                    ),
                )
            if attempts:
                await self.store.set_request(written.id, attempts=attempts)
            async with AsyncSession(self.store.engine, expire_on_commit=False) as db:
                assert await request_crud.mark_queued(db, written.id, self.ingest.identity.instance_id) is not None
                record = await request_crud.get_request(db, written.id)
            response = await self.handler.submit(submit_request_of(record, recovered=recovered))
            await asyncio.wait_for(self.client.signal.wait(), GUARD_S)

            async def answered() -> None:
                # No await between the check and the clear, so a delivery cannot set the signal unseen.
                while len(self.client.answers) < answers and not self.client.refusals:
                    self.client.signal.clear()
                    await self.client.signal.wait()

            await asyncio.wait_for(answered(), GUARD_S)
        assert self.client.refusals == [], f'the store refused the delivered report: {self.client.refusals}'
        return written.id, response


async def _rows(store: Store, statement) -> list[sa.Row]:
    async with AsyncSession(store.engine) as db:
        return list((await db.execute(statement)).all())


async def _snapshots(store: Store, request_id: uuid.UUID) -> list[sa.Row]:
    return await _rows(store, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == request_id))


async def _symbols(store: Store, snapshot_id: uuid.UUID) -> list[str]:
    statement = sa.select(ROWS.c.symbol).where(ROWS.c.snapshot_id == snapshot_id).order_by(sa.literal_column('rowid'))
    return [row.symbol for row in await _rows(store, statement)]


# --------------------------------------------------------------------------------------------- (1) and (2)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_an_asset_list_submit_the_store_wrote_is_read_as_that_list_and_filed_under_it(store, status):
    trip = RoundTrip(store, BOTH_LISTS)

    request_id, response = await trip.run(status)

    assert response.ack.refused is None
    assert (response.ack.accepted.feed, response.ack.accepted.quota_key) == (None, TRADING_SCOPE)
    assert trip.ingest.reader.calls == (('assets', status.value),), 'not one read of the list the store asked for'
    assert trip.client.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)]
    (run,) = await store.runs(request_id)
    assert run.state is IngestRunState.SUCCEEDED
    assert (run.served_start, run.served_end, run.feed, run.vendor_asset_id) == (None,) * 4
    assert (run.quota_key, run.attempt_no) == (TRADING_SCOPE, 1)
    (snapshot,) = await _snapshots(store, request_id)
    expected = [asset.symbol for asset in (_LISTED if status is ACTIVE else _DELISTED)]
    assert (snapshot.asset_list, snapshot.row_count, snapshot.attempt_no) == (status, len(expected), 1)
    assert await _symbols(store, snapshot.id) == expected
    request = await store.request_row(request_id)
    assert (request.state, request.asset_list_status, request.attempts) == (IngestRequestState.SUCCEEDED, status, 1)


@pytest.mark.asyncio
async def test_an_empty_inactive_list_is_a_success_with_no_parts_filed_as_an_inactive_zero_row_snapshot(store):
    trip = RoundTrip(store, NO_INACTIVE)

    request_id, _ = await trip.run(INACTIVE)

    [report] = trip.client.delivered
    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED and report.parts == []
    assert trip.ingest.reader.calls == (('assets', INACTIVE.value),)
    assert trip.client.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)]
    (snapshot,) = await _snapshots(store, request_id)
    assert (snapshot.asset_list, snapshot.row_count) == (INACTIVE, 0)
    assert await _symbols(store, snapshot.id) == []
    (run,) = await store.runs(request_id)
    assert run.state is IngestRunState.EMPTY
    assert (await store.request_row(request_id)).state is IngestRequestState.EMPTY


# --------------------------------------------------------------------------------------------- (3) the control


@pytest.mark.asyncio
async def test_the_control_the_delivered_report_passes_as_an_asset_list_and_fails_rule_i_as_another_kind(store):
    trip = RoundTrip(store, BOTH_LISTS)
    request_id, _ = await trip.run(ACTIVE)
    [report] = trip.client.delivered

    # As built: vendor stats and a quota key on the success, as_of set, no served range, no feed, no vendor asset id.
    assert report.trailer.vendor_stats is not None and report.trailer.vendor_stats.calls == 1
    assert report.header.quota_key == TRADING_SCOPE
    assert report.trailer.as_of is not None
    assert report.trailer.served_range is None
    assert validate_report(report, IngestRequestKind.ASSET_LIST) is None
    with pytest.raises(InvalidRequestError, match='served range'):
        validate_report(report, IngestRequestKind.CORPORATE_ACTIONS)

    before = await store.snapshot()
    again = await store.deliver(report)

    assert again == DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)
    assert await store.snapshot() == before, 'a replayed delivery wrote something'
    assert len(await _snapshots(store, request_id)) == 1


# --------------------------------------------------------------------------------------------- (4) and (5)


@pytest.mark.asyncio
async def test_a_failed_last_asset_list_attempt_is_written_as_a_failed_attempt_row_with_no_snapshot(store, monkeypatch):
    trip = RoundTrip(store, BOTH_LISTS, max_attempts=1)
    queries: list[AssetListQuery] = []

    async def unavailable(query: AssetListQuery) -> AssetListFailure:
        queries.append(query)
        return AssetListFailure(ExogenousError(Reason.VENDOR_UNAVAILABLE, 'the vendor did not answer'))

    monkeypatch.setattr(trip.ingest.reader, 'get_assets', unavailable)

    request_id, response = await trip.run(INACTIVE)

    assert response.ack.accepted is not None
    assert [query.status for query in queries] == [INACTIVE]
    [report] = trip.client.delivered
    assert report.trailer.outcome is DeliveryOutcome.FAILED and report.trailer.last_attempt is True
    assert trip.client.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)]
    (run,) = await store.runs(request_id)
    assert run.state is IngestRunState.FAILED
    assert await _snapshots(store, request_id) == []
    request = await store.request_row(request_id)
    assert (request.state, request.failure_cause) == (
        IngestRequestState.FAILED,
        IngestRequestFailureCause.ATTEMPTS_EXHAUSTED.value,
    )


@pytest.mark.asyncio
async def test_an_asset_list_expiring_in_the_queue_is_written_failed_expired_with_no_attempt_row(store):
    trip = RoundTrip(store, BOTH_LISTS)

    request_id, response = await trip.run(ACTIVE, ready_by=datetime.now(UTC) - timedelta(seconds=1))

    assert response.ack.accepted is not None
    assert trip.ingest.reader.calls == (), 'an expired list reached the vendor'
    [report] = trip.client.delivered
    assert report.trailer.outcome is DeliveryOutcome.EXPIRED
    assert trip.client.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=0)]
    assert await store.runs(request_id) == []
    assert await _snapshots(store, request_id) == []
    request = await store.request_row(request_id)
    assert (request.state, request.failure_cause) == (
        IngestRequestState.FAILED,
        IngestRequestFailureCause.EXPIRED.value,
    )


@pytest.mark.asyncio
async def test_a_recovered_asset_list_already_past_the_budget_is_written_failed_exhausted_never_read(store):
    trip = RoundTrip(store, BOTH_LISTS)

    request_id, response = await trip.run(ACTIVE, attempts=MAX_ATTEMPTS, recovered=True)

    assert response.ack.accepted.quota_key == TRADING_SCOPE
    assert trip.ingest.reader.calls == ()
    [report] = trip.client.delivered
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.header.attempt_no == MAX_ATTEMPTS + 1
    assert trip.client.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=MAX_ATTEMPTS)]
    assert await store.runs(request_id) == []
    assert await _snapshots(store, request_id) == []
    request = await store.request_row(request_id)
    assert (request.state, request.failure_cause, request.attempts) == (
        IngestRequestState.FAILED,
        IngestRequestFailureCause.ATTEMPTS_EXHAUSTED.value,
        MAX_ATTEMPTS,
    )


@pytest.mark.asyncio
async def test_a_recovered_asset_list_submit_is_accepted_read_and_written_as_the_next_attempt(store):
    trip = RoundTrip(store, BOTH_LISTS)

    request_id, response = await trip.run(INACTIVE, attempts=1, recovered=True)

    assert response.ack.accepted.quota_key == TRADING_SCOPE
    assert trip.ingest.reader.calls == (('assets', INACTIVE.value),)
    assert trip.client.answers == [DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=2)]
    (run,) = await store.runs(request_id)
    assert (run.state, run.attempt_no) == (IngestRunState.SUCCEEDED, 2)
    (snapshot,) = await _snapshots(store, request_id)
    assert (snapshot.asset_list, snapshot.attempt_no) == (INACTIVE, 2)


# --------------------------------------------------------------------------------------------- (6) the guard


@pytest.mark.asyncio
async def test_a_list_the_guard_refuses_is_retried_to_the_budget_and_filed_failed_with_its_detail_and_no_snapshot(
    store, monkeypatch
):
    """tj-7zjov1.218 D1 and D2, through the REAL AlpacaRead and the store's real intake.

    The vendor answers every attempt with a list holding one vendor id twice. Each attempt is FAILED TRANSIENT, the
    store answers CONTINUE until ingest spends its budget, every run row carries the guard's fixed detail as its error,
    no snapshot is written, and the request ends FAILED with ATTEMPTS_EXHAUSTED. Retries wait 0 s, so the budget runs
    inside the guard.
    """
    trip = RoundTrip(store, BOTH_LISTS, retry_delay_s=0.0)
    record = (
        b'{"id":"b0b6dd9d-8b9b-48a9-ba46-b9d54906e415","class":"us_equity","exchange":"NASDAQ","symbol":"%s",'
        b'"name":"","status":"active","tradable":true,"marginable":true,"shortable":true,"fractionable":true}'
    )
    body = b'[' + record % b'AAPL' + b',' + record % b'AAPL2' + b']'
    vendor = stub_trading_vendor(*[raw_answer(body)] * MAX_ATTEMPTS, request_cap=MAX_ATTEMPTS)
    with ThreadPoolExecutor(max_workers=1) as pool:
        alpaca = AlpacaRead(
            executor_provider=lambda: pool,
            clock=lambda: datetime.now(UTC),
            scheduler=RecordingScheduler(),
            scope=ALPACA_DATA_SCOPE,
            trading_client=vendor.client,
            trading_scope=ALPACA_TRADING_SCOPE,
        )
        monkeypatch.setattr(trip.ingest.reader, 'get_assets', alpaca.get_assets)

        request_id, response = await trip.run(ACTIVE, answers=MAX_ATTEMPTS)

    detail = 'asset list refused as malformed: G6 duplicate vendor id (1)'
    assert response.ack.accepted is not None
    assert len(vendor.sent) == MAX_ATTEMPTS
    assert [report.trailer.detail for report in trip.client.delivered] == [detail] * MAX_ATTEMPTS
    assert {report.trailer.outcome for report in trip.client.delivered} == {DeliveryOutcome.FAILED}
    assert [answer.verdict for answer in trip.client.answers] == [DeliveryVerdict.CONTINUE] * (MAX_ATTEMPTS - 1) + [
        DeliveryVerdict.FINAL
    ]
    runs = await store.runs(request_id)
    assert [(run.state, run.attempt_no) for run in runs] == [
        (IngestRunState.FAILED, attempt_no) for attempt_no in range(1, MAX_ATTEMPTS + 1)
    ]
    assert [run.error_message for run in runs] == [detail] * MAX_ATTEMPTS, 'the run history lost the guard detail'
    assert await _snapshots(store, request_id) == [], 'a refused list wrote a snapshot'
    request = await store.request_row(request_id)
    assert (request.state, request.failure_cause, request.attempts) == (
        IngestRequestState.FAILED,
        IngestRequestFailureCause.ATTEMPTS_EXHAUSTED.value,
        MAX_ATTEMPTS,
    )
