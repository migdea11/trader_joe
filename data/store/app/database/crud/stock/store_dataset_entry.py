import traceback
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, func, not_, or_, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.logging import get_logger
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_store import asset_dataset_store


if TYPE_CHECKING:
    from datetime import datetime

log = get_logger(__name__)


# The eight-column equality prefix of the identity key, minus the range (tj-vhboky.1 section 2).
# feed is deliberately absent: tj-rh4b7f (2026-09-25) deferred it off the entry to the gRPC
# transport work, so what the epic's earlier drafts called a nine-column prefix is eight here.
# Order does not matter for these equality checks; StoreDatasetEntry.NATURAL_KEY has the order
# that matters, for the index.
_IDENTITY_EQUALITY_COLUMNS = (
    'owner',
    'asset_symbol',
    'asset_type',
    'data_type',
    'source',
    'granularity',
    'expiry_type',
    'update_type',
)


class EntryNotFound(ValueError):
    """Raised by an id-addressed write naming an entry that does not exist."""

    def __init__(self, entry_id: uuid.UUID):
        super().__init__(f'No entry found with ID {entry_id}')
        self.entry_id = entry_id


class OwnerMismatch(ValueError):
    """Raised by an id-addressed write whose declared principal does not own the entry.

    Reachable ONLY here, and never on the create path (delete_entry_by_id, update_entry,
    update_entry_lifecycle) -- tj-vhboky.1 section 5. Because owner is identity, two principals
    asking for the same spec on create get two different entries, so there is never a cross-owner
    conflict to detect there; anyone expecting a check on create is looking for something that
    should not exist. Deliberately does NOT carry or format the real owner: reads are open, but
    an error body is not a read endpoint.
    """

    def __init__(self, entry_id: uuid.UUID):
        super().__init__(f'Entry {entry_id} is not owned by the declared principal')
        self.entry_id = entry_id


class OwnOverlapConflict(ValueError):
    """Raised when a create request overlaps one of the SAME owner's existing datasets.

    Not an exact repeat (tj-vhboky.1 section 3(b)): an exact repeat -- every identity field
    equal, the range included -- is handled by the
    upsert's ON CONFLICT clause as a no-op and must never reach here, or a retried POST would
    stop being idempotent. A different owner's overlapping dataset is not a collision either: it
    is a different dataset by construction, because owner is part of the key.
    """

    def __init__(self, colliding_ids: list[uuid.UUID]):
        super().__init__(f'Overlaps existing dataset(s) of the same owner: {colliding_ids}')
        self.colliding_ids = colliding_ids


class RangeShrink(ValueError):
    """Raised when an update to an entry's range would shrink it rather than grow it.

    Ruling 8 (tj-vhboky.1 section 4): shrinking strands already-stored bars outside the entry's
    declared coverage -- a quieter version of the coverage lie this epic exists to remove.
    Shrinking is a delete and should be one.
    """

    def __init__(self, entry_id: uuid.UUID):
        super().__init__(f'Update would shrink entry {entry_id}; growth only, shrink by deleting')
        self.entry_id = entry_id


class RangeCollision(ValueError):
    """Raised when growing an entry's range would make it identical to another entry the same owner already holds.

    tj-vhboky.1 section 4: "An extend can collide... reject with that id -- the same rule and the
    same response shape as create."
    """

    def __init__(self, colliding_id: uuid.UUID):
        super().__init__(f'New range collides with existing dataset {colliding_id}')
        self.colliding_id = colliding_id


def _equality_conditions(values: dict) -> list:
    """The _IDENTITY_EQUALITY_COLUMNS conditions, built from a dict of column name to value."""
    return [getattr(StoreDatasetEntry, column) == values[column] for column in _IDENTITY_EQUALITY_COLUMNS]


