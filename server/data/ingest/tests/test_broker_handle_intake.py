"""One Submit intake in BrokerHandle; each request kind registers only its own part (P12r, tj-7zjov1.151).

Against builder-ingest's f9dbd70, checked against the bead's design: BrokerHandle.submit holds the shared tail
once (kind lookup, the accepted ack, a duplicate request id answered from the one shared active set, an exhausted
request reported and never queued -- charged first if recovered --, a recovered request charged before the queue,
the queue), a kind supplies an intake through register_intake, and a kind whose intake names no tape gets none
(the P36c feed rule, tj-7zjov1.22 and tj-7zjov1.23 notes of 14:49). AlpacaHandle registers the asset_list intake
and has no submit, active or finished of its own.

WHAT IS REAL. Every handle here is the production BrokerHandle or AlpacaHandle over its own real Scheduler and
AckGatedExecutor. The only stand-ins are the store (it answers every report at once), a second kind's intake on a
bare BrokerHandle, and pass-through spies on the handle's own scheduler and executor that log charge_restart,
the queue and report_exhausted in the order they are called, so "charged before it is queued" is observed, not
inferred. No vendor key is read and no vendor is reached.

The tail tests are parametrised over EVERY one of AlpacaHandle's kinds (bars and asset_list, and corporate_actions
since tj-7zjov1.23), so a change to the shared tail reds a test of each kind, which is what the bead's acceptance
asks of the validator's mutants.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import ERROR_DOMAIN, InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.asset_attempt import AssetListWork
from data.ingest.app.brokers.alpaca.corporate_actions_attempt import CorporateActionsWork
from data.ingest.app.brokers.alpaca.handle import ALPACA_DATA_SCOPE, ALPACA_TRADING_SCOPE, AlpacaHandle
from data.ingest.app.brokers.bars_attempt import BarsWork
from data.ingest.app.brokers.handle import BrokerHandle, Intake
from data.ingest.app.scheduler import Credential, Limit, SchedulerBuilder, StaticCredentialProvider
from data.ingest.app.scheduler.executor import AckGatedExecutor, ExecutorSettings
from data.ingest.app.scheduler.queue import ScheduledRequest
from data.ingest.app.scheduler.retry import RetryPolicy
from schemas.data_ingest.delivery import AttemptDelivery, DeliverAttemptResponse, DeliveryVerdict
from schemas.data_ingest.fetch_dataset import FetchRefused
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

MAX_ATTEMPTS = 5
# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 5.0
INSTANCE = 'instance-under-test'

# The bare handle's scopes: its own (bars) and the second kind's.
BARE_SCOPE = 'TEST_DATA'
OTHER_SCOPE = 'TEST_OTHER'

LISTING = AssetListRequest(source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE)
ACTIONS = CorporateActionsRequest(
    source=DataSource.ALPACA_API,
    asset_symbol='VFV',
    start=datetime(2026, 1, 1, tzinfo=UTC),
    end=datetime(2026, 2, 1, tzinfo=UTC),
)


# ---------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------


def bars_submit(request_id: str = 'req-1', *, feed: Feed | None = None, **overrides) -> SubmitRequest:
    bars = BarsRequest(
        source=DataSource.ALPACA_API,
        asset_symbol='VFV',
        asset_type=AssetType.STOCK,
        data_types=[DataType.MARKET_ACTIVITY],
        granularity=Granularity.ONE_DAY,
        start=datetime(2026, 1, 2, tzinfo=UTC),
        end=datetime(2026, 1, 9, tzinfo=UTC),
        update_type=UpdateType.STATIC,
        feed=feed,
    )
    return SubmitRequest(**{'request_id': request_id, 'owner': 'rebalancer', 'bars': bars, **overrides})


def asset_list_submit(request_id: str = 'req-1', **overrides) -> SubmitRequest:
    return SubmitRequest(**{'request_id': request_id, 'owner': 'world-manager', 'asset_list': LISTING, **overrides})


def actions_submit(request_id: str = 'req-1', **overrides) -> SubmitRequest:
    return SubmitRequest(
        **{'request_id': request_id, 'owner': 'world-manager', 'corporate_actions': ACTIONS, **overrides}
    )


@dataclass(frozen=True)
class Kind:
    """One of AlpacaHandle's kinds: how to build its request and what its ack and queued request carry."""

    name: str
    build: object
    scope: str
    feed: Feed | None
    work: type

    def submit(self, request_id: str = 'req-1', **overrides) -> SubmitRequest:
        return self.build(request_id, **overrides)


