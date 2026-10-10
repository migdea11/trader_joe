"""Dataset identity, own-overlap failure, the owner check on id-addressed writes, and the cascade delete.

WHY THIS FILE EXISTS (validator, gating tj-vhboky.6). Commit c1117c9 rewrote
data/store/app/database/crud/stock/store_dataset_entry.py and reported its own coverage as `none`
for all of it: five new exception classes (EntryNotFound, OwnerMismatch, OwnOverlapConflict,
RangeShrink, RangeCollision), a rewritten upsert_entry, a rewritten update_entry, and a new
required `owner` parameter on delete_entry_by_id and update_entry_lifecycle. Only the
search_entries expiry fix was reachable from an existing test, through test_http_smoke.py. Nothing
else in the suite calls this module at all, so every semantic ruling below could have been reversed
without a red test.

WHAT TIER THIS IS. These drive the real crud functions against a recording fake session, and assert
on the SQL they actually build plus the exceptions they actually raise. There is no database. Three
claims in this epic are therefore NOT provable here and are not asserted anywhere in this file:
that Postgres rejects a duplicate on uq_store_dataset_entry_identity, that ON DELETE CASCADE
removes exactly one entry's bars, and that ON CONFLICT DO UPDATE fires at all. Those are the
host-verified tier (tj-vhboky.14) and must never be inferred from this file being green. What IS
proved here is the half that a unit test owns: that the statements we hand the database say what
the rulings say they should, and that the checks placed in front of them fire before any SQL is
sent.

THE RULINGS PINNED, each traceable to tj-vhboky.1 and its amendments:
* A byte-identical repeat returns the existing id and creates nothing -- ON CONFLICT targets the
  identity constraint BY NAME and its SET clause touches no identity column. A test that let an
  exact repeat create a second row would be pinning a regression: a retried POST, a restarted
  subscription and a re-run backfill all rest on this (tj-3mk3u5.3, accepted).
* An overlapping-but-not-identical request FAILS and names the colliding ids, checked before the
  insert -- a caller can build auto-extend on top of fail, never the reverse.
* The EPOCH sentinel is handled on BOTH sides of the overlap query: the stored `end` and an
  open-ended requested `end`. 1970-01-01 is in the PAST, so a naive comparison reads backwards.
* The owner check lives ONLY on id-addressed writes and is UNREACHABLE on create by design, because
  owner is part of identity. A test asserting a create-path owner check would be pinning something
  that should not exist, so this file asserts the opposite (see
  test_the_create_path_never_consults_the_owner_check).
* An update changes the range and only by growth, and never touches an identity column or the id.
"""

import dataclasses
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import ExogenousError, Reason
from data.store.app.database.crud.stock import store_dataset_entry as crud
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_store.asset_dataset_store import (
    AssetDatasetStore,
    AssetDatasetStoreCreate,
    AssetDatasetStoreUpdate,
    StoreAssetDatasetPath,
    StoreAssetDatasetQuery,
)


pytestmark = pytest.mark.data_store

JANUARY = datetime(2026, 1, 1, tzinfo=UTC)
FEBRUARY = datetime(2026, 2, 1, tzinfo=UTC)
MARCH = datetime(2026, 3, 1, tzinfo=UTC)
APRIL = datetime(2026, 4, 1, tzinfo=UTC)
OWNER = 'strategy-a'


class FakeResult:
    """One canned answer, faithful to the four accessors this module actually uses.

    rowcount is a real attribute rather than a MagicMock: delete_entry_by_id compares it to 0, and
    a MagicMock compares unequal to 0 forever -- which is the exact shape of the dead
    `if result == 0` check Amendment 1 item R was raised to fix. A permissive fake here would let
    that defect back in silently.
    """

    def __init__(self, rows: list[tuple], rowcount: int = 1):
        self._rows = rows
        self.rowcount = rowcount

    def all(self) -> list[tuple]:
        return self._rows

    def first(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0][0]

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None


class FakeSession:
    """An async session that records every statement and reaches no database.

    The recording is what lets a test assert a check fired BEFORE the statement rather than after
    it. A check placed after the SQL was built and sent would still raise the same exception and
    would still be the defect these checks exist to prevent, so "it raised" is never sufficient on
    its own.
    """

    def __init__(self, *results: FakeResult):
        self._results = list(results)
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        assert self._results, 'the crud path issued more statements than the fixture planned for'
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _sql(statement) -> str:
    """The statement as Postgres would receive it, whitespace-normalised.

    The postgresql dialect specifically: ON CONFLICT and the custom column types are dialect-level
    constructs, and compiling against the default dialect would not render them.
    """
    return ' '.join(str(statement.compile(dialect=postgresql.dialect())).split())


def _params(statement) -> dict:
    """The bound values, as Python objects.

    Preferred over reading literals out of the SQL text wherever the VALUE is the claim -- an EPOCH
    sentinel or a scoped owner is a datetime and a string, not a rendering.
    """
    return statement.compile(dialect=postgresql.dialect()).params


def _only(statements: list, keyword: str):
    """The single statement of the given kind, failing loudly if there is not exactly one.

    Used instead of indexing by position so that adding a statement to the path breaks the test
    that cares about the count rather than silently shifting what another test asserts on.
    """
    matching = [statement for statement in statements if _sql(statement).startswith(keyword)]
    assert len(matching) == 1, f'expected exactly one {keyword} statement, got {len(matching)}'
    return matching[0]


def _insert_columns(sql: str) -> set[str]:
    listed = re.search(r'INSERT INTO store_dataset_entry \((.*?)\) VALUES', sql)
    assert listed, f'the insert does not name its columns: {sql}'
    return {column.strip().strip('"') for column in listed.group(1).split(',')}


def _equality_columns(sql: str) -> set[str]:
    """Every column the statement compares with `=`, by name.

    Deliberately `=` only. `>=`, `<=` and `!=` are range and exclusion operators and are asserted
    by the tests that own them; this one exists so the equality PREFIX can be checked against a
    derived column set rather than a list restated here.
    """
    return {name.strip('"') for name in re.findall(r'store_dataset_entry\.("?\w+"?) =', sql)}


def _do_update_clause(sql: str) -> str:
    clause = re.search(r'DO UPDATE SET (.*?) RETURNING', sql)
    assert clause, f'the upsert has no DO UPDATE ... RETURNING: {sql}'
    return clause.group(1)


def _update_set_columns(sql: str) -> set[str]:
    clause = re.search(r'^UPDATE store_dataset_entry SET (.*?) WHERE', sql)
    assert clause, f'not an UPDATE ... SET ... WHERE statement: {sql}'
    return {assignment.split('=')[0].strip().strip('"') for assignment in clause.group(1).split(', ')}


def create_request(
    end: datetime | None = None,
    owner: str = OWNER,
    symbol: str = 'AAPL',
    feed: Feed = Feed.IEX,
    start: datetime = JANUARY,
) -> AssetDatasetStoreCreate:
    """A create request. end defaults to None -- the open-ended case, where the sentinel bites.

    `feed` IS THE RESOLVED TAPE, not a caller preference (tj-3mk3u5.31). AssetDatasetStoreCreate
    declares it REQUIRED on purpose, overriding StoreAssetDatasetBody's optional field of the same
    name, so there is no default to omit here -- a fixture that left it out stopped building at all,
    which is how this file found out the column had landed.

    Spelled as a PARAMETER rather than a constant because the tests below have to be able to move
    it independently of everything else: feed is identity but is NOT an overlap term (tj-xn3qa6 D1),
    so the two keys disagree about exactly this one field and no fixture that pins it can show that.
    """
    return AssetDatasetStoreCreate(
        owner=owner,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        feed=feed,
        start=start,
        end=end,
        expiry=FEBRUARY,
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
    )


def update_request(
    entry_id: UUID,
    start: datetime = JANUARY,
    end: datetime | None = APRIL,
    owner: str = OWNER,
    symbol: str = 'AAPL',
    feed: Feed = Feed.IEX,
) -> AssetDatasetStoreUpdate:
    return AssetDatasetStoreUpdate(
        id=entry_id,
        owner=owner,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        feed=feed,
        start=start,
        end=end,
        expiry=FEBRUARY,
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
    )


def overlap_key(
    end: datetime | None = None, owner: str = OWNER, symbol: str = 'AAPL', start: datetime = JANUARY
) -> crud.OverlapKey:
    """What check_own_overlap takes now: the overlap key, not an entry (tj-xn3qa6 D3).

    IT TAKES NO feed PARAMETER AND CANNOT BE GIVEN ONE, which is the structural half of the
    decision rather than an omission here -- OverlapKey declares ten fields and the tape is not
    among them. See test_the_overlap_key_has_no_tape_to_be_keyed_on.

    Projected from a create request through the real `of` classmethod rather than built field by
    field, so this helper cannot drift from the fixture every other test in this file uses: the
    same request produces the same key, and the one difference between them stays visible.
    """
    return crud.OverlapKey.of(create_request(end=end, owner=owner, symbol=symbol, start=start))


def stored_entry(
    entry_id: UUID,
    start: datetime = JANUARY,
    end: datetime = MARCH,
    owner: str = OWNER,
    symbol: str = 'AAPL',
    feed: Feed = Feed.IEX,
) -> StoreDatasetEntry:
    """A detached ORM instance standing in for a row already in the table.

    `end` takes the stored representation, so an open-ended stored entry is spelled
    NullableDateTime.EPOCH here, exactly as the column holds it.

    `feed` is a real NOT NULL column now (tj-3mk3u5.31). It is settable here because
    _find_exact_collision reads identity -- feed included -- off the STORED entry rather than off
    the incoming request, so a test of that path needs a stored row whose tape it can choose.
    """
    return StoreDatasetEntry(
        id=entry_id,
        owner=owner,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        feed=feed,
        start=start,
        end=end,
        expiry=FEBRUARY,
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
        created_at=JANUARY,
        updated_at=JANUARY,
    )


# ---------------------------------------------------------------------------------------------
# upsert_entry: identity replaces merging
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_conflict_target_is_the_named_identity_constraint():
    """Targeting the constraint by NAME is what stops this code and the migration drifting apart.

    The previous version hand-copied a six-column list
    ('asset_symbol','granularity','start','end','source','data_type'), which is a subset of
    today's ten-column key -- so the clause would have compiled, fired against the wrong index,
    and deduplicated on the wrong thing. Asserted against
    StoreDatasetEntry.NATURAL_KEY_CONSTRAINT rather than the literal string so renaming the
    constraint in one place cannot leave this test passing against the old name.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([]), FakeResult([(entry_id,)]))

    await crud.upsert_entry(db, create_request())

    sql = _sql(_only(db.statements, 'INSERT'))
    assert f'ON CONFLICT ON CONSTRAINT {StoreDatasetEntry.NATURAL_KEY_CONSTRAINT} DO UPDATE' in sql, sql
    # A column-list conflict target renders as ON CONFLICT (col, ...) -- the shape being ruled out.
    assert 'ON CONFLICT (' not in sql, 'the conflict target regressed to a hand-copied column list'


@pytest.mark.asyncio
async def test_the_conflict_update_touches_no_identity_column():
    """An exact repeat refreshes expiry and updated_at and NOTHING else (Amendment 1 item P).

    THIS IS THE TEST THAT KEEPS THE MAX-RANK MERGE DELETED. The removed code took the
    higher-ranked expiry_type / update_type on conflict, which reconciled two requests that are now
    two different datasets by ruling and handed the caller a lifetime nobody asked for. Re-adding
    either case(...) expression puts an identity column into this SET clause and reddens this test.

    Asserted as "no natural-key column appears at all" rather than as an exact equality on the
    clause, so it states the invariant -- identity is immutable on conflict -- instead of today's
    rendering.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([]), FakeResult([(entry_id,)]))

    await crud.upsert_entry(db, create_request())

    clause = _do_update_clause(_sql(_only(db.statements, 'INSERT')))
    assert 'expiry' in clause and 'updated_at' in clause, clause
    leaked = [column for column in StoreDatasetEntry.NATURAL_KEY if column in clause]
    assert leaked == [], f'the conflict update writes identity columns {leaked}: {clause}'


