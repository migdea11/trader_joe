import traceback
import uuid
from datetime import datetime
from typing import List, Tuple

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_store import store_dataset_request

log = get_logger(__name__)


async def upsert_entry(
    db: AsyncSession, entry: store_dataset_request.StoreDatasetEntryCreate
) -> store_dataset_request.StoreDatasetEntry:
    try:
        stmt = postgresql.insert(StoreDatasetEntry).values(
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
        result = await db.execute(stmt)
        result_id = result.scalar_one()
        await db.commit()
        return result_id
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while creating or updating entry: {e}")


async def update_entry(db: AsyncSession, entry: store_dataset_request.StoreDatasetEntryUpdate):
    """
    Update an existing entry.
    """
    try:
        stmt = update(StoreDatasetEntry).where(StoreDatasetEntry.id == entry.id).values(
            entry.model_dump(),
        )
        await db.execute(stmt)
        await db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while updating entry: {e}")


async def update_entry_lifecycle(db: AsyncSession, id: uuid.UUID):
    """
    Updates only the `updated_at` for an existing entry.
    """
    # TODO might not be necessary
    try:
        stmt = update(StoreDatasetEntry).where(StoreDatasetEntry.id == id).values(updated_at=func.now())
        await db.execute(stmt)
        await db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while updating entry lifecycle: {e}")


async def get_entry_by_id(db: AsyncSession, id: uuid.UUID) -> store_dataset_request.StoreDatasetEntry:
    """
    Retrieve an entry by its ID.
    """
    stmt = select(StoreDatasetEntry).where(StoreDatasetEntry.id == id)
    result = await db.execute(stmt)
    return store_dataset_request.StoreDatasetEntry.model_validate(result.first())


async def search_entries(
    db: AsyncSession,
    request_path: store_dataset_request.StoreDatasetRequestPath,
    request_query: store_dataset_request.StoreDatasetEntrySearch
) -> List[store_dataset_request.StoreDatasetEntry]:
    """
    Search for entries based on optional criteria.
    """
    joined_table = StockMarketActivity
    stmt = select(
        StoreDatasetEntry,
        func.min(joined_table.expiry).label("expiry"),
        func.count(joined_table.id).label("item_count"),
    ).outerjoin(
        joined_table, StoreDatasetEntry.id == joined_table.dataset_id
    ).where(
        StoreDatasetEntry.symbol == request_path.symbol
    )

    # Apply filters to the subquery
    for column, value in request_query.model_dump().items():
        log.debug(f"Filtering by {column}: {value}")
        if value is not None:
            stmt = stmt.where(getattr(StoreDatasetEntry, column) == value)

    # Execute query (example, depending on your session setup)
    stmt = stmt.group_by(StoreDatasetEntry.id)
    # log.debug(f"Query: {stmt.compile(dialect=postgresql.dialect())}")
    result = await db.execute(stmt)
    entries: List[Tuple[StoreDatasetEntry, datetime, int]] = result.all()
    # log.debug(f"Entries: {entries}")

    return [
        entry.to_validated_schema(
            store_dataset_request.StoreDatasetEntry,
            additional={"expiry": expiry, "item_count": item_count}
        )
        for entry, expiry, item_count in entries
    ]


async def delete_entry_by_id(db: AsyncSession, id: uuid.UUID):
    """
    Delete an entry by its ID.
    """
    try:
        stmt = delete(StoreDatasetEntry).filter(StoreDatasetEntry.id == id)
        result = await db.execute(stmt)
        await db.commit()
        if result == 0:
            raise ValueError(f"No entry found with ID {id}")
    except SQLAlchemyError as e:
        db.rollback()
        traceback.print_exc()
        raise RuntimeError(f"Error while deleting entry with ID {id}: {e}")
