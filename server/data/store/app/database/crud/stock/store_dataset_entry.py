import uuid
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete, func, not_, or_, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import InvalidRequestError, Reason
from common.logging import get_logger
from common.sensitive import REDACTED
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.database.transaction import write_transaction
from schemas.data_store import asset_dataset_store


if TYPE_CHECKING:
    from datetime import datetime

log = get_logger(__name__)


@dataclass(frozen=True)
class OverlapKey:
    """WHAT THE OWN-OVERLAP REFUSAL ASKS ABOUT: ten fields, and feed is deliberately not one of them.

    A NAMED VALUE RATHER THAN AN AssetDatasetStoreCreate (tj-xn3qa6 D3). The check used to be handed
    a whole create model and key on a subset of it, which is the shape
    data/store/app/database/asset_data_interface.py:101 warns about -- a contract that accepts a
    field and ignores it only looks validating. Worse, it made the check structurally impossible to
    run where it has to run: tj-hywf7w hoisted it AHEAD of the FetchDataset stream so a 409 stops
    costing a live vendor call, but AssetDatasetStoreCreate.feed is required and RESOLVED, and the
    resolved feed does not exist until FetchAccepted. Three things could not all hold -- the check
    runs before the stream, the check keys on a create model, feed arrives on the ack -- and this
    value is which one gave way.

    feed IS IDENTITY AND IS NOT A TERM HERE (tj-xn3qa6 D1), because the two keys answer different
    questions. IDENTITY -- StoreDatasetEntry.NATURAL_KEY, the unique constraint, ON CONFLICT --
    asks IS THIS THE SAME DATASET, and an IEX dataset and a SIP dataset are not the same one
    (tj-f2qz44). REFUSAL asks DOES THIS OWNER ALREADY HOLD DATA COVERING THIS WINDOW THAT THEY
    SHOULD BE TOLD ABOUT BEFORE WE FETCH MORE. A caller holding AAPL one-minute for January on IEX
    who asks for SIP over the same window is being told something true and useful by a feed-blind
    409 carrying colliding_ids, and can then proceed deliberately -- this module's own stated
    principle, that a caller can build auto-extend on top of fail and not the reverse. Told nothing,
    that caller is charged a vendor call and handed a second overlapping entry.

    THE ENTRY IS A STRICT EXTENSION OF THIS VALUE, built by `extend` below once the ack has resolved
    a tape -- never from a second splat of model_dump(). That is tj-hywf7w's single-construction
    property SHARPENED rather than traded away: what the check and the write share is now exactly
    the part that must agree, instead of everything.

    NAMING IT PAYS ELSEWHERE TOO (tj-xn3qa6 D3): tj-uqlsf9's likeliest answer is an exclusion
    constraint, which needs this key spelled out as a tuple; tj-a07boi is about which callers must
    run the check; tj-3mk3u5.34's coverage ledger keys on coverage.
    """

    # repr=False, the dataclass equivalent of the SensitiveStr marker the schemas use
    # (common/sensitive.py M2): this value is cheap to drop into a log line, and the owner must not
    # ride along when someone does. Everything else about the field is an ordinary str.
    owner: str = field(repr=False)
    asset_type: AssetType
    asset_symbol: str
    data_type: DataType
    source: DataSource
    granularity: Granularity
    expiry_type: ExpiryType
    update_type: UpdateType
    start: 'datetime'
    end: 'datetime | None'

    @classmethod
    def of(cls, entry: asset_dataset_store.AssetDatasetStoreCreate) -> 'OverlapKey':
        """The overlap key of an entry that is already fully built.

        For the caller that has a whole entry in hand before it checks anything -- upsert_entry
        below. The dataset path does NOT use this: it has no entry until the ack, which is the
        whole reason this type exists.
        """
        return cls(**{name: getattr(entry, name) for name in _OVERLAP_KEY_FIELDS})

    def extend(self, *, expiry: 'datetime | None', feed: Feed) -> asset_dataset_store.AssetDatasetStoreCreate:
        """The entry this key becomes once the ack has resolved a tape.

        CONSTRUCTION, NOT model_copy(update=...) (tj-xn3qa6 D4). model_copy does NOT re-validate, so
        it would set the one field tj-3mk3u5.30 exists to make trustworthy while skipping its
        validation -- on an identity column, where no later correction could rewrite a wrong value.
        Going through the model's constructor validates every field, feed included.

        Args:
            expiry: When this dataset's data dies. Not identity, which is why it is not on this key.
            feed: The RESOLVED tape, off FetchAccepted. Never the caller's preference.

        Returns:
            asset_dataset_store.AssetDatasetStoreCreate: The entry, as it will be written.
        """
        return asset_dataset_store.AssetDatasetStoreCreate(**vars(self), expiry=expiry, feed=feed)

    def column_values(self) -> dict:
        """This key as StoreDatasetEntry column values, with the custom types applied.

        `end` goes through NullableDateTime here, so an open-ended key compares against the EPOCH
        sentinel the column actually holds rather than against a NULL.

        A METHOD ON THE KEY, not a get_fields() call at the call site: get_fields reads whatever
        __dict__ it is handed, so handing it an AssetDatasetStoreCreate by mistake would quietly
        add a feed term to the refusal rather than fail. Only this type has this method.
        """
        return StoreDatasetEntry.get_fields(self, exclude_none=True)


