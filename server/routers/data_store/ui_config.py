"""GET /ui/v1/config: what the UI shell needs to know about this deployment (tj-grna9p.45, ADR tj-grna9p.4).

Served as protobuf canonical JSON of trader_joe.proto.ui.v1.UiConfig. Open and read-only like the dataset
reads; the edge auth (tj-grna9p.6) protects it in production. The allowed groups come from the server, SIMULATION
only in PR 4 (ADR tj-grna9p.10), and no credential material is read or returned.
"""

from fastapi import APIRouter

from data.store.app.ui_config import ui_config
from routers.common.proto_json import ProtoJSONResponse, proto_route
from routers.data_store.app_endpoints import UiConfigInterface
from routers.data_store.ui_mapping import UiConfigMessage, ui_config_message


router = APIRouter()


@router.get(UiConfigInterface.GET_UI_CONFIG, **proto_route(UiConfigMessage))
async def get_ui_config() -> ProtoJSONResponse:
    """The shell's configuration, as a UiConfig.

    Returns:
        ProtoJSONResponse: A UiConfig: allowed_groups (ACCOUNT_GROUP_SIMULATION only), the display-only
            deployment_label (empty when unset) and server_version.
    """
    return ProtoJSONResponse(ui_config_message(ui_config()))