BARS = Kind('bars', bars_submit, ALPACA_DATA_SCOPE, Feed.IEX, BarsWork)
ASSET_LIST = Kind('asset_list', asset_list_submit, ALPACA_TRADING_SCOPE, None, AssetListWork)
CORPORATE_ACTIONS = Kind('corporate_actions', actions_submit, ALPACA_DATA_SCOPE, None, CorporateActionsWork)
# Every kind AlpacaHandle registers (bars and asset_list from P12r, corporate_actions from P13, tj-7zjov1.23).
EVERY_KIND = pytest.mark.parametrize('kind', [BARS, ASSET_LIST, CORPORATE_ACTIONS], ids=lambda kind: kind.name)


# ---------------------------------------------------------------------------------------------
# Doubles and harness
# ---------------------------------------------------------------------------------------------


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


class CountingTape:
    """The deployment's tape resolver: always IEX, counting how often it is asked."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Feed:
        self.calls += 1
        return Feed.IEX


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


class Watched:
    """A production handle whose own scheduler and executor log, in order, each charge, queueing and exhausted report.

    The spies are pass-throughs set on the handle's own scheduler and executor objects, so the handle's code path is
    unchanged and the real queue and window still receive every call.
    """

    def __init__(self, handle: BrokerHandle) -> None:
        self.handle = handle
        self.order: list[tuple[str, str]] = []
        self.queued: list[ScheduledRequest] = []
        scheduler = handle.scheduler
        executor = handle.executor
        charge_restart = scheduler.charge_restart
        submit = scheduler.submit
        report_exhausted = executor.report_exhausted

        def logged_charge_restart(request: ScheduledRequest) -> int:
            self.order.append(('charge', request.request_id))
            return charge_restart(request)

        async def logged_submit(request: ScheduledRequest) -> None:
            self.order.append(('queue', request.request_id))
            self.queued.append(request)
            await submit(request)

        def logged_report_exhausted(request: ScheduledRequest) -> None:
            self.order.append(('exhausted', request.request_id))
            report_exhausted(request)

        scheduler.charge_restart = logged_charge_restart
        scheduler.submit = logged_submit
        executor.report_exhausted = logged_report_exhausted


async def _no_expired_sink(_expired) -> None:
    raise AssertionError('no request should expire in this test')


def bare_handle(tape: CountingTape | None = None) -> BrokerHandle:
    """A bare production BrokerHandle (bars only, as built) over a real Scheduler with two scopes and a real executor."""
    minute = timedelta(minutes=1)
    scheduler = (
        SchedulerBuilder()
        .declare_scope(BARE_SCOPE, [Limit(minute, 6000)])
        .set_limit(minute, 6000, scope=BARE_SCOPE)
        .declare_scope(OTHER_SCOPE, [Limit(minute, 6000)])
        .set_limit(minute, 6000, scope=OTHER_SCOPE)
        .cost_function(lambda _request, _page_size: 1, page_size=1)
        .credential_provider(StaticCredentialProvider(Credential(key_id='key', secret='secret')))
        .on_expired(_no_expired_sink)
        .build()
    )
    holder: dict[str, BrokerHandle] = {}
    executor = AckGatedExecutor(
        name='BARE',
        scheduler=scheduler,
        run_attempt=Mock(),
        settings=settings(),
        on_finished=lambda request: holder['handle'].finished(request),
    )
    handle = BrokerHandle(
        source=DataSource.ALPACA_API,
        scope=BARE_SCOPE,
        scheduler=scheduler,
        executor=executor,
        reader=Mock(),
        resolve_feed=tape if tape is not None else CountingTape(),
    )
    holder['handle'] = handle
    return handle


@dataclass(frozen=True)
class ActionsWork:
    """The second kind's payload on the bare handle."""

    request: CorporateActionsRequest


def actions_intake(request: SubmitRequest) -> Intake:
    """A second kind's intake, as P13 would register one: its own scope, its own work, no tape."""
    return Intake(scope=OTHER_SCOPE, payload=ActionsWork(request=request.corporate_actions))


@pytest.fixture
def tape(monkeypatch) -> CountingTape:
    """The Alpaca deployment's tape, IEX, counted. AlpacaHandle binds broker_api.resolve_feed when it is built."""
    counting = CountingTape()
    monkeypatch.setattr(broker_api, 'resolve_feed', counting)
    return counting


