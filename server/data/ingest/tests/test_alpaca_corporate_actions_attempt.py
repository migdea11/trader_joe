"""The corporate-actions request kind: its attempt runner and the Alpaca handle's Submit of it (P13, tj-7zjov1.23).

Against builder-ingest's daa41c6, checked against the bead's design notes: the 14:49 UTC P36c feed rule (a
CORPORATE_ACTIONS report is always FEED_UNSPECIFIED; the submit path that queues one leaves ScheduledRequest.feed
None; through the handle's submit, a corporate-actions request that arrives exhausted and one that expires in the
queue each report FEED_UNSPECIFIED on the wire, the no-call shape otherwise unchanged; a mutant that sets a tape on
the queued request must red), the 14:32 UTC note (a registered intake on BrokerHandle on the data-API scope with no
tape, a runner beside the asset-list runner, submit, active and finished never overridden), and the owner's ruling
of 04:46 UTC (captured as published, nothing interpreted, nothing filtered: the store judges).

WHAT IS REAL. CorporateActionsAttemptRunner is driven by a scripted BrokerRead double, so its report is checked for
each answer the interface allows. The handle tests build the production AlpacaHandle with its own Scheduler and
AckGatedExecutor; only the store is a double, and the vendor, where one is reached, is the real alpaca-py
CorporateActionsClient over the recorded transport answering a recorded capture. No vendor key is read.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.asset_attempt import AssetListWork
from data.ingest.app.brokers.alpaca.corporate_actions_attempt import CorporateActionsAttemptRunner, CorporateActionsWork
from data.ingest.app.brokers.alpaca.handle import (
    ALPACA_BARS_PAGE_SIZE,
    ALPACA_DATA_SCOPE,
    AlpacaHandle,
    _KindRunner,
    estimate_request_calls,
)
from data.ingest.app.brokers.bars_attempt import BarsWork
from data.ingest.app.brokers.broker_errors import MissingCredentialsError
from data.ingest.app.brokers.interface import (
    CallTally,
    CorporateAction,
    CorporateActionsFailure,
    CorporateActionsQuery,
    CorporateActionsResponse,
)
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler import ScheduledRequest
from data.ingest.app.scheduler.executor import AttemptResult, ExecutorSettings
from data.ingest.app.scheduler.retry import RetryPolicy
from data.ingest.tests.alpaca_recorded import load_raw, stub_corporate_actions_vendor, stub_trading_vendor, stub_vendor
from data.ingest.tests.feed_rule import assert_a_no_tape_report_is_unspecified
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecord,
    CorporateActionRecords,
    DeliverAttemptResponse,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
)
from schemas.data_ingest.fetch_dataset import ServedRange
from schemas.data_ingest.submit import (
    AssetListRequest,
    AssetListStatus,
    BarsRequest,
    CorporateActionsRequest,
    RequestKind,
    SubmitAccepted,
    SubmitAck,
    SubmitRequest,
)


pytestmark = pytest.mark.data_ingest

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
MAX_ATTEMPTS = 5
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 5.0
INSTANCE = 'instance-under-test'

ACTIONS = CorporateActionsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol='SIVB',
    start=datetime(2015, 1, 1, tzinfo=UTC),
    end=datetime(2026, 10, 24, tzinfo=UTC),
)

MERGER = CorporateAction(
    vendor_action_id='m-1',
    action_type='stock_mergers',
    raw_payload=b'{"id":"m-1","acquirer_symbol":"AMD"}',
    effective_date=datetime(2022, 2, 14, tzinfo=UTC),
    process_date=datetime(2022, 2, 14, tzinfo=UTC),
    identifiers={'acquirer_symbol': 'AMD', 'acquiree_cusip': '983919101'},
)
DIVIDEND = CorporateAction(
    vendor_action_id='d-1',
    action_type='cash_dividends',
    raw_payload=b'{"id": "d-1"}',
    ex_date=datetime(2026, 1, 2, tzinfo=UTC),
    record_date=datetime(2026, 1, 3, tzinfo=UTC),
    payable_date=datetime(2026, 1, 10, tzinfo=UTC),
)


# ---------------------------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------------------------


class ScriptedActions:
    """A BrokerRead whose get_corporate_actions answers from a script and records each query; calls count."""

    def __init__(self, answer: CorporateActionsResponse | CorporateActionsFailure, pages: int = 1) -> None:
        self.queries: list[CorporateActionsQuery] = []
        self.__answer = answer
        self.__pages = pages

    async def get_corporate_actions(self, query: CorporateActionsQuery):
        self.queries.append(query)
        if query.tally is not None:
            for _ in range(self.__pages):
                query.tally.record(NOW)
                if isinstance(self.__answer, CorporateActionsResponse):
                    query.tally.record_page()
        return self.__answer

    async def get_assets(self, query):
        raise AssertionError('a corporate-actions attempt never reads an asset list')

    async def get_asset(self, query):
        raise AssertionError('a corporate-actions attempt never looks a symbol up')

    async def get_bars(self, query):
        raise AssertionError('a corporate-actions attempt never reads bars')


class AnsweringStore:
    """A StoreDeliveryClient that answers every delivery at once, FINAL, and records it."""

    def __init__(self) -> None:
        self.received: list[AttemptDelivery] = []
        self.__arrived = asyncio.Event()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        self.received.append(attempt)
        self.__arrived.set()
        return DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=attempt.header.attempt_no)

    async def register_instance(self, request):
        raise AssertionError('the executor never registers an instance')

    async def first(self) -> AttemptDelivery:
        await asyncio.wait_for(self.__arrived.wait(), GUARD_S)
        return self.received[0]


async def settle() -> None:
    """Let every task that can run, run."""
    for _ in range(200):
        await asyncio.sleep(0)


def scheduled_actions(ready_by: datetime | None = NOW + timedelta(minutes=5)) -> ScheduledRequest:
    return ScheduledRequest(
        request_id='ca-1',
        quota_scope=ALPACA_DATA_SCOPE,
        payload=CorporateActionsWork(request=ACTIONS, priority=RequestPriority.BACKFILL),
        ready_by=ready_by,
    )


def settings() -> ExecutorSettings:
    return ExecutorSettings(
        buffer_size=8,
        workers=1,
        stall_timeout_s=300.0,
        stall_check_interval_s=3600.0,
        redelivery_base_s=1.0,
        redelivery_max_s=1.5,
        retry=RetryPolicy(max_attempts=MAX_ATTEMPTS, base_delay_s=1.0, max_delay_s=60.0, factor=2.0),
    )


def alpaca_handle() -> AlpacaHandle:
    return AlpacaHandle(settings=settings(), calls_per_minute=60)


def actions_submit(request_id: str = 'ca-1', **overrides) -> SubmitRequest:
    fields = {'request_id': request_id, 'owner': 'world-manager', 'corporate_actions': ACTIONS}
    return SubmitRequest(**{**fields, **overrides})


def bars_request() -> BarsRequest:
    return BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_DAY,
        start=datetime(2026, 1, 2, tzinfo=UTC),
        end=datetime(2026, 1, 9, tzinfo=UTC),
        update_type=UpdateType.STATIC,
    )


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """An IEX deployment (so a copied tape would be visible), no credential, no trading host, no cached client."""
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)
    broker_api.set_client(None)
    broker_api.set_trading_client(None)
    broker_api.set_corporate_actions_client(None)
    broker_api.forget_resets()
    yield
    broker_api.set_client(None)
    broker_api.set_trading_client(None)
    broker_api.set_corporate_actions_client(None)
    broker_api.forget_resets()


# ---------------------------------------------------------------------------------------------
# The attempt runner
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_attempt_asks_for_the_symbol_and_window_the_request_names_under_its_priority_and_ready_by():
    reader = ScriptedActions(CorporateActionsResponse(actions=[], as_of=NOW))
    ready_by = NOW + timedelta(minutes=9)

    await CorporateActionsAttemptRunner(reader)(scheduled_actions(ready_by), 1)

    [query] = reader.queries
    assert (query.symbol, query.start, query.end, query.priority, query.deadline) == (
        'SIVB',
        ACTIONS.start,
        ACTIONS.end,
        RequestPriority.BACKFILL,
        ready_by,
    )
    assert isinstance(query.tally, CallTally), 'the attempt cannot report its vendor calls without a tally'


@pytest.mark.asyncio
async def test_served_actions_succeed_with_every_action_carried_raw_in_one_records_part_fields_mapped():
    reader = ScriptedActions(CorporateActionsResponse(actions=[MERGER, DIVIDEND], as_of=NOW))

    result = await CorporateActionsAttemptRunner(reader)(scheduled_actions(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.trailer.as_of == NOW
    assert result.trailer.error_class is None
    # tj-7zjov1.124, architect option A: the served range is the requested window exactly as given (was None).
    assert result.trailer.served_range == ServedRange(start=ACTIONS.start, end=ACTIONS.end)
    assert result.parts == [
        CorporateActionRecords(
            records=[
                CorporateActionRecord(
                    vendor_action_id='m-1',
                    action_type='stock_mergers',
                    effective_date=datetime(2022, 2, 14, tzinfo=UTC),
                    process_date=datetime(2022, 2, 14, tzinfo=UTC),
                    identifiers={'acquirer_symbol': 'AMD', 'acquiree_cusip': '983919101'},
                    raw_payload=b'{"id":"m-1","acquirer_symbol":"AMD"}',
                ),
                CorporateActionRecord(
                    vendor_action_id='d-1',
                    action_type='cash_dividends',
                    ex_date=datetime(2026, 1, 2, tzinfo=UTC),
                    record_date=datetime(2026, 1, 3, tzinfo=UTC),
                    payable_date=datetime(2026, 1, 10, tzinfo=UTC),
                    raw_payload=b'{"id": "d-1"}',
                ),
            ]
        )
    ]


@pytest.mark.asyncio
async def test_a_succeeded_attempt_names_no_tape_no_instrument_and_the_data_scope_and_counts_its_pages():
    reader = ScriptedActions(CorporateActionsResponse(actions=[MERGER], as_of=NOW), pages=3)

    result = await CorporateActionsAttemptRunner(reader)(scheduled_actions(), 1)

    assert result.feed is None
    assert result.vendor_asset_id == ''
    assert result.quota_key == ALPACA_DATA_SCOPE
    assert result.reset_at is None
    stats = result.trailer.vendor_stats
    assert (stats.calls, stats.pages, stats.retries, stats.throttled_429) == (3, 3, 0, 0)
    assert stats.first_call_at == stats.last_call_at == NOW


@pytest.mark.asyncio
async def test_no_action_served_succeeds_with_no_part():
    reader = ScriptedActions(CorporateActionsResponse(actions=[], as_of=NOW))

    result = await CorporateActionsAttemptRunner(reader)(scheduled_actions(), 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    assert result.trailer.as_of == NOW
    assert result.parts == []
    assert result.trailer.served_range == ServedRange(start=ACTIONS.start, end=ACTIONS.end), (
        'an empty success names no served range: the store refuses it (rule i) and ingest redelivers it for ever'
    )


# A window that starts and ends inside a day: the read sends the vendor whole UTC days (P13f, tj-7zjov1.154: the
# start day as a floor, the end day as a ceiling), so a runner that copied the days sent would report 2026-03-02 and
# 2026-03-06 here; the served range is the window as given (tj-7zjov1.124, architect ruling of 22:05 UTC).
MID_DAY = CorporateActionsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol='SIVB',
    start=datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
    end=datetime(2026, 3, 5, 16, 0, tzinfo=UTC),
)


@pytest.mark.asyncio
@pytest.mark.parametrize('actions', [[], [MERGER]], ids=['empty', 'with-records'])
async def test_a_mid_day_window_is_served_exactly_as_requested_never_as_the_whole_days_read(actions):
    reader = ScriptedActions(CorporateActionsResponse(actions=actions, as_of=NOW))
    request = ScheduledRequest(
        request_id='ca-mid',
        quota_scope=ALPACA_DATA_SCOPE,
        payload=CorporateActionsWork(request=MID_DAY, priority=RequestPriority.BACKFILL),
        ready_by=NOW + timedelta(minutes=5),
    )

    result = await CorporateActionsAttemptRunner(reader)(request, 1)

    assert result.trailer.outcome is DeliveryOutcome.SUCCEEDED
    served = result.trailer.served_range
    assert served == ServedRange(start=MID_DAY.start, end=MID_DAY.end)
    assert served.end == datetime(2026, 3, 5, 16, 0, tzinfo=UTC), 'the served end was rounded to a day'
    assert served.start == datetime(2026, 3, 2, 9, 30, tzinfo=UTC), 'the served start was floored to a day'


@pytest.mark.asyncio
async def test_the_served_range_through_the_real_alpaca_read_is_the_requested_window_not_the_days_sent():
    """The production AlpacaRead widens a mid-day window to whole days on the wire; the served range does not."""
    vendor = stub_corporate_actions_vendor(load_raw('corporate_actions_empty'))
    broker_api.set_corporate_actions_client(vendor.client)
    handle = alpaca_handle()
    store = AnsweringStore()
    handle.start(store, INSTANCE)
    try:
        await handle.submit(
            SubmitRequest(
                request_id='ca-mid',
                owner='world-manager',
                corporate_actions=MID_DAY,
                ready_by=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        report = await store.first()
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED, report.trailer.detail
    [sent] = vendor.sent
    assert (sent.params['start'], sent.params['end']) == ('2026-03-02', '2026-03-06'), 'the read no longer widens'
    assert report.trailer.served_range == ServedRange(start=MID_DAY.start, end=MID_DAY.end)


RESET = NOW + timedelta(seconds=30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'klass', 'reset_at'),
    [
        pytest.param(
            ExogenousError(Reason.VENDOR_UNAVAILABLE, 'the vendor failed'),
            DeliveryErrorClass.TRANSIENT,
            None,
            id='unavailable-transient',
        ),
        pytest.param(
            ExogenousError(Reason.VENDOR_UNAVAILABLE, 'the vendor failed', reset_at=RESET, clock=lambda: NOW),
            DeliveryErrorClass.TRANSIENT,
            None,
            id='transient-carries-no-reset-even-if-the-error-named-one',
        ),
        pytest.param(MissingCredentialsError('no key'), DeliveryErrorClass.TRANSIENT, None, id='auth-transient'),
        pytest.param(
            ExogenousError(Reason.VENDOR_RATE_LIMITED, 'slow down', reset_at=RESET, clock=lambda: NOW),
            DeliveryErrorClass.RATE_LIMITED,
            RESET,
            id='rate-limited-with-its-reset',
        ),
        pytest.param(
            ExogenousError(Reason.RATE_BUDGET, 'no token', reset_at=RESET, clock=lambda: NOW),
            DeliveryErrorClass.RATE_LIMITED,
            RESET,
            id='rate-budget-with-its-reset',
        ),
        pytest.param(
            InvalidRequestError(Reason.INVALID_REQUEST, 'no symbol'),
            DeliveryErrorClass.PERMANENT,
            None,
            id='invalid-permanent',
        ),
    ],
)
async def test_a_failed_read_reports_its_class_its_detail_the_data_scope_and_no_part(error, klass, reset_at):
    reader = ScriptedActions(CorporateActionsFailure(error))

    result = await CorporateActionsAttemptRunner(reader)(scheduled_actions(), 1)

    assert result.trailer.outcome is DeliveryOutcome.FAILED
    assert result.trailer.error_class is klass
    assert result.trailer.detail == error.detail
    assert result.trailer.served_range is None, 'a FAILED report names a served range (store rule d refuses it)'
    assert result.reset_at == reset_at
    assert result.parts == []
    assert result.feed is None
    assert result.quota_key == ALPACA_DATA_SCOPE
    assert (result.trailer.vendor_stats.calls, result.trailer.vendor_stats.pages) == (1, 0)


# ---------------------------------------------------------------------------------------------
# The cost estimate and the kind dispatch
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize('page_size', [1, 2, ALPACA_BARS_PAGE_SIZE])
def test_a_corporate_actions_request_is_estimated_at_one_call_whatever_the_page_size(page_size):
    assert estimate_request_calls(scheduled_actions(), page_size) == 1


class SpyRunner:
    """An AttemptRunner that records the requests it ran and answers a fixed result."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ran: list[str] = []

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        self.ran.append(request.request_id)
        return AttemptResult(trailer=DeliveryTrailer(outcome=DeliveryOutcome.SUCCEEDED), quota_key=self.name)


