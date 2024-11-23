from fastapi import APIRouter, Path, Body
from fastapi import APIRouter

from common.enums.data_select import AssetType
from data.ingest.app.ingest_control import store_retrieve_stock, store_retrieve_crypto, store_retrieve_option
from schemas.store_broker_data import DataRequest, DataRequestBody


router = APIRouter()


@router.post("/store/{asset_type}/{symbol}/")
async def store_data(
        asset_type: AssetType = Path(..., description="The type of the asset"),
        symbol: str = Path(..., description="The symbol of the asset"),
        body: DataRequestBody = Body(..., description="Data request parameters")
    ):
    asset_map = {
        AssetType.STOCK: store_retrieve_stock,
        AssetType.CRYPTO: store_retrieve_crypto,
        AssetType.OPTION: store_retrieve_option
    }
    body_dict = body.model_dump()

    request = {**body_dict, "asset_type": asset_type, "symbol": symbol}
    print(request)
    await asset_map[asset_type](DataRequest(**request))

    return {"message": "Data retrieval started."}