_OVERLAP_KEY_FIELDS = tuple(key_field.name for key_field in fields(OverlapKey))

# The RANGE, which the overlap check compares rather than equates.
_RANGE_COLUMNS = ('start', 'end')

# The own-overlap REFUSAL's equality columns: the OverlapKey minus its range, so the two cannot
# drift. Order does not matter for these equality checks; StoreDatasetEntry.NATURAL_KEY has the
# order that matters, for the index.
_OVERLAP_EQUALITY_COLUMNS = tuple(name for name in _OVERLAP_KEY_FIELDS if name not in _RANGE_COLUMNS)

# The IDENTITY key's equality columns: the refusal's, PLUS feed (tj-vhboky.1 section 2, tj-xn3qa6
# D1). Spelled as an extension of the tuple above because that is exactly the relationship between
# the two keys -- identity is the overlap key plus the tape -- and writing it that way means a
# column added to one can never be forgotten in the other.
_IDENTITY_EQUALITY_COLUMNS = (*_OVERLAP_EQUALITY_COLUMNS, 'feed')


# THE FIVE DOMAIN REFUSALS OF THIS MODULE ARE LEAVES OF InvalidRequestError (ADR tj-fa1rpu D5,
# TE-6). Each keeps its own constructor, its own attributes and the message it has always formatted
# -- that message is now the error's `detail` -- and passes its reason from the one table in
# common/errors/vocabulary.py. The HTTP edge reads the reason's row for the status (404, 403, 409),
# so no route maps these by hand any more and no route may swallow one (D6). None of them is a
# rate-limit reason, so none carries reset_at and none uses from_retry_after, which is only valid
# on a class that keeps the base constructor's signature.
#
# They are no longer ValueError. That is the point: a database write's own ValueError and a refusal
# the caller can act on were indistinguishable to an `except ValueError`, and the vocabulary exists
# to tell them apart.


def _joined_ids(entry_ids: list[uuid.UUID]) -> str:
    """Render entry ids for a `detail` sentence: comma-separated plain ids, never a Python repr.

    ONE FUNCTION FOR BOTH SENTENCES, which is the whole point (tj-8feral): the two refusals that
    name colliding ids format them identically, so neither can drift into a language-specific
    rendering the way the list interpolation did. str(), not repr(): a UUID's str is the plain
    canonical form, its repr is `UUID('...')`, and a list interpolates its elements with the
    latter.
    """
    return ', '.join(str(entry_id) for entry_id in entry_ids)


class EntryNotFound(InvalidRequestError):
    """Raised by an id-addressed write naming an entry that does not exist."""

    def __init__(self, entry_id: uuid.UUID):
        super().__init__(Reason.NOT_FOUND, f'No entry found with ID {entry_id}')
        self.entry_id = entry_id