@pytest.mark.asyncio
async def test_the_kind_runner_sends_each_payload_to_its_own_runner_and_corporate_actions_never_to_bars():
    bars, assets, actions = SpyRunner('bars'), SpyRunner('assets'), SpyRunner('actions')
    dispatch = _KindRunner(bars, assets, actions)
    listing = AssetListRequest(source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE)
    requests_by_kind = {
        'b': BarsWork(request=bars_request(), priority=RequestPriority.BACKFILL, feed=Feed.IEX),
        'a': AssetListWork(request=listing, priority=RequestPriority.BACKFILL),
        'c': CorporateActionsWork(request=ACTIONS, priority=RequestPriority.BACKFILL),
    }

    answered = [
        (await dispatch(ScheduledRequest(request_id=rid, quota_scope='S', payload=payload), 1)).quota_key
        for rid, payload in requests_by_kind.items()
    ]

    assert answered == ['bars', 'assets', 'actions']
    assert (bars.ran, assets.ran, actions.ran) == (['b'], ['a'], ['c'])


# ---------------------------------------------------------------------------------------------
# The handle's Submit of corporate actions
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corporate_actions_are_accepted_with_no_tape_on_the_data_scope_and_queued_with_no_tape():
    """The 14:49 UTC feed rule: the queued request's feed stays None; it never takes the deployment tape."""
    handle = alpaca_handle()
    ready_by = NOW + timedelta(days=3650)

    ack = await handle.submit(actions_submit(ready_by=ready_by, attempt_count=2, client='world'))

    assert ack == SubmitAck(accepted=SubmitAccepted(feed=None, quota_key=ALPACA_DATA_SCOPE))
    assert handle.scheduler.queued == 1
    queued = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)
    assert queued.feed is None, 'the queued corporate-actions request carries a tape'
    assert queued.quota_scope == ALPACA_DATA_SCOPE
    assert queued.payload == CorporateActionsWork(request=ACTIONS, priority=RequestPriority.BACKFILL)
    assert (queued.request_id, queued.ready_by, queued.attempt_count, queued.client_label) == (
        'ca-1',
        ready_by,
        2,
        'world',
    )