async def _find_own_overlap(db: AsyncSession, field_values: dict) -> list[uuid.UUID]:
    """Find existing entries of the SAME owner whose range overlaps this request's, excluding an exact repeat.

    The formula, from the ADR (tj-vhboky.1 section 3):

        existing.start <= request.end AND existing.end >= request.start
        AND NOT (existing.start = request.start AND existing.end = request.end)

    `end` uses the EPOCH sentinel for "no end", not a NULL (common/database/
    sql_alchemy_nullable_datetime.py: None in the schema maps to 1970-01-01 in the column, so two
    open-ended entries still collide on the unique key). EPOCH is far in the PAST, so a naive
    `existing.end >= request.start` reads backwards for an open-ended EXISTING entry, and a naive
    `existing.start <= request.end` reads backwards for an open-ended REQUEST. Both are handled
    explicitly rather than compared as literal values.
    """
    new_start = field_values['start']
    new_end = field_values['end']

    conditions = _equality_conditions(field_values)
    # existing.end >= request.start, with an open-ended existing entry (end == EPOCH) always
    # satisfying it: an entry with no declared end covers everything from its start onward.
    conditions.append(or_(StoreDatasetEntry.end == NullableDateTime.EPOCH, StoreDatasetEntry.end >= new_start))
    if new_end != NullableDateTime.EPOCH:
        # existing.start <= request.end, only when the request itself has a declared end.
        conditions.append(StoreDatasetEntry.start <= new_end)
    # else: the request is open-ended, so nothing can start "after" its end -- no upper-bound
    # predicate is needed; every existing start already satisfies it.
    conditions.append(not_(and_(StoreDatasetEntry.start == new_start, StoreDatasetEntry.end == new_end)))

    stmt = select(StoreDatasetEntry.id).where(*conditions)
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def _find_exact_collision(
    db: AsyncSession, existing: StoreDatasetEntry, new_start: 'datetime', new_end: 'datetime'
) -> uuid.UUID | None:
    """Find another entry of the SAME owner that a range grow on `existing` would exactly collide with.

    tj-vhboky.1 section 4. The non-range fields are read off `existing` itself, not off the
    incoming request, because they are immutable -- an update only ever changes start and/or end.
    """
    values = {column: getattr(existing, column) for column in _IDENTITY_EQUALITY_COLUMNS}
    conditions = _equality_conditions(values)
    conditions.append(StoreDatasetEntry.id != existing.id)
    conditions.append(StoreDatasetEntry.start == new_start)
    conditions.append(StoreDatasetEntry.end == new_end)

    stmt = select(StoreDatasetEntry.id).where(*conditions)
    result = await db.execute(stmt)
    row = result.first()
    return row[0] if row is not None else None


def _is_growth(old_start: 'datetime', old_end: 'datetime', new_start: 'datetime', new_end: 'datetime') -> bool:
    """An update may change the range and nothing else, and only by growth: the new range must contain the old one.

    tj-vhboky.1 section 4. EPOCH means "no end", a value far in the PAST, not the future (see _find_own_overlap), so a
    naive `new_end >= old_end` reads backwards for an entry that was already open-ended: turning
    an open-ended entry into a bounded one is a shrink even though the literal new_end value is
    larger than EPOCH.
    """
    if new_start > old_start:
        return False
    if old_end == NullableDateTime.EPOCH:
        return new_end == NullableDateTime.EPOCH
    return new_end == NullableDateTime.EPOCH or new_end >= old_end


async def _get_entry_or_raise(db: AsyncSession, id: uuid.UUID) -> StoreDatasetEntry:
    result = await db.execute(select(StoreDatasetEntry).where(StoreDatasetEntry.id == id))
    entry = result.scalar_one_or_none()
    if entry is None:
        raise EntryNotFound(id)
    return entry


def _check_owner(existing: StoreDatasetEntry, declared_owner: str) -> None:
    """The owner check on id-addressed writes.

    tj-vhboky.1 section 5. See OwnerMismatch for why this is the only place it is reachable.
    """
    if existing.owner != declared_owner:
        raise OwnerMismatch(existing.id)


