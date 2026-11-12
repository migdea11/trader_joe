"""The verdict waiter, await_verdict (P35, tj-7zjov1.28): POST waits on the REQUEST ROW, never on an answer ingest got.

THE DESIGN: the bead's acceptance (POST awaits the verdict up to the fetch deadline; a POST whose request fails
expired returns that failure), the description's R4 ruling and its assumption (a request still open at the deadline
returns the existing deadline error and CONTINUES, its outcome in the run history), architect notes B (the served
range comes from the finishing attempt row, never rebuilt from the request) and E (the waiter never judges a request
by an answer ingest received; it reads the request row after the commit), and the store CLAUDE.md: EXPIRED is
DEADLINE (504), PERMANENT is VENDOR_REJECTED (422), exhausted attempts VENDOR_UNAVAILABLE (503);
STORE_VERDICT_DEADLINE_SECONDS defaults to the 300 s fetch deadline, a non-positive or malformed value is that default.

THE CLOCK AND THE SLEEP ARE INJECTED, so nothing here waits: the fake sleep advances the fake clock, and it is also
where a test plays ingest delivering a report (nothing interleaves on the one SQLite connection).
"""

import inspect
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import REASONS, ExogenousError, InvalidRequestError, Reason
from data.store.app.database.crud import ingest_request as request_crud
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.ingest import data_action_request
from data.store.app.ingest.data_action_request import (
    DEFAULT_VERDICT_DEADLINE_S,
    DEFAULT_VERDICT_POLL_S,
    VERDICT_DEADLINE_ENV,
    VERDICT_POLL_ENV,
    StoredDataset,
    await_verdict,
    verdict_deadline_seconds,
    verdict_poll_seconds,
)
from data.store.tests.delivery_reports import report, with_trailer
from data.store.tests.request_path_harness import RUNS, START, Harness, make_engine
from schemas.data_ingest.delivery import DeliveryErrorClass, DeliveryOutcome
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = await make_engine(monkeypatch)
    try:
        yield Harness(engine, monkeypatch)
    finally:
        await engine.dispose()


async def _queued_request(harness: Harness, *, dataset: bool = True) -> uuid.UUID:
    dataset_id = await harness.entry() if dataset else None
    async with AsyncSession(harness.engine, expire_on_commit=False) as db:
        record = await request_crud.create_request(
            db,
            request_crud.NewIngestRequest(
                kind=IngestRequestKind.BARS,
                source=DataSource.ALPACA_API,
                client_label='owner',
                dataset_id=dataset_id,
                asset_symbol='VFV',
                asset_type=AssetType.STOCK,
                data_types=(DataType.MARKET_ACTIVITY.value,),
                granularity=Granularity.ONE_DAY,
                update_type=UpdateType.DAILY,
                feed=Feed.IEX,
                requested_start=START,
                expiry_type=ExpiryType.ROLLING,
            ),
        )
    await harness.set_request(record.id, state=IngestRequestState.QUEUED, instance_id='instance-a')
    return record.id


class Clock:
    """A monotonic clock the fake sleep advances. Each sleep first runs the next scripted step (ingest's report)."""

    def __init__(self, steps: list[Callable[[], Awaitable[object]]] | None = None, deadline: float = math.inf):
        self.now = 1000.0
        self.slept: list[float] = []
        self.steps = list(steps or [])
        self.deadline = deadline

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds > 0, 'a sleep of nothing would spin'
        self.slept.append(seconds)
        self.now += seconds
        assert self.now <= self.deadline + 1e-9, f'the waiter slept past its deadline ({self.now} > {self.deadline})'
        if self.steps:
            await self.steps.pop(0)()


async def _wait(request_id: uuid.UUID, clock: Clock, *, deadline_s: float = 30, poll_s: float = 1) -> StoredDataset:
    clock.deadline = clock.now + deadline_s
    return await await_verdict(request_id, deadline_s=deadline_s, poll_s=poll_s, clock=clock, sleep=clock.sleep)


# --------------------------------------------------------------------------------------------- the deadline


