from typing import Dict, List
from sqlalchemy.orm import Session

from common.enums.data_select import AssetType
from common.logging import get_logger
from data.store.app.db.models.stock_market_activity import StockMarketActivity
from schemas.data_store import asset_market_activity_data

log = get_logger(__name__)


def create_asset_market_activity_data(
    db: Session,
    asset_data: asset_market_activity_data.AssetMarketActivityDataCreate,
):
    log.debug("Storing asset market activity data")
    if asset_data.asset_type is AssetType.STOCK:
        db_asset_market_activity_data = StockMarketActivity(**asset_data.model_dump(exclude={"asset_type"}))
    db.add(db_asset_market_activity_data)
    db.commit()


def batch_insert_asset_market_activity_data(
    db: Session, batch_asset_data: Dict[AssetType, List[asset_market_activity_data.AssetMarketActivityDataCreate]]
):
    try:
        # Insert in batches for each asset type
        for asset_type, data_list in batch_asset_data.items():
            log.debug(f"Batch storing market activity: {asset_type.value}[{len(data_list)}]")
            if asset_type == AssetType.STOCK:
                db.bulk_insert_mappings(
                    StockMarketActivity,
                    [data.model_dump(exclude={"asset_type"}) for data in data_list],
                )
            # Add additional asset types here as needed (e.g., AssetType.CRYPTO)

        db.commit()
        log.debug("Batch insert completed successfully")
    except Exception as e:
        db.rollback()
        log.error(f"Failed to batch insert asset market activity data: {e}")
        raise


# TODO replace with a search function
def read_asset_market_activity_data(
    db: Session, stock_symbol: str
) -> List[asset_market_activity_data.AssetMarketActivityData]:
    log.debug("Reading stock market activity data")
    db_asset_market_activities = db.query(StockMarketActivity).filter(
        StockMarketActivity.symbol == stock_symbol
    ).all()
    return [
        asset_market_activity_data.AssetMarketActivityData.model_validate(obj) for obj in db_asset_market_activities
    ]


def read_all_asset_market_activity_data(db: Session) -> List[asset_market_activity_data.AssetMarketActivityData]:
    log.debug("Reading stock market activity data")
    db_stock_market_activities = db.query(StockMarketActivity).all()

    schema_objects = [
        asset_market_activity_data.AssetMarketActivityData.model_validate(obj) for obj in db_stock_market_activities
    ]
    return schema_objects


def delete_asset_market_activity_data(db: Session, stock_data_id: int):
    log.debug("Deleting stock market activity data")
    db_asset_market_activity_data = db.query(StockMarketActivity).filter(
        StockMarketActivity.id == stock_data_id
    ).first()
    db.delete(db_asset_market_activity_data)
    db.commit()


def delete_all_asset_market_activity_data(db: Session):
    log.debug("Deleting all stock market activity data")
    db.query(StockMarketActivity).delete()
    db.commit()
