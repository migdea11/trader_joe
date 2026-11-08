"""The Submit path's pre-ack refusals, carried over from the deleted FetchDataset handler's tests (tj-7zjov1.276).

test_fetch_dataset_handler.py pinned, through IngestFetchHandler, that what bars_validation.py can decide is
refused BEFORE the ack and before any vendor call -- quotes, trades, crypto and option -- and that a source with
no reader raised before the ack. tj-7zjov1.275 deleted that handler (owner ruling 2026-11-02 on tj-7zjov1.18), so
Submit is the only caller of validate_bars_request, and these are its cases on that path. test_ack_gated_retry.py
pins quote, empty data types and crypto against a real AlpacaHandle and is left as it is (a KEEP GREEN file);
this adds the cases only the deleted file reached: TRADE and OPTION, the combination of a supported data type
with a refused one, and the unknown source.

The handle is a recording double: every refusal here is decided before the handler touches it, and the double
having been asked nothing is the "before the ack" half.
"""

from datetime import UTC, datetime, timedelta

import pytest

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from routers.data_ingest.submit_handler import IngestSubmitHandler
from schemas.data_ingest.submit import BarsRequest, SubmitAccepted, SubmitAck, SubmitRequest


pytestmark = pytest.mark.data_ingest

INSTANCE = 'instance-under-test'
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
END = START + timedelta(hours=1)
ACK = SubmitAck(accepted=SubmitAccepted(feed=Feed.IEX, quota_key='alpaca-test'))


class RecordingHandle:
    """A BrokerHandle by the one method the Submit handler calls; records and accepts."""

    def __init__(self) -> None:
        self.submitted: list[SubmitRequest] = []

    async def submit(self, request: SubmitRequest) -> SubmitAck:
        """Record and accept.

        Args:
            request: The request.

        Returns:
            SubmitAck: ACK.
        """
        self.submitted.append(request)
        return ACK


def bars_request(**overrides) -> BarsRequest:
    fields = {
        'source': DataSource.ALPACA_API,
        'asset_symbol': 'VFV',
        'asset_type': AssetType.STOCK,
        'data_types': [DataType.MARKET_ACTIVITY],
        'granularity': Granularity.THIRTY_MINUTES,
        'start': START,
        'end': END,
        'update_type': UpdateType.STATIC,
    }
    return BarsRequest(**{**fields, **overrides})


def submit_request(**overrides) -> SubmitRequest:
    return SubmitRequest(request_id='req-1', owner='rebalancer', bars=bars_request(**overrides))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'overrides',
    [
        pytest.param({'data_types': [DataType.TRADE]}, id='trade'),
        pytest.param({'data_types': [DataType.MARKET_ACTIVITY, DataType.QUOTE]}, id='bars-and-quote'),
        pytest.param({'data_types': [DataType.MARKET_ACTIVITY, DataType.TRADE]}, id='bars-and-trade'),
        pytest.param({'asset_type': AssetType.OPTION}, id='option'),
    ],
)
async def test_an_unservable_bars_request_is_refused_unsupported_before_the_handle_is_asked(overrides):
    handle = RecordingHandle()
    handler = IngestSubmitHandler({DataSource.ALPACA_API: handle}, INSTANCE)

    with pytest.raises(InvalidRequestError) as raised:
        await handler.submit(submit_request(**overrides))

    assert raised.value.reason is Reason.UNSUPPORTED_ASSET_TYPE
    assert handle.submitted == [], 'the request reached the handle, so it could have been acked and queued'


@pytest.mark.asyncio
async def test_a_servable_bars_request_reaches_the_handle_and_its_ack_is_answered_with_the_instance():
    """The control: the same handler and double accept a stock bars request, so the refusals above are the rule's."""
    handle = RecordingHandle()
    handler = IngestSubmitHandler({DataSource.ALPACA_API: handle}, INSTANCE)

    response = await handler.submit(submit_request())

    assert response.ack == ACK
    assert response.instance_id == INSTANCE
    assert handle.submitted == [submit_request()]


@pytest.mark.asyncio
async def test_a_source_with_no_handle_raises_before_any_handle_is_asked():
    handle = RecordingHandle()
    handler = IngestSubmitHandler({DataSource.ALPACA_API: handle}, INSTANCE)
    unserved = next(source for source in DataSource if source is not DataSource.ALPACA_API)

    with pytest.raises(NotImplementedError, match='data source not implemented'):
        await handler.submit(submit_request(source=unserved))

    assert handle.submitted == []
