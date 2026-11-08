"""THE DOMAIN SIDE of the Submit server seam (tj-7zjov1.48): hold the request, route it by source, answer at once.

common/rpc/ingest.py owns every generated message on this hop. This module never imports trader_joe.proto and
conforms STRUCTURALLY to common.rpc.ingest.SubmitHandler, by shape, so it does not import that module either.

THE SUBMIT ENQUEUES AND ANSWERS AT ONCE. Nothing here calls a vendor or waits for the store: the broker handle puts
the request on its scheduler's queue and the ack goes back with the instance id. The attempt, its retries and its
reports to the store happen behind the queue (data/ingest/app/brokers/handle.py). One handle serves each broker,
routed by the request's source. What is knowable before the vendor is checked here, before the ack, as FetchDataset
does (routers/data_ingest/bars_validation.py).

Only bars requests are served in this release. The asset list and corporate actions are request kinds the Alpaca
handle gains with P12 and P13 (tj-7zjov1.22, .23); until then they are refused as INVALID_REQUEST.
"""

from collections.abc import Mapping

from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from data.ingest.app.brokers.handle import BrokerHandle
from routers.data_ingest.bars_validation import validate_bars_request
from schemas.data_ingest.submit import RequestKind, SubmitRequest, SubmitResponse


class IngestSubmitHandler:
    """Serves Submit by handing each request to the broker handle for its source."""

    def __init__(self, handles: Mapping[DataSource, BrokerHandle], instance_id: str) -> None:
        """Bind the handler to the broker handles and the id of this ingest process.

        Args:
            handles (Mapping[DataSource, BrokerHandle]): One handle per supported broker. Read only.
            instance_id (str): The id this process minted at start; every answer carries it.
        """
        self._handles = handles
        self._instance_id = instance_id

    async def submit(self, request: SubmitRequest) -> SubmitResponse:
        """Hold one request and answer it.

        Args:
            request (SubmitRequest): The request as decoded.

        Returns:
            SubmitResponse: The handle's ack (accepted with the resolved feed and quota key, or refused for a
            feed this deployment cannot serve) and this process's instance id.

        Raises:
            InvalidRequestError: UNSUPPORTED_ASSET_TYPE or INVALID_REQUEST for a bars request this service cannot
                serve, and INVALID_REQUEST for a kind not served yet. All are known before any vendor call.
            NotImplementedError: If no handle serves the request's source.
        """
        if request.kind is not RequestKind.BARS or request.bars is None:
            raise InvalidRequestError(Reason.INVALID_REQUEST, f'{request.kind.value} requests are not served yet')
        bars = request.bars
        validate_bars_request(bars.data_types, bars.asset_type)
        handle = self._handles.get(bars.source)
        if handle is None:
            raise NotImplementedError(f'data source not implemented: source={bars.source!r}')
        return SubmitResponse(ack=await handle.submit(request), instance_id=self._instance_id)