@pytest.mark.asyncio
async def test_an_exact_repeat_resolves_to_the_existing_id_without_a_second_insert():
    """The idempotency contract, at the level a unit test can reach it.

    A repeat is ONE statement that returns an id, not a select-then-insert and not two inserts.
    The returned id is the statement's, so a rewrite that discarded RETURNING and invented a fresh
    uuid -- the plausible wrong implementation, since it looks like it works -- fails here.

    What this does NOT prove: that Postgres actually matches the constraint and takes the DO UPDATE
    branch. That needs a live database (tj-vhboky.14) and must not be read off this test.
    """
    existing_id = uuid4()
    db = FakeSession(FakeResult([]), FakeResult([(existing_id,)]))

    returned = await crud.upsert_entry(db, create_request())

    assert returned == existing_id
    assert len(db.statements) == 2, 'the create path issued statements beyond the overlap check and the upsert'
    assert _sql(db.statements[1]).startswith('INSERT'), 'the upsert is no longer a single insert'
    assert db.commits == 1
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_the_insert_names_every_identity_column():
    """exclude_none=True must not be able to leave a key column out of the insert.

    Postgres treats NULL as distinct from NULL in a unique index, so one omitted key column turns
    "an exact repeat returns the existing id" into "an exact repeat creates a second row" -- the
    silent failure the bead demanded be verified rather than assumed. Derived from
    StoreDatasetEntry.NATURAL_KEY so a column joining the key later is covered without editing
    this test.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request())

    named = _insert_columns(_sql(_only(db.statements, 'INSERT')))
    missing = sorted(set(StoreDatasetEntry.NATURAL_KEY) - named)
    assert missing == [], f'the insert omits identity columns, so the unique key cannot dedup: {missing}'


@pytest.mark.asyncio
async def test_an_overlapping_but_not_identical_request_fails_naming_the_colliding_ids():
    """Ruling: fail and report the id rather than auto-extend, because fail is the composable half.

    Both ids are asserted, not just the first: a caller that has to resolve the collision needs the
    whole set, and an implementation returning result.first() would satisfy a one-id assertion.
    """
    first, second = uuid4(), uuid4()
    db = FakeSession(FakeResult([(first,), (second,)]))

    with pytest.raises(crud.OwnOverlapConflict) as raised:
        await crud.upsert_entry(db, create_request())

    assert raised.value.colliding_ids == [first, second]
    assert str(first) in str(raised.value) and str(second) in str(raised.value)


@pytest.mark.asyncio
async def test_the_overlap_check_runs_before_any_insert_is_sent():
    """Placement, not just behaviour.

    A collision detected by letting the insert fail would raise a unique violation on an EXACT
    repeat too, which is precisely the conflation FLAG 1 on the epic exists to prevent. So the
    check has to be a select that runs first, and the absence of an INSERT is the real assertion.
    """
    db = FakeSession(FakeResult([(uuid4(),)]))

    with pytest.raises(crud.OwnOverlapConflict):
        await crud.upsert_entry(db, create_request())

    assert len(db.statements) == 1, 'more than the overlap select was sent'
    assert _sql(db.statements[0]).startswith('SELECT')
    assert db.commits == 0, 'a rejected create committed something'


@pytest.mark.asyncio
async def test_the_overlap_check_excludes_an_exact_repeat_from_its_own_result_set():
    """The one clause the idempotency guarantee rests on, and the one nothing else here reaches.

    An exact repeat overlaps itself perfectly, so without
    `NOT (existing.start = request.start AND existing.end = request.end)` the overlap select
    returns the existing row and every retried POST, restarted subscription and re-run backfill
    raises OwnOverlapConflict instead of returning the id it already has -- tj-3mk3u5.3 reversed,
    and exactly the exact-repeat/overlap conflation FLAG 1 on the epic exists to prevent.

    The delete-the-line mutation is invisible to every other test in this file: the fake session
    decides what the select RETURNS, so a path that would collide against a real database still
    reads back an empty result here. The term is therefore asserted on the compiled SQL, the same
    way the stored-sentinel disjunction above is, and it is the create-path analogue of the
    `id !=` self-exclusion asserted on the update path in
    test_the_collision_check_reads_identity_off_the_stored_entry_not_the_request.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=MARCH))

    statement = db.statements[0]
    sql = _sql(statement)
    term = re.search(
        r'NOT \(store_dataset_entry\.start = %\((\w+)\)s AND store_dataset_entry\."end" = %\((\w+)\)s\)', sql
    )
    assert term, f'an exact repeat is no longer excluded from the overlap set, so a retry becomes a 409: {sql}'
    params = _params(statement)
    assert (params[term.group(1)], params[term.group(2)]) == (JANUARY, MARCH), (
        'the exclusion names the mirror of the requested range, so an exact repeat is not excluded'
    )


# THE OWN-OVERLAP REFUSAL'S EQUALITY COLUMNS, RESTATED FROM THE DESIGN AND NOT DERIVED, which
# reverses what this file used to do here and the reversal is the point (tj-xn3qa6 D1,
# tj-3mk3u5.31).
#
# Until feed landed, this set was NATURAL_KEY minus the range, and deriving it was right: the two
# were the same set, and a derived set could not desynchronise from an amendment. They are NO
# LONGER THE SAME SET. Identity is eleven columns and the refusal keys on eight -- identity asks
# "is this the same dataset", the refusal asks "does this owner already hold data covering this
# window", and only the first gets the tape. So NATURAL_KEY minus the range is now the WRONG
# expectation and nothing derivable from the model is the right one.
#
# NOR IS IT DERIVED FROM crud._OVERLAP_EQUALITY_COLUMNS, which is the tempting repair and is
# vacuous: that constant IS the thing under test, so a test reading it follows a wrong edit down
# and stays green through exactly the regression it exists to catch. (The sibling gate in
# schemas/tests made the same call for the same reason -- it listed the models taking a resolved
# feed by name rather than deriving them from is_required().) Restated here, a column added to or
# removed from the refusal reddens this test, which is what a hand-maintained list buys.
_REFUSAL_EQUALITY_COLUMNS = {
    'owner',
    'asset_symbol',
    'asset_type',
    'data_type',
    'source',
    'granularity',
    'expiry_type',
    'update_type',
}


@pytest.mark.asyncio
async def test_the_overlap_check_equates_exactly_the_eight_columns_the_refusal_keys_on():
    """The equality prefix, pinned in BOTH directions against a restated list.

    Two rows only conflict when the owner already holds data covering this window, which means
    every identity column but the range pair AND THE TAPE must be compared. Drop one and the check
    WIDENS: a different granularity, a different source or a different data type starts reading as
    a collision, and a legitimate create is rejected with someone else's id. Nothing else here
    notices -- only `owner` is otherwise pinned, by
    test_the_overlap_check_is_scoped_to_the_requesting_owner.

    ADD one -- feed being the only candidate -- and the check NARROWS, which is the regression in
    the other direction and the one tj-xn3qa6 D1 was written to prevent: a feed term makes the
    refusal depend on a value only FetchAccepted carries, so the check could no longer run before
    the stream and tj-hywf7w's saving (a 409 that costs no vendor call) silently becomes nominal.
    Equality rather than a one-sided `missing` check is what catches that half.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=MARCH))

    # start and end are expected here and are NOT part of the prefix: they come from the
    # exact-repeat exclusion, NOT(existing.start = request.start AND existing.end = request.end),
    # which _equality_columns cannot tell from a prefix term because both spell `column =`. The
    # exclusion has its own tests (test_the_overlap_check_excludes_an_exact_repeat_from_its_own_
    # result_set and the two bound-predicate tests); naming the pair here rather than subtracting
    # it keeps this assertion two-sided, so a ninth equality column still shows up as an extra.
    compared = _equality_columns(_sql(db.statements[0]))
    expected = _REFUSAL_EQUALITY_COLUMNS | {'start', 'end'}
    assert compared == expected, (
        'the own-overlap refusal does not key on exactly the eight columns the design gives it: '
        f'missing {sorted(expected - compared)}, extra {sorted(compared - expected)}'
    )


@pytest.mark.asyncio
async def test_the_overlap_check_does_not_equate_the_tape_although_it_is_identity():
    """tj-xn3qa6 D1, asserted as its own claim rather than as a side effect of the set above.

    THE TWO KEYS ARE DIFFERENT KEYS. This is the single column they disagree about, so it is the
    whole content of the decision, and it is stated here separately so that the reason survives a
    future edit to the eight-column list: a reader who sees only an equality assertion learns that
    feed is absent, not that its absence is deliberate.

    The second assertion is what keeps the first one honest. "feed is not an equality term" is also
    trivially true of a build in which feed is not a column at all -- which is what HEAD looked
    like before tj-3mk3u5.31 -- so this test would have passed for the wrong reason throughout the
    deferral. Pinning that feed IS in NATURAL_KEY makes the pair say what it means: the column
    exists, it is identity, and the refusal still does not read it.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=MARCH))

    compared = _equality_columns(_sql(db.statements[0]))
    assert 'feed' not in compared, (
        'the own-overlap refusal keys on feed, so it now depends on a value only FetchAccepted '
        'carries and cannot run before the stream (tj-xn3qa6 D1)'
    )
    assert 'feed' in StoreDatasetEntry.NATURAL_KEY, (
        'feed is not an identity column, so the assertion above passes vacuously -- it would hold '
        'of a build with no feed column at all (tj-f2qz44)'
    )


def test_the_overlap_key_has_no_tape_to_be_keyed_on():
    """The STRUCTURAL half of tj-xn3qa6 D1, which is the half that cannot be undone by accident.

    The test above reads the SQL and so describes one build of _find_own_overlap. This one reads
    the TYPE: OverlapKey declares ten fields and `feed` is not one of them, so there is no tape in
    the value the refusal is handed and a feed term cannot be added to the refusal without first
    changing this type. That is a much louder edit than adding a string to a tuple, and it is why
    the builder's answer to D3 is better than passing feed=None.

    THE SECOND ASSERTION IS THE ONE WITH TEETH. "feed is absent" is also true of a nine-field key
    that dropped `source`, or of an empty dataclass, so the exact field set is pinned too -- a
    refusal that silently stopped keying on granularity would otherwise read as this test's
    success.
    """
    declared = tuple(key_field.name for key_field in dataclasses.fields(crud.OverlapKey))

    assert 'feed' not in declared, (
        'OverlapKey carries a tape, so the own-overlap refusal can be keyed on a value only '
        'FetchAccepted carries and the check can no longer run before the stream (tj-xn3qa6 D1)'
    )
    assert declared == (
        'owner',
        'asset_type',
        'asset_symbol',
        'data_type',
        'source',
        'granularity',
        'expiry_type',
        'update_type',
        'start',
        'end',
    ), f'the overlap key is not the ten fields tj-xn3qa6 D3 names: {declared}'


def test_the_identity_key_is_the_refusal_key_plus_the_tape():
    """The RELATIONSHIP between the two key constants, which is the thing the design actually states.

    Derived on purpose, and the hazard that usually makes deriving wrong does not apply: this test
    makes no claim about what either tuple CONTAINS -- test_the_overlap_check_equates_exactly_the_
    eight_columns_the_refusal_keys_on owns that, from a restated list -- only that identity is the
    refusal's columns with the tape appended and nothing else. A column added to one and forgotten
    in the other reddens here; a column added wrongly to both reddens there.

    WHY IT IS WORTH A TEST AT ALL: the two tuples are one `*` splat apart in the source today, so
    the relationship looks self-evident. It is exactly the kind of thing a later edit "unrolls" for
    readability, and the first divergence after that is silent -- a search filter or an ON CONFLICT
    keyed on nine columns while the refusal keys on eight is not a syntax error.
    """
    # The computed side first: ruff reads an UPPER_CASE attribute as the constant of the comparison
    # (SIM300), so the module constant goes on the right.
    expected_identity = (*crud._OVERLAP_EQUALITY_COLUMNS, 'feed')
    assert expected_identity == crud._IDENTITY_EQUALITY_COLUMNS, (
        'identity is no longer the own-overlap key plus the tape, so the two keys have drifted: '
        f'refusal {crud._OVERLAP_EQUALITY_COLUMNS}, identity {crud._IDENTITY_EQUALITY_COLUMNS}'
    )


def test_the_tape_sits_last_among_the_identity_key_s_equality_columns():
    """The ORDERING ruling, which is about an index and is invisible to every other test here.

    NATURAL_KEY's order is the order of the unique index behind it. A unique constraint is
    order-blind, so nothing about correctness moves if feed moves -- but the own-overlap refusal
    does NOT filter on feed and DOES filter on the other eight, so a feed sitting anywhere earlier
    truncates the index prefix that check can use at feed. Last among the equality columns, and
    immediately before the range, keeps the eight contiguous and leading.

    Asserted as a POSITION rather than as the whole tuple so that the claim is the ruling and not
    an incidental spelling: a column legitimately added later moves the literal tuple and must not
    redden this.
    """
    key = StoreDatasetEntry.NATURAL_KEY
    assert key[-3:] == ('feed', 'start', 'end'), (
        'feed is not the last equality column before the range, so the own-overlap check loses the '
        f'index prefix it does not need feed for (tj-3mk3u5.31): {key}'
    )
    assert len(key) == 11, f'the entry identity is not eleven columns: {key}'


# ---------------------------------------------------------------------------------------------
# check_own_overlap: the check as a thing a caller holds, and WHICH caller now holds it (tj-hywf7w)
#
# 12e1251 split the own-overlap refusal out of upsert_entry_in_transaction into a public
# check_own_overlap, so that the dataset path can run it BEFORE it opens the FetchDataset stream --
# ingest performs its vendor call and takes a rate-budget slot before it yields the ack, so a check
# that ran on the ack had already cost a live fetch for a request that was about to be refused.
#
# IT WAS MOVED, NOT COPIED, and that is the contract this section exists for. Every test above
# drives upsert_entry, the COMMITTING wrapper, which still runs the check -- so all of them stayed
# green through the split and none of them can see it. Nothing anywhere pinned that the IN-
# TRANSACTION core no longer runs it, which is exactly the thing a future caller gets silently
# wrong: reach for upsert_entry_in_transaction because you own the transaction, and you skip the
# overlap check entirely with no error and no red test. The two cases at the end of this section are
# a PAIR and must be read as one claim -- the check is on the wrapper, and it is NOT on the core.
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_own_overlap_raises_naming_every_colliding_id_and_sends_only_its_select():
    """The new public symbol, driven directly rather than through either of its two callers.

    Both ids, not just the first: one request can overlap several of the same owner's datasets and a
    caller builds auto-extend by reading the whole set, so an implementation returning
    ``result.first()`` would satisfy a one-id assertion and strand a caller on the second collision.
    That property is also asserted through upsert_entry above; what is new here is that it is
    asserted of the FUNCTION, which now has callers that are not upsert_entry.

    EXACTLY ONE STATEMENT, AND IT IS A SELECT, is the half that makes this a check rather than a
    write. The dataset path runs this inside its transaction but before anything else, so anything
    this function sent beyond its own probe would be issued for every fetch and, on the refusal path,
    for a request that is about to be refused.
    """
    first, second = uuid4(), uuid4()
    db = FakeSession(FakeResult([(first,), (second,)]))

    with pytest.raises(crud.OwnOverlapConflict) as raised:
        await crud.check_own_overlap(db, overlap_key())

    assert raised.value.colliding_ids == [first, second], 'the refusal does not name every colliding dataset'
    assert len(db.statements) == 1, f'check_own_overlap sent {len(db.statements)} statements, not just its probe'
    assert _sql(db.statements[0]).startswith('SELECT'), 'the overlap check is no longer a select'