async def upsert_entry(db: AsyncSession, entry: asset_dataset_store.AssetDatasetStoreCreate) -> uuid.UUID:
    """Create a dataset entry, or resolve an exact repeat to the entry it already matches.

    IDENTITY REPLACES MERGING (tj-vhboky.1 section 2): every field, the range included, is part
    of the single ten-column unique key StoreDatasetEntry.NATURAL_KEY_CONSTRAINT. Two requests
    that disagree on anything but the range are two different datasets, so the max-rank merge
    that used to reconcile expiry_type/update_type on conflict is gone outright -- it silently
    handed a caller a lifetime nobody asked for.

    Two outcomes, not one on top of the other (tj-vhboky.1 section 3):
    * An EXACT identity match is a no-op: the ON CONFLICT branch below returns the existing id,
      refreshing only expiry and updated_at.
    * An overlap that is NOT an exact match is a conflict of the SAME owner's own datasets,
      checked BEFORE the insert is attempted (_find_own_overlap) and reported by id, so the
      caller can act on it -- a caller can build auto-extend on top of fail, not the reverse.
    A different owner's overlapping dataset is neither case: owner is part of the key, so it is a
    different dataset by construction and this function never compares across owners.
    """
    log.debug(f'Upserting entry: {entry}')
    field_values = StoreDatasetEntry.get_fields(entry, exclude_none=True)

    colliding_ids = await _find_own_overlap(db, field_values)
    if colliding_ids:
        raise OwnOverlapConflict(colliding_ids)

    try:
        stmt = postgresql.insert(StoreDatasetEntry).values(**field_values, created_at=func.now(), updated_at=func.now())
        stmt = stmt.on_conflict_do_update(
            constraint=StoreDatasetEntry.NATURAL_KEY_CONSTRAINT,
            set_={
                # An exact repeat refreshes expiry and updated_at and nothing else (tj-vhboky.1
                # section 3(a) / Amendment 1 item P) -- still a no-op in the sense idempotency
                # requires: the entry id does not change and no bar moves.
                #
                # A literal bind, not stmt.excluded.expiry: StoreDatasetEntry carries CustomColumn
                # fields (end, expiry_type, update_type -- common/database/sql_alchemy_types.py),
                # and CustomColumn's __init__ signature does not accept the positional arguments
                # SQLAlchemy's proxy machinery passes when it builds the `excluded` pseudo-table,
                # so touching `stmt.excluded` on ANY column of this table raises TypeError. Out of
                # scope here (common/ is builder-shared's) -- worked around, not fixed.
                'expiry': field_values['expiry'],
                'updated_at': func.now(),
            },
        )
        stmt = stmt.returning(StoreDatasetEntry.id)
        result = await db.execute(stmt)
        result_id = result.scalar_one()
        await db.commit()
        return result_id
    except SQLAlchemyError as e:
        await db.rollback()
        traceback.print_exc()
        raise RuntimeError(f'Error while creating or updating entry: {e}')  # noqa: B904  # see tj-76u8ip


async def update_entry(db: AsyncSession, entry: asset_dataset_store.AssetDatasetStoreUpdate) -> None:
    """Grow an existing entry's range in place, keeping its id (tj-vhboky.1 section 4).

    Only start and/or end are written. Every other field on `entry` is identity and immutable by
    ruling -- symbol, source and granularity define what the stored bars ARE, and expiry_type,
    update_type and owner could otherwise mutate this entry into colliding with another of the
    same owner's entries for a reason the caller cannot see. Growth only: shrinking strands
    already-stored bars outside the entry's declared coverage, and shrinking is a delete's job.
    """
    existing = await _get_entry_or_raise(db, entry.id)
    _check_owner(existing, entry.owner)

    field_values = StoreDatasetEntry.get_fields(entry, exclude_none=True)
    new_start = field_values['start']
    new_end = field_values['end']

    if not _is_growth(existing.start, existing.end, new_start, new_end):
        raise RangeShrink(entry.id)

    colliding_id = await _find_exact_collision(db, existing, new_start, new_end)
    if colliding_id is not None:
        raise RangeCollision(colliding_id)

    try:
        stmt = (
            update(StoreDatasetEntry)
            .where(StoreDatasetEntry.id == entry.id)
            .values(start=new_start, end=new_end, updated_at=func.now())
        )
        await db.execute(stmt)
        await db.commit()
    except SQLAlchemyError as e:
        await db.rollback()
        traceback.print_exc()
        raise RuntimeError(f'Error while updating entry: {e}')  # noqa: B904  # see tj-76u8ip