@pytest.mark.asyncio
async def test_a_corporate_actions_submit_is_idempotent_on_request_id_until_it_finishes():
    handle = alpaca_handle()

    first = await handle.submit(actions_submit())
    again = await handle.submit(actions_submit())

    assert again == first
    assert handle.scheduler.queued == 1, 'a re-sent corporate-actions request was queued twice'
    assert handle.active == 1

    handle.finished(ScheduledRequest(request_id='ca-1', quota_scope=ALPACA_DATA_SCOPE, payload=None))
    assert handle.active == 0
    await handle.submit(actions_submit())
    assert handle.scheduler.queued == 2


@pytest.mark.asyncio
async def test_a_request_naming_corporate_actions_with_no_body_raises_invalid_request_and_is_never_held():
    handle = alpaca_handle()
    # What a valid SubmitRequest can never be (its exactly-one validator forbids it): the kind names corporate
    # actions and the body is missing. The stand-in reaches the intake's own guard, as a caller past validation would.
    bodiless = SimpleNamespace(
        kind=RequestKind.CORPORATE_ACTIONS,
        request_id='ca-1',
        bars=None,
        asset_list=None,
        corporate_actions=None,
        recovered=False,
        attempt_count=0,
        ready_by=None,
        client=None,
    )

    with pytest.raises(InvalidRequestError) as raised:
        await handle.submit(bodiless)

    assert raised.value.reason is Reason.INVALID_REQUEST
    assert raised.value.detail == 'invalid corporate_actions request: no body for its kind'
    assert handle.scheduler.queued == 0
    assert handle.active == 0


