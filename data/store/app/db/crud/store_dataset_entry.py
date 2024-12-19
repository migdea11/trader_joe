from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import Select, and_, func, case, or_, select
from typing import List, Optional
import uuid

from common.enums.data_select import DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from data.store.app.db.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_store import store_dataset_request

# Enum ranking maps
EXPIRY_TYPE_RANKING = {
    ExpiryType.BULK: 1,
    ExpiryType.BUFFER_1K: 2,
    ExpiryType.BUFFER_10K: 3,
    ExpiryType.BUFFER_100K: 4,
    ExpiryType.ROLLING: 5,
}

UPDATE_TYPE_RANKING = {
    UpdateType.STATIC: 1,
    UpdateType.DAILY: 2,
    UpdateType.STREAM: 3,
}


def upsert_entry(
    db: Session, entry: store_dataset_request.StoreDatasetEntryCreate
) -> store_dataset_request.StoreDatasetEntry:
    try:
        stmt = insert(StoreDatasetEntry).values(
            **entry.model_dump(),
            created_at=func.now(),
            updated_at=func.now()
        ).on_conflict_do_update(
            index_elements=['symbol', 'granularity', 'start', 'end', 'source', 'data_type'],
            set_={
                # Update expiry_type based on custom ranking
                'expiry_type': case(
                    [
                        (
                            EXPIRY_TYPE_RANKING[StoreDatasetEntry.expiry_type] > EXPIRY_TYPE_RANKING[entry.expiry_type],
                            StoreDatasetEntry.expiry_type
                        )
                    ],
                    else_=entry.expiry_type
                ),

                # Update update_type based on custom ranking
                'update_type': case(
                    [
                        (
                            UPDATE_TYPE_RANKING[StoreDatasetEntry.update_type] > UPDATE_TYPE_RANKING[entry.update_type],
                            StoreDatasetEntry.update_type
                        )
                    ],
                    else_=entry.update_type
                ),

                # Always update updated_at timestamp
                'updated_at': func.now()
            }
        ).returning(StoreDatasetEntry)
        created = db.execute(stmt).fetchone()
        db.commit()
        entry = db.get(StoreDatasetEntry, created.id) if created else None
        return store_dataset_request.StoreDatasetEntry.model_validate(entry)
    except SQLAlchemyError as e:
        db.rollback()
        raise RuntimeError(f"Error while creating or updating entry: {e}")


def update_entry(db: Session, entry: store_dataset_request.StoreDatasetEntryUpdate):
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
        raise RuntimeError(f"Error while updating entry: {e}")


def update_entry_lifecycle(db: Session, id: uuid.UUID, item_count: int):
    """
    Updates only the `updated_at` and `item_count` fields for an existing entry.
    """
    try:
        db.query(StoreDatasetEntry).filter(StoreDatasetEntry.id == id).update(
            {
                'updated_at': func.now(),
                'item_count': item_count
            },
            synchronize_session=False
        )
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        raise RuntimeError(f"Error while updating entry lifecycle: {e}")


def get_entry_by_id(db: Session, id: uuid.UUID) -> store_dataset_request.StoreDatasetEntry:
    """
    Retrieve an entry by its ID.
    """
    entry = db.query(StoreDatasetEntry).filter(StoreDatasetEntry.id == id).first()
    return store_dataset_request.StoreDatasetEntry.model_validate(entry)


def search_entries(
    db: Session,
    symbol: Optional[str] = None,
    source: Optional[DataSource] = None,
    data_type: Optional[DataType] = None,
    granularity: Optional[Granularity] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    expiry: Optional[datetime] = None,
    expiry_type: Optional[ExpiryType] = None,
    update_type: Optional[UpdateType] = None,
    item_count: Optional[int] = None,
    created_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None
) -> List[store_dataset_request.StoreDatasetEntry]:
    """
    Search for entries based on optional criteria.
    """
    query = db.query(StoreDatasetEntry)

    if symbol is not None:
        query = query.filter(StoreDatasetEntry.symbol == symbol)
    if source is not None:
        query = query.filter(StoreDatasetEntry.source == source)
    if data_type is not None:
        query = query.filter(StoreDatasetEntry.data_type == data_type)
    if granularity is not None:
        query = query.filter(StoreDatasetEntry.granularity == granularity)
    if start is not None:
        query = query.filter(StoreDatasetEntry.start == start)
    if end is not None:
        query = query.filter(StoreDatasetEntry.end == end)
    if expiry is not None:
        query = query.filter(StoreDatasetEntry.expiry == expiry)
    if expiry_type is not None:
        query = query.filter(StoreDatasetEntry.expiry_type == expiry_type)
    if update_type is not None:
        query = query.filter(StoreDatasetEntry.update_type == update_type)
    if item_count is not None:
        query = query.filter(StoreDatasetEntry.item_count == item_count)
    if created_at is not None:
        query = query.filter(StoreDatasetEntry.created_at == created_at)
    if updated_at is not None:
        query = query.filter(StoreDatasetEntry.updated_at == updated_at)

    entries = query.all()
    return [store_dataset_request.StoreDatasetEntry.model_validate(entry) for entry in entries]


def _query_overlap(select: Select, entryIdentifier: store_dataset_request.StoreDatasetIdentifiers) -> Select:
    return select.where(
        and_(
            StoreDatasetEntry.symbol == entryIdentifier.symbol,
            StoreDatasetEntry.source == entryIdentifier.source,
            StoreDatasetEntry.granularity == entryIdentifier.granularity,
            StoreDatasetEntry.data_type == entryIdentifier.data_type,
            or_(
                and_(StoreDatasetEntry.start <= entryIdentifier.start, StoreDatasetEntry.end >= entryIdentifier.start),
                and_(StoreDatasetEntry.start <= entryIdentifier.end, StoreDatasetEntry.end >= entryIdentifier.end),
                and_(StoreDatasetEntry.start >= entryIdentifier.start, StoreDatasetEntry.end <= entryIdentifier.end)
            )
        )
    )


def search_overlapping_datasets(
    db: Session,
    entryIdentifier: store_dataset_request.StoreDatasetIdentifiers
) -> List[uuid.UUID]:
    query = _query_overlap(select(StoreDatasetEntry.id), entryIdentifier)
    return db.execute(query).scalars().all()


def delete_entry_by_id(db: Session, id: uuid.UUID):
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
        raise RuntimeError(f"Error while deleting entry with ID {id}: {e}")