@pytest.mark.parametrize('collision_count', [1, 2], ids=['one-colliding-dataset', 'two-colliding-datasets'])
@pytest.mark.asyncio
async def test_the_colliding_ids_metadata_says_the_same_as_the_attribute(collision_count: int):
    """TWO COPIES OF ONE FACT, and the 409 body is built from the one nothing else pins.

    TE-6 re-parented OwnOverlapConflict onto InvalidRequestError and gave it
    ``metadata={'colliding_ids': [...]}``, because an extension member of a problem+json body can
    only come from metadata (METADATA_KEYS is the allowlist). The class ALSO keeps its original
    ``colliding_ids`` attribute, which is what every existing assertion in this file reads and what
    a Python caller catching the exception uses.

    SO THE SAME FACT IS NOW STORED TWICE, BY HAND, IN ONE CONSTRUCTOR, and nothing made them agree.
    A constructor that passed the attribute but built the metadata from a stale local, sliced it,
    or forgot to stringify leaves every attribute assertion in this file green while the 409 body
    -- the thing tj-vhboky.8's caller contract is actually about -- carries the wrong ids or none.
    The route-level case in test_store_dataset_entry_route.py would catch the body being wrong; it
    could not say the attribute and the member had diverged, which is the shape of the bug.

    THE METADATA IS STRINGS and the attribute is UUIDs: metadata values are str or sequences of str
    by the vocabulary's own rule, since the raiser formats them so every renderer writes the same
    text. So they are compared after stringifying, which is the conversion the constructor must do.

    Args:
        collision_count: How many of the owner's own datasets the request is told it overlaps.
    """
    colliding = [uuid4() for _ in range(collision_count)]
    db = FakeSession(FakeResult([(entry_id,) for entry_id in colliding]))

    with pytest.raises(crud.OwnOverlapConflict) as raised:
        await crud.check_own_overlap(db, overlap_key())

    assert list(raised.value.metadata['colliding_ids']) == [str(entry_id) for entry_id in raised.value.colliding_ids], (
        f'the colliding_ids ATTRIBUTE says {raised.value.colliding_ids} and the METADATA says '
        f'{raised.value.metadata["colliding_ids"]}. The problem+json body is built from the metadata, '
        f'so these drifting apart means a caller reading the 409 gets different ids from one catching '
        f'the exception.'
    )
    assert list(raised.value.metadata['colliding_ids']) == [str(entry_id) for entry_id in colliding], (
        'the metadata does not name the ids the overlap select actually returned'
    )


@pytest.mark.parametrize(
    'refusal',
    [lambda ids: crud.OwnOverlapConflict(ids), lambda ids: crud.RangeCollision(ids[0])],
    ids=['own-overlap-conflict', 'range-collision'],
)
def test_no_refusal_detail_carries_a_python_repr(refusal):
    """tj-8feral: `detail` is read by a human who is not us, so it may not publish our language.

    WHAT IT RENDERED. OwnOverlapConflict interpolated its `list[uuid.UUID]` straight into the
    sentence, and a list formats its elements with repr(), so an operator and an SDK user read
    "Overlaps existing dataset(s) of the same owner: [UUID('1111-...'), UUID('2222-...')]".

    WHY IT IS WORTH A TEST THOUGH IT IS COSMETIC. RFC 9457 says a client never parses `detail` and
    colliding_ids carries clean strings alongside, so nothing was broken. But this repo is the
    public, generic framework consumed through a typed client SDK: `UUID(...)` tells a Go or
    TypeScript caller's user that they are talking to Python, in the one field whose whole job is to
    be read by a human. data/ingest's pitfall 4 already rules against the same shape with the vendor
    in place of the standard library.

    SUBSTRING-ABSENCE, DELIBERATELY, AND NOT THE TEXT. The rendering was NEVER PINNED -- the
    existing assertion is that the sentence names every colliding id, which was true of both forms
    -- and that restraint is why the fix was one line and not a negotiation. Pinning the exact
    sentence now would re-create the problem for whoever next improves the wording. So this asserts
    the one thing that must not come back, plus that the ids are still named.

    BOTH REFUSALS, because they now share one helper. RangeCollision never had the defect -- a bare
    UUID str()s correctly -- but it was rewritten the same way so the two cannot drift, and a test
    of only the one that was broken would not notice the other acquiring a second id and a list.

    Args:
        refusal: Builds the refusal from a list of colliding ids.
    """
    ids = [uuid4(), uuid4()]
    raised = refusal(ids)

    assert 'UUID(' not in raised.detail, (
        f'the refusal detail carries a Python repr, which publishes the server implementation '
        f'language into a language-neutral contract (tj-8feral): {raised.detail!r}'
    )
    assert '[' not in raised.detail and ']' not in raised.detail, (
        f'the refusal detail interpolates a collection, which is how the repr got in: {raised.detail!r}'
    )
    named = [str(entry_id) for entry_id in ids if str(entry_id) in raised.detail]
    assert named, f'the refusal detail names none of the colliding ids: {raised.detail!r}'


@pytest.mark.asyncio
async def test_check_own_overlap_neither_commits_nor_rolls_back_on_either_outcome():
    """IT OWNS NO TRANSACTION, on the passing path and on the raising one alike.

    This is the property that makes hoisting it legal at all. The dataset path calls it INSIDE a
    ``write_transaction`` it opened itself and then goes on to open a stream and write pages in that
    same transaction; upsert_entry calls it inside its own. A commit here would end the caller's
    transaction before its first write, and a rollback here would double the one the caller's
    helper already performs on the way out -- and test_dataset_fetch_transaction.py's
    ``session.rollbacks == 1`` is what would catch the second only on the paths that fail.

    BOTH OUTCOMES ARE DRIVEN because they are different code, and the raising one is the one that
    looks like it wants a rollback: a function that refuses a request reads as if it should clean up
    after itself. It must not. ``write_transaction`` rolls back any exception leaving the block,
    OwnOverlapConflict included, and already pins that it is not logged as a database error.
    """
    passing = FakeSession(FakeResult([]))
    await crud.check_own_overlap(passing, overlap_key())
    assert (passing.commits, passing.rollbacks) == (0, 0), 'the overlap check ended a transaction it does not own'

    refusing = FakeSession(FakeResult([(uuid4(),)]))
    with pytest.raises(crud.OwnOverlapConflict):
        await crud.check_own_overlap(refusing, overlap_key())
    assert (refusing.commits, refusing.rollbacks) == (0, 0), (
        'the overlap check rolled back on refusal, so the failure path rolls back twice -- once here '
        "and once in the caller's write_transaction"
    )


@pytest.mark.asyncio
async def test_the_in_transaction_core_does_not_run_the_overlap_check():
    """HALF ONE OF THE PAIR: upsert_entry_in_transaction was relieved of the check, not given a copy.

    WHY THIS NEEDS A TEST OF ITS OWN, measured rather than assumed. Leaving a second copy of the
    check in the core is the plausible wrong repair -- it looks strictly safer, it narrows the
    check-to-insert window the hoist widens, and it is what "add the check to the caller" most
    naturally becomes. It is also GREEN across every other case in this file and in
    test_dataset_fetch_transaction.py, except for the journals, because a redundant SELECT answering
    "no overlap" changes no outcome on any path those cases drive. What it costs in production is an
    identical second SELECT on every successful fetch, for a race a re-check narrows but cannot
    close (no lock is taken; the unique key catches only exact repeats). The builder made that call
    deliberately and the architect is ruling on it; this case is what makes the ruling observable.

    THE SESSION ANSWERS A COLLISION, which is what makes the assertion strong. If the core ran the
    check it would read that row set and raise OwnOverlapConflict, so a copied-back check reds here
    on the exception rather than on a statement count. Because the core does not, the row set is
    consumed by the INSERT's RETURNING instead -- one statement, and the id comes back.

    NO COMMIT AND NO ROLLBACK is asserted for the same reason as on check_own_overlap: this is the
    non-committing core and its caller owns the transaction.
    """
    returned_id = uuid4()
    db = FakeSession(FakeResult([(returned_id,)]))

    returned = await crud.upsert_entry_in_transaction(db, create_request())

    assert returned == returned_id
    assert len(db.statements) == 1, (
        f'upsert_entry_in_transaction sent {len(db.statements)} statements. The own-overlap check was '
        f'MOVED out of it to check_own_overlap (tj-hywf7w), not copied: a second probe here is an '
        f'identical SELECT on every successful fetch, since the caller has already run it'
    )
    assert _sql(db.statements[0]).startswith('INSERT'), 'the in-transaction core sends something other than its upsert'
    assert (db.commits, db.rollbacks) == (0, 0), 'the non-committing core ended a transaction it does not own'


@pytest.mark.asyncio
async def test_the_committing_wrapper_still_runs_the_overlap_check_before_its_insert():
    """HALF TWO OF THE PAIR: upsert_entry, which owns its transaction, kept the check.

    The companion to the case above, and the reason the two are written together. Each on its own is
    satisfied by a check that exists in neither place: delete ``check_own_overlap`` from
    ``upsert_entry`` and the core case stays green, because it says nothing about the wrapper. A
    caller that writes one entry and nothing else -- which is what this wrapper is for, and what the
    POST /store route reaches when it is not fetching -- would then insert over an overlap with no
    refusal at all.

    The no-INSERT ordering half is test_the_overlap_check_runs_before_any_insert_is_sent's, above,
    and is not restated. What this adds is the SEQUENCE: select THEN insert, in one transaction the
    wrapper commits, on the path where there is no overlap. That is what the split had to preserve
    and it is what "the committing wrapper is unchanged" means.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([]), FakeResult([(entry_id,)]))

    returned = await crud.upsert_entry(db, create_request())

    assert returned == entry_id
    kinds = [_sql(statement).split()[0] for statement in db.statements]
    assert kinds == ['SELECT', 'INSERT'], (
        f'upsert_entry sent {kinds}. It owns its transaction and nothing expensive runs inside it, so '
        f'it keeps the own-overlap check next to its insert (tj-hywf7w); a lone INSERT means a caller '
        f'that writes a single entry no longer has its overlap refused at all'
    )
    assert (db.commits, db.rollbacks) == (1, 0), 'the committing wrapper no longer commits its own transaction'


# ---------------------------------------------------------------------------------------------
# The EPOCH sentinel, on both sides of the overlap query
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_open_ended_request_omits_the_upper_bound_predicate():
    """The half the builder only spot-checked, and the one most likely to stop working silently.

    An open-ended request stores end = 1970-01-01, a value in the PAST. The ADR's
    `existing.start < request.end` would then read "starts before 1970" and match nothing, so an
    open-ended request would collide with NOTHING and every overlap would be created as a
    duplicate dataset. The predicate is therefore dropped rather than evaluated, because an
    open-ended range has no upper bound to test.

    STRENGTHENED for half-open ranges (tj-86g751.4). The probe used to be the substring
    'start <=', which the strict predicate the store now builds ('start <') never contains -- so
    after a3b9d0a this test would have stayed green with the predicate put BACK for an open-ended
    request. 'start <' matches both spellings.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=None))

    sql = _sql(db.statements[0])
    assert 'store_dataset_entry.start <' not in sql, (
        'an open-ended request compares existing.start against the EPOCH sentinel, so it can never collide'
    )


@pytest.mark.asyncio
async def test_a_bounded_request_keeps_the_upper_bound_predicate():
    """The negative control, without which the test above passes over a query that lost the predicate entirely.

    A request with a declared end DOES have an upper bound, and dropping it there would make every
    later dataset of the same owner look like a collision.

    STRICT since tj-vhboky.1 addendum HALF-OPEN RANGES (2026-09-30), item 2: existing.start <
    request.end, so an entry that STARTS at the request's end is adjacent, not overlapping. The
    superseded closed-range form, `start <=`, is asserted absent. The behaviour on real rows is
    test_same_owner_ranges_overlap_only_when_they_share_an_instant below.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=MARCH))

    statement = db.statements[0]
    sql = _sql(statement)
    assert re.search(r'store_dataset_entry\.start < %\(\w+\)s', sql), (
        f'a bounded request lost its strict upper bound: {sql}'
    )
    assert 'store_dataset_entry.start <=' not in sql, f'the upper bound is closed, so adjacent ranges collide: {sql}'
    assert MARCH in _params(statement).values(), 'the upper bound is not the request end'


@pytest.mark.asyncio
@pytest.mark.parametrize('requested_end', [None, MARCH], ids=['open-ended request', 'bounded request'])
async def test_a_stored_open_ended_entry_is_never_excluded_by_the_lower_bound(requested_end: datetime | None):
    """The other side of the sentinel: the STORED end, which is the one the ADR warned about.

    An entry with no declared end covers everything from its start onward, so it must satisfy
    `existing.end > request.start` unconditionally. Compared literally it would fail -- 1970 is
    before any real start -- and an open-ended stored dataset would become invisible to every
    subsequent overlap check. The disjunction is asserted in BOTH request shapes because the
    open-ended branch above removes a predicate, and a refactor that removed this one with it would
    pass the open-ended test alone.

    The bounded half of the disjunction is STRICT (`>`, not `>=`) since tj-vhboky.1 addendum
    HALF-OPEN RANGES, item 2: an entry that ENDS at the request's start is adjacent, not
    overlapping.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=requested_end))

    statement = db.statements[0]
    sql = _sql(statement)
    assert re.search(r'\(store_dataset_entry\."end" = %\(\w+\)s OR store_dataset_entry\."end" > %\(\w+\)s\)', sql), (
        f'the stored EPOCH sentinel is no longer handled as "covers everything onward", or the lower bound '
        f'is no longer strict: {sql}'
    )
    assert NullableDateTime.EPOCH in _params(statement).values(), 'the sentinel value itself is not bound'
    assert JANUARY in _params(statement).values(), 'the lower bound is not the request start'


