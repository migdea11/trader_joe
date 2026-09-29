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

import logging
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
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


def create_request(end: datetime | None = None, owner: str = OWNER, symbol: str = 'AAPL') -> AssetDatasetStoreCreate:
    """A create request. end defaults to None -- the open-ended case, where the sentinel bites."""
    return AssetDatasetStoreCreate(
        owner=owner,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        start=JANUARY,
        end=end,
        expiry=FEBRUARY,
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
    )


def update_request(
    entry_id: UUID, start: datetime = JANUARY, end: datetime | None = APRIL, owner: str = OWNER, symbol: str = 'AAPL'
) -> AssetDatasetStoreUpdate:
    return AssetDatasetStoreUpdate(
        id=entry_id,
        owner=owner,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
        start=start,
        end=end,
        expiry=FEBRUARY,
        expiry_type=ExpiryType.BULK,
        update_type=UpdateType.STATIC,
    )


def stored_entry(
    entry_id: UUID, start: datetime = JANUARY, end: datetime = MARCH, owner: str = OWNER, symbol: str = 'AAPL'
) -> StoreDatasetEntry:
    """A detached ORM instance standing in for a row already in the table.

    `end` takes the stored representation, so an open-ended stored entry is spelled
    NullableDateTime.EPOCH here, exactly as the column holds it.
    """
    return StoreDatasetEntry(
        id=entry_id,
        owner=owner,
        asset_symbol=symbol,
        asset_type=AssetType.STOCK,
        data_type=DataType.MARKET_ACTIVITY,
        source=DataSource.ALPACA_API,
        granularity=Granularity.ONE_DAY,
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


@pytest.mark.asyncio
async def test_the_overlap_check_equates_every_non_range_identity_column():
    """The equality prefix, DERIVED from the key rather than restated, so an amendment cannot desynchronise it.

    Two rows only conflict when they are the same dataset, which means every identity column but
    the range pair must be compared. Drop one and the check WIDENS: a different granularity, a
    different source or a different data type starts reading as a collision, and a legitimate
    create is rejected with someone else's id. Nothing else here notices -- only `owner` was
    pinned, by test_the_overlap_check_is_scoped_to_the_requesting_owner.

    Derived the way test_the_insert_names_every_identity_column derives its set, because this set
    has already moved once: tj-rh4b7f took it from nine columns to eight when `feed` was deferred
    off the entry. A restated list would have had to be edited by hand then, and will again.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=MARCH))

    expected = set(StoreDatasetEntry.NATURAL_KEY) - {'start', 'end'}
    compared = _equality_columns(_sql(db.statements[0]))
    missing = sorted(expected - compared)
    assert missing == [], f'the overlap check ignores identity columns, so it reports false conflicts: {missing}'


# ---------------------------------------------------------------------------------------------
# The EPOCH sentinel, on both sides of the overlap query
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_open_ended_request_omits_the_upper_bound_predicate():
    """The half the builder only spot-checked, and the one most likely to stop working silently.

    An open-ended request stores end = 1970-01-01, a value in the PAST. The ADR's
    `existing.start <= request.end` would then read "starts before 1970" and match nothing, so an
    open-ended request would collide with NOTHING and every overlap would be created as a
    duplicate dataset. The predicate is therefore dropped rather than evaluated, because an
    open-ended range has no upper bound to test.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=None))

    sql = _sql(db.statements[0])
    assert 'store_dataset_entry.start <=' not in sql, (
        'an open-ended request compares existing.start against the EPOCH sentinel, so it can never collide'
    )


@pytest.mark.asyncio
async def test_a_bounded_request_keeps_the_upper_bound_predicate():
    """The negative control, without which the test above passes over a query that lost the predicate entirely.

    A request with a declared end DOES have an upper bound, and dropping it there would make every
    later dataset of the same owner look like a collision.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=MARCH))

    statement = db.statements[0]
    assert 'store_dataset_entry.start <=' in _sql(statement), 'a bounded request lost its upper-bound predicate'
    assert MARCH in _params(statement).values(), 'the upper bound is not the request end'


@pytest.mark.asyncio
@pytest.mark.parametrize('requested_end', [None, MARCH], ids=['open-ended request', 'bounded request'])
async def test_a_stored_open_ended_entry_is_never_excluded_by_the_lower_bound(requested_end: datetime | None):
    """The other side of the sentinel: the STORED end, which is the one the ADR warned about.

    An entry with no declared end covers everything from its start onward, so it must satisfy
    `existing.end >= request.start` unconditionally. Compared literally it would fail -- 1970 is
    before any real start -- and an open-ended stored dataset would become invisible to every
    subsequent overlap check. The disjunction is asserted in BOTH request shapes because the
    open-ended branch above removes a predicate, and a refactor that removed this one with it would
    pass the open-ended test alone.
    """
    db = FakeSession(FakeResult([]), FakeResult([(uuid4(),)]))

    await crud.upsert_entry(db, create_request(end=requested_end))

    statement = db.statements[0]
    sql = _sql(statement)
    assert re.search(r'\(store_dataset_entry\."end" = %\(\w+\)s OR store_dataset_entry\."end" >= %\(\w+\)s\)', sql), (
        f'the stored EPOCH sentinel is no longer handled as "covers everything onward": {sql}'
    )
    assert NullableDateTime.EPOCH in _params(statement).values(), 'the sentinel value itself is not bound'
    assert JANUARY in _params(statement).values(), 'the lower bound is not the request start'


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
    assert raised.value is db.error, f'{case} did not leave as the original error: {raised.value!r}'
    assert raised.value.__cause__ is None, f'{case} gained a chained cause: {raised.value.__cause__!r}'


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

    An extension may change only start and end, so the other eight identity columns come off the
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
    'start': JANUARY,
    'end': None,
    'expiry': FEBRUARY,
}

# One entry per identity field of tj-vhboky.1 section 2, as amended by tj-rh4b7f: feed is NOT an
# entry field any more, so there are ten, not the eleven tj-ilo73k was written against. This is
# the DESIGN's list, written out on purpose -- test_every_entry_key_column_has_a_one_field_case
# compares it against the model so that neither can move without the other.
_ONE_FIELD_CHANGES: dict = {
    'owner': 'strategy-b',
    'asset_symbol': 'MSFT',
    'asset_type': AssetType.CRYPTO,
    'data_type': DataType.QUOTE,
    'source': DataSource.IB_API,
    'granularity': Granularity.ONE_HOUR,
    'expiry_type': ExpiryType.BUFFER_1K,
    'update_type': UpdateType.DAILY,
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
    one removed from it reddens its case above and this. feed coming back onto the entry
    (tj-rh4b7f defers it to the gRPC transport work) lands here first, which is the prompt to add
    its case deliberately rather than by accident.
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
