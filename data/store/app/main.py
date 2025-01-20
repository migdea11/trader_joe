from fastapi import FastAPI
from data.store.app.app_depends import lifespan
from routers.common import ping
from routers.data_store import (
    asset_market_activity_data,
    store_dataset_request
)


app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(asset_market_activity_data.router)
app.include_router(store_dataset_request.router)