# ---------------------------------------------------------------------------------------------
# HALF-OPEN RANGES on real rows: check_own_overlap's own SELECT, evaluated (tj-86g751.4)
# ---------------------------------------------------------------------------------------------
#
# The design is tj-vhboky.1 addendum HALF-OPEN RANGES (2026-09-30), item 2: same owner, same every
# non-range field, not an exact repeat, and
#
#     (existing.end is EPOCH OR existing.end > request.start) AND
#     (request.end is EPOCH OR existing.start < request.end)
#
# so ADJACENT entries [a, m) and [m, b) do not collide -- they share no bar.
#
# WHY SQLITE. The SQL-text tests above say which operators the statement carries; they cannot say
# which ROWS it selects, and a fake session never evaluates a predicate. Here the stored entries are
# real rows in StoreDatasetEntry's own table on an in-memory SQLite (the harness
# test_a_null_owner_turns_an_exact_repeat_into_a_second_row_unless_the_column_refuses_it already
# uses), and the statement check_own_overlap builds is EXECUTED against them, through the column
# types' own bind processing -- the EPOCH sentinel included.
#
# WHAT THIS DOES NOT PROVE. SQLite's DateTime stores the wall clock and drops the offset, so every
# instant here is UTC and comparisons are between equal-offset strings; that the comparison is
# between INSTANTS on a timestamptz column is Postgres's to show (the read's version of that claim is
# tests/system/test_http_bars.py, test_range_bounds_are_half_open_and_compare_instants). The 409
# mapping above the exception is test_store_dataset_entry_route.py's, both ways: a conflict is 409
# naming the ids, and an empty overlap set is a 200 write.
_TICK = timedelta(microseconds=1)


class _SqliteSession:
    """Executes what the crud sends on a real SQLite connection. Only `execute` is used by the check."""

    def __init__(self, connection):
        self._connection = connection
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._connection.execute(statement)


def _stored_row(start: datetime, end: datetime | None, owner: str = OWNER) -> tuple[UUID, dict]:
    """One stored entry as column values, projected from a create request through the real key.

    end None is stored as the EPOCH sentinel by OverlapKey.column_values, exactly as the column holds it.
    """
    request = create_request(start=start, end=end, owner=owner)
    entry_id = uuid4()
    values = crud.OverlapKey.of(request).column_values() | {
        'id': entry_id,
        'feed': request.feed,
        'expiry': request.expiry,
        'created_at': JANUARY,
        'updated_at': JANUARY,
    }
    return entry_id, values


async def _overlaps_on_real_rows(
    stored: list[tuple[datetime, datetime | None]], request: tuple[datetime, datetime | None], owner: str = OWNER
) -> tuple[list[UUID], list[UUID]]:
    """Insert `stored` (same owner, same spec), run check_own_overlap for `request`, and return (stored ids, colliding ids).

    Returns the colliding ids the refusal carried, or [] when the request was let through.
    """
    engine = create_engine('sqlite://')
    StoreDatasetEntry.__table__.create(engine)
    ids = []
    with engine.begin() as connection:
        for start, end in stored:
            entry_id, values = _stored_row(start, end, owner=OWNER)
            connection.execute(StoreDatasetEntry.__table__.insert().values(**values))
            ids.append(entry_id)
    with engine.connect() as connection:
        session = _SqliteSession(connection)
        try:
            await crud.check_own_overlap(session, overlap_key(start=request[0], end=request[1], owner=owner))
        except crud.OwnOverlapConflict as raised:
            return ids, list(raised.colliding_ids)
        finally:
            assert len(session.statements) == 1, (
                f'the check sent {len(session.statements)} statements, not its one probe'
            )
    return ids, []


# (stored ranges, request range, index of the stored entry that must collide, or None). JANUARY..APRIL
# are UTC month starts; _TICK is one microsecond, the column's resolution.
_HALF_OPEN_CASES = {
    # Item 2's headline: adjacent same-owner entries are BOTH accepted, in either order.
    'adjacent-request-after-existing [J,F) then [F,M)': ([(JANUARY, FEBRUARY)], (FEBRUARY, MARCH), None),
    'adjacent-request-before-existing [F,M) then [J,F)': ([(FEBRUARY, MARCH)], (JANUARY, FEBRUARY), None),
    # One shared instant of interior overlap is still an overlap, on each side.
    'one-instant-overlap-at-existing-end [J,F) vs [F-1us,M)': ([(JANUARY, FEBRUARY)], (FEBRUARY - _TICK, MARCH), 0),
    'one-instant-overlap-at-existing-start [F,M) vs [J,F+1us)': ([(FEBRUARY, MARCH)], (JANUARY, FEBRUARY + _TICK), 0),
    # An open-ended EXISTING entry covers everything from its start onward.
    'open-existing [F,inf) vs request starting at its start': ([(FEBRUARY, None)], (FEBRUARY, MARCH), 0),
    'open-existing [F,inf) vs request starting after its start': ([(FEBRUARY, None)], (MARCH, APRIL), 0),
    'open-existing [F,inf) vs request ending at its start': ([(FEBRUARY, None)], (JANUARY, FEBRUARY), None),
    # An open-ended REQUEST against an entry that ends exactly at its start is adjacent.
    'open-request [F,inf) vs existing [J,F)': ([(JANUARY, FEBRUARY)], (FEBRUARY, None), None),
    'open-request [F,inf) vs existing [J,F+1us)': ([(JANUARY, FEBRUARY + _TICK)], (FEBRUARY, None), 0),
    # The exact repeat is excluded from the overlap set; the upsert's ON CONFLICT returns its id
    # (test_an_exact_repeat_resolves_to_the_existing_id_without_a_second_insert).
    'exact-repeat [J,F)': ([(JANUARY, FEBRUARY)], (JANUARY, FEBRUARY), None),
    'exact-repeat open [J,inf)': ([(JANUARY, None)], (JANUARY, None), None),
    # Both adjacent neighbours stored, request in the gap between them exactly: nothing collides.
    'request filling the gap [J,F) [M,A) vs [F,M)': ([(JANUARY, FEBRUARY), (MARCH, APRIL)], (FEBRUARY, MARCH), None),
    # ...and widened by one instant on each side: both neighbours collide, both are named.
    'request overlapping both neighbours by one instant': (
        [(JANUARY, FEBRUARY), (MARCH, APRIL)],
        (FEBRUARY - _TICK, MARCH + _TICK),
        'both',
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(('stored', 'asked', 'collides'), list(_HALF_OPEN_CASES.values()), ids=list(_HALF_OPEN_CASES))
async def test_same_owner_ranges_overlap_only_when_they_share_an_instant(stored, asked, collides):
    """Addendum item 2, on real rows: a same-owner request is refused exactly when it shares a bar instant.

    Each case is a boundary: the adjacent pairs and the one-microsecond overlaps differ by one tick,
    so moving either strict comparison back to its closed form reds the adjacent cases, and moving
    it the other way (or dropping a bound) reds the one-instant ones. The colliding ids are compared
    whole, so the refusal names exactly the overlapped entries.
    """
    ids, colliding = await _overlaps_on_real_rows(stored, asked)

    expected = ids if collides == 'both' else ([] if collides is None else [ids[collides]])
    assert sorted(colliding) == sorted(expected), (
        f'stored {stored}, request {asked}: expected collisions {expected}, the check reported {colliding}'
    )


@pytest.mark.asyncio
async def test_another_owners_overlapping_range_never_collides_on_real_rows():
    """The control that shows the harness evaluates the OWNER term too: the same overlap, another owner, is let through."""
    _, colliding = await _overlaps_on_real_rows([(JANUARY, MARCH)], (FEBRUARY, APRIL), owner='strategy-b')

    assert colliding == [], 'another owner was refused for overlapping a range it does not hold'


# ---------------------------------------------------------------------------------------------
# UNCHANGED under half-open ranges, spot-pinned (addendum item 2, last sentence; tj-86g751.4 item 4)
# ---------------------------------------------------------------------------------------------
#
# "Section 4's growth rule and exact-collision rule are unchanged (containment and equality mean the
# same under either convention)." The behaviour of each is pinned elsewhere in this file
# (test_an_update_that_shrinks_the_range_is_rejected, test_opening_a_bounded_entry_is_growth,
# test_a_grow_that_collides_names_the_other_entry_and_sends_no_update); these two pin the boundary
# that a half-open "consistency" edit is most likely to move by mistake.


@pytest.mark.parametrize(
    ('old', 'new', 'grows'),
    [
        ((JANUARY, MARCH), (JANUARY, MARCH), True),
        ((FEBRUARY, MARCH), (JANUARY, MARCH), True),
        ((JANUARY, MARCH), (JANUARY, MARCH + _TICK), True),
        ((JANUARY, MARCH), (JANUARY, MARCH - _TICK), False),
        ((JANUARY, MARCH), (JANUARY + _TICK, MARCH), False),
        ((JANUARY, NullableDateTime.EPOCH), (JANUARY, NullableDateTime.EPOCH), True),
    ],
    ids=[
        'same-range',
        'start-pulled-back-end-equal',
        'end-one-tick-later',
        'end-one-tick-earlier',
        'start-one-tick-later',
        'open-stays-open',
    ],
)
def test_growth_is_containment_with_equal_bounds_allowed(old: tuple, new: tuple, grows: bool):
    """_is_growth: the new range contains the old one, equality included -- unchanged by half-open ranges.

    The two equal-bound cases are the ones a reader "making it consistent" with the strict overlap
    operators would break: under either convention [s, e) contains [s, e), so an update that keeps a
    bound where it was is still growth.
    """
    assert crud._is_growth(old[0], old[1], new[0], new[1]) is grows


@pytest.mark.asyncio
async def test_the_grow_collision_is_exact_equality_on_both_bounds():
    """_find_exact_collision: a grow collides only with an entry of EXACTLY the new range -- unchanged.

    Equality on start and on end, not a range comparison: identity is the unique key, and the key
    holds the bounds as values. A strict or closed range operator here would turn a grow next to
    another entry into a spurious RangeCollision.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH),)]), FakeResult([]), FakeResult([]))

    await crud.update_entry(db, update_request(entry_id, end=APRIL))

    sql = _sql(db.statements[1])
    assert re.search(r'store_dataset_entry\.start = %\(\w+\)s', sql), (
        f'the grow collision no longer equates start: {sql}'
    )
    assert re.search(r'store_dataset_entry\."end" = %\(\w+\)s', sql), f'the grow collision no longer equates end: {sql}'
    assert not re.search(r'store_dataset_entry\.(start|"end") [<>]', sql), f'the grow collision compares a range: {sql}'


# ---------------------------------------------------------------------------------------------
# The owner check: absent on create by design, present on every id-addressed write
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_create_path_never_consults_the_owner_check(monkeypatch: pytest.MonkeyPatch):
    """A NEGATIVE ruling, asserted deliberately: there is no owner check on create, and there must not be.

    owner is part of identity, so two principals asking for the same spec get two entries and there
    is never a cross-owner conflict to detect. An owner comparison added here would be unreachable
    code at best, and at worst would reject a second principal's legitimate dataset. This test is
    the thing that makes someone read the ruling before adding one.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError('upsert_entry consulted the owner check, which is unreachable on create by design')

    monkeypatch.setattr(crud, '_check_owner', forbidden)
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request())

    assert db.commits == 1


@pytest.mark.asyncio
async def test_the_overlap_check_is_scoped_to_the_requesting_owner():
    """Why the create path needs no owner comparison: the query can only ever return your own rows.

    "Shoot yourself in the foot, not others" -- a different owner's overlapping dataset is a
    different dataset and the request proceeds. That property lives entirely in this predicate, so
    it is asserted on the bound value rather than inferred from the fake returning nothing.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(owner='strategy-b'))

    statement = db.statements[0]
    assert 'store_dataset_entry.owner =' in _sql(statement), 'the overlap check is not owner-scoped'
    assert 'strategy-b' in _params(statement).values(), 'the overlap check is scoped to some other owner'


@pytest.mark.asyncio
async def test_delete_rejects_a_principal_that_does_not_own_the_entry():
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, owner='someone-else'),)]))

    with pytest.raises(crud.OwnerMismatch):
        await crud.delete_entry_by_id(db, entry_id, OWNER)

    assert len(db.statements) == 1, 'the delete was sent despite the owner mismatch'
    assert db.commits == 0


@pytest.mark.asyncio
async def test_update_rejects_a_principal_that_does_not_own_the_entry():
    """Checked before the growth test, so a rejected principal learns nothing about the stored range."""
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, owner='someone-else'),)]))

    with pytest.raises(crud.OwnerMismatch):
        await crud.update_entry(db, update_request(entry_id))

    assert len(db.statements) == 1, 'a statement beyond the entry lookup was sent'
    assert db.commits == 0


@pytest.mark.asyncio
async def test_lifecycle_update_rejects_a_principal_that_does_not_own_the_entry():
    """update_entry_lifecycle has no route today, so nothing else in the suite can reach its check."""
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, owner='someone-else'),)]))

    with pytest.raises(crud.OwnerMismatch):
        await crud.update_entry_lifecycle(db, entry_id, OWNER)

    assert len(db.statements) == 1
    assert db.commits == 0


@pytest.mark.asyncio
async def test_the_mismatch_never_names_the_real_owner():
    """Reads are open, but an error body is not a read endpoint (tj-vhboky.1 section 5).

    The exception is what a route turns into a 4xx body, so the owner leaking into its message or
    its args would make a write endpoint answer a question only a read endpoint should. Both are
    checked: formatting it into the message and stashing it on the instance are the two ways it
    gets out.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, owner='confidential-principal'),)]))

    with pytest.raises(crud.OwnerMismatch) as raised:
        await crud.delete_entry_by_id(db, entry_id, OWNER)

    assert 'confidential-principal' not in str(raised.value)
    assert 'confidential-principal' not in repr(raised.value.args)
    assert 'confidential-principal' not in repr(vars(raised.value))