async def test_a_request_still_open_at_the_deadline_raises_deadline_and_keeps_running(harness):
    """R4 option (a): the existing deadline error, and the request is NOT failed: it continues."""
    request_id = await _queued_request(harness)
    before = tuple(await harness.request_row(request_id))
    clock = Clock()

    with pytest.raises(ExogenousError) as caught:
        await _wait(request_id, clock, deadline_s=10, poll_s=3)

    assert caught.value.reason is Reason.DEADLINE
    assert REASONS[Reason.DEADLINE].http_status == 504
    assert 'keeps running' in caught.value.detail
    assert clock.slept == [3, 3, 3, 1], 'polls every interval and never sleeps past the deadline'
    assert tuple(await harness.request_row(request_id)) == before, 'the request is left exactly as it was'


async def test_the_waiter_never_sleeps_past_the_deadline_when_the_interval_is_longer(harness):
    request_id = await _queued_request(harness)
    clock = Clock()

    with pytest.raises(ExogenousError):
        await _wait(request_id, clock, deadline_s=2, poll_s=60)

    assert clock.slept == [2]


async def test_a_request_that_finishes_inside_the_deadline_is_answered_without_waiting_it_out(harness):
    request_id = await _queued_request(harness)
    clock = Clock([lambda: harness.deliver(report(request_id))])

    stored = await _wait(request_id, clock, deadline_s=30, poll_s=1)

    assert stored.data_points == 2
    assert clock.slept == [1], 'answered on the read after the report committed'


async def test_a_request_already_finished_is_answered_without_sleeping(harness):
    request_id = await _queued_request(harness)
    await harness.deliver(report(request_id))
    clock = Clock()

    await _wait(request_id, clock)

    assert clock.slept == []


async def test_the_deadline_defaults_to_the_300_second_verdict_deadline(monkeypatch):
    # The value is unchanged from the FetchDataset client's DEFAULT_FETCH_DEADLINE_S, which this default was until that
    # client went (tj-7zjov1.272): the store owns its own constant now, and both it and the read are pinned to 300.
    monkeypatch.delenv(VERDICT_DEADLINE_ENV, raising=False)
    assert DEFAULT_VERDICT_DEADLINE_S == 300.0
    assert verdict_deadline_seconds() == 300.0


@pytest.mark.parametrize('value', ['0', '-5', 'nan', 'inf', 'soon'])
async def test_a_non_positive_or_malformed_deadline_is_the_default(monkeypatch, value):
    monkeypatch.setenv(VERDICT_DEADLINE_ENV, value)
    assert verdict_deadline_seconds() == DEFAULT_VERDICT_DEADLINE_S == 300.0


async def test_the_store_request_path_no_longer_takes_its_deadline_from_the_fetch_client():
    # The FetchDataset client module is removed in a later step (tj-7zjov1.280); the request path must not import it,
    # or that removal would break the store at import time.
    assert not hasattr(data_action_request, 'DEFAULT_FETCH_DEADLINE_S')
    source = inspect.getsource(data_action_request)
    assert 'common.rpc.clients.ingest_fetch' not in source


async def test_a_set_deadline_is_read(monkeypatch):
    monkeypatch.setenv(VERDICT_DEADLINE_ENV, '42.5')
    assert verdict_deadline_seconds() == 42.5


