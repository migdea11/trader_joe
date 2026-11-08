"""The /ui/v1 panel routes: eight reads and three writes (tj-7zjov1.36, P24; ADR tj-26jt9w addenda 20, 21 and 22).

A PANEL is a named selection of one source's assets whose datasets the panel manager keeps. Panels are UI-ONLY (owner
ruling 2026-10-27): these routes are the whole panel surface, there is no client or SDK panel API, and no route here takes
a dataset owner. The messages are panels.proto's; it declares no service, so the paths are set in app_endpoints.py.

SERVED AS PROTOBUF CANONICAL JSON, each route declared with proto_route. Panel and dataset ids are UUIDs (422 when
malformed, 404 when unknown); {source} is the DataSource NAME with or without the wire prefix (UNSPECIFIED is a 422).

THE READS (R1 to R8) are open, like every /ui/v1 GET, read-only and record no usage. Their rules are in
data/store/app/panel_views.py and the statements in data/store/app/database/crud/panel_reads.py. limit defaults to 100
and is 1 to 500; a cursor is opaque and bound to its route and parent (a foreign one is a 422).

THE WRITES (W1 to W3) are POST only: each appends a row and a repeat appends another, so none is PUT or PATCH. Each is
behind the instance secret, takes a body parsed strictly by ProtoBody, and answers 201 with the new row. The actor is the
operator principal and the reason is optional (blank or absent is stored as none). THE VERSION ROW IS THE AUDIT: it is
append-only and carries who, why and when, so a panel write adds NO dataset_override_log row (addendum 20). Every store
refusal is INVALID_REQUEST, 422, with the store's message. Nothing is published on the events socket: there is no panel
event type. A create does not start a pass: the panel manager's tick runs the first one.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AwareDatetime, Field
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app import panel_views
from data.store.app.client_label import OPERATOR_PRINCIPAL
from data.store.app.database.crud import panel_reads
from data.store.app.database.crud.panel_reads import SettingsVersionRow, SourceVersionRow
from data.store.app.database.crud.panel_settings import (
    append_settings_version,
    append_source_settings_version,
    create_panel,
)
from data.store.app.database.database import async_db
from data.store.app.dataset_catalog import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from routers.common.instance_secret import require_instance_secret
from routers.common.proto_json import ProtoBody, ProtoJSONResponse, proto_route
from routers.data_store.app_endpoints import UiPanelsInterface, UiPanelWritesInterface
from routers.data_store.ui_datasets import _member_by_name, utc_now
from routers.data_store.ui_mapping import (
    AssetListHistoryMessage,
    CorporateActionEventPageMessage,
    CreatePanelRequestMessage,
    PanelBoundariesMessage,
    PanelDetailMessage,
    PanelListMessage,
    PanelMemberPageMessage,
    PanelSettingsVersionMessage,
    PanelSettingsVersionPageMessage,
    PanelSourceSettingsVersionMessage,
    PanelSourceSettingsVersionPageMessage,
    SavePanelSettingsRequestMessage,
    SaveSourceSettingsRequestMessage,
    asset_list_history_message,
    corporate_action_page_message,
    create_panel_values,
    panel_boundaries_message,
    panel_detail_message,
    panel_list_message,
    panel_member_page_message,
    panel_settings_version_message,
    panel_settings_version_page_message,
    panel_source_settings_version_message,
    panel_source_settings_version_page_message,
    save_panel_settings_values,
    save_source_settings_values,
)
from schemas.inbound_contract import InboundContract


router = APIRouter()

SourcePath = Annotated[DataSource, *_member_by_name(DataSource, 'DATA_SOURCE_'), Path()]


class UiPageQuery(InboundContract):
    """A keyset page.

    Attributes:
        cursor: The next_cursor of the previous page. Opaque.
        limit: The page size, 1 to 500.
    """

    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT


class UiNoQuery(InboundContract):
    """No parameters: a stray one is a 422, as on every route here that takes a query."""


class UiWindowQuery(InboundContract):
    """A half-open window [start, end), both required.

    Attributes:
        start: Included. Timezone-aware.
        end: Excluded. Timezone-aware; after start.
    """

    start: AwareDatetime
    end: AwareDatetime


def _panel_not_found(panel_id: UUID) -> InvalidRequestError:
    return InvalidRequestError(Reason.NOT_FOUND, f'No panel found with ID {panel_id}')


@router.get(UiPanelsInterface.GET_UI_PANELS, **proto_route(PanelListMessage))
async def list_panels(
    db: Annotated[AsyncSession, Depends(async_db)], _query: Annotated[UiNoQuery, Query()]
) -> ProtoJSONResponse:
    """R1. Every panel by name then id, with the enabled flag of its latest settings version.

    Args:
        db: The request's session.
        _query: No parameters are accepted.

    Returns:
        ProtoJSONResponse: A PanelList.
    """
    return ProtoJSONResponse(panel_list_message(await panel_views.list_panels(db)))


@router.get(UiPanelsInterface.GET_UI_PANEL, **proto_route(PanelDetailMessage))
async def get_panel(
    db: Annotated[AsyncSession, Depends(async_db)],
    panel_id: Annotated[UUID, Path()],
    _query: Annotated[UiNoQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """R2. A panel with its current settings and source settings versions (each with its effective pass) and its counts.

    Args:
        db: The request's session.
        panel_id: The panel.
        _query: No parameters are accepted.
        now: The instant member health is judged at.

    Returns:
        ProtoJSONResponse: A PanelDetail.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown panel.
    """
    detail = await panel_views.panel_detail(db, panel_id, now)
    if detail is None:
        raise _panel_not_found(panel_id)
    return ProtoJSONResponse(panel_detail_message(detail))


@router.get(UiPanelsInterface.GET_UI_PANEL_SETTINGS_VERSIONS, **proto_route(PanelSettingsVersionPageMessage))
async def get_panel_settings_versions(
    db: Annotated[AsyncSession, Depends(async_db)],
    panel_id: Annotated[UUID, Path()],
    request_query: Annotated[UiPageQuery, Query()],
) -> ProtoJSONResponse:
    """R3. A panel's settings versions, newest first, keyset on the version number, each with its effective pass.

    Args:
        db: The request's session.
        panel_id: The panel.
        request_query: The cursor and page size.

    Returns:
        ProtoJSONResponse: A PanelSettingsVersionPage.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown panel; INVALID_REQUEST (422) for a bad cursor.
    """
    page = await panel_views.settings_version_page(db, panel_id, cursor=request_query.cursor, limit=request_query.limit)
    if page is None:
        raise _panel_not_found(panel_id)
    return ProtoJSONResponse(panel_settings_version_page_message(page))


@router.get(
    UiPanelsInterface.GET_UI_PANEL_SOURCE_SETTINGS_VERSIONS, **proto_route(PanelSourceSettingsVersionPageMessage)
)
async def get_panel_source_settings_versions(
    db: Annotated[AsyncSession, Depends(async_db)], source: SourcePath, request_query: Annotated[UiPageQuery, Query()]
) -> ProtoJSONResponse:
    """R4. A source's settings versions, newest first; the effective pass is that of any panel on the source.

    A source with no version is an empty page, never a 404: that is how the create form learns the rates must be given.

    Args:
        db: The request's session.
        source: The source.
        request_query: The cursor and page size.

    Returns:
        ProtoJSONResponse: A PanelSourceSettingsVersionPage.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a bad cursor.
    """
    page = await panel_views.source_version_page(db, source, cursor=request_query.cursor, limit=request_query.limit)
    return ProtoJSONResponse(panel_source_settings_version_page_message(page))


@router.get(UiPanelsInterface.GET_UI_PANEL_BOUNDARIES, **proto_route(PanelBoundariesMessage))
async def get_panel_boundaries(
    db: Annotated[AsyncSession, Depends(async_db)],
    panel_id: Annotated[UUID, Path()],
    request_query: Annotated[UiWindowQuery, Query()],
) -> ProtoJSONResponse:
    """R5. Every marker of a panel in [start, end), oldest first: settings versions and operator collect stops and resumes.

    Args:
        db: The request's session.
        panel_id: The panel.
        request_query: The window; both bounds required, start before end.

    Returns:
        ProtoJSONResponse: A PanelBoundaries.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown panel; INVALID_REQUEST (422) when start is not before end.
    """
    view = await panel_views.boundaries(db, panel_id, request_query.start, request_query.end)
    if view is None:
        raise _panel_not_found(panel_id)
    return ProtoJSONResponse(panel_boundaries_message(view))


@router.get(UiPanelsInterface.GET_UI_PANEL_MEMBERS, **proto_route(PanelMemberPageMessage))
async def get_panel_members(
    db: Annotated[AsyncSession, Depends(async_db)],
    panel_id: Annotated[UUID, Path()],
    request_query: Annotated[UiPageQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """R6. A page of a panel's members, keyset over (symbol, vendor asset id), each with its dataset's last bar and health.

    Args:
        db: The request's session.
        panel_id: The panel.
        request_query: The cursor and page size.
        now: The instant health is judged at.

    Returns:
        ProtoJSONResponse: A PanelMemberPage.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown panel; INVALID_REQUEST (422) for a bad cursor.
    """
    page = await panel_views.member_page(db, panel_id, cursor=request_query.cursor, limit=request_query.limit, now=now)
    if page is None:
        raise _panel_not_found(panel_id)
    return ProtoJSONResponse(panel_member_page_message(page))


@router.get(UiPanelsInterface.GET_UI_PANEL_ASSET_LIST_HISTORY, **proto_route(AssetListHistoryMessage))
async def get_panel_asset_list_history(
    db: Annotated[AsyncSession, Depends(async_db)],
    panel_id: Annotated[UUID, Path()],
    request_query: Annotated[UiWindowQuery, Query()],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """R7. The asset-list history of the panel's source: one entry per session in [start, end), ascending, never per asset.

    Args:
        db: The request's session.
        panel_id: The panel.
        request_query: The window; both bounds required, start before end, at most 366 days.
        now: The instant a day is judged due or missing at.

    Returns:
        ProtoJSONResponse: An AssetListHistory.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown panel; INVALID_REQUEST (422) for a bad window or a source
            with no trading calendar.
    """
    view = await panel_views.asset_list_history(db, panel_id, request_query.start, request_query.end, now)
    if view is None:
        raise _panel_not_found(panel_id)
    return ProtoJSONResponse(asset_list_history_message(view))


@router.get(UiPanelsInterface.GET_UI_DATASET_CORPORATE_ACTIONS, **proto_route(CorporateActionEventPageMessage))
async def get_dataset_corporate_actions(
    db: Annotated[AsyncSession, Depends(async_db)],
    dataset_id: Annotated[UUID, Path()],
    request_query: Annotated[UiPageQuery, Query()],
) -> ProtoJSONResponse:
    """R8. A dataset's corporate-action observations, newest as_of first, a vendor revision being its own item.

    Matched by the dataset's source and symbol, whichever request delivered them, and not filtered by any window.

    Args:
        db: The request's session.
        dataset_id: The dataset.
        request_query: The cursor and page size.

    Returns:
        ProtoJSONResponse: A CorporateActionEventPage.

    Raises:
        InvalidRequestError: NOT_FOUND (404) for an unknown dataset; INVALID_REQUEST (422) for a bad cursor.
    """
    page = await panel_views.corporate_action_events(
        db, dataset_id, cursor=request_query.cursor, limit=request_query.limit
    )
    if page is None:
        raise InvalidRequestError(Reason.NOT_FOUND, f'No dataset found with ID {dataset_id}')
    return ProtoJSONResponse(corporate_action_page_message(page))


@router.post(
    UiPanelWritesInterface.POST_UI_PANELS,
    dependencies=[Depends(require_instance_secret)],
    status_code=201,
    **proto_route(PanelDetailMessage),
)
async def post_panel(
    db: Annotated[AsyncSession, Depends(async_db)],
    body: Annotated[CreatePanelRequestMessage, Depends(ProtoBody(CreatePanelRequestMessage))],
    now: Annotated[datetime, Depends(utc_now)],
) -> ProtoJSONResponse:
    """W1. Create a panel: its version 1 and, when given, its source's first settings, in one transaction.

    Does not start a pass; the detail has effective_pass unset and zero counts. The reason is optional.

    Args:
        db: The request's session.
        body: The panel, its first settings, its source and optionally the source's first settings.
        now: The instant the detail is judged at.

    Returns:
        ProtoJSONResponse: 201 with the created PanelDetail.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a bad body or any store refusal (a blank name, a name
            already used, a negative number, a source with no settings and none given, settings given for a source that
            has some). Nothing is written.
    """
    values = create_panel_values(body)
    panel, _ = await create_panel(
        db,
        name=values.name,
        source=values.source,
        settings=values.settings,
        actor=OPERATOR_PRINCIPAL,
        reason=values.reason,
        source_settings=values.source_settings,
    )
    detail = await panel_views.panel_detail(db, panel.id, now)
    if detail is None:
        raise _panel_not_found(panel.id)
    return ProtoJSONResponse(panel_detail_message(detail), status_code=201)


@router.post(
    UiPanelWritesInterface.POST_UI_PANEL_SETTINGS_VERSIONS,
    dependencies=[Depends(require_instance_secret)],
    status_code=201,
    **proto_route(PanelSettingsVersionMessage),
)
async def post_panel_settings_version(
    db: Annotated[AsyncSession, Depends(async_db)],
    panel_id: Annotated[UUID, Path()],
    body: Annotated[SavePanelSettingsRequestMessage, Depends(ProtoBody(SavePanelSettingsRequestMessage))],
) -> ProtoJSONResponse:
    """W2. Append a panel's next settings version. Enable and disable are this write with enabled changed.

    The order: the secret, 422 for the body, 404 for an unknown panel, 422 for a store refusal, then the write.

    Args:
        db: The request's session.
        panel_id: The panel.
        body: The complete new settings and the optional reason.

    Returns:
        ProtoJSONResponse: 201 with the new PanelSettingsVersion, effective_pass unset.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a bad body or a store refusal; NOT_FOUND (404) for an unknown panel.
    """
    settings, reason = save_panel_settings_values(body)
    if await panel_reads.get_panel(db, panel_id) is None:
        raise _panel_not_found(panel_id)
    record = await append_settings_version(
        db, panel_id=panel_id, settings=settings, actor=OPERATOR_PRINCIPAL, reason=reason
    )
    return ProtoJSONResponse(
        panel_settings_version_message(SettingsVersionRow(record=record, effective=None)), status_code=201
    )


@router.post(
    UiPanelWritesInterface.POST_UI_PANEL_SOURCE_SETTINGS_VERSIONS,
    dependencies=[Depends(require_instance_secret)],
    status_code=201,
    **proto_route(PanelSourceSettingsVersionMessage),
)
async def post_panel_source_settings_version(
    db: Annotated[AsyncSession, Depends(async_db)],
    source: SourcePath,
    body: Annotated[SaveSourceSettingsRequestMessage, Depends(ProtoBody(SaveSourceSettingsRequestMessage))],
) -> ProtoJSONResponse:
    """W3. Append a source's next settings version. It applies to every panel on the source.

    The order: the secret, 422 for the source or the body, 422 for a store refusal, then the write.

    Args:
        db: The request's session.
        source: The source.
        body: The four numbers (all required) and the reason.

    Returns:
        ProtoJSONResponse: 201 with the new PanelSourceSettingsVersion, effective_pass unset.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a bad source or body or a store refusal.
    """
    settings, reason = save_source_settings_values(body)
    record = await append_source_settings_version(
        db, source=source, settings=settings, actor=OPERATOR_PRINCIPAL, reason=reason
    )
    return ProtoJSONResponse(
        panel_source_settings_version_message(SourceVersionRow(record=record, effective=None)), status_code=201
    )
