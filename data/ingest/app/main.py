
from fastapi import FastAPI

from data.ingest.app.app_depends import lifespan
from routers.common import ping
from routers.data_ingest import get_dataset_request

app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(get_dataset_request.router)
