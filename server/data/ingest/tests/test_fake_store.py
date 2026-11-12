"""FakeStore's own rules (tests/fakes/store.py), so a scenario that leans on them leans on something pinned.

Bead tj-7zjov1.24. The verdict follows the real intake's rule as data/store/CLAUDE.md states it (Delivery intake):
FINAL on a success, a PERMANENT refusal, last_attempt, EXPIRED, EXHAUSTED, a superseded instance or an already finished
request, else CONTINUE; idempotent on (request_id, attempt_no); EXPIRED and EXHAUSTED leave the count alone. The
request side re-submits what is open, flagged recovered, with the durable count (ADR tj-26jt9w addendum 5).
"""

from datetime import UTC, datetime

import pytest

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import ExogenousError, Reason
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    DeliveryErrorClass,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
    RegisterInstanceRequest,
)
from schemas.data_ingest.fetch_dataset import FetchRefused
from schemas.data_ingest.submit import AssetListRequest, AssetListStatus, SubmitAccepted, SubmitAck, SubmitRequest
from tests.fakes.store import FakeStore


pytestmark = pytest.mark.data_ingest

STARTED = datetime(2026, 10, 26, tzinfo=UTC)


def report(
    attempt_no: int,
    outcome: DeliveryOutcome = DeliveryOutcome.FAILED,
    error_class: DeliveryErrorClass | None = DeliveryErrorClass.TRANSIENT,
    *,
    request_id: str = 'r1',
    instance_id: str = 'i1',
    last_attempt: bool = False,
) -> AttemptDelivery:
    return AttemptDelivery(
        header=DeliveryHeader(request_id=request_id, instance_id=instance_id, attempt_no=attempt_no),
        trailer=DeliveryTrailer(
            outcome=outcome,
            error_class=error_class if outcome is DeliveryOutcome.FAILED else None,
            last_attempt=last_attempt,
        ),
    )


def request(request_id: str) -> SubmitRequest:
    return SubmitRequest(
        request_id=request_id,
        owner='rebalancer',
        asset_list=AssetListRequest(
            source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE
        ),
    )


class Ingest:
    def __init__(self, refuse: bool = False) -> None:
        self.received: list[SubmitRequest] = []
        self.refuse = refuse

    async def __call__(self, submitted: SubmitRequest) -> SubmitAck:
        self.received.append(submitted)
        if self.refuse:
            return SubmitAck(refused=FetchRefused(reason='INVALID_REQUEST', domain='trader_joe', detail='no'))
        return SubmitAck(accepted=SubmitAccepted(quota_key='Q'))


async def verdicts(store: FakeStore, *reports: AttemptDelivery) -> list[tuple[DeliveryVerdict, int]]:
    return [((answer := await store.deliver(each)).verdict, answer.attempt_count) for each in reports]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('delivered', 'expected'),
    [
        pytest.param(report(1), (DeliveryVerdict.CONTINUE, 1), id='transient-continues'),
        pytest.param(report(1, error_class=DeliveryErrorClass.RATE_LIMITED), (DeliveryVerdict.CONTINUE, 1), id='rate'),
        pytest.param(report(1, error_class=DeliveryErrorClass.PERMANENT), (DeliveryVerdict.FINAL, 1), id='permanent'),
        pytest.param(report(2, last_attempt=True), (DeliveryVerdict.FINAL, 2), id='last-attempt'),
        pytest.param(report(1, DeliveryOutcome.SUCCEEDED), (DeliveryVerdict.FINAL, 1), id='success'),
        pytest.param(report(1, DeliveryOutcome.EXPIRED), (DeliveryVerdict.FINAL, 0), id='expired-not-counted'),
        pytest.param(report(4, DeliveryOutcome.EXHAUSTED), (DeliveryVerdict.FINAL, 0), id='exhausted-not-counted'),
    ],
)
async def test_the_verdict_and_the_count_of_a_first_report(delivered, expected):
    store = FakeStore()

    assert await verdicts(store, delivered) == [expected]


@pytest.mark.asyncio
async def test_a_duplicate_is_answered_from_the_stored_answer_and_written_once():
    store = FakeStore()

    answers = await verdicts(store, report(1), report(1), report(2, DeliveryOutcome.SUCCEEDED), report(1))

    assert answers == [
        (DeliveryVerdict.CONTINUE, 1),
        (DeliveryVerdict.CONTINUE, 1),
        (DeliveryVerdict.FINAL, 2),
        (DeliveryVerdict.CONTINUE, 1),
    ]
    assert [entry.attempt.header.attempt_no for entry in store.written] == [1, 2]
    assert len(store.received) == 4


@pytest.mark.asyncio
async def test_a_report_for_a_finished_request_is_final_and_an_expiry_after_it_writes_nothing():
    store = FakeStore()

    answers = await verdicts(store, report(1, DeliveryOutcome.SUCCEEDED), report(2), report(3, DeliveryOutcome.EXPIRED))

    assert answers == [(DeliveryVerdict.FINAL, 1), (DeliveryVerdict.FINAL, 2), (DeliveryVerdict.FINAL, 2)]
    assert len(store.written) == 2


@pytest.mark.asyncio
async def test_a_superseded_instance_is_answered_final_and_its_inconclusive_failure_leaves_the_request_open():
    store = FakeStore()
    await store.register_instance(RegisterInstanceRequest(instance_id='old', started_at=STARTED))
    await store.register_instance(RegisterInstanceRequest(instance_id='new', started_at=STARTED))

    assert await verdicts(store, report(1, instance_id='old')) == [(DeliveryVerdict.FINAL, 1)]
    assert not store.is_finished('r1')
    assert await verdicts(store, report(2, instance_id='new')) == [(DeliveryVerdict.CONTINUE, 2)]
    assert store.live_instance == 'new'


@pytest.mark.asyncio
async def test_a_down_store_refuses_every_call_receives_nothing_and_keeps_its_records():
    store = FakeStore()
    await store.deliver(report(1))
    store.go_down()

    with pytest.raises(ExogenousError) as raised:
        await store.deliver(report(2))
    with pytest.raises(ExogenousError):
        await store.register_instance(RegisterInstanceRequest(instance_id='i2', started_at=STARTED))

    assert raised.value.reason is Reason.PEER_UNAVAILABLE
    assert (store.refused_while_down, len(store.received)) == (2, 1)
    store.come_up()
    assert store.count('r1') == 1


@pytest.mark.asyncio
async def test_recover_re_submits_only_open_requests_flagged_recovered_with_their_durable_count():
    store = FakeStore()
    first = Ingest()
    for request_id in ('done', 'counted', 'queued'):
        await store.submit(first, request(request_id))
    await store.deliver(report(1, DeliveryOutcome.SUCCEEDED, request_id='done'))
    await store.deliver(report(1, request_id='counted'))
    await store.deliver(report(2, request_id='counted'))

    second = Ingest()
    await store.recover(second)

    assert [(each.request_id, each.recovered, each.attempt_count) for each in second.received] == [
        ('counted', True, 2),
        ('queued', True, 0),
    ]
    assert [each.recovered for each in first.received] == [False] * 3


@pytest.mark.asyncio
async def test_a_refused_submit_is_finished_and_never_recovered():
    store = FakeStore()
    await store.submit(Ingest(refuse=True), request('refused'))

    assert store.open_requests() == []
