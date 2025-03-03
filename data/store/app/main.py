from fastapi import FastAPI
from data.store.app.app_depends import lifespan
from routers.common import ping
from routers.data_store import (
    asset_dataset_store,
    internal_asset_data
)

app = FastAPI(lifespan=lifespan)
app.include_router(ping.router)
app.include_router(internal_asset_data.router)
app.include_router(asset_dataset_store.router)
