"""One attempt at an asset-list request: BrokerRead in, an attempt report out.

The AttemptRunner of the asset_list kind (scheduler/executor.py), the sibling of BarsAttemptRunner. It calls
BrokerRead.get_assets once and turns the answer into the report: SUCCEEDED with every asset carried raw in one
AssetRecords part (the delivery mapper splits it to the wire bounds), or FAILED with the error class the executor's
retry gate reads, the class taken from the typed error exactly as a bars attempt takes it.

NOTHING IS INTERPRETED. No active, tradable or exchange judgement is made here or in the reader: every asset the
vendor listed is passed on, the store judges. The report has no feed (an asset list has no tape), no served range
and no vendor asset id (the attempt has no single instrument). Its quota_key is the TRADING scope, the one the list
was charged to; the data API's scope is never touched.
"""

from dataclasses import dataclass

from common.errors.vocabulary import TraderJoeError
from data.ingest.app.brokers.bars_attempt import error_class
from data.ingest.app.brokers.interface import AssetListFailure, AssetListQuery, BrokerRead, CallTally
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler.executor import AttemptResult
from data.ingest.app.scheduler.queue import ScheduledRequest
from schemas.data_ingest.delivery import (
    AssetRecord,
    AssetRecords,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryTrailer,
    VendorCallStats,
)
from schemas.data_ingest.submit import AssetListRequest


@dataclass(frozen=True)
class AssetListWork:
    """The payload of an asset-list ScheduledRequest.

    Attributes:
        request (AssetListRequest): Which list to fetch.
        priority (RequestPriority): The rate budget's priority class for the vendor call.
    """

    request: AssetListRequest
    priority: RequestPriority


class AssetListAttemptRunner:
    """Runs one attempt of an asset-list request through a BrokerRead. Conforms to scheduler.executor.AttemptRunner."""

    def __init__(self, reader: BrokerRead) -> None:
        """Bind the runner to the broker's reader.

        Args:
            reader (BrokerRead): What the attempt reads the list from.
        """
        self.__reader = reader

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        """Run one attempt: one get_assets.

        Args:
            request (ScheduledRequest): A request whose payload is AssetListWork. Its ready_by bounds the rate wait.
            attempt_no (int): The attempt's number; carried by the executor's header, not read here.

        Returns:
            AttemptResult: SUCCEEDED with the records, or FAILED with its class and, for a rate limit, the reset.
        """
        work: AssetListWork = request.payload
        tally = CallTally()
        response = await self.__reader.get_assets(
            AssetListQuery(
                asset_type=work.request.asset_type,
                status=work.request.status,
                priority=work.priority,
                deadline=request.ready_by,
                tally=tally,
            )
        )
        if isinstance(response, AssetListFailure):
            return self.__failed(request, response.error, tally)
        records = [
            AssetRecord(
                vendor_asset_id=asset.vendor_asset_id,
                symbol=asset.symbol,
                raw_payload=asset.raw_payload,
                active=asset.active,
                tradable=asset.tradable,
                exchange=asset.exchange,
                name=asset.name,
            )
            for asset in response.assets
        ]
        return AttemptResult(
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.SUCCEEDED, vendor_stats=self.__stats(tally), as_of=response.as_of
            ),
            parts=[AssetRecords(records=records)] if records else [],
            quota_key=request.quota_scope,
        )

    def __failed(self, request: ScheduledRequest, error: TraderJoeError, tally: CallTally) -> AttemptResult:
        klass = error_class(error)
        return AttemptResult(
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.FAILED,
                error_class=klass,
                reason=error.reason,
                detail=error.detail,
                vendor_stats=self.__stats(tally),
            ),
            quota_key=request.quota_scope,
            reset_at=error.reset_at if klass is DeliveryErrorClass.RATE_LIMITED else None,
        )

    @staticmethod
    def __stats(tally: CallTally) -> VendorCallStats:
        return VendorCallStats(
            calls=tally.calls,
            pages=tally.pages,
            throttled_429=tally.throttled_429,
            retries=tally.retries,
            first_call_at=tally.first_call_at,
            last_call_at=tally.last_call_at,
        )