class ChargeSpy:
    """Wraps the handle's real scheduler's charge_restart and submit, logging each in order."""

    def __init__(self, handle: AlpacaHandle, monkeypatch) -> None:
        self.log: list[tuple[str, str, int | None]] = []
        scheduler = handle.scheduler
        charge, submit = scheduler.charge_restart, scheduler.submit

        def charge_restart(request: ScheduledRequest) -> int:
            charged = charge(request)
            self.log.append(('charge_restart', request.quota_scope, charged))
            return charged

        async def logged_submit(request: ScheduledRequest) -> None:
            self.log.append(('submit', request.quota_scope, None))
            await submit(request)

        monkeypatch.setattr(scheduler, 'charge_restart', charge_restart)
        monkeypatch.setattr(scheduler, 'submit', logged_submit)


@pytest.mark.asyncio
async def test_a_recovered_corporate_actions_request_is_charged_its_one_call_to_the_data_scope_before_it_is_queued(
    monkeypatch,
):
    handle = alpaca_handle()
    spy = ChargeSpy(handle, monkeypatch)

    await handle.submit(actions_submit(attempt_count=1, recovered=True))

    assert spy.log == [('charge_restart', ALPACA_DATA_SCOPE, 1), ('submit', ALPACA_DATA_SCOPE, None)]