# ---------------------------------------------------------------------------------------------
# "no entry found with that id", and the dead check Amendment 1 item R fixed
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'call',
    [
        lambda db, entry_id: crud.delete_entry_by_id(db, entry_id, OWNER),
        lambda db, entry_id: crud.update_entry(db, update_request(entry_id)),
        lambda db, entry_id: crud.update_entry_lifecycle(db, entry_id, OWNER),
    ],
    ids=['delete_entry_by_id', 'update_entry', 'update_entry_lifecycle'],
)
async def test_an_id_addressed_write_against_an_unknown_id_raises_entry_not_found(call):
    """'No such entry' and 'not yours' are different answers and the route has to tell them apart.

    All three id-addressed writes are covered by one parametrized case because the check is one
    shared helper -- and because a fourth write added without it should show up as a missing id
    here rather than as a silent success in production.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([]))

    with pytest.raises(crud.EntryNotFound) as raised:
        await call(db, entry_id)

    assert str(entry_id) in str(raised.value)
    assert len(db.statements) == 1, 'a write was sent for an id that does not exist'
    assert db.commits == 0


@pytest.mark.asyncio
async def test_a_delete_that_races_another_reports_not_found_before_committing():
    """Amendment 1 item R, both halves of it.

    The old code was `if result == 0`, comparing a Result object to an int -- never true, so
    deleting a nonexistent id succeeded silently. It is now result.rowcount == 0, AND it runs
    BEFORE the commit: as written the check would otherwise raise after the transaction had already
    committed, reporting a failure for work that was done. `commits == 0` is the assertion that
    pins the ordering; raising alone would pass a version that committed first.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id),)]), FakeResult([], rowcount=0))

    with pytest.raises(crud.EntryNotFound):
        await crud.delete_entry_by_id(db, entry_id, OWNER)

    assert db.commits == 0, 'the transaction was committed before the no-such-entry check ran'
    assert db.rollbacks == 1, 'the failed delete left its transaction open'


# ---------------------------------------------------------------------------------------------
# Every rejected write rolls back before the domain error reaches the caller (tj-ck5spw)
# ---------------------------------------------------------------------------------------------
#
# The file's convention is an explicit rollback on every error path. Before 39b9d19 the domain
# rejections were raised from OUTSIDE each function's try, so they skipped it. Nothing noticed,
# because a request-scoped session rolls back when FastAPI closes it. That property belongs to a
# caller this module does not control, and update_entry has no HTTP caller at all. Until these
# tests, the delete-race case above was the only one that observed a rollback. The existing tests
# for each path keep the exception type, message and statement assertions; these add only
# rollbacks == 1 and commits == 0, one test per path, so a red names the path it lost.

_ID_ADDRESSED_WRITES = {
    'delete_entry_by_id': lambda db, entry_id: crud.delete_entry_by_id(db, entry_id, OWNER),
    'update_entry': lambda db, entry_id: crud.update_entry(db, update_request(entry_id)),
    'update_entry_lifecycle': lambda db, entry_id: crud.update_entry_lifecycle(db, entry_id, OWNER),
}


@pytest.mark.asyncio
async def test_a_create_rejected_as_an_own_overlap_rolls_back():
    """OwnOverlapConflict out of upsert_entry. The overlap select has run, so the session was touched."""
    db = FakeSession(FakeResult([(uuid4(),)]))

    with pytest.raises(crud.OwnOverlapConflict):
        await crud.upsert_entry(db, create_request())

    assert (db.commits, db.rollbacks) == (0, 1), 'a create rejected for own overlap did not roll back'


@pytest.mark.asyncio
async def test_an_update_rejected_as_a_range_shrink_rolls_back():
    """RangeShrink out of update_entry, raised after the entry lookup has run."""
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, start=JANUARY, end=MARCH),)]))

    with pytest.raises(crud.RangeShrink):
        await crud.update_entry(db, update_request(entry_id, start=FEBRUARY, end=MARCH))

    assert (db.commits, db.rollbacks) == (0, 1), 'an update rejected as a shrink did not roll back'


@pytest.mark.asyncio
async def test_an_update_rejected_as_a_range_collision_rolls_back():
    """RangeCollision out of update_entry, raised after the lookup and the collision select have run."""
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH),)]), FakeResult([(uuid4(),)]))

    with pytest.raises(crud.RangeCollision):
        await crud.update_entry(db, update_request(entry_id, end=APRIL))

    assert (db.commits, db.rollbacks) == (0, 1), 'an update rejected as a range collision did not roll back'


@pytest.mark.asyncio
@pytest.mark.parametrize('write', list(_ID_ADDRESSED_WRITES), ids=list(_ID_ADDRESSED_WRITES))
async def test_an_id_addressed_write_against_an_unknown_id_rolls_back(write: str):
    """EntryNotFound from the lookup, not the delete race above: no entry was found by the SELECT.

    Args:
        write: Which id-addressed write is called, and the case id a red is reported under.
    """
    db = FakeSession(FakeResult([]))

    with pytest.raises(crud.EntryNotFound):
        await _ID_ADDRESSED_WRITES[write](db, uuid4())

    assert (db.commits, db.rollbacks) == (0, 1), f'{write} against an unknown id did not roll back'


@pytest.mark.asyncio
@pytest.mark.parametrize('write', list(_ID_ADDRESSED_WRITES), ids=list(_ID_ADDRESSED_WRITES))
async def test_an_id_addressed_write_by_a_non_owner_rolls_back(write: str):
    """OwnerMismatch from _check_owner, after the lookup has read the entry.

    Args:
        write: Which id-addressed write is called, and the case id a red is reported under.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, owner='someone-else'),)]))

    with pytest.raises(crud.OwnerMismatch):
        await _ID_ADDRESSED_WRITES[write](db, entry_id)

    assert (db.commits, db.rollbacks) == (0, 1), f'{write} by a non-owner did not roll back'


# ---------------------------------------------------------------------------------------------
# A database error rolls back and leaves unchanged; a success never rolls back (tj-vhboky.39,
# tj-76u8ip)
# ---------------------------------------------------------------------------------------------
#
# Error handling for all four writes is write_transaction's, per tj-vhboky.41 Addendum 1.


class ErroringSession(FakeSession):
    """A FakeSession whose execute raises a SQLAlchemyError on the statement at index `fail_at`.

    The statements before it are answered from `results` exactly as FakeSession answers them, so
    the path reaches the failing statement honestly rather than by skipping the checks before it.
    """

    def __init__(self, fail_at: int, *results: FakeResult):
        super().__init__(*results)
        self._fail_at = fail_at
        self.error = SQLAlchemyError(f'simulated database failure on statement {fail_at}')

    async def execute(self, statement):
        if len(self.statements) == self._fail_at:
            self.statements.append(statement)
            raise self.error
        return await super().execute(statement)


_WRITES = {
    'upsert_entry': lambda db, entry_id: crud.upsert_entry(db, create_request()),
    'update_entry': lambda db, entry_id: crud.update_entry(db, update_request(entry_id, end=APRIL)),
    'update_entry_lifecycle': lambda db, entry_id: crud.update_entry_lifecycle(db, entry_id, OWNER),
    'delete_entry_by_id': lambda db, entry_id: crud.delete_entry_by_id(db, entry_id, OWNER),
}

# The operation each write names to write_transaction, which its ERROR record leads with
# (tj-76u8ip build step 2).
_OPERATIONS = {
    'upsert_entry': 'create or update entry',
    'update_entry': 'update entry',
    'update_entry_lifecycle': 'update entry lifecycle',
    'delete_entry_by_id': 'delete entry {entry_id}',
}

_TRANSACTION_LOGGER = 'data.store.app.database.transaction'


def _owned(entry_id: UUID) -> FakeResult:
    """The lookup's answer: the entry exists, is owned by OWNER, and ends in March so April is growth."""
    return FakeResult([(stored_entry(entry_id, end=MARCH),)])


# (write, the statement that fails) -> the answers to every statement BEFORE it. The failing
# statement's index is the length of that list, so the table cannot disagree with itself.
_DATABASE_ERROR_CASES = {
    ('upsert_entry', 'overlap select'): lambda entry_id: [],
    ('upsert_entry', 'insert'): lambda entry_id: [FakeResult([])],
    ('update_entry', 'lookup'): lambda entry_id: [],
    ('update_entry', 'collision select'): lambda entry_id: [_owned(entry_id)],
    ('update_entry', 'update'): lambda entry_id: [_owned(entry_id), FakeResult([])],
    ('update_entry_lifecycle', 'lookup'): lambda entry_id: [],
    ('update_entry_lifecycle', 'update'): lambda entry_id: [_owned(entry_id)],
    ('delete_entry_by_id', 'lookup'): lambda entry_id: [],
    ('delete_entry_by_id', 'delete'): lambda entry_id: [_owned(entry_id)],
}


# write -> the answers to every statement its successful path issues. Used by the success case AND
# the failing-commit case: a commit only fails after every statement has succeeded.
_SUCCESS_ANSWERS = {
    'upsert_entry': lambda entry_id: [FakeResult([]), FakeResult([(entry_id,)])],
    'update_entry': lambda entry_id: [_owned(entry_id), FakeResult([]), FakeResult([])],
    'update_entry_lifecycle': lambda entry_id: [_owned(entry_id), FakeResult([])],
    'delete_entry_by_id': lambda entry_id: [_owned(entry_id), FakeResult([], rowcount=1)],
}


class CommitFailingSession(FakeSession):
    """A FakeSession whose every statement succeeds and whose commit raises a SQLAlchemyError.

    commits stays 0, because no commit succeeded; commit_attempts is what shows the commit was
    reached, so a case cannot pass by failing earlier than the commit it targets.
    """

    def __init__(self, *results: FakeResult):
        super().__init__(*results)
        self.commit_attempts = 0
        self.error = SQLAlchemyError('simulated database failure on commit')

    async def commit(self) -> None:
        self.commit_attempts += 1
        raise self.error


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_every_write_has_a_database_error_case_and_every_id_addressed_one_a_lookup_case():
    """The tables above are the coverage claim, so they are checked rather than trusted.

    Every write appears in the statement-error table, every id-addressed write has a `lookup` case
    -- the path 39b9d19 moved inside the try -- and every write has a success/failing-commit case
    (tj-76u8ip MUST PIN 1). A fifth write added to _WRITES without cases fails here.
    """
    covered = {write for write, _ in _DATABASE_ERROR_CASES}
    assert covered == set(_WRITES)
    for write in set(_WRITES) - {'upsert_entry'}:
        assert (write, 'lookup') in _DATABASE_ERROR_CASES, f'{write} has no lookup-error case'
    assert set(_SUCCESS_ANSWERS) == set(_WRITES), 'a write has no success / failing-commit case'
    assert set(_OPERATIONS) == set(_WRITES), 'a write has no expected operation name'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', list(_DATABASE_ERROR_CASES), ids=[f'{w}: {s}' for w, s in _DATABASE_ERROR_CASES])
