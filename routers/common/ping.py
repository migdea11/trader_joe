
from fastapi import APIRouter

from common.logging import get_logger


router = APIRouter()
log = get_logger(__name__)


@router.get("/ping")
async def ping():
    log.debug("Pinging...")
    return {"message": "pong"}