@pytest.mark.asyncio
async def test_a_corporate_actions_request_not_flagged_recovered_is_never_charged(monkeypatch):
    handle = alpaca_handle()
    spy = ChargeSpy(handle, monkeypatch)

    await handle.submit(actions_submit(attempt_count=1))

    assert spy.log == [('submit', ALPACA_DATA_SCOPE, None)]


# ---------------------------------------------------------------------------------------------
# Through the handle and its executor: the no-call reports name no tape on the wire
# ---------------------------------------------------------------------------------------------


def assert_shaped_as_a_no_call_report(attempt: AttemptDelivery) -> None:
    """store.proto DELIVERY_OUTCOME_EXPIRED and EXHAUSTED: nothing of an attempt."""
    assert attempt.trailer.vendor_stats is None
    assert attempt.trailer.served_range is None
    assert attempt.trailer.as_of is None
    assert attempt.trailer.error_class is None
    assert attempt.trailer.last_attempt is False
    assert attempt.header.quota_key == ''
    assert attempt.header.vendor_asset_id == ''
    assert attempt.parts == []


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [True, False], ids=['recovered', 'not-recovered'])
@pytest.mark.parametrize('attempt_count', [MAX_ATTEMPTS, MAX_ATTEMPTS + 2], ids=['at-max', 'past-max'])
async def test_corporate_actions_arriving_exhausted_report_feed_unspecified_with_no_call(
    monkeypatch, recovered, attempt_count
):
    """The 14:49 UTC validator item: through the handle's submit, a corporate-actions request that arrives exhausted."""
    handle = alpaca_handle()
    spy = ChargeSpy(handle, monkeypatch)
    store = AnsweringStore()
    handle.start(store, INSTANCE)
    try:
        ack = await handle.submit(actions_submit(attempt_count=attempt_count, recovered=recovered))
        report = await store.first()
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert ack.accepted.feed is None
    assert report.trailer.outcome is DeliveryOutcome.EXHAUSTED
    assert report.header.request_id == 'ca-1'
    assert report.header.attempt_no == attempt_count + 1
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)
    assert_shaped_as_a_no_call_report(report)
    assert len(store.received) == 1
    assert [entry[0] for entry in spy.log] == (['charge_restart'] if recovered else []), 'queued, or charged wrongly'
    assert handle.active == 0, 'the exhausted request was not forgotten after the acknowledgement'