async def test_a_database_error_rolls_back_once_and_leaves_as_the_original_error(case: tuple[str, str]):
    """One rollback, no commit, and the raised object IS the session's SQLAlchemyError.

    SUPERSEDED DESIGN, kept so the change of assertion is traceable (tj-8fxxfb (iii)): until
    d43582f this test was test_a_database_error_rolls_back_once_and_leaves_as_runtime_error. Each
    write then caught SQLAlchemyError itself and raised RuntimeError with a per-function message
    prefix, and the test asserted that type, the prefix, the original's text in the message and
    the original on __context__ -- pytest.raises(RuntimeError) was the "not raw" assertion. The
    user rejected that catch/rethrow forwarding (tj-76u8ip ruling, 2026-09-28), and tj-vhboky.41
    Addendum 1 (D1 interim) now requires the opposite: write_transaction rolls back, logs, and
    re-raises the ORIGINAL error unchanged, with no wrapper type until tj-fa1rpu is ruled. So the
    assertion is identity, not type: a wrapper of any type, or a copy, fails here. The (0, 1)
    commit/rollback check is unchanged by that ruling.

    ADDENDUM (validator, gating tj-3mk3u5.37.8): tj-fa1rpu IS RULED, AND THIS IS NOW THE BUG BRANCH.
    The paragraph above says "with no wrapper type until tj-fa1rpu is ruled"; TE-6 ruled it, and
    write_transaction now converts a SQLAlchemyError it can CLASSIFY into a typed ExogenousError.
    These 9 cases did not go red, and the reason is the fixture rather than the design: ErroringSession
    raises a bare ``SQLAlchemyError``, which is not a DBAPIError, carries no SQLSTATE and is none of
    OperationalError / InterfaceError / IntegrityError -- so ``_reason_for`` returns None and the
    helper takes its OTHER branch, re-raising unchanged because an unclassifiable error is a bug of
    ours (D5). Identity is therefore still exactly right here, and these cases are still a genuine
    pin -- of the bug branch.

    THAT WAS AN ACCIDENT UNTIL NOW AND IS A DECISION FROM NOW ON. Nothing recorded which branch the
    fixture selected, so a later edit making the fixture's error an OperationalError -- the obvious
    "let's use a realistic error" tidy-up -- would have flipped all 9 cases to the conversion branch
    and reported the identity assertion as a regression in production rather than in the fixture. The
    assertion below makes the choice explicit, and the conversion branch gets its own case after this
    one, so the two are visibly a pair rather than one of them being whatever the fixture happened to
    produce.

    Args:
        case: (the write, the statement whose execute raises), and the case id a red is reported under.
    """
    write, _ = case
    entry_id = uuid4()
    answers = _DATABASE_ERROR_CASES[case](entry_id)
    db = ErroringSession(len(answers), *answers)

    with pytest.raises(SQLAlchemyError) as raised:
        await _WRITES[write](db, entry_id)

    assert len(db.statements) == len(answers) + 1, 'the error was not raised by the statement this case targets'
    assert (db.commits, db.rollbacks) == (0, 1), f'{case} did not roll back exactly once without committing'
    assert not isinstance(raised.value, ExogenousError), (
        f'{case} was CONVERTED to a reason. This case is the bug branch, selected by the fixture '
        f'raising a bare SQLAlchemyError that _reason_for cannot classify; if the fixture now raises '
        f'something classifiable, that is a fixture change and the identity assertion below is the '
        f'wrong one to keep -- see the conversion case that follows.'
    )
    assert raised.value is db.error, f'{case} did not leave as the original error: {raised.value!r}'
    assert raised.value.__cause__ is None, f'{case} gained a chained cause: {raised.value.__cause__!r}'


@pytest.mark.asyncio
@pytest.mark.parametrize('write', list(_SUCCESS_ANSWERS), ids=list(_SUCCESS_ANSWERS))
async def test_a_classifiable_database_error_reaches_every_write_as_its_reason(write: str):
    """The conversion branch, through each crud write rather than through the helper alone.

    THE COMPANION TO THE CASE ABOVE, and the half the bare-SQLAlchemyError fixture cannot reach.
    test_write_transaction.py proves the classification table exhaustively; what it cannot show is
    that each of THESE functions actually goes through the helper, so that a caller of any of them
    gets the typed answer. A write that kept its own try/except -- which is what every one of them
    had before d43582f -- would re-raise the OperationalError raw, answer a 500 instead of a 503,
    and leave every assertion in this file green.

    The first statement raises, so this reaches each write at its earliest database contact and does
    not depend on how many statements that write goes on to send.

    Args:
        write: The crud write under test.
    """
    entry_id = uuid4()
    db = ErroringSession(0)
    db.error = OperationalError('SELECT ...', {}, Exception('server closed the connection'))

    with pytest.raises(ExogenousError) as raised:
        await _WRITES[write](db, entry_id)

    assert raised.value.reason is Reason.DATABASE_UNAVAILABLE, (
        f'{write} reported an OperationalError as {raised.value.reason}, so it is not going through '
        f'write_transaction or the helper is not classifying it'
    )
    assert raised.value.__cause__ is db.error, f'{write} lost the original error from the cause chain'
    assert (db.commits, db.rollbacks) == (0, 1), f'{write} did not roll back exactly once without committing'


@pytest.mark.asyncio
@pytest.mark.parametrize('write', list(_SUCCESS_ANSWERS), ids=list(_SUCCESS_ANSWERS))
async def test_a_successful_write_commits_once_and_never_rolls_back(write: str):
    """F2 of the tj-ck5spw gate: an unconditional rollback would pass every error-path assertion.

    Every error path above asserts rollbacks == 1, which a write that ALWAYS rolled back satisfies
    too, and the happy-path tests elsewhere in this file assert commits alone. On a real session a
    rollback before the commit discards the write, so (1, 0) is the only correct success.

    Args:
        write: Which write is called, and the case id a red is reported under.
    """
    entry_id = uuid4()
    db = FakeSession(*_SUCCESS_ANSWERS[write](entry_id))

    await _WRITES[write](db, entry_id)

    assert (db.commits, db.rollbacks) == (1, 0), f'{write} succeeded with commits/rollbacks {db.commits}/{db.rollbacks}'


@pytest.mark.asyncio
@pytest.mark.parametrize('write', list(_SUCCESS_ANSWERS), ids=list(_SUCCESS_ANSWERS))
async def test_a_failing_commit_rolls_back_once_and_leaves_as_the_original_error(write: str):
    """tj-76u8ip MUST PIN 1. Every statement succeeds; the commit itself raises.

    A commit failure has to take the same branch as a statement failure (tj-vhboky.41 S1). A
    helper that commits outside its try would let this error out with the transaction still open,
    and no statement-error case above can see that, because none of them reaches the commit.

    Args:
        write: Which write is called, and the case id a red is reported under.
    """
    entry_id = uuid4()
    db = CommitFailingSession(*_SUCCESS_ANSWERS[write](entry_id))

    with pytest.raises(SQLAlchemyError) as raised:
        await _WRITES[write](db, entry_id)

    assert db.commit_attempts == 1, f'{write} did not reach its commit exactly once'
    assert db.rollbacks == 1, f'{write} left a failed commit without rolling back'
    assert raised.value is db.error, f'{write} did not leave as the original commit error: {raised.value!r}'
    assert raised.value.__cause__ is None


def _database_error_session(case: tuple, entry_id: UUID) -> ErroringSession | CommitFailingSession:
    if case[1] == 'commit':
        return CommitFailingSession(*_SUCCESS_ANSWERS[case[0]](entry_id))
    answers = _DATABASE_ERROR_CASES[case](entry_id)
    return ErroringSession(len(answers), *answers)


_LOGGED_CASES = [*_DATABASE_ERROR_CASES, *((write, 'commit') for write in _SUCCESS_ANSWERS)]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', _LOGGED_CASES, ids=[f'{w}: {s}' for w, s in _LOGGED_CASES])
async def test_a_database_error_is_logged_once_through_the_module_logger_and_never_to_stderr(
    case: tuple[str, str], caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
):
    """tj-76u8ip MUST PIN 2, tj-vhboky.41 S3 revised: logger, not stderr.

    The code this replaced called traceback.print_exc(), which writes straight to stderr and
    bypasses the logging config. Exactly one ERROR record, from write_transaction's module logger,
    naming the operation and carrying the original error as its exc_info, so the traceback travels
    through logging. "Exactly one" also catches a caller that logs the same failure a second time.

    Args:
        case: (the write, the statement or commit that raises), and the case id a red is reported under.
        caplog: Captures the log records.
        capfd: Captures what reaches file descriptor 2.
    """
    write, _ = case
    entry_id = uuid4()
    db = _database_error_session(case, entry_id)
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()

    with pytest.raises(SQLAlchemyError):
        await _WRITES[write](db, entry_id)

    errors = _error_records(caplog)
    assert len(errors) == 1, f'{case} logged {len(errors)} ERROR records: {[r.getMessage() for r in errors]}'
    (record,) = errors
    assert record.name == _TRANSACTION_LOGGER, f'{case} logged through {record.name}'
    assert record.exc_info is not None and record.exc_info[1] is db.error, f'{case} logged without the original error'
    assert record.getMessage().startswith(_OPERATIONS[write].format(entry_id=entry_id)), record.getMessage()
    assert capfd.readouterr().err == '', f'{case} wrote to stderr'


def _rejected_by_own_overlap():
    return FakeSession(FakeResult([(uuid4(),)])), lambda db, entry_id: crud.upsert_entry(db, create_request())


# case id -> (the domain exception, a factory for (session, call) given the entry id)
_DOMAIN_REJECTIONS = {
    'upsert_entry: OwnOverlapConflict': (crud.OwnOverlapConflict, lambda entry_id: _rejected_by_own_overlap()),
    'update_entry: RangeShrink': (
        crud.RangeShrink,
        lambda entry_id: (
            FakeSession(FakeResult([(stored_entry(entry_id, start=JANUARY, end=MARCH),)])),
            lambda db, entry_id: crud.update_entry(db, update_request(entry_id, start=FEBRUARY, end=MARCH)),
        ),
    ),
    'update_entry: RangeCollision': (
        crud.RangeCollision,
        lambda entry_id: (
            FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH),)]), FakeResult([(uuid4(),)])),
            lambda db, entry_id: crud.update_entry(db, update_request(entry_id, end=APRIL)),
        ),
    ),
    'delete_entry_by_id: EntryNotFound (race)': (
        crud.EntryNotFound,
        lambda entry_id: (
            FakeSession(FakeResult([(stored_entry(entry_id),)]), FakeResult([], rowcount=0)),
            _ID_ADDRESSED_WRITES['delete_entry_by_id'],
        ),
    ),
    **{
        f'{write}: EntryNotFound': (
            crud.EntryNotFound,
            lambda entry_id, write=write: (FakeSession(FakeResult([])), _ID_ADDRESSED_WRITES[write]),
        )
        for write in _ID_ADDRESSED_WRITES
    },
    **{
        f'{write}: OwnerMismatch': (
            crud.OwnerMismatch,
            lambda entry_id, write=write: (
                FakeSession(FakeResult([(stored_entry(entry_id, owner='someone-else'),)])),
                _ID_ADDRESSED_WRITES[write],
            ),
        )
        for write in _ID_ADDRESSED_WRITES
    },
}


def test_every_domain_exception_has_a_rejection_case():
    """The five domain exceptions tj-vhboky.41 S4 names, each reached by at least one case above."""
    raised = {exception for exception, _ in _DOMAIN_REJECTIONS.values()}
    assert raised == {
        crud.EntryNotFound,
        crud.OwnerMismatch,
        crud.OwnOverlapConflict,
        crud.RangeShrink,
        crud.RangeCollision,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('case', list(_DOMAIN_REJECTIONS), ids=list(_DOMAIN_REJECTIONS))
async def test_a_domain_rejection_passes_through_unwrapped_and_is_not_logged_at_error(
    case: str, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
):
    """tj-76u8ip MUST PIN 2 (domain half) and 3; tj-vhboky.41 S3 and S4.

    A domain rejection is an expected outcome, not a database failure, so it produces no ERROR
    record and nothing on stderr. It reaches the caller as exactly its own type with no chained
    cause -- the routers map these to 409/403/404 by type, so a wrapper would turn every one of
    them into a 500. The rollback for each is pinned by the tj-ck5spw tests above; it is repeated
    here only so a case that silently stopped reaching its rejection shows up as a red.

    Args:
        case: Which write and which rejection, and the case id a red is reported under.
        caplog: Captures the log records.
        capfd: Captures what reaches file descriptor 2.
    """
    exception, build = _DOMAIN_REJECTIONS[case]
    entry_id = uuid4()
    db, call = build(entry_id)
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()

    with pytest.raises(exception) as raised:
        await call(db, entry_id)

    assert type(raised.value) is exception, f'{case} left as {type(raised.value).__name__}'
    assert raised.value.__cause__ is None, f'{case} gained a chained cause: {raised.value.__cause__!r}'
    assert (db.commits, db.rollbacks) == (0, 1), f'{case} did not roll back exactly once without committing'
    assert _error_records(caplog) == [], f'{case} was logged at ERROR: {[r.getMessage() for r in caplog.records]}'
    assert capfd.readouterr().err == '', f'{case} wrote to stderr'


# ---------------------------------------------------------------------------------------------
# update_entry: growth only, range only, id preserved
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_growing_the_range_writes_only_the_range_and_keeps_the_id():
    """Amendment 2 item AA: the old statement dumped the whole schema into SET.

    That wrote the primary key into its own SET clause and would happily rewrite asset_symbol,
    asset_type and data_type -- mutating what the stored bars ARE while leaving them in place. The
    SET clause is asserted as an exact set, which is the one place an exact equality is right:
    every additional column here is a field the ruling says an extension may not touch, and `id`
    appearing would mean extension stopped preserving the id.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH),)]), FakeResult([]), FakeResult([]))

    await crud.update_entry(db, update_request(entry_id, end=APRIL))

    statement = _only(db.statements, 'UPDATE')
    assert _update_set_columns(_sql(statement)) == {'start', 'end', 'updated_at'}
    assert entry_id in _params(statement).values(), 'the update no longer addresses the entry by its id'
    assert db.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('stored_start', 'stored_end', 'new_start', 'new_end'),
    [
        (JANUARY, MARCH, FEBRUARY, MARCH),
        (JANUARY, MARCH, JANUARY, FEBRUARY),
        (JANUARY, NullableDateTime.EPOCH, JANUARY, APRIL),
    ],
    ids=['start moved forward', 'end pulled back', 'open-ended entry closed'],
)
async def test_an_update_that_shrinks_the_range_is_rejected(
    stored_start: datetime, stored_end: datetime, new_start: datetime, new_end: datetime
):
    """Growth only. Shrinking strands already-stored bars outside the entry's declared coverage.

    THE THIRD CASE IS THE SENTINEL ONE AND IS THE REASON THIS IS PARAMETRIZED. A stored open-ended
    entry holds end = 1970-01-01, so a naive `new_end >= old_end` reads backwards: closing an
    open-ended dataset would look like growth because April is later than the EPOCH, when it is in
    fact the largest shrink available -- it strands everything after April.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, start=stored_start, end=stored_end),)]))

    with pytest.raises(crud.RangeShrink):
        await crud.update_entry(db, update_request(entry_id, start=new_start, end=new_end))

    assert len(db.statements) == 1, 'the shrinking update was sent anyway'
    assert db.commits == 0


