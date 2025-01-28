
from fastapi import APIRouter

from common.logging import get_logger
from routers.common.app_endpoints import InterfaceRest


router = APIRouter()
log = get_logger(__name__)


@router.get(InterfaceRest.PING)
async def ping():
    log.debug("Pinging...")
    return {"message": "pong"}