class OwnerMismatch(InvalidRequestError):
    """Raised by an id-addressed write whose declared principal does not own the entry.

    Reachable ONLY here, and never on the create path (delete_entry_by_id, update_entry,
    update_entry_lifecycle) -- tj-vhboky.1 section 5. Because owner is identity, two principals
    asking for the same spec on create get two different entries, so there is never a cross-owner
    conflict to detect there; anyone expecting a check on create is looking for something that
    should not exist. Deliberately does NOT carry or format the real owner: reads are open, but
    an error body is not a read endpoint. That still holds now the detail reaches the wire as
    problem+json -- the sentence names the entry, never the principal, and `owner` is not a
    METADATA_KEYS member, so no metadata route could carry it either.
    """

    def __init__(self, entry_id: uuid.UUID):
        super().__init__(Reason.OWNER_MISMATCH, f'Entry {entry_id} is not owned by the declared principal')
        self.entry_id = entry_id


class OwnOverlapConflict(InvalidRequestError):
    """Raised when a create request overlaps one of the SAME owner's existing datasets.

    Not an exact repeat (tj-vhboky.1 section 3(b)): an exact repeat -- every identity field
    equal, the range included -- is handled by the
    upsert's ON CONFLICT clause as a no-op and must never reach here, or a retried POST would
    stop being idempotent. A different owner's overlapping dataset is not a collision either: it
    is a different dataset by construction, because owner is part of the key.

    THE IDS TRAVEL AS METADATA, not only inside the sentence (tj-vhboky.8): the caller's contract
    is "read the ids, then extend", so colliding_ids is an allowlisted metadata key and becomes a
    member of the 409 problem+json body. They are formatted here, once, so both renderers write
    the same text.

    THE SENTENCE JOINS THE IDS BY HAND AND MUST KEEP DOING SO (tj-8feral). Interpolating the list
    itself rendered `[UUID('1111-...'), UUID('2222-...')]`, because a list formats its elements
    with repr(). RFC 9457 says a client never parses `detail` and colliding_ids carries clean
    strings alongside, so nothing was broken -- but this repo is the PUBLIC, GENERIC framework
    consumed through a typed client SDK, and `detail` is the sentence an operator reads in a log
    and an SDK user reads in an error. `UUID(...)` publishes the server's IMPLEMENTATION LANGUAGE
    into a language-neutral contract: a Go or TypeScript caller learns it is talking to Python,
    in the one field whose whole job is to be read by a human who is not us. It is also the shape
    this project already ruled against one service over -- data/ingest's pitfall 4 records that
    `str(APIError)` is the raw vendor body and that the remedy is to write your own `detail`;
    letting a language's default repr become wire text is the same mistake with the vendor
    replaced by the standard library.
    """

    def __init__(self, colliding_ids: list[uuid.UUID]):
        super().__init__(
            Reason.OWN_OVERLAP_CONFLICT,
            f'Overlaps existing dataset(s) of the same owner: {_joined_ids(colliding_ids)}',
            metadata={'colliding_ids': [str(entry_id) for entry_id in colliding_ids]},
        )
        self.colliding_ids = colliding_ids


class RangeShrink(InvalidRequestError):
    """Raised when an update to an entry's range would shrink it rather than grow it.

    Ruling 8 (tj-vhboky.1 section 4): shrinking strands already-stored bars outside the entry's
    declared coverage -- a quieter version of the coverage lie this epic exists to remove.
    Shrinking is a delete and should be one.
    """

    def __init__(self, entry_id: uuid.UUID):
        super().__init__(Reason.RANGE_SHRINK, f'Update would shrink entry {entry_id}; growth only, shrink by deleting')
        self.entry_id = entry_id


