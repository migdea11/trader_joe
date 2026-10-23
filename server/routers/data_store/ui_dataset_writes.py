"""The /ui/v1 write routes of the Data section: set or clear a dataset's expiry, and delete it (tj-grna9p.86).

BODIES ARE PROTOBUF CANONICAL JSON, parsed by ProtoBody into SetExpiryRequest / DeleteDatasetRequest and mapped
to plain values in ui_mapping. There is no pydantic body model; a malformed body or an unknown field
(expiryType, updateType included) is a 422 problem+json before anything else is looked at.

THE RULES, IN ORDER (architect ruling 2026-10-14): 422 for a bad body; 404 for an unknown dataset; 409
OVERRIDE_REQUIRED when the dataset's owner is not the operator and the override is missing, unconfirmed or
blank; an override sent for the operator's own dataset is ignored; 503 DATABASE_UNAVAILABLE when the audit
tables are missing and the write needs one (refused before any write); then the write, with its override-log
row, in one transaction, and the change event strictly after the commit. The 409's detail and metadata never carry
the owner.

SEMANTICS. Setting an expiry on a DAILY or STREAM dataset retires it at once (the scheduler stops) and clearing
it resumes it; on a STATIC dataset it sets the time. The expiry TYPE cannot be changed: the body has no field
for it. PATCH answers 200 with the updated DatasetSummary; DELETE answers 204 with no body.

BOTH ROUTES REQUIRE THE INSTANCE SECRET, as a per-route dependency like the /store writes. The existing /store
routes keep their owner-scoped writes unchanged.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.database.crud.stock.store_dataset_entry import operator_delete_entry, operator_set_expiry
from data.store.app.database.database import async_db
from data.store.app.dataset_catalog import dataset_detail
from data.store.app.events import DatasetChange, DatasetChanged, publish
from routers.common.instance_secret import require_instance_secret
from routers.common.proto_json import ProtoBody, ProtoJSONResponse, proto_route
from routers.data_store.app_endpoints import UiDatasetWritesInterface
from routers.data_store.ui_datasets import utc_now
from routers.data_store.ui_mapping import (
    DatasetSummaryMessage,
    DeleteDatasetRequestMessage,
    SetExpiryRequestMessage,
    dataset_summary_message,
    delete_request_body,
    delete_values,
    set_expiry_values,
)


router = APIRouter()


@router.patch(
    UiDatasetWritesInterface.PATCH_UI_DATASET_EXPIRY,
    dependencies=[Depends(require_instance_secret)],
    **proto_route(DatasetSummaryMessage),
)
async def set_dataset_expiry(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    body: Annotated[SetExpiryRequestMessage, Depends(ProtoBody(SetExpiryRequestMessage))],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """Set or clear a dataset's expiry, answering with the updated DatasetSummary.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        body: The new expiry (unset clears it) and the override.
        now: The instant freshness is computed at.

    Returns:
        ProtoJSONResponse: The DatasetSummary after the write.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown dataset; OVERRIDE_REQUIRED (409) for another owner's
            dataset without a confirmed override carrying a reason; DATABASE_UNAVAILABLE (503) when the audit
            tables are missing and the write needs one, refused before any write.
    """
    expiry, override = set_expiry_values(body)
    await operator_set_expiry(db, dataset_id, expiry, override)
    # AFTER the commit operator_set_expiry made, never inside it.
    publish(DatasetChanged(dataset_id, DatasetChange.UPDATED))
    detail = await dataset_detail(db, dataset_id, now)
    if detail is None:
        # Deleted between the write and this read.
        raise InvalidRequestError(Reason.NOT_FOUND, f'No dataset found with ID {dataset_id}')
    return ProtoJSONResponse(dataset_summary_message(detail))


@router.delete(
    UiDatasetWritesInterface.DELETE_UI_DATASET, dependencies=[Depends(require_instance_secret)], status_code=204
)
async def delete_dataset(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    body: Annotated[DeleteDatasetRequestMessage, Depends(delete_request_body)],
) -> Response:
    """Delete a dataset and its bars, recording the deletion. No body in the answer.

    The body is optional: the operator's own dataset needs no override.

    Args:
        db: The request's session.
        dataset_id: The dataset's id.
        body: The override, when the dataset is another owner's.

    Returns:
        Response: 204.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown dataset; OVERRIDE_REQUIRED (409) for another owner's
            dataset without a confirmed override carrying a reason; DATABASE_UNAVAILABLE (503) when the audit
            tables are missing and the write needs one, refused before any write.
    """
    await operator_delete_entry(db, dataset_id, delete_values(body))
    publish(DatasetChanged(dataset_id, DatasetChange.DELETED))
    return Response(status_code=204)