async def update_entry_lifecycle(db: AsyncSession, id: uuid.UUID, owner: str) -> None:
    """Updates only the `updated_at` for an existing entry."""
    # TODO might not be necessary
    existing = await _get_entry_or_raise(db, id)
    _check_owner(existing, owner)
    try:
        stmt = update(StoreDatasetEntry).where(StoreDatasetEntry.id == id).values(updated_at=func.now())
        await db.execute(stmt)
        await db.commit()
    except SQLAlchemyError as e:
        await db.rollback()
        traceback.print_exc()
        raise RuntimeError(f'Error while updating entry lifecycle: {e}')  # noqa: B904  # see tj-76u8ip


async def get_entry_by_id(db: AsyncSession, id: uuid.UUID) -> asset_dataset_store.AssetDatasetStore:
    """Retrieve an entry by its ID."""
    stmt = select(StoreDatasetEntry).where(StoreDatasetEntry.id == id)
    result = await db.execute(stmt)
    return asset_dataset_store.AssetDatasetStore.model_validate(result.first())


async def search_entries(
    db: AsyncSession,
    request_path: asset_dataset_store.StoreAssetDatasetPath,
    request_query: asset_dataset_store.StoreAssetDatasetQuery,
) -> list[asset_dataset_store.AssetDatasetStore]:
    """Search for entries based on optional criteria."""
    joined_table = StockMarketActivity
    stmt = (
        select(
            StoreDatasetEntry,
            # expiry is the entry's own column now (tj-vhboky.1 section 6 / Amendment 1 item P):
            # StoreDatasetEntry is selected whole below, so entry.expiry reads it directly and
            # no aggregate over the bar table is needed for it. The join stays for count(id)
            # ONLY -- AssetDatasetStore.item_count is a required int, and an entry with no bars
            # must still list with item_count 0 rather than vanish, which is what the OUTER in
            # outerjoin buys.
            func.count(joined_table.id).label('item_count'),
        )
        .outerjoin(joined_table, StoreDatasetEntry.id == joined_table.dataset_id)
        .where(StoreDatasetEntry.asset_symbol == request_path.asset_symbol)
    )

    # owner IS a filterable column here (StoreAssetDatasetQuery exposes it and it is a real
    # StoreDatasetEntry column, so this loop already picks it up with no change -- verified,
    # not assumed). feed is NOT: there is no feed column on the entry to filter (tj-rh4b7f), and
    # StoreAssetDatasetQuery deliberately does not expose one.
    for column, value in request_query.model_dump().items():
        log.debug(f'Filtering by {column}: {value}')
        if value is not None:
            stmt = stmt.where(getattr(StoreDatasetEntry, column) == value)

    stmt = stmt.group_by(StoreDatasetEntry.id)
    result = await db.execute(stmt)
    entries: list[tuple[StoreDatasetEntry, int]] = result.all()

    return [
        # additional carries item_count only: expiry is a plain column on StoreDatasetEntry and
        # AssetDatasetStore declares it, so to_validated_schema already reads it off the ORM row
        # without help -- passing it here too would be the same value down two paths.
        entry.to_validated_schema(asset_dataset_store.AssetDatasetStore, additional={'item_count': item_count})
        for entry, item_count in entries
    ]


async def delete_entry_by_id(db: AsyncSession, id: uuid.UUID, owner: str) -> None:
    """Delete an entry by its ID.

    Cascades to its bars via ON DELETE CASCADE on the bar's dataset_id foreign key (T3: every bar
    belongs to exactly one entry now), so the database removes exactly this entry's bars and
    nothing else. There is no membership sweep, no RETURNING capture and no IN-list here anymore
    -- that removes the ~65,000-bar IN-list ceiling (D1) structurally rather than by code.
    """
    existing = await _get_entry_or_raise(db, id)
    _check_owner(existing, owner)
    try:
        stmt = delete(StoreDatasetEntry).where(StoreDatasetEntry.id == id)
        result = await db.execute(stmt)
        if result.rowcount == 0:
            # Raced with a concurrent delete between the SELECT above and this statement.
            await db.rollback()
            raise EntryNotFound(id)
        await db.commit()
    except SQLAlchemyError as e:
        await db.rollback()
        traceback.print_exc()
        raise RuntimeError(f'Error while deleting entry with ID {id}: {e}')  # noqa: B904  # see tj-76u8ip
