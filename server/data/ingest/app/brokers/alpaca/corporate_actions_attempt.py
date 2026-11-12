"""One attempt at a corporate-actions request: BrokerRead in, an attempt report out.

The AttemptRunner of the corporate_actions kind (scheduler/executor.py), the sibling of AssetListAttemptRunner. It
calls BrokerRead.get_corporate_actions once and turns the answer into the report: SUCCEEDED with every action carried
raw in one CorporateActionRecords part (the delivery mapper splits it to the wire bounds), or FAILED with the error
class the executor's retry gate reads, the class taken from the typed error exactly as a bars attempt takes it.

NOTHING IS INTERPRETED. No type is dropped and no date is judged here or in the reader: every action the vendor listed
is passed on, the store judges. The report has no feed (a corporate action has no tape) and no vendor asset id (the
attempt has no single instrument). Its quota_key is the DATA scope, the one the pages were charged to.

THE SERVED RANGE of a SUCCEEDED report, an empty success included, is the requested window exactly as given (start
inclusive, end exclusive), never the whole days the read sent to the vendor. It is PROVENANCE: the window this attempt
answered for. It is not a completeness claim: the vendor's date filtering is unconfirmed and records can appear late, so
no reader may treat it as every action in the window being stored. A FAILED report carries none.
"""

from dataclasses import dataclass

from common.errors.vocabulary import TraderJoeError
from data.ingest.app.brokers.bars_attempt import error_class
from data.ingest.app.brokers.interface import BrokerRead, CallTally, CorporateActionsFailure, CorporateActionsQuery
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler.executor import AttemptResult
from data.ingest.app.scheduler.queue import ScheduledRequest
from schemas.data_ingest import fetch_dataset as domain
from schemas.data_ingest.delivery import (
    CorporateActionRecord,
    CorporateActionRecords,
    DeliveryErrorClass,
    DeliveryOutcome,
    DeliveryTrailer,
    VendorCallStats,
)
from schemas.data_ingest.submit import CorporateActionsRequest


@dataclass(frozen=True)
class CorporateActionsWork:
    """The payload of a corporate-actions ScheduledRequest.

    Attributes:
        request (CorporateActionsRequest): Which symbol and window to fetch.
        priority (RequestPriority): The rate budget's priority class for the vendor calls.
    """

    request: CorporateActionsRequest
    priority: RequestPriority


class CorporateActionsAttemptRunner:
    """Runs one attempt of a corporate-actions request through a BrokerRead. Conforms to scheduler.executor.AttemptRunner."""

    def __init__(self, reader: BrokerRead) -> None:
        """Bind the runner to the broker's reader.

        Args:
            reader (BrokerRead): What the attempt reads the actions from.
        """
        self.__reader = reader

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        """Run one attempt: one get_corporate_actions, every page of it.

        Args:
            request (ScheduledRequest): A request whose payload is CorporateActionsWork. Its ready_by bounds the rate
                wait.
            attempt_no (int): The attempt's number; carried by the executor's header, not read here.

        Returns:
            AttemptResult: SUCCEEDED with the records, or FAILED with its class and, for a rate limit, the reset.
        """
        work: CorporateActionsWork = request.payload
        tally = CallTally()
        response = await self.__reader.get_corporate_actions(
            CorporateActionsQuery(
                symbol=work.request.asset_symbol,
                start=work.request.start,
                end=work.request.end,
                priority=work.priority,
                deadline=request.ready_by,
                tally=tally,
            )
        )
        if isinstance(response, CorporateActionsFailure):
            return self.__failed(request, response.error, tally)
        records = [
            CorporateActionRecord(
                vendor_action_id=action.vendor_action_id,
                action_type=action.action_type,
                ex_date=action.ex_date,
                record_date=action.record_date,
                payable_date=action.payable_date,
                process_date=action.process_date,
                effective_date=action.effective_date,
                identifiers=dict(action.identifiers),
                raw_payload=action.raw_payload,
            )
            for action in response.actions
        ]
        return AttemptResult(
            trailer=DeliveryTrailer(
                outcome=DeliveryOutcome.SUCCEEDED,
                vendor_stats=self.__stats(tally),
                served_range=domain.ServedRange(start=work.request.start, end=work.request.end),
                as_of=response.as_of,
            ),
            parts=[CorporateActionRecords(records=records)] if records else [],
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