class RangeCollision(InvalidRequestError):
    """Raised when growing an entry's range would make it identical to another entry the same owner already holds.

    tj-vhboky.1 section 4: "An extend can collide... reject with that id -- the same rule and the
    same response shape as create." THE SAME RESPONSE SHAPE is why the one colliding id travels
    under colliding_ids, the same metadata key OwnOverlapConflict uses, as a one-element list: a
    caller that reads the ids off a create conflict reads them off an extend conflict unchanged.

    ITS SENTENCE IS WRITTEN THE SAME WAY AS OwnOverlapConflict's (tj-8feral), through _joined_ids,
    although a bare UUID already str()s correctly here and this one never had the repr defect. The
    point is that the two cannot drift: the next person to add a second id to this message must not
    have to rediscover why a list may not be interpolated.
    """

    def __init__(self, colliding_id: uuid.UUID):
        super().__init__(
            Reason.RANGE_COLLISION,
            f'New range collides with existing dataset {_joined_ids([colliding_id])}',
            metadata={'colliding_ids': [str(colliding_id)]},
        )
        self.colliding_id = colliding_id


def _equality_conditions(values: dict, columns: tuple[str, ...]) -> list:
    """The equality conditions for `columns`, built from a dict of column name to value.

    WHICH TUPLE THE CALLER PASSES IS THE DECISION, not a detail (tj-xn3qa6 D1): the own-overlap
    refusal passes _OVERLAP_EQUALITY_COLUMNS and the identity-collision check passes
    _IDENTITY_EQUALITY_COLUMNS, which is the same tuple plus feed. It is a parameter rather than a
    module constant read in here precisely so neither caller can pick up the other's key by
    accident.
    """
    return [getattr(StoreDatasetEntry, column) == values[column] for column in columns]