@pytest.mark.asyncio
async def test_opening_a_bounded_entry_is_growth():
    """The negative control for the sentinel case above: EPOCH as the NEW end is unbounded growth.

    Without this, an implementation that rejected every EPOCH end outright would pass the shrink
    cases and quietly make "extend this dataset indefinitely" impossible.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH),)]), FakeResult([]), FakeResult([]))

    await crud.update_entry(db, update_request(entry_id, end=None))

    statement = _only(db.statements, 'UPDATE')
    assert NullableDateTime.EPOCH in _params(statement).values(), 'the open-ended end was not written as the sentinel'
    assert db.commits == 1


@pytest.mark.asyncio
async def test_a_grow_that_collides_names_the_other_entry_and_sends_no_update():
    """The range columns are identity columns, so growing a range MUTATES THE UNIQUE KEY.

    The new range can make this row collide with another of the same owner's entries. Amendment 1
    item Q requires a named error carrying the other id -- the same response shape as create --
    rather than a raw IntegrityError surfacing from the database, which names a constraint and no
    id the caller can act on. Checked proactively, so no UPDATE is sent at all.
    """
    entry_id, other_id = uuid4(), uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH),)]), FakeResult([(other_id,)]))

    with pytest.raises(crud.RangeCollision) as raised:
        await crud.update_entry(db, update_request(entry_id, end=APRIL))

    assert raised.value.colliding_id == other_id
    assert str(other_id) in str(raised.value)
    assert [s for s in db.statements if _sql(s).startswith('UPDATE')] == [], 'the colliding update was sent anyway'
    assert db.commits == 0


@pytest.mark.asyncio
async def test_the_collision_check_reads_identity_off_the_stored_entry_not_the_request():
    """The request's non-range fields are untrustworthy here, and this is the test that says so.

    An extension may change only start and end, so the other nine identity columns come off the
    stored row. Reading them off the request instead would let a caller send a different symbol and
    have the collision searched for under that symbol -- a check that passes while the real
    collision stands. The request below names MSFT; the stored entry is AAPL, and AAPL is what must
    be bound. The entry is also excluded from its own result set, or every grow would collide with
    itself.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, symbol='AAPL', end=MARCH),)]), FakeResult([]), FakeResult([]))

    await crud.update_entry(db, update_request(entry_id, symbol='MSFT', end=APRIL))

    collision_select = db.statements[1]
    bound = _params(collision_select).values()
    assert 'AAPL' in bound, 'the collision check trusted the request instead of the stored entry'
    assert 'MSFT' not in bound, 'the request could redirect the collision check to another symbol'
    assert 'store_dataset_entry.id !=' in _sql(collision_select), 'the entry is not excluded from its own collision set'


@pytest.mark.asyncio
async def test_the_grow_collision_check_keys_on_the_tape_unlike_the_own_overlap_refusal():
    """THE OTHER SIDE OF tj-xn3qa6 D1, and the one the decision record does not spell out.

    _find_exact_collision asks "would this grow make `existing` the SAME DATASET as another row",
    which is the eleven-column unique constraint's question, so it keys on IDENTITY -- the tape
    included. _find_own_overlap asks a different question and keys on eight. The two are therefore
    deliberately inconsistent with each other, and that is the single most likely thing for a later
    reader to "fix": both are overlap-ish checks over the same table in the same module, and one
    reading `_OVERLAP_EQUALITY_COLUMNS` looks like the tidier code.

    WHAT THE "TIDY" VERSION BREAKS, which is why this is a test and not a comment: left feed-blind,
    a grow whose new range lands exactly on ANOTHER TAPE's entry is refused with a 409 naming that
    entry -- a write Postgres would have accepted quite happily, because the two rows differ in
    feed and so do not collide on the constraint. A spurious refusal of a legitimate extension,
    reported against someone else's dataset id.

    DRIVEN THROUGH update_entry, so it is the statement the real path builds. The stored entry is
    SIP; the assertion is that the tape is bound and that it is the STORED one, since the same
    argument that sends the other identity columns to the stored row sends this one there too.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH, feed=Feed.SIP),)]), FakeResult([]), FakeResult([]))

    await crud.update_entry(db, update_request(entry_id, end=APRIL, feed=Feed.IEX))

    collision_select = db.statements[1]
    assert 'store_dataset_entry.feed =' in _sql(collision_select), (
        "the grow-collision check is feed-blind, so an extension landing on another tape's entry "
        'is refused with a 409 the database would not have raised (tj-xn3qa6 D1)'
    )
    bound = list(_params(collision_select).values())
    assert Feed.SIP in bound, 'the collision check does not bind the STORED tape'
    assert Feed.IEX not in bound, 'the request could redirect the collision check to another tape'


# ---------------------------------------------------------------------------------------------
# delete_entry_by_id: a constraint, not a sweep
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_delete_is_one_statement_with_no_bar_sweep():
    """What killed D1, the ~65,000-bar IN-list ceiling: there is no IN-list, because there is no sweep.

    The removed code deleted memberships, captured them with RETURNING, built a Python list of ids
    and swept orphans with an IN clause whose length grew with the dataset. The replacement is one
    DELETE against one table. Asserted as "the bar table is not named and no IN-list is built",
    which is the structural claim -- a reintroduced sweep would have to break one of those.

    NOT PROVED HERE: that the cascade removes exactly this entry's bars and nothing else. That is a
    live-database property (tj-vhboky.14) and no green result in this file is evidence of it.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id),)]), FakeResult([], rowcount=1))

    await crud.delete_entry_by_id(db, entry_id, OWNER)

    sql = _sql(_only(db.statements, 'DELETE'))
    assert StockMarketActivity.TABLE_NAME not in sql, f'the delete still sweeps the bar table: {sql}'
    assert ' IN (' not in sql, f'the delete still builds an IN-list: {sql}'
    assert len(db.statements) == 2, 'the delete path issued statements beyond the lookup and the delete'
    assert db.commits == 1


def test_the_bar_foreign_key_declares_on_delete_cascade():
    """The half of the cascade ruling that IS agent-verifiable: what the model asks the database for.

    Without ondelete='CASCADE' on the bar's dataset_id the delete above leaves every bar behind, or
    fails on the foreign key -- and with the sweep gone there is no code left to catch it. The
    migration having actually applied the clause to the live table is the other half and stays on
    the host-verified tier.
    """
    cascading = [
        fk
        for column in StockMarketActivity.__table__.columns
        if column.name == 'dataset_id'
        for fk in column.foreign_keys
    ]
    assert cascading, 'stock_market_activity.dataset_id has no foreign key to cascade from'
    for fk in cascading:
        assert fk.target_fullname == f'{StoreDatasetEntry.TABLE_NAME}.id'
        assert fk.ondelete == 'CASCADE', 'the bar FK no longer cascades, so deleting an entry orphans its bars'


# ---------------------------------------------------------------------------------------------
# search_entries
# ---------------------------------------------------------------------------------------------


def _path() -> StoreAssetDatasetPath:
    return StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL')


@pytest.mark.asyncio
async def test_search_filters_by_owner():
    """Amendment 2 item Y asked for this to be VERIFIED, not assumed, and this is the verification.

    The filter is applied by a loop over the query model's fields, so owner being filterable is a
    property of the loop plus the schema rather than of any line naming owner. A filter that
    silently does nothing is worse than one that is absent, and only a bound value proves the
    difference.
    """
    db = FakeSession(FakeResult([]))

    await crud.search_entries(db, _path(), StoreAssetDatasetQuery(owner='strategy-b'))

    statement = db.statements[0]
    assert 'store_dataset_entry.owner =' in _sql(statement), 'the owner filter is inert'
    assert 'strategy-b' in _params(statement).values()


@pytest.mark.asyncio
async def test_search_reads_expiry_off_the_entry_and_keeps_the_outer_join_for_the_count():
    """The live AttributeError this bead was assigned, and the join that must survive fixing it.

    func.min(joined_table.expiry) raised the moment search_entries was called, because commit
    9e0dd22 moved expiry off the bar. The fix is to read StoreDatasetEntry.expiry directly -- but
    the OUTER join has to STAY, for count(id) only: AssetDatasetStore.item_count is a required int
    and an entry with no bars must still list. Deleting the join along with the aggregate is the
    plausible over-correction, so both halves are asserted.
    """
    db = FakeSession(FakeResult([]))

    await crud.search_entries(db, _path(), StoreAssetDatasetQuery())

    sql = _sql(db.statements[0])
    assert 'min(' not in sql, 'the expiry aggregate over the bar table is back; the bar has no expiry column'
    assert f'LEFT OUTER JOIN {StockMarketActivity.TABLE_NAME}' in sql, 'the outer join was lost with the aggregate'
    assert f'count({StockMarketActivity.TABLE_NAME}.id)' in sql, 'item_count is no longer counted from the bar table'


@pytest.mark.asyncio
async def test_an_entry_with_no_bars_still_lists_with_item_count_zero():
    """What the OUTER in outerjoin buys, asserted on the returned schema rather than on the SQL.

    An inner join drops the row entirely, and the endpoint would answer "this dataset does not
    exist" for a dataset that does. expiry is checked on the way out too: it now travels as the
    entry's own column through to_validated_schema rather than through the `additional` dict, so a
    value arriving as None would mean the conversion stopped reading it.
    """
    entry_id = uuid4()
    db = FakeSession(FakeResult([(stored_entry(entry_id, end=MARCH), 0)]))

    listed = await crud.search_entries(db, _path(), StoreAssetDatasetQuery())

    assert len(listed) == 1
    assert isinstance(listed[0], AssetDatasetStore)
    assert listed[0].id == entry_id
    assert listed[0].item_count == 0
    assert listed[0].expiry == FEBRUARY, 'expiry is not being read off the entry row'
    assert listed[0].end == MARCH


@pytest.mark.asyncio
async def test_an_open_ended_entry_lists_with_no_end_rather_than_1970():
    """The sentinel on the way OUT, which is the reader-facing half of the same convention.

    A caller reading end = 1970-01-01 would conclude the dataset covers nothing. NullableDateTime
    maps the stored sentinel back to None on the read, and that mapping is part of the read contract
    rather than a detail of the column.
    """
    db = FakeSession(FakeResult([(stored_entry(uuid4(), end=NullableDateTime.EPOCH), 3)]))

    listed = await crud.search_entries(db, _path(), StoreAssetDatasetQuery())

    assert listed[0].end is None, 'the EPOCH sentinel is being served to callers as a real date'
    assert listed[0].item_count == 3


# ---------------------------------------------------------------------------------------------
# The entry key: what makes two requests two entries (tj-vhboky.11 items 1, 4, 6, 20)
# ---------------------------------------------------------------------------------------------
#
# WHAT "THE ENTRY KEY" MEANS HERE, because every test below turns on it. The only thing that
# decides whether a create lands on an existing row or makes a new one is the constraint the
# upsert's ON CONFLICT names. So the key is read off the statement upsert_entry actually builds:
# the constraint name is parsed out of the compiled ON CONFLICT clause, that constraint is looked
# up on the TABLE (not on NATURAL_KEY, which is only the list the constraint happens to be built
# from), and the key is the INSERT's bound values for that constraint's columns. A field dropped
# from the constraint, dropped from the insert, or bound to the wrong value changes this tuple.
#
# WHAT THIS DOES NOT PROVE: that Postgres enforces the constraint or matches it on conflict. That
# is tj-vhboky.14's. What it proves is that the statement we send asks for the right thing.

# The one-field variants depart from this base. Chosen so every variant is a body the schema
# accepts: update_type=DAILY needs a non-BULK expiry_type and no end (StoreAssetDatasetBody.
# validate_fields), so the base is open-ended with a ROLLING expiry, and the `end` variant closes
# it. expiry is fixed rather than defaulted so no clock is involved.
_KEY_BASE: dict = {
    'owner': OWNER,
    'asset_symbol': 'AAPL',
    'asset_type': AssetType.STOCK,
    'data_type': DataType.MARKET_ACTIVITY,
    'source': DataSource.ALPACA_API,
    'granularity': Granularity.ONE_DAY,
    'expiry_type': ExpiryType.ROLLING,
    'update_type': UpdateType.STATIC,
    'feed': Feed.IEX,
    'start': JANUARY,
    'end': None,
    'expiry': FEBRUARY,
}

# One entry per identity field of tj-vhboky.1 section 2. ELEVEN now: tj-rh4b7f took the key from
# eleven to ten by DEFERRING feed off the entry, and tj-3mk3u5.31 put it back (closing tj-f2qz44),
# so the list is the one tj-ilo73k was originally written against again. This is the DESIGN's list,
# written out on purpose -- test_every_entry_key_column_has_a_one_field_case compares it against
# the model so that neither can move without the other.
#
# feed's case is the one tj-f2qz44 is ABOUT: an IEX request and a SIP request over the same window
# must not resolve to one entry. It is the only member of this list that is identity WITHOUT being
# an own-overlap term (tj-xn3qa6 D1), which is why the overlap section above pins its own column
# set separately instead of deriving both from one list.
_ONE_FIELD_CHANGES: dict = {
    'owner': 'strategy-b',
    'asset_symbol': 'MSFT',
    'asset_type': AssetType.CRYPTO,
    'data_type': DataType.QUOTE,
    'source': DataSource.IB_API,
    'granularity': Granularity.ONE_HOUR,
    'expiry_type': ExpiryType.BUFFER_1K,
    'update_type': UpdateType.DAILY,
    'feed': Feed.SIP,
    'start': FEBRUARY,
    'end': MARCH,
}