@pytest.mark.parametrize(('value', 'expected'), [(None, DEFAULT_VERDICT_POLL_S), ('0', 0.25), ('-1', 0.25), ('2', 2.0)])
async def test_the_poll_interval_defaults_to_a_quarter_second(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(VERDICT_POLL_ENV, raising=False)
    else:
        monkeypatch.setenv(VERDICT_POLL_ENV, value)
    assert DEFAULT_VERDICT_POLL_S == 0.25
    assert verdict_poll_seconds() == expected


async def test_with_no_deadline_given_the_waiter_reads_the_setting(harness, monkeypatch):
    monkeypatch.setenv(VERDICT_DEADLINE_ENV, '5')
    monkeypatch.setenv(VERDICT_POLL_ENV, '2')
    request_id = await _queued_request(harness)
    clock = Clock(deadline=1005.0)

    with pytest.raises(ExogenousError):
        await await_verdict(request_id, clock=clock, sleep=clock.sleep)

    assert clock.slept == [2, 2, 1]


# --------------------------------------------------------------------------------------------- the failures


@pytest.mark.parametrize(
    ('finish', 'reason', 'status'),
    [
        (lambda rid: report(rid, DeliveryOutcome.EXPIRED), Reason.DEADLINE, 504),
        (
            lambda rid: report(rid, DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT, detail='unknown'),
            Reason.VENDOR_REJECTED,
            422,
        ),
        (lambda rid: report(rid, DeliveryOutcome.FAILED, last_attempt=True), Reason.VENDOR_UNAVAILABLE, 503),
        (lambda rid: report(rid, DeliveryOutcome.EXHAUSTED), Reason.VENDOR_UNAVAILABLE, 503),
    ],
    ids=['expired', 'permanent', 'last-attempt', 'exhausted'],
)
async def test_a_failed_request_is_answered_with_its_causes_error(harness, finish, reason, status):
    request_id = await _queued_request(harness)
    clock = Clock([lambda: harness.deliver(finish(request_id))])

    with pytest.raises(ExogenousError) as caught:
        await _wait(request_id, clock)

    assert caught.value.reason is reason
    assert REASONS[reason].http_status == status


async def test_a_permanent_failure_carries_the_vendors_sentence(harness):
    request_id = await _queued_request(harness)
    await harness.deliver(
        report(request_id, DeliveryOutcome.FAILED, error_class=DeliveryErrorClass.PERMANENT, detail='symbol unknown')
    )

    with pytest.raises(ExogenousError) as caught:
        await _wait(request_id, Clock())

    assert 'symbol unknown' in caught.value.detail


async def test_an_expired_request_says_nothing_was_fetched(harness):
    request_id = await _queued_request(harness)
    await harness.deliver(report(request_id, DeliveryOutcome.EXPIRED))

    with pytest.raises(ExogenousError) as caught:
        await _wait(request_id, Clock())

    assert 'expired' in caught.value.detail and 'nothing was fetched' in caught.value.detail


async def test_a_request_the_store_does_not_hold_is_not_found(harness):
    with pytest.raises(InvalidRequestError) as caught:
        await _wait(uuid.uuid4(), Clock())
    assert caught.value.reason is Reason.NOT_FOUND


# --------------------------------------------------------------------------------------------- note B: the answer is
# the finishing attempt row's


async def test_the_answer_is_the_finishing_attempts_bars_and_served_range_not_the_requests_window(harness):
    """The answer is the finishing attempt row's, as stored.

    Attempt 1 failed, attempt 2 succeeded with its own window, a late attempt 3 failed after the finish: the answer
    is attempt 2's bars and served range, copied as stored.
    """
    request_id = await _queued_request(harness)
    served = ServedRange(start=START + timedelta(hours=3, minutes=1), end=START + timedelta(days=17, minutes=2))
    await harness.deliver(report(request_id, DeliveryOutcome.FAILED))
    await harness.deliver(
        with_trailer(
            report(request_id, attempt_no=2, parts=[BarPage(bars=[report(request_id).parts[0].bars[0]])]),
            served_range=served,
        )
    )
    await harness.deliver(report(request_id, DeliveryOutcome.FAILED, attempt_no=3))

    stored = await _wait(request_id, Clock())

    assert stored == StoredDataset(data_points=1, served_range=served)


async def test_a_finished_request_whose_attempt_row_has_no_served_range_is_a_peer_protocol_error(harness):
    """A finished request with no served range on its attempt row is reported, never invented.

    A SUCCEEDED bars report without one is refused (note G), so this is a row written some other way: the waiter
    reports it, never invents a range from the request.
    """
    request_id = await _queued_request(harness)
    await harness.deliver(report(request_id))
    async with AsyncSession(harness.engine) as db:
        await db.execute(
            RUNS.update().where(RUNS.c.request_id == request_id).values(served_start=None, served_end=None)
        )
        await db.commit()

    with pytest.raises(ExogenousError) as caught:
        await _wait(request_id, Clock())

    assert caught.value.reason is Reason.PEER_PROTOCOL_ERROR


async def test_e_the_waiter_judges_by_the_request_row_not_by_what_ingest_was_answered(harness):
    """The waiter judges by the request row, never by an answer ingest received.

    Note E: a late redelivery of attempt 1 (stored CONTINUE) after the request finished changes nothing the waiter
    reads. Here the request is finished by attempt 2; ingest is answered FINAL for the redelivery and the waiter
    answers from the rows.
    """
    request_id = await _queued_request(harness)
    await harness.deliver(report(request_id, DeliveryOutcome.FAILED))
    await harness.deliver(report(request_id, attempt_no=2))
    await harness.deliver(report(request_id, DeliveryOutcome.FAILED))

    stored = await _wait(request_id, Clock())

    assert stored.data_points == 2