@pytest.fixture
def alpaca(tape) -> Watched:
    return Watched(AlpacaHandle(settings=settings(), calls_per_minute=60))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No credential, no trading host, no rate-limit override, no cached client."""
    monkeypatch.delenv('ALPACA_SIP_ENABLED', raising=False)
    monkeypatch.delenv('ALPACA_RATE_LIMIT_PER_MINUTE', raising=False)
    for name in (*broker_api.ALPACA_CREDENTIAL_VARS, broker_api.ALPACA_TRADING_HOST_VAR):
        monkeypatch.delenv(name, raising=False)
    broker_api.set_client(None)
    broker_api.set_trading_client(None)
    yield
    broker_api.set_client(None)
    broker_api.set_trading_client(None)


# ---------------------------------------------------------------------------------------------
# Registration: a kind adds itself with an intake, never by overriding submit
# ---------------------------------------------------------------------------------------------


def test_alpaca_handle_defines_no_submit_active_or_finished_of_its_own():
    own = vars(AlpacaHandle)

    assert 'submit' not in own, 'AlpacaHandle overrides submit again'
    assert 'active' not in own, 'AlpacaHandle keeps an active of its own'
    assert 'finished' not in own, 'AlpacaHandle keeps a finished of its own'
    assert AlpacaHandle.submit is BrokerHandle.submit
    assert AlpacaHandle.active is BrokerHandle.active
    assert AlpacaHandle.finished is BrokerHandle.finished


@pytest.mark.asyncio
async def test_a_second_kind_registers_its_intake_on_a_bare_handle_and_is_queued_by_the_shared_submit():
    tape = CountingTape()
    watched = Watched(bare_handle(tape))
    handle = watched.handle
    handle.register_intake(RequestKind.CORPORATE_ACTIONS, actions_intake)

    ack = await handle.submit(actions_submit('ca-1', ready_by=datetime(2026, 12, 1, tzinfo=UTC), client='label'))

    assert type(handle).submit is BrokerHandle.submit
    assert ack == SubmitAck(accepted=SubmitAccepted(feed=None, quota_key=OTHER_SCOPE))
    assert handle.active == 1
    assert handle.scheduler.queued == 1
    assert watched.order == [('queue', 'ca-1')]
    queued = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)
    assert queued.request_id == 'ca-1'
    assert queued.quota_scope == OTHER_SCOPE
    assert queued.payload == ActionsWork(request=ACTIONS)
    assert queued.ready_by == datetime(2026, 12, 1, tzinfo=UTC)
    assert queued.client_label == 'label'
    assert queued.attempt_count == 0
    assert queued.feed is None, 'the deployment tape was copied onto a kind whose intake gave none'
    assert tape.calls == 0, 'the tape was resolved for a kind that has none'


@pytest.mark.asyncio
async def test_the_bare_handle_still_serves_bars_through_its_own_registered_intake():
    tape = CountingTape()
    watched = Watched(bare_handle(tape))
    handle = watched.handle

    ack = await handle.submit(bars_submit('b-1'))

    assert ack == SubmitAck(accepted=SubmitAccepted(feed=Feed.IEX, quota_key=BARE_SCOPE))
    queued = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)
    assert isinstance(queued.payload, BarsWork)
    assert queued.payload.feed is Feed.IEX
    assert queued.feed is Feed.IEX
    assert queued.quota_scope == BARE_SCOPE
    assert tape.calls == 1


@pytest.mark.asyncio
async def test_a_second_registration_of_a_kind_replaces_the_first():
    handle = bare_handle()
    handle.register_intake(RequestKind.CORPORATE_ACTIONS, lambda _request: Intake(scope=BARE_SCOPE, payload='first'))
    handle.register_intake(RequestKind.CORPORATE_ACTIONS, actions_intake)

    ack = await handle.submit(actions_submit('ca-1'))

    assert ack.accepted.quota_key == OTHER_SCOPE
    queued = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)
    assert queued.payload == ActionsWork(request=ACTIONS)


# ---------------------------------------------------------------------------------------------
# A refusal from an intake: answered, never held
# ---------------------------------------------------------------------------------------------


REFUSAL = SubmitAck(
    refused=FetchRefused(reason=Reason.INVALID_REQUEST.value, domain=ERROR_DOMAIN, detail='refused by the intake')
)


@pytest.mark.asyncio
@pytest.mark.parametrize('recovered', [False, True])
@pytest.mark.parametrize('attempt_count', [0, MAX_ATTEMPTS])
async def test_an_intake_refusal_is_answered_as_given_and_is_neither_queued_charged_reported_nor_held(
    recovered, attempt_count
):
    watched = Watched(bare_handle())
    handle = watched.handle
    handle.register_intake(RequestKind.CORPORATE_ACTIONS, lambda _request: REFUSAL)

    ack = await handle.submit(actions_submit('ca-1', recovered=recovered, attempt_count=attempt_count))

    assert ack is REFUSAL
    assert watched.order == [], 'a refused request was charged, queued or reported'
    assert handle.scheduler.queued == 0
    assert handle.active == 0, 'a refused request was added to the active set'


@pytest.mark.asyncio
async def test_a_refused_id_is_not_held_so_a_later_accepted_submit_of_it_is_queued(alpaca):
    handle = alpaca.handle

    refused = await handle.submit(bars_submit('b-1', feed=Feed.SIP))
    accepted = await handle.submit(bars_submit('b-1'))

    assert refused.refused is not None
    assert refused.refused.reason == Reason.FEED_NOT_AVAILABLE.value
    assert accepted.accepted is not None
    assert alpaca.order == [('queue', 'b-1')]
    assert handle.active == 1


# ---------------------------------------------------------------------------------------------
# A kind with no intake, and a request whose kind's body is missing
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unregistered_kind_raises_invalid_request_with_the_old_detail_on_a_bare_handle():
    watched = Watched(bare_handle())

    with pytest.raises(InvalidRequestError) as raised:
        await watched.handle.submit(asset_list_submit('a-1'))

    assert raised.value.reason is Reason.INVALID_REQUEST
    assert raised.value.detail == 'asset_list requests are not served yet'
    assert watched.order == []
    assert watched.handle.active == 0


@pytest.mark.asyncio
async def test_the_alpaca_handle_has_every_kind_registered_so_corporate_actions_are_queued_not_refused(alpaca, tape):
    """FLIPPED by tj-7zjov1.23: it pinned corporate_actions as the Alpaca handle's unregistered kind.

    P13 registers it, so no kind is left unregistered on AlpacaHandle; the unregistered-kind refusal stays pinned
    on the bare handle above. The registered intake is queued through the shared tail with no tape.
    """
    ack = await alpaca.handle.submit(actions_submit('ca-1'))

    assert ack == SubmitAck(accepted=SubmitAccepted(feed=None, quota_key=ALPACA_DATA_SCOPE))
    assert alpaca.order == [('queue', 'ca-1')]
    assert alpaca.handle.active == 1
    assert tape.calls == 0, 'the tape was resolved for a kind that has none'


def bodiless(kind: RequestKind) -> SimpleNamespace:
    """A request that names a kind but carries no body for it: what a valid SubmitRequest can never be.

    SubmitRequest's exactly-one validator makes this unreachable through the schema, so the stand-in reaches the
    intakes' own guard directly, as a caller that bypassed validation would.
    """
    return SimpleNamespace(
        kind=kind,
        request_id='nobody',
        bars=None,
        asset_list=None,
        corporate_actions=None,
        recovered=False,
        attempt_count=0,
        ready_by=None,
        client=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', list(RequestKind), ids=lambda kind: kind.value)
async def test_a_kind_with_its_body_missing_raises_invalid_request_with_the_old_detail(alpaca, kind):
    with pytest.raises(InvalidRequestError) as raised:
        await alpaca.handle.submit(bodiless(kind))

    assert raised.value.reason is Reason.INVALID_REQUEST
    assert raised.value.detail == f'{kind.value} requests are not served yet'
    assert alpaca.order == []
    assert alpaca.handle.active == 0


# ---------------------------------------------------------------------------------------------
# The shared tail, through both of AlpacaHandle's kinds
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@EVERY_KIND
async def test_the_tape_comes_from_the_kinds_intake_alone_and_is_resolved_at_most_once(alpaca, tape, kind):
    ack = await alpaca.handle.submit(kind.submit('r-1'))

    assert ack == SubmitAck(accepted=SubmitAccepted(feed=kind.feed, quota_key=kind.scope))
    [queued] = alpaca.queued
    assert queued.feed is kind.feed, 'the queued request carries a tape its kind did not give'
    assert queued.quota_scope == kind.scope
    assert isinstance(queued.payload, kind.work)
    # Bars decide their tape once per request; a no-tape kind never asks for one.
    assert tape.calls == (1 if kind.feed is not None else 0)


@pytest.mark.asyncio
@EVERY_KIND
@pytest.mark.parametrize('recovered', [False, True])
async def test_a_duplicate_id_is_answered_with_the_same_accept_ack_and_is_neither_queued_nor_charged_again(
    alpaca, kind, recovered
):
    first = await alpaca.handle.submit(kind.submit('r-1', recovered=recovered))
    again = await alpaca.handle.submit(kind.submit('r-1', recovered=recovered))

    assert first.accepted is not None
    assert again == first
    expected = [('charge', 'r-1'), ('queue', 'r-1')] if recovered else [('queue', 'r-1')]
    assert alpaca.order == expected, 'the duplicate was charged or queued again'
    assert alpaca.handle.scheduler.queued == 1
    assert alpaca.handle.active == 1


@pytest.mark.asyncio
@EVERY_KIND
async def test_a_recovered_request_is_charged_before_it_is_queued(alpaca, kind):
    await alpaca.handle.submit(kind.submit('r-1', recovered=True, attempt_count=2))

    assert alpaca.order == [('charge', 'r-1'), ('queue', 'r-1')]
    assert alpaca.queued[0].attempt_count == 2


@pytest.mark.asyncio
@EVERY_KIND
async def test_a_request_not_flagged_recovered_is_queued_and_never_charged(alpaca, kind):
    await alpaca.handle.submit(kind.submit('r-1', attempt_count=2))

    assert alpaca.order == [('queue', 'r-1')]


@pytest.mark.asyncio
@EVERY_KIND
@pytest.mark.parametrize('recovered', [False, True])
async def test_an_exhausted_request_is_reported_and_never_queued_and_charged_first_only_if_recovered(
    alpaca, kind, recovered
):
    store = AnsweringStore()
    handle = alpaca.handle
    handle.start(store, INSTANCE)
    try:
        ack = await handle.submit(kind.submit('r-1', recovered=recovered, attempt_count=MAX_ATTEMPTS))
        report = await store.first()
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert ack == SubmitAck(accepted=SubmitAccepted(feed=kind.feed, quota_key=kind.scope))
    expected = [('charge', 'r-1'), ('exhausted', 'r-1')] if recovered else [('exhausted', 'r-1')]
    assert alpaca.order == expected, 'not reported exactly once, never queued, charged first only if recovered'
    assert alpaca.queued == []
    assert report.header.request_id == 'r-1'
    assert report.header.feed is kind.feed


# ---------------------------------------------------------------------------------------------
# One active set, shared by every kind
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@EVERY_KIND
async def test_finished_frees_the_id_so_the_same_id_is_queued_afresh(alpaca, kind):
    handle = alpaca.handle

    await handle.submit(kind.submit('r-1'))
    assert handle.active == 1
    held = await asyncio.wait_for(handle.scheduler.take(), GUARD_S)

    handle.finished(held)

    assert handle.active == 0, 'finished did not free the id from the active set'
    await handle.submit(kind.submit('r-1'))
    assert alpaca.order == [('queue', 'r-1'), ('queue', 'r-1')], 'a finished id was answered as a duplicate'
    assert handle.active == 1


@pytest.mark.asyncio
async def test_active_counts_both_kinds_in_one_set_and_finished_frees_each(alpaca):
    handle = alpaca.handle

    await handle.submit(bars_submit('b-1'))
    await handle.submit(asset_list_submit('a-1'))
    assert handle.active == 2, 'active does not count both kinds'

    by_id = {request.request_id: request for request in alpaca.queued}
    handle.finished(by_id['a-1'])
    assert handle.active == 1
    handle.finished(by_id['b-1'])
    assert handle.active == 0


@pytest.mark.asyncio
async def test_the_executor_finishing_a_request_frees_its_id_on_the_handle(alpaca):
    """The executor is wired to the handle's own finished, for a no-tape kind as for bars (end to end)."""
    store = AnsweringStore()
    handle = alpaca.handle
    handle.start(store, INSTANCE)
    try:
        await handle.submit(asset_list_submit('a-1', attempt_count=MAX_ATTEMPTS))
        await handle.submit(bars_submit('b-1', attempt_count=MAX_ATTEMPTS))
        assert handle.active == 2
        for _ in range(400):
            if handle.active == 0:
                break
            await asyncio.sleep(0)
    finally:
        await asyncio.wait_for(handle.stop(), GUARD_S)

    assert len(store.received) == 2
    assert handle.active == 0, 'an acknowledged exhausted report did not free its id'