async def _find_own_overlap(db: AsyncSession, field_values: dict) -> list[uuid.UUID]:
    """Find existing entries of the SAME owner whose range overlaps this request's, excluding an exact repeat.

    Ranges are half-open, [start, end). The formula, from the ADR (tj-vhboky.1 section 3 as amended
    by the addendum HALF-OPEN RANGES, 2026-09-30):

        existing.start < request.end AND existing.end > request.start
        AND NOT (existing.start = request.start AND existing.end = request.end)

    so two ranges that merely touch (one's end equals the other's start) do NOT overlap.

    `end` uses the EPOCH sentinel for "no end", not a NULL (common/database/
    sql_alchemy_nullable_datetime.py: None in the schema maps to 1970-01-01 in the column, so two
    open-ended entries still collide on the unique key). EPOCH is far in the PAST, so a naive
    `existing.end > request.start` reads backwards for an open-ended EXISTING entry, and a naive
    `existing.start < request.end` reads backwards for an open-ended REQUEST. Both are handled
    explicitly rather than compared as literal values.

    FEED IS NOT ONE OF THE EQUALITY TERMS (tj-xn3qa6 D1). See OverlapKey for why: this asks whether
    the owner already holds data covering this window, not whether this is the same dataset.

    AND THE EXCLUSION IS FEED-BLIND TOO, which is what makes tj-f2qz44 reachable -- read this
    before "tightening" it. The NOT(...) below excludes a row whose range is EXACTLY the request's,
    on range alone. So a request differing from an existing entry ONLY by tape excludes that entry
    here, is not refused, and goes on to the upsert, where the eleven-column ON CONFLICT does not
    match it either -- because feed IS identity -- and a SECOND entry is created. That is precisely
    tj-f2qz44's criterion, "requests differing only by feed resolve to different entries rather
    than merging". Adding feed to this exclusion would make that criterion unreachable through the
    only path that leads to it: the same-window SIP request would be refused with a 409 instead.
    A different-tape entry over a merely OVERLAPPING window is still reported, which is the case
    D1 argues is worth telling the caller about.
    """
    new_start = field_values['start']
    new_end = field_values['end']

    conditions = _equality_conditions(field_values, _OVERLAP_EQUALITY_COLUMNS)
    # existing.end > request.start (strict, half-open), with an open-ended existing entry (end ==
    # EPOCH) always satisfying it: an entry with no declared end covers everything from its start onward.
    conditions.append(or_(StoreDatasetEntry.end == NullableDateTime.EPOCH, StoreDatasetEntry.end > new_start))
    if new_end != NullableDateTime.EPOCH:
        # existing.start < request.end (strict), only when the request itself has a declared end.
        conditions.append(StoreDatasetEntry.start < new_end)
    # else: the request is open-ended, so nothing can start "after" its end -- no upper-bound
    # predicate is needed; every existing start already satisfies it.
    conditions.append(not_(and_(StoreDatasetEntry.start == new_start, StoreDatasetEntry.end == new_end)))

    stmt = select(StoreDatasetEntry.id).where(*conditions)
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def check_own_overlap(db: AsyncSession, key: OverlapKey) -> None:
    """Refuse a create that overlaps one of the SAME owner's datasets, BEFORE the caller spends anything on it.

    SPLIT OUT OF upsert_entry_in_transaction BY tj-hywf7w, and the cutover's order is the reason.
    The dataset path opens the FetchDataset stream FIRST and upserts the entry on the ack, because
    only the ack carries the resolved feed (tj-3mk3u5.10). With the overlap check still buried in
    that upsert, a request the store already knew it would refuse had, by the time it was refused,
    cost a rate-budget slot and a live vendor call -- ingest performs the fetch before it yields the
    ack. This check depends on nothing the ack carries: it is one local SELECT over this store's own
    rows, so a caller runs it first and an own-overlap 409 costs what it cost on the Kafka path,
    which is nothing.

    THE CHECK IS THE CALLER'S TO MAKE NOW. upsert_entry_in_transaction no longer runs it, so a
    caller that writes an entry calls this first: data/store/app/ingest/data_action_request.py does
    it before opening the stream, and upsert_entry below does it inside its own transaction. Those
    are the two write paths, and there are no others.

    IT NEITHER COMMITS NOR ROLLS BACK: it only reads, inside whatever transaction the caller owns.

    IT TAKES AN OverlapKey, NOT AN ENTRY, AND THAT IS WHAT MAKES THE HOIST POSSIBLE (tj-xn3qa6 D3).
    A create model carries a REQUIRED, RESOLVED feed that does not exist until FetchAccepted, so a
    check keyed on one could never run before the stream. See OverlapKey for the two-keys argument
    and for why the entry is a strict extension of this value rather than a second construction.

    Args:
        db: The session, inside the caller's transaction.
        key: The window the caller is about to ask for, as the refusal keys on it.

    Raises:
        OwnOverlapConflict: If the request overlaps one of the same owner's existing datasets
            without being an exact repeat. An exact repeat is excluded by the select itself and
            resolved by the upsert's ON CONFLICT, so a retried POST stays idempotent.
    """
    colliding_ids = await _find_own_overlap(db, key.column_values())
    if colliding_ids:
        raise OwnOverlapConflict(colliding_ids)


async def _find_exact_collision(
    db: AsyncSession, existing: StoreDatasetEntry, new_start: 'datetime', new_end: 'datetime'
) -> uuid.UUID | None:
    """Find another entry of the SAME owner that a range grow on `existing` would exactly collide with.

    tj-vhboky.1 section 4. The non-range fields are read off `existing` itself, not off the
    incoming request, because they are immutable -- an update only ever changes start and/or end.

    THIS ONE KEYS ON IDENTITY, feed INCLUDED, and it is the counterpart to _find_own_overlap's
    feed-blind key rather than an inconsistency with it (tj-xn3qa6 D1). "Collides" here means
    "would be the SAME DATASET as another row", i.e. would violate the eleven-column unique
    constraint -- so it must ask identity's question with identity's key. Left feed-blind it would
    refuse a grow that lands on another tape's entry, which the database would have accepted
    quite happily: a spurious 409 on a write that was never a duplicate.
    """
    values = {column: getattr(existing, column) for column in _IDENTITY_EQUALITY_COLUMNS}
    conditions = _equality_conditions(values, _IDENTITY_EQUALITY_COLUMNS)
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


