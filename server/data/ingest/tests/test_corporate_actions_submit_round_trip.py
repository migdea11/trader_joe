"""A corporate-actions Submit end to end on the fake stack: the Submit handler, FakeIngest, FakeStore (P13s).

Bead tj-7zjov1.124. Design: the architect's ruling of 22:05 UTC on the bead (option A: a SUCCEEDED corporate-actions
report, an empty one included, names the requested window exactly as given as its served range, end exclusive, never
the whole days the read sends; FAILED, EXPIRED and EXHAUSTED name none; tests/fakes/ingest.py needs no change) and ADR
tj-26jt9w addendum (9) b (a SUCCEEDED attempt's served range is stored on its attempt row).

WHAT IS REAL. The production IngestSubmitHandler routes the request to FakeIngest's handle, which is the production
BrokerHandle, Scheduler, AckGatedExecutor and CorporateActionsAttemptRunner around a paced FakeRead built with the
world (tests/fakes/market_data.py). The store is FakeStore in process: it models the verdicts, not the store's rules,
so what the REAL intake does with this report on Postgres (the attempt row's served_start and served_end, and rule (i)
refusing the report without a served range) is tests/system/test_corporate_actions_submit_pg.py's. What this file
proves is the ingest half: in fake mode a corporate_actions Submit is accepted, read, and delivered with the requested
window as its served range, and the store finishes it.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from routers.data_ingest.submit_handler import IngestSubmitHandler
from schemas.data_ingest.delivery import (
    AttemptDelivery,
    CorporateActionRecords,
    DeliverAttemptResponse,
    DeliveryOutcome,
    DeliveryVerdict,
)
from schemas.data_ingest.fetch_dataset import ServedRange
from schemas.data_ingest.submit import (
    AssetListRequest,
    AssetListStatus,
    CorporateActionsRequest,
    SubmitAck,
    SubmitRequest,
)
from tests.fakes.ingest import FakeIngest
from tests.fakes.market_data import WORLD
from tests.fakes.store import FakeStore


pytestmark = pytest.mark.data_ingest

# Every wait for the code under test is bounded by this, so a regression fails rather than hangs.
GUARD_S = 10.0
# The world's XLNX into AMD stock merger has its process date on 2022-02-14; this window holds it and starts and ends
# inside a day, so a served range copied from the whole days the read sends would differ from the request.
WITH_MERGER = (datetime(2022, 2, 1, 9, 30, tzinfo=UTC), datetime(2022, 2, 15, 16, 0, tzinfo=UTC))
# A window in which the world serves XLNX no action: an empty success.
NOTHING = (datetime(2023, 5, 1, 9, 30, tzinfo=UTC), datetime(2023, 5, 3, 16, 0, tzinfo=UTC))


def actions_submit(request_id: str, window: tuple[datetime, datetime]) -> SubmitRequest:
    start, end = window
    return SubmitRequest(
        request_id=request_id,
        owner='world-manager',
        ready_by=datetime.now(UTC) + timedelta(minutes=5),
        corporate_actions=CorporateActionsRequest(
            source=DataSource.ALPACA_API, asset_symbol='XLNX', start=start, end=end
        ),
    )


class SignallingStore(FakeStore):
    """FakeStore that signals each answered delivery, so a test waits on the answer rather than a sleep."""

    def __init__(self) -> None:
        super().__init__()
        self.answered = asyncio.Event()

    async def deliver(self, attempt: AttemptDelivery) -> DeliverAttemptResponse:
        answer = await super().deliver(attempt)
        self.answered.set()
        return answer


@pytest.mark.asyncio
@pytest.mark.parametrize(('window', 'records'), [(WITH_MERGER, 1), (NOTHING, 0)], ids=['with-records', 'empty-success'])
async def test_a_corporate_actions_submit_is_delivered_with_the_requested_window_as_its_served_range(window, records):
    store = SignallingStore()
    ingest = FakeIngest(store, world=WORLD)
    handler = IngestSubmitHandler({DataSource.ALPACA_API: ingest.handle}, ingest.identity.instance_id)

    async def submit(request: SubmitRequest) -> SubmitAck:
        return (await handler.submit(request)).ack

    async with ingest:
        await ingest.announce()
        ack = await store.submit(submit, actions_submit('ca-1', window))
        await asyncio.wait_for(store.answered.wait(), GUARD_S)

    assert ack.refused is None and ack.accepted.feed is None
    assert store.is_finished('ca-1')
    [written] = store.written
    report = written.attempt
    assert report.header.request_id == 'ca-1'
    assert report.trailer.outcome is DeliveryOutcome.SUCCEEDED, report.trailer.detail
    assert report.trailer.served_range == ServedRange(start=window[0], end=window[1])
    assert written.answer.verdict is DeliveryVerdict.FINAL
    delivered = [record for part in report.parts if isinstance(part, CorporateActionRecords) for record in part.records]
    assert len(delivered) == records
    assert [call[0] for call in ingest.reader.calls] == ['corporate_actions']


@pytest.mark.asyncio
async def test_an_asset_list_submit_is_refused_on_the_fake_stack_and_reaches_no_reader():
    store = FakeStore()
    ingest = FakeIngest(store, world=WORLD)
    handler = IngestSubmitHandler({DataSource.ALPACA_API: ingest.handle}, ingest.identity.instance_id)
    listing = SubmitRequest(
        request_id='al-1',
        owner='world-manager',
        asset_list=AssetListRequest(
            source=DataSource.ALPACA_API, asset_type=AssetType.STOCK, status=AssetListStatus.ACTIVE
        ),
    )

    with pytest.raises(InvalidRequestError) as raised:
        await handler.submit(listing)

    assert raised.value.reason is Reason.INVALID_REQUEST
    assert ingest.scheduler.queued == 0
    assert ingest.reader.calls == ()