@pytest.mark.asyncio
async def test_corporate_actions_expiring_in_the_queue_report_feed_unspecified_with_no_call():
    """The 14:49 UTC validator item: through the handle's submit, a corporate-actions request that expires queued."""
    handle = alpaca_handle()
    store = AnsweringStore()
    handle.start(store, INSTANCE)
    try:
        ack = await handle.submit(actions_submit(ready_by=datetime.now(UTC) - timedelta(seconds=1)))
        report = await store.first()
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert ack.accepted.feed is None
    assert report.trailer.outcome is DeliveryOutcome.EXPIRED
    assert report.header.attempt_no == 1
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)
    assert_shaped_as_a_no_call_report(report)
    assert handle.active == 0


# ---------------------------------------------------------------------------------------------
# Through the handle end to end: paced on the data scope, the recorded actions on the wire
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_submitted_corporate_actions_request_is_read_on_the_data_scope_and_delivered_raw_with_no_tape(
    monkeypatch,
):
    recorded = load_raw('corporate_actions_sivb_untyped')
    vendor = stub_corporate_actions_vendor(recorded)
    # Bars and trading clients that fail the test if reached: neither belongs on this path.
    bars_vendor = stub_vendor()
    trading_vendor = stub_trading_vendor()
    broker_api.set_corporate_actions_client(vendor.client)
    broker_api.set_client(bars_vendor.client)
    broker_api.set_trading_client(trading_vendor.client)
    handle = alpaca_handle()
    acquired: list[str] = []
    acquire = handle.scheduler.acquire

    async def recording_acquire(scope, *args, **kwargs):
        acquired.append(scope)
        await acquire(scope, *args, **kwargs)

    monkeypatch.setattr(handle.scheduler, 'acquire', recording_acquire)
    store = AnsweringStore()
    handle.start(store, INSTANCE)
    try:
        await handle.submit(actions_submit(ready_by=datetime.now(UTC) + timedelta(minutes=5)))
        report = await store.first()
        await settle()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED, report.trailer.detail
    assert acquired == [ALPACA_DATA_SCOPE], 'the read was not paced by exactly one data-scope token'
    assert [sent.params for sent in vendor.sent] == [
        {'symbols': 'SIVB', 'start': '2015-01-01', 'end': '2026-10-24', 'sort': 'asc', 'limit': '1000'}
    ]
    assert bars_vendor.sent == [] and trading_vendor.sent == [], 'another host was reached'
    assert report.header.feed is None
    assert_a_no_tape_report_is_unspecified(report.header)
    assert report.header.quota_key == ALPACA_DATA_SCOPE
    assert report.header.vendor_asset_id == ''
    assert (report.trailer.vendor_stats.calls, report.trailer.vendor_stats.pages) == (1, 1)
    [part] = report.parts
    assert isinstance(part, CorporateActionRecords)
    [action] = part.records
    assert action.vendor_action_id == '69dbd57d-28b8-41bb-bb0b-c34e9409ba62'
    assert action.action_type == 'stock_and_cash_mergers'
    assert action.raw_payload in recorded.body
    assert action.identifiers == {
        'acquiree_cusip': '101119105',
        'acquiree_symbol': 'BPFH',
        'acquirer_cusip': '78486Q101',
        'acquirer_symbol': 'SIVB',
    }
