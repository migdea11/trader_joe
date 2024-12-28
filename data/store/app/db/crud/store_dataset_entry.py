from datetime import datetime
import traceback
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, case, select
from typing import List, Tuple
import uuid

from common.logging import get_logger
from data.store.app.db.models.stock_market_activity import StockMarketActivity
from data.store.app.db.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_store import store_dataset_request

log = get_logger(__name__)


async def upsert_entry(
    db: Session, entry: store_dataset_request.StoreDatasetEntryCreate
) -> store_dataset_request.StoreDatasetEntry:
    try:
        test = StoreDatasetEntry.get_fields(entry, exclude={"expiry"}, exclude_none=True)
        log.debug("Inserting or updating entry with the following values:")
        for name, column in test.items():
            log.debug(f"  test - {name}: {column}")
        stmt = insert(StoreDatasetEntry).values(
            **StoreDatasetEntry.get_fields(entry, exclude={"expiry"}, exclude_none=True),
            created_at=func.now(),
            updated_at=func.now()
        ).on_conflict_do_update(
            index_elements=['symbol', 'granularity', 'start', 'end', 'source', 'data_type'],
            set_={
                # Update expiry_type based on custom ranking
                'expiry_type': case(
                    (StoreDatasetEntry.expiry_type > entry.expiry_type, StoreDatasetEntry.expiry_type),
                    else_=entry.expiry_type
                ),

                # Update update_type based on custom ranking
                'update_type': case(
                    (StoreDatasetEntry.update_type > entry.update_type, StoreDatasetEntry.update_type),
                    else_=entry.update_type
                ),

                # Always update updated_at timestamp
                'updated_at': func.now()
            }
        ).returning(StoreDatasetEntry.id)
        result_id: StoreDatasetEntry = db.execute(stmt).scalar_one()
        db.commit()

        return result_id
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while creating or updating entry: {e}")


async def update_entry(db: Session, entry: store_dataset_request.StoreDatasetEntryUpdate):
    """
    Update an existing entry.
    """
    try:
        db.query(StoreDatasetEntry).filter(StoreDatasetEntry.id == entry.id).update(
            entry.model_dump(),
            synchronize_session=False
        )
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while updating entry: {e}")


async def update_entry_lifecycle(db: Session, id: uuid.UUID):
    """
    Updates only the `updated_at` for an existing entry.
    """
    # TODO might not be necessary
    try:
        db.query(StoreDatasetEntry).filter(StoreDatasetEntry.id == id).update(
            {
                'updated_at': func.now(),
            },
            synchronize_session=False
        )
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while updating entry lifecycle: {e}")


async def get_entry_by_id(db: Session, id: uuid.UUID) -> store_dataset_request.StoreDatasetEntry:
    """
    Retrieve an entry by its ID.
    """
    entry = db.query(StoreDatasetEntry).filter(StoreDatasetEntry.id == id).first()
    return store_dataset_request.StoreDatasetEntry.model_validate(entry)


async def search_entries(
    db: Session,
    request_path: store_dataset_request.StoreDatasetRequestPath,
    request_query: store_dataset_request.StoreDatasetEntrySearch
) -> List[store_dataset_request.StoreDatasetEntry]:
    """
    Search for entries based on optional criteria.
    """
    joined_table = StockMarketActivity
    query = select(
        StoreDatasetEntry,
        func.min(joined_table.expiry).label("expiry"),
        func.count(joined_table.id).label("item_count"),
    ).outerjoin(
        joined_table, StoreDatasetEntry.id == joined_table.dataset_id
    ).where(
        joined_table.symbol == request_path.symbol
    )

    # Apply filters to the subquery
    for column, value in request_query.__dict__.items():
        log.debug(f"Filtering by {column}: {value}")
        if value is not None:
            query = query.where(getattr(StoreDatasetEntry, column) == value)

    # Execute query (example, depending on your session setup)
    query = query.group_by(StoreDatasetEntry.id)
    log.debug(f"Query: {query}")
    entries: List[Tuple[StoreDatasetEntry, datetime, int]] = db.execute(query).fetchall()

    return [
        entry.to_validated_schema(
            store_dataset_request.StoreDatasetEntry,
            additional={"expiry": expiry, "item_count": item_count}
        )
        for entry, expiry, item_count in entries
    ]


async def delete_entry_by_id(db: Session, id: uuid.UUID):
    """
    Delete an entry by its ID.
    """
    try:
        result = db.query(StoreDatasetEntry).filter(StoreDatasetEntry.id == id).delete()
        db.commit()
        if result == 0:
            raise ValueError(f"No entry found with ID {id}")
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while deleting entry with ID {id}: {e}")