async def upsert_entry_in_transaction(
    db: AsyncSession, entry: asset_dataset_store.AssetDatasetStoreCreate
) -> uuid.UUID:
    """Create a dataset entry, or resolve an exact repeat, INSIDE A TRANSACTION THE CALLER OWNS.

    THIS FUNCTION NEVER COMMITS AND NEVER ROLLS BACK (tj-vz1eta s2, carried onto tj-3mk3u5.10).
    The caller opens one write_transaction and decides when the work is done. That is what lets
    data/store/app/ingest/data_action_request.py put the entry upsert and every page of bars from
    one fetch into a SINGLE transaction committed only after the stream's FetchDone: a fetch that
    breaks, times out or is cancelled then leaves neither a new entry nor partial bars. Burying
    the commit here would make that impossible, and the ledger work (tj-3mk3u5.34) moves the
    commit again, to once per page, against this same function.

    THE OWN-OVERLAP CHECK IS NOT RUN HERE ANY MORE (tj-hywf7w). It is check_own_overlap above, and
    the caller runs it before this -- the dataset path before it opens the fetch stream, upsert_entry
    inside its transaction. It was moved rather than copied because a check the caller already made
    is a second identical SELECT on every successful write, and because the ONLY reason it lived in
    here was that this used to be the first thing the create path did. An exact repeat is still
    resolved by the ON CONFLICT clause below, which is this function's own business and stays.

    upsert_entry below is the committing wrapper for callers that write one entry and nothing
    else. See it for what the upsert itself does and why.

    Args:
        db: The session, already inside the caller's transaction.
        entry: The dataset to create or match.

    Returns:
        uuid.UUID: The id of the created entry, or of the one an exact repeat matched.
    """
    log.debug(f'Upserting entry: {entry}')
    field_values = StoreDatasetEntry.get_fields(entry, exclude_none=True)

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
    return result.scalar_one()


async def upsert_entry(db: AsyncSession, entry: asset_dataset_store.AssetDatasetStoreCreate) -> uuid.UUID:
    """Create a dataset entry, or resolve an exact repeat to the entry it already matches.

    IDENTITY REPLACES MERGING (tj-vhboky.1 section 2): every field, the range AND the resolved
    tape included, is part of the single ELEVEN-column unique key
    StoreDatasetEntry.NATURAL_KEY_CONSTRAINT (feed joined it under tj-3mk3u5.31). Two requests
    that disagree on anything but the range are two different datasets, so the max-rank merge
    that used to reconcile expiry_type/update_type on conflict is gone outright -- it silently
    handed a caller a lifetime nobody asked for.

    Two outcomes, not one on top of the other (tj-vhboky.1 section 3):
    * An EXACT identity match is a no-op: the ON CONFLICT branch below returns the existing id,
      refreshing only expiry and updated_at.
    * An overlap that is NOT an exact match is a conflict of the SAME owner's own datasets,
      checked BEFORE the insert is attempted (check_own_overlap, called first inside this
      function's transaction) and reported by id, so the caller can act on it -- a caller can
      build auto-extend on top of fail, not the reverse.
    A different owner's overlapping dataset is neither case: owner is part of the key, so it is a
    different dataset by construction and this function never compares across owners.

    THE TRANSACTION IS THIS FUNCTION'S, which is what distinguishes it from
    upsert_entry_in_transaction above: it opens one write_transaction and commits the entry on its
    own. A caller writing an entry AND bars in one unit of work calls the other one instead.
    """
    async with write_transaction(db, 'create or update entry'):
        # The overlap refusal, kept adjacent to the insert for this caller: it writes one entry and
        # nothing else, so there is nothing expensive here to run it ahead of (tj-hywf7w). This
        # caller already holds a whole entry, so it projects the overlap key out of it rather than
        # building one -- the dataset path, which has no entry until the ack, builds the key first
        # and extends it afterwards (tj-xn3qa6 D3).
        await check_own_overlap(db, OverlapKey.of(entry))
        return await upsert_entry_in_transaction(db, entry)


