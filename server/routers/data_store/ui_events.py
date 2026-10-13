"""WS /ui/v1/events: the UI's one live socket (tj-grna9p.73, ADR tj-grna9p.5).

Each frame is one text message of protobuf canonical JSON of trader_joe.proto.ui.v1.UiEvent, sent with the
shared WebSocket helper (routers/common/proto_json.send_proto_json), with server_time set at the moment of
sending. Events are NOTIFICATIONS: a client reloads over REST on each one and on every reconnect, so a frame
dropped or missed is never the only copy of anything.

OPEN, like the GETs: network-only access (ADR tj-grna9p.6), no instance secret. No inbound message is
expected; anything a client sends is read and ignored, so a closed socket is noticed at once rather than at
the next event.

A SLOW CLIENT is dropped by the hub (data/store/app/events.py) and closed here with 1013 (try again later).
No publisher ever waits on a socket.
"""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from common.logging import get_logger
from data.store.app.events import hub
from routers.common.proto_json import send_proto_json
from routers.data_store.app_endpoints import UiEventsInterface
from routers.data_store.ui_mapping import ui_event_message


router = APIRouter()
log = get_logger(__name__)


async def _until_disconnect(websocket: WebSocket) -> None:
    """Read and discard inbound messages until the client goes away."""
    try:
        while True:
            await websocket.receive()
            if websocket.client_state.name == 'DISCONNECTED':
                return
    except (WebSocketDisconnect, RuntimeError):
        return


@router.websocket(UiEventsInterface.UI_EVENTS)
async def ui_events(websocket: WebSocket) -> None:
    """Accept, subscribe to the hub and forward each event as a UiEvent frame until either side leaves.

    Args:
        websocket: The client's socket.
    """
    await websocket.accept()
    subscription = hub.subscribe()
    listener = asyncio.create_task(_until_disconnect(websocket))
    try:
        while True:
            next_event = asyncio.create_task(subscription.get())
            done, _ = await asyncio.wait({next_event, listener}, return_when=asyncio.FIRST_COMPLETED)
            if next_event not in done:
                next_event.cancel()
                return
            event = next_event.result()
            if event is None:
                await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
                return
            await send_proto_json(websocket, ui_event_message(event, datetime.now(UTC)))
    except (WebSocketDisconnect, RuntimeError):
        # The client left between the wait and the send.
        return
    finally:
        listener.cancel()
        subscription.close()