async def _upsert(request: AssetDatasetStoreCreate, returned_id: UUID | None = None) -> tuple[UUID, FakeSession]:
    """Run the real upsert with no overlap found, returning what it returned and what it sent."""
    db = FakeSession(FakeResult([]), FakeResult([(returned_id or uuid4(),)]))
    returned = await crud.upsert_entry(db, request)
    return returned, db


def _key_constraint_columns(sql: str) -> list[str]:
    """The columns of the constraint the compiled ON CONFLICT clause actually targets, looked up on the table."""
    target = re.search(r'ON CONFLICT ON CONSTRAINT (\w+) DO UPDATE', sql)
    assert target, f'the upsert does not target a named constraint, so it has no entry key to read: {sql}'
    constraints = [c for c in StoreDatasetEntry.__table__.constraints if c.name == target.group(1)]
    assert len(constraints) == 1, f'ON CONFLICT names {target.group(1)!r}, which the table does not declare'
    return [column.name for column in constraints[0].columns]


def _entry_key(db: FakeSession) -> dict:
    """The entry key the recorded upsert sends: the targeted constraint's columns, mapped to their bound values."""
    insert = _only(db.statements, 'INSERT')
    params = _params(insert)
    columns = _key_constraint_columns(_sql(insert))
    unbound = [column for column in columns if column not in params]
    assert unbound == [], f'key columns are not bound by the insert, so the key cannot dedup on them: {unbound}'
    return {column: params[column] for column in columns}


@pytest.mark.asyncio
async def test_an_identical_request_resolves_to_the_same_entry_key():
    """The negative control for the one-field cases below.

    Without it, a key that differed on EVERY call -- a fresh uuid or a now() slipping into the
    constraint -- would pass all ten of them while breaking the exact-repeat guarantee outright.
    """
    _, first = await _upsert(AssetDatasetStoreCreate(**_KEY_BASE))
    _, second = await _upsert(AssetDatasetStoreCreate(**_KEY_BASE))

    assert _entry_key(first) == _entry_key(second)


@pytest.mark.asyncio
@pytest.mark.parametrize('field', list(_ONE_FIELD_CHANGES))
async def test_a_request_differing_in_one_identity_field_is_a_different_entry(field: str):
    """tj-ilo73k's acceptance criterion, one case per identity field (tj-vhboky.11 item 1).

    THE REGRESSION THIS EXISTS FOR IS SILENT. Drop a field from the entry's unique constraint and
    the write keeps succeeding: two requests that differ only in that field collide on ON CONFLICT
    and the second quietly resolves to the FIRST one's id -- a different owner's dataset, a
    different granularity's bars, a weaker lifetime. The existing tests in this file derive their
    column sets FROM NATURAL_KEY, so they follow a shrunken key down and stay green; this test
    names the fields from the design instead.

    For `start` and `end` the second create would, for the SAME owner, be refused as an own-overlap
    before it inserted anything (test_an_overlapping_but_not_identical_request_fails_naming_the_
    colliding_ids). That is still a different entry, which is the claim: what matters here is that
    the key cannot merge the two, so a range change can never be answered with the other range's id.
    """
    _, base = await _upsert(AssetDatasetStoreCreate(**_KEY_BASE))
    _, variant = await _upsert(AssetDatasetStoreCreate(**_KEY_BASE | {field: _ONE_FIELD_CHANGES[field]}))

    base_key, variant_key = _entry_key(base), _entry_key(variant)
    assert field in base_key, f'{field} is not part of the entry key, so two requests differing in it are one entry'
    differing = sorted(column for column in base_key if base_key[column] != variant_key[column])
    assert differing == [field], f'changing only {field} changed the entry key in {differing}'


def test_every_entry_key_column_has_a_one_field_case():
    """The model's key and the design's field list, compared both ways.

    A column added to the constraint without a case here would be an identity field nothing pins;
    one removed from it reddens its case above and this. That is exactly how feed arrived: this
    test went red when tj-3mk3u5.31 put it back on the key, which was the prompt to add its case
    deliberately rather than by accident.
    """
    constraints = [
        c for c in StoreDatasetEntry.__table__.constraints if c.name == StoreDatasetEntry.NATURAL_KEY_CONSTRAINT
    ]
    assert len(constraints) == 1
    assert sorted(column.name for column in constraints[0].columns) == sorted(_ONE_FIELD_CHANGES)


def test_no_column_of_the_entry_key_is_nullable():
    """The model half of "a key column can never reach the database as NULL" (tj-vhboky.11 item 4).

    Postgres treats NULL as distinct from NULL in a unique index, so ONE nullable key column is
    enough to make every null-bearing row unique and to turn an exact repeat into a second row.
    The schema half -- an explicit null is refused at the edge -- is in
    schemas/tests/test_schemas_smoke_data_store.py. Derived from the constraint, so a column that
    joins the key later is covered here without an edit.
    """
    constraints = [
        c for c in StoreDatasetEntry.__table__.constraints if c.name == StoreDatasetEntry.NATURAL_KEY_CONSTRAINT
    ]
    assert len(constraints) == 1
    nullable = sorted(column.name for column in constraints[0].columns if column.nullable)
    assert nullable == [], f'entry key columns accept NULL, so the unique key cannot dedup on them: {nullable}'


@pytest.mark.asyncio
async def test_two_owners_asking_for_the_same_spec_get_two_entries_and_neither_fails():
    """tj-vhboky.11 item 6: owner is identity, so the same spec twice is two datasets, not a conflict.

    test_the_overlap_check_is_scoped_to_the_requesting_owner pins that the second owner's overlap
    check binds ITS owner. This pins the rest of the claim: that it does not ALSO see the first
    owner (so it cannot fail against them), that both creates commit and return their own ids,
    and that the two inserts carry keys differing in owner and nothing else -- so the constraint
    cannot fold the second owner onto the first owner's row.
    """
    first_id, second_id = uuid4(), uuid4()

    first_returned, first = await _upsert(create_request(owner='strategy-a', end=MARCH), first_id)
    second_returned, second = await _upsert(create_request(owner='strategy-b', end=MARCH), second_id)

    assert (first_returned, second_returned) == (first_id, second_id)
    assert (first.commits, second.commits) == (1, 1)
    assert 'strategy-a' not in _params(second.statements[0]).values(), (
        "the second owner's overlap check can match the first owner's entry, so it can fail against it"
    )
    first_key, second_key = _entry_key(first), _entry_key(second)
    assert sorted(column for column in first_key if first_key[column] != second_key[column]) == ['owner']


def test_the_entry_expiry_column_is_timezone_aware():
    """tj-vhboky.11 item 20, the column half. A plain TIMESTAMP is the defect being fixed.

    The bar's old expiry was a naive DateTime while every other timestamp here is timestamptz, and
    a naive column makes "when does this data die" depend on the session timezone. Nothing else
    reads this attribute, so without an explicit assertion a revert to DateTime() is invisible.
    The compiled-DDL half is in test_head_revision_shape.py.

    NOT COVERED, AND NOT COVERABLE WITHOUT A PRODUCTION CHANGE: a NAIVE expiry sent in the request
    body is accepted and bound unchanged into the insert today -- neither refused nor converted.
    Filed as tj-1bl90i rather than pinned here.
    """
    expiry = StoreDatasetEntry.__table__.columns['expiry']
    assert expiry.type.timezone is True, 'store_dataset_entry.expiry is a naive TIMESTAMP again'


# ---------------------------------------------------------------------------------------------
# The owner column stays NOT NULL, in its own right (tj-ap3he4)
# ---------------------------------------------------------------------------------------------
#
# test_no_column_of_the_entry_key_is_nullable already reds on a nullable owner, but only because
# owner happens to be in the constraint it derives from, and it names neither of the two things
# that depend on owner being NOT NULL. The delete-route test asserts the same property as a side
# effect of an authorisation argument. Neither reaches the consequence: that a NULL in an identity
# column turns an exact repeat into a second row. The two tests below pin the column by name and
# then show that consequence against the model's own table.


def test_the_entry_owner_column_is_not_null_for_authorisation_and_for_identity():
    """StoreDatasetEntry.owner is NOT NULL, and two separate properties rest on it.

    AUTHORISATION. _check_owner refuses an id-addressed write when ``existing.owner !=
    declared_owner``. An owner-less DELETE declares None, and it is refused only because the stored
    owner is never None, so the comparison always holds. A NULL stored owner would make None equal
    None, and an owner-less caller could delete or grow that entry.

    IDENTITY. owner is a column of uq_store_dataset_entry_identity, and a unique index treats NULL
    as distinct from NULL. A nullable owner would stop the exact-repeat-returns-the-existing-id
    path from matching, which is shown against a real table by
    test_a_null_owner_turns_an_exact_repeat_into_a_second_row_unless_the_column_refuses_it.

    Named rather than derived from the constraint, so dropping owner from the key does not also
    drop this check. The authorisation reason holds whether or not owner is identity.
    """
    owner = StoreDatasetEntry.__table__.columns['owner']
    key_columns = [
        column.name
        for constraint in StoreDatasetEntry.__table__.constraints
        if constraint.name == StoreDatasetEntry.NATURAL_KEY_CONSTRAINT
        for column in constraint.columns
    ]

    assert owner.nullable is False, (
        'store_dataset_entry.owner accepts NULL. Authorisation: _check_owner would let an owner-less '
        'request (declared None) pass against a NULL-owned entry. Identity: owner is in '
        f'{StoreDatasetEntry.NATURAL_KEY_CONSTRAINT}, where NULL is distinct from NULL, so an exact '
        'repeat would insert a second row instead of returning the existing id.'
    )
    assert 'owner' in key_columns, (
        f'owner is no longer in {StoreDatasetEntry.NATURAL_KEY_CONSTRAINT}. The identity reason above '
        'has changed, so revisit this test and tj-vhboky.1 section 5 together.'
    )


def _replay_on_sqlite(engine, row: dict, key_columns: list[str]) -> UUID:
    """Send one upsert of `row` to the model's own table, with an ON CONFLICT on the identity key.

    The SQLite spelling of what upsert_entry sends Postgres. SQLite cannot target a constraint by
    name, so it names the constraint's own columns, which resolve to the same unique index. The SET
    clause matches production: expiry and updated_at only.
    """
    stmt = sqlite.insert(StoreDatasetEntry.__table__).values(id=uuid4(), created_at=JANUARY, updated_at=JANUARY, **row)
    stmt = stmt.on_conflict_do_update(
        index_elements=key_columns, set_={'expiry': row['expiry'], 'updated_at': FEBRUARY}
    ).returning(StoreDatasetEntry.__table__.c.id)
    with engine.begin() as connection:
        return connection.execute(stmt).scalar_one()


def _row_count(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(StoreDatasetEntry.__table__)).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', [OWNER, None], ids=['control-a-declared-owner', 'a-null-owner'])
async def test_a_null_owner_turns_an_exact_repeat_into_a_second_row_unless_the_column_refuses_it(owner):
    """A NULL in an identity column defeats the exact-repeat path. The NOT NULL on owner is what stops it.

    THE CONSEQUENCE, SHOWN RATHER THAN DESCRIBED. The row is the INSERT the real upsert_entry
    builds, with its key columns read off the constraint the ON CONFLICT names. That row is then
    sent twice to StoreDatasetEntry's own table on an in-memory SQLite, the second time as an exact
    repeat.

    * CONTROL, a declared owner. The repeat hits the unique key and returns the first id. There is
      one row. This shows the harness's ON CONFLICT really matches, so a second row in the other
      case is caused by the NULL and not by the harness.
    * A NULL owner. Both SQLite and Postgres (without NULLS NOT DISTINCT) treat NULL as distinct in
      a unique index, so the ON CONFLICT never fires. Today the column refuses the NULL outright,
      and both attempts raise IntegrityError. Make owner nullable and both attempts succeed. The
      table then holds two rows for one dataset under two different ids, with no error anywhere.

    WHAT THIS DOES NOT PROVE: that Postgres behaves the same. SQLite stands in for its unique-index
    NULL semantics, which the two share by default. The Postgres-verified tier is tj-vhboky.14.
    The schema already refuses an owner-less create (owner is required), so this row cannot reach
    the table through the API today. The column is the backstop this test pins.
    """
    _, db = await _upsert(AssetDatasetStoreCreate(**_KEY_BASE))
    key = _entry_key(db)
    insert_params = _params(_only(db.statements, 'INSERT'))
    table_columns = set(StoreDatasetEntry.__table__.columns.keys())
    row = {column: value for column, value in insert_params.items() if column in table_columns} | {'owner': owner}

    engine = create_engine('sqlite://')
    StoreDatasetEntry.__table__.create(engine)

    outcomes = []
    for _attempt in range(2):
        try:
            outcomes.append(_replay_on_sqlite(engine, row, list(key)))
        except IntegrityError as error:
            outcomes.append(f'refused: {error.orig}')
    rows = _row_count(engine)

    if owner is not None:
        assert rows == 1, f'the control repeat made {rows} rows, so the harness ON CONFLICT does not match'
        assert outcomes[0] == outcomes[1], f'the control repeat returned a different id: {outcomes}'
        return

    assert all(str(outcome).startswith('refused: NOT NULL') for outcome in outcomes), (
        f'a NULL owner was accepted. The exact repeat produced {rows} rows, outcomes {outcomes}: the '
        f'NULL is distinct in {StoreDatasetEntry.NATURAL_KEY_CONSTRAINT}, so ON CONFLICT never matched '
        'and a second row was written instead of the existing id being returned.'
    )
    assert rows == 0