async def update_entry(db: AsyncSession, entry: asset_dataset_store.AssetDatasetStoreUpdate) -> None:
    """Grow an existing entry's range in place, keeping its id (tj-vhboky.1 section 4).

    Only start and/or end are written. Every other field on `entry` is identity and immutable by
    ruling -- symbol, source and granularity define what the stored bars ARE, and expiry_type,
    update_type and owner could otherwise mutate this entry into colliding with another of the
    same owner's entries for a reason the caller cannot see. Growth only: shrinking strands
    already-stored bars outside the entry's declared coverage, and shrinking is a delete's job.
    """
    async with write_transaction(db, 'update entry'):
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

        stmt = (
            update(StoreDatasetEntry)
            .where(StoreDatasetEntry.id == entry.id)
            .values(start=new_start, end=new_end, updated_at=func.now())
        )
        await db.execute(stmt)


async def update_entry_lifecycle(db: AsyncSession, id: uuid.UUID, owner: str) -> None:
    """Updates only the `updated_at` for an existing entry."""
    # TODO might not be necessary
    async with write_transaction(db, 'update entry lifecycle'):
        existing = await _get_entry_or_raise(db, id)
        _check_owner(existing, owner)
        stmt = update(StoreDatasetEntry).where(StoreDatasetEntry.id == id).values(updated_at=func.now())
        await db.execute(stmt)


async def get_entry_by_id(db: AsyncSession, id: uuid.UUID) -> asset_dataset_store.AssetDatasetStore:
    """Retrieve an entry by its ID, with its bar count, as GET /store/{id} answers it (tj-967trx).

    Shaped exactly like one element of search_entries: the entry whole, plus item_count from an OUTER join so
    an entry with no bars reads 0 rather than vanishing. This used to hand a Row to model_validate, which
    never worked (tj-b2uqfl), and had no caller.

    Args:
        db: The session.
        id: The entry's id.

    Returns:
        asset_dataset_store.AssetDatasetStore: The entry.

    Raises:
        EntryNotFound: If no entry has this id (NOT_FOUND, a 404 at the edge).
    """
    stmt = (
        select(StoreDatasetEntry, func.count(StockMarketActivity.id).label('item_count'))
        .outerjoin(StockMarketActivity, StoreDatasetEntry.id == StockMarketActivity.dataset_id)
        .where(StoreDatasetEntry.id == id)
        .group_by(StoreDatasetEntry.id)
    )
    found = (await db.execute(stmt)).first()
    if found is None:
        raise EntryNotFound(id)
    entry, item_count = found
    return entry.to_validated_schema(asset_dataset_store.AssetDatasetStore, additional={'item_count': item_count})


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

    # owner and feed are BOTH filterable columns here: each is exposed by StoreAssetDatasetQuery
    # and is a real StoreDatasetEntry column, so this loop picks them up with no change --
    # verified, not assumed. feed's filter was declared by tj-3mk3u5.30 and the column it needs
    # landed with tj-3mk3u5.31; before the column existed the filter was not inert but a live
    # AttributeError on any search naming a tape, which is why it was removed rather than left
    # declared under tj-rh4b7f. Having recorded which tape served a dataset, a caller can now ask
    # for one.
    #
    # The log line renders the value directly, which neither the bind type (M1) nor the schema
    # alias (M2) reaches, so a SensitiveString column logs the marker (tj-vhboky.41 Addendum 1,
    # D3). The type is read off the table so the column declaration stays the one list.
    for column, value in request_query.model_dump().items():
        table_column = StoreDatasetEntry.__table__.c.get(column)
        is_sensitive = table_column is not None and isinstance(table_column.type, SensitiveString)
        log.debug(f'Filtering by {column}: {REDACTED if is_sensitive else value}')
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
    async with write_transaction(db, f'delete entry {id}'):
        existing = await _get_entry_or_raise(db, id)
        _check_owner(existing, owner)
        stmt = delete(StoreDatasetEntry).where(StoreDatasetEntry.id == id)
        result = await db.execute(stmt)
        if result.rowcount == 0:
            # Raced with a concurrent delete between the SELECT above and this statement. The
            # rollback for this path comes from write_transaction, the same as every other error
            # path here (tj-ck5spw).
            raise EntryNotFound(id)
