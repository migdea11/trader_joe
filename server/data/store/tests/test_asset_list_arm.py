"""The asset-list arm end to end over SQLite: DeliveryIntake, app/world_snapshot.py, crud/asset_list.py (P17).

THE DESIGN: tj-7zjov1.30 (P17), its description as shrunk on 2026-10-26 (a raw dated snapshot plus appeared, vanished
and status_flip events derived by vendor asset id against the previous snapshot of the same list, in the delivery
transaction; a delisting that renames on the same id is ONE status_flip; a replayed delivery writes nothing twice), the
14:31 UTC EMPTY LIST RULE (an empty list is recorded as a zero-row snapshot of the request's list, derives no event,
and is never a diff base; the base is the newest NON-EMPTY snapshot of that list), the 21:56 UTC served-range ruling
(asset_list stays out of _SERVED_RANGE_KINDS; the attempt row has NULL labels and no served range; a SUCCEEDED report
exactly as AssetListAttemptRunner builds it is written, not refused), and the architect's ruling of 23:31 UTC (ADR
tj-26jt9w addendum 17): the arm files EVERY delivery under the REQUEST's list only, a NULL list is refused and never
defaulted, a record whose active flag contradicts the list is stored raw, counted as a member and logged as ONE
WARNING with the count, active None is a member with no warning.

THE EVENT RULES RULED AT THE P17 GATE (ADR tj-26jt9w addendum 18; fix bead P17f tj-7zjov1.164), tests named ruled_*:
  (a) FLIP RULE: an id new to a list that ANY earlier snapshot of the OTHER list held (taken strictly before this one)
    is a status_flip carrying both symbols and naming the latest such sighting, whether or not the other list's latest
    snapshot still holds it.
  (b) NO VANISHED SUPPRESSION: an id in the base and not in this snapshot is ALWAYS a vanished of this list. A move
    between lists is a vanished on the list it left plus a status_flip on the list it reached, the same two events
    whichever list arrives first; a re-delisting against a stale other-list snapshot is reported.
  (d) A LATE SNAPSHOT (its as_of not after, that is earlier than or EQUAL to, the as_of of the newest non-empty
    snapshot of its list) is stored raw, derives NO event, and logs ONE WARNING carrying the request id and both
    instants and no symbol; it is never a base, so the next in-order snapshot still diffs against the newest one.
The mass-vanish guard is still unbuilt (addendum 18 (c), question tj-7zjov1.165): judgement_call_no_mass_vanish_*.

TIER. The intake's real statements on SQLite through the harness of test_delivery_intake.py (its Store, engine and
fixtures, imported). What SQLite cannot show is in tests/system/test_asset_list_pg.py: the enum types, the CHECK on
Postgres, the advisory lock serialising two deliveries of one (source, list), and the arm on Postgres.
"""

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app import world_snapshot
from data.store.app.database.crud import asset_list as crud
from data.store.app.database.models.asset_list import (
    AssetListEvent,
    AssetListEventKind,
    AssetListRow,
    AssetListSnapshot,
)
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRunState
from data.store.app.ingest import delivery_intake
from data.store.tests.delivery_reports import INSTANCE_A, INSTANCE_B, QUOTA_KEY, report, with_header
from data.store.tests.test_delivery_intake import (
    Store,
    aware_sqlite_datetimes,  # noqa: F401 (a fixture)
)
from data.store.tests.test_delivery_intake import engine as _intake_engine  # noqa: F401 (a fixture)
from schemas.data_ingest.delivery import (
    AssetRecord,
    AssetRecords,
    AttemptDelivery,
    DeliverAttemptResponse,
    DeliveryHeader,
    DeliveryOutcome,
    DeliveryTrailer,
    DeliveryVerdict,
    VendorCallStats,
)
from schemas.data_ingest.submit import AssetListStatus


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

ACTIVE, INACTIVE = AssetListStatus.ACTIVE, AssetListStatus.INACTIVE
APPEARED, VANISHED, FLIP = AssetListEventKind.APPEARED, AssetListEventKind.VANISHED, AssetListEventKind.STATUS_FLIP
SNAPSHOTS, ROWS, EVENTS = AssetListSnapshot.__table__, AssetListRow.__table__, AssetListEvent.__table__
DAY_0 = datetime(2026, 4, 1, 14, 0, tzinfo=UTC)
# A symbol a log line must never carry: the WARNING is a fixed sentence plus a count.
SECRET_SYMBOL = 'ZZLOGSYMBOL'


@pytest.fixture
def engine(request: pytest.FixtureRequest) -> AsyncEngine:
    """test_delivery_intake.py's SQLite engine, the asset-list tables included."""
    return request.getfixturevalue('_intake_engine')


@pytest.fixture
def store(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Store:
    """test_delivery_intake.py's harness: the real DeliveryIntake pointed at that engine."""
    return Store(engine, monkeypatch)


# --------------------------------------------------------------------------------------------- builders


def asset(vendor_asset_id: str, symbol: str, active: bool | None = True, **fields: Any) -> AssetRecord:
    raw = f'{{"id":"{vendor_asset_id}","symbol":"{symbol}","active":{active}}}'.encode()
    return AssetRecord(vendor_asset_id=vendor_asset_id, symbol=symbol, raw_payload=raw, active=active, **fields)


def runner_report(
    request_id: uuid.UUID, records: list[AssetRecord], as_of: datetime, *, attempt_no: int = 1
) -> AttemptDelivery:
    """A SUCCEEDED report exactly as AssetListAttemptRunner builds it (ingest asset_attempt.py).

    No feed, no served range, no vendor asset id, the trading quota key, vendor stats without a vendor_reported block,
    as_of, and one AssetRecords part, or none at all for an empty list.
    """
    return AttemptDelivery(
        header=DeliveryHeader(
            request_id=str(request_id), instance_id=INSTANCE_A, attempt_no=attempt_no, quota_key=QUOTA_KEY
        ),
        parts=[AssetRecords(records=records)] if records else [],
        trailer=DeliveryTrailer(
            outcome=DeliveryOutcome.SUCCEEDED,
            vendor_stats=VendorCallStats(
                calls=1, pages=1, throttled_429=0, retries=0, first_call_at=as_of, last_call_at=as_of
            ),
            as_of=as_of,
        ),
    )


async def deliver_list(
    store: Store, status: AssetListStatus, records: list[AssetRecord], as_of: datetime, *, source=DataSource.ALPACA_API
) -> uuid.UUID:
    """One asset-list request of that list, delivered once; returns the request id."""
    request_id = await store.request(IngestRequestKind.ASSET_LIST, asset_list_status=status)
    if source is not DataSource.ALPACA_API:
        await store.set_request(request_id, source=source)
    await store.deliver(runner_report(request_id, records, as_of))
    return request_id


async def _all(store: Store, statement) -> list[sa.Row]:
    async with AsyncSession(store.engine) as db:
        return list((await db.execute(statement)).all())


async def snapshot_of(store: Store, request_id: uuid.UUID) -> sa.Row:
    (row,) = await _all(store, sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == request_id))
    return row


async def rows_of(store: Store, snapshot_id: uuid.UUID) -> list[sa.Row]:
    return await _all(
        store, sa.select(ROWS).where(ROWS.c.snapshot_id == snapshot_id).order_by(sa.literal_column('rowid'))
    )


async def events_of(store: Store, request_id: uuid.UUID) -> list[tuple]:
    """The events the snapshot of that request derived, as (kind, id, symbol, previous symbol, base id, base as_of)."""
    snapshot = await snapshot_of(store, request_id)
    found = await _all(store, sa.select(EVENTS).where(EVENTS.c.snapshot_id == snapshot.id))
    for event in found:
        assert (event.as_of, event.asset_list, event.source) == (snapshot.as_of, snapshot.asset_list, snapshot.source)
    return sorted(
        (
            AssetListEventKind(event.kind),
            event.vendor_asset_id,
            event.symbol,
            event.previous_symbol,
            event.previous_snapshot_id,
            event.previous_as_of,
        )
        for event in found
    )


async def all_events(store: Store) -> list[sa.Row]:
    return await _all(store, sa.select(EVENTS))


# --------------------------------------------------------------------------------------------- the report as built


async def test_a_runner_shaped_success_is_written_with_null_labels_no_served_range_and_no_vendor_asset_id(store):
    """The 21:56 UTC ruling: written, not refused; every label column NULL, served_start and served_end NULL."""
    records = [asset('id-1', 'AAA', True, tradable=True, exchange='NYSE', name='Alpha'), asset('id-2', 'BBB', None)]
    request_id = await store.request(IngestRequestKind.ASSET_LIST)

    answer = await store.deliver(runner_report(request_id, records, DAY_0))

    assert answer == DeliverAttemptResponse(verdict=DeliveryVerdict.FINAL, attempt_count=1)
    (run,) = await store.runs(request_id)
    assert run.state is IngestRunState.SUCCEEDED
    assert (run.asset_symbol, run.asset_type, run.data_type, run.granularity, run.update_type) == (None,) * 5
    assert (run.served_start, run.served_end, run.vendor_asset_id, run.feed) == (None,) * 4
    assert (run.bars_written, run.quota_key, run.attempt_no) == (0, QUOTA_KEY, 1)
    assert (await store.request_row(request_id)).state is IngestRequestState.SUCCEEDED
    assert IngestRequestKind.ASSET_LIST not in delivery_intake._SERVED_RANGE_KINDS


async def test_the_snapshot_header_names_the_requests_list_the_attempt_and_the_reports_as_of(store):
    request_id = await store.request(IngestRequestKind.ASSET_LIST, asset_list_status=INACTIVE)

    await store.deliver(runner_report(request_id, [asset('id-1', 'AAA', False), asset('id-2', 'BBB', False)], DAY_0))

    snapshot = await snapshot_of(store, request_id)
    assert (snapshot.source, snapshot.asset_type, snapshot.asset_list) == (
        DataSource.ALPACA_API,
        AssetType.STOCK,
        INACTIVE,
    )
    assert (snapshot.request_id, snapshot.attempt_no, snapshot.as_of, snapshot.row_count) == (request_id, 1, DAY_0, 2)


async def test_every_row_is_stored_exactly_as_served_in_payload_order_across_parts(store):
    """Raw: the vendor's flags with None kept as NULL, its strings as given, the payload bytes byte for byte."""
    first = [asset('id-2', 'BBB', None), asset('id-1', 'AAA', True, tradable=False, exchange='X', name='N')]
    second = [asset('id-3', 'CCC', False, tradable=None)]
    request_id = await store.request(IngestRequestKind.ASSET_LIST)
    attempt = runner_report(request_id, first, DAY_0)
    attempt = attempt.model_copy(update={'parts': [AssetRecords(records=first), AssetRecords(records=second)]})

    await store.deliver(attempt)

    snapshot = await snapshot_of(store, request_id)
    stored = await rows_of(store, snapshot.id)
    assert snapshot.row_count == 3
    assert [
        (row.vendor_asset_id, row.symbol, row.name, row.exchange, row.active, row.tradable, row.raw_payload)
        for row in stored
    ] == [
        (r.vendor_asset_id, r.symbol, r.name, r.exchange, r.active, r.tradable, r.raw_payload) for r in first + second
    ]


async def test_a_failed_asset_list_attempt_writes_its_attempt_row_and_no_snapshot(store):
    request_id = await store.request(IngestRequestKind.ASSET_LIST)

    await store.deliver(report(request_id, DeliveryOutcome.FAILED, kind=IngestRequestKind.ASSET_LIST))

    (run,) = await store.runs(request_id)
    assert run.state is IngestRunState.FAILED
    assert (run.asset_symbol, run.asset_type, run.served_start) == (None, None, None)
    assert await store.asset_list_rows() == [[], [], []]


async def test_a_counted_attempt_row_of_an_asset_list_stores_no_vendor_asset_id_even_if_the_header_carries_one(store):
    request_id = await store.request(IngestRequestKind.ASSET_LIST)
    attempt = runner_report(request_id, [asset('id-1', 'AAA')], DAY_0)

    await store.deliver(
        attempt.model_copy(update={'header': attempt.header.model_copy(update={'vendor_asset_id': 'v'})})
    )

    (run,) = await store.runs(request_id)
    assert run.vendor_asset_id is None


# --------------------------------------------------------------------------------------------- the list is the request's


async def test_an_empty_inactive_delivery_is_recorded_as_an_inactive_zero_row_snapshot(store):
    """The 14:31 UTC rule (1) and addendum 17: the list comes from the request row, since there is no part to read.

    Mutant shown red: hard-code ACTIVE in write_asset_list.
    """
    request_id = await deliver_list(store, INACTIVE, [], DAY_0)

    snapshot = await snapshot_of(store, request_id)
    assert (snapshot.asset_list, snapshot.row_count, snapshot.as_of) == (INACTIVE, 0, DAY_0)
    assert await rows_of(store, snapshot.id) == []
    assert await all_events(store) == []
    (run,) = await store.runs(request_id)
    assert run.state is IngestRunState.EMPTY
    assert (await store.request_row(request_id)).state is IngestRequestState.EMPTY


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_every_record_flag_disagreeing_never_moves_the_snapshot_to_the_other_list(store, status):
    """The records' active flags never choose the list: all of them saying the opposite still files under the request's."""
    opposite = status is not ACTIVE
    request_id = await deliver_list(
        store, status, [asset('id-1', 'AAA', opposite), asset('id-2', 'BBB', opposite)], DAY_0
    )

    assert (await snapshot_of(store, request_id)).asset_list is status


@pytest.mark.parametrize('missing', ['asset_list_status', 'asset_type'])
async def test_a_request_naming_no_list_or_no_asset_type_is_refused_and_nothing_is_written(store, monkeypatch, missing):
    """Addendum 17 (2): NULL is unreachable (the CHECK), and if met it is REFUSED, never filed under a guessed list.

    The row cannot hold NULL past the CHECK, so the locked record is altered on its way to the arm.
    """
    request_id = await store.request(IngestRequestKind.ASSET_LIST, asset_list_status=INACTIVE)
    real_lock = delivery_intake.lock_request

    async def lock_without(db, wanted):
        return replace(await real_lock(db, wanted), **{missing: None})

    monkeypatch.setattr(delivery_intake, 'lock_request', lock_without)

    refusal = await store.refused(runner_report(request_id, [asset('id-1', 'AAA')], DAY_0))

    assert 'names no' in refusal.detail
    assert await store.asset_list_rows() == [[], [], []]
    assert (await store.request_row(request_id)).state is IngestRequestState.QUEUED


# --------------------------------------------------------------------------------------------- disagreement


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith('data.store') and r.levelno == logging.WARNING]


async def test_a_contradicting_record_is_stored_raw_counted_a_member_and_logged_once_with_the_count(store, caplog):
    """Addendum 17 (6), the validator case (f).

    One active False and one active None on the active list: both stored, the delivery commits, ONE WARNING naming the
    list and the count 1, never a symbol; both are members for the diff.
    """
    caplog.set_level(logging.WARNING)
    first = await deliver_list(
        store,
        ACTIVE,
        [asset('id-1', 'AAA', True), asset('id-2', SECRET_SYMBOL, False), asset('id-3', 'CCC', None)],
        DAY_0,
    )

    snapshot = await snapshot_of(store, first)
    assert [(row.vendor_asset_id, row.active) for row in await rows_of(store, snapshot.id)] == [
        ('id-1', True),
        ('id-2', False),
        ('id-3', None),
    ]
    assert (await store.request_row(first)).state is IngestRequestState.SUCCEEDED
    (warning,) = _warnings(caplog)
    message = warning.getMessage()
    assert 'holds 1 record(s)' in message and 'active list' in message and str(first) in message
    assert SECRET_SYMBOL not in message and 'id-2' not in message

    # Members: dropping both from the next active snapshot reports both vanished, exactly as for any record.
    second = await deliver_list(store, ACTIVE, [asset('id-1', 'AAA', True)], DAY_0 + timedelta(days=1))
    assert [(kind, vid) for kind, vid, *_ in await events_of(store, second)] == [(VANISHED, 'id-2'), (VANISHED, 'id-3')]


async def test_on_the_inactive_list_active_true_contradicts_and_false_does_not(store, caplog):
    caplog.set_level(logging.WARNING)

    request_id = await deliver_list(
        store, INACTIVE, [asset('id-1', 'AAA', True), asset('id-2', 'BBB', True), asset('id-3', 'CCC', False)], DAY_0
    )

    (warning,) = _warnings(caplog)
    assert 'holds 2 record(s)' in warning.getMessage() and 'inactive list' in warning.getMessage()
    assert (await snapshot_of(store, request_id)).row_count == 3


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_active_none_is_a_member_with_no_warning(store, caplog, status):
    caplog.set_level(logging.WARNING)

    request_id = await deliver_list(store, status, [asset('id-1', 'AAA', None), asset('id-2', 'BBB', None)], DAY_0)

    assert _warnings(caplog) == []
    assert (await snapshot_of(store, request_id)).row_count == 2


# --------------------------------------------------------------------------------------------- the diff


async def test_the_first_non_empty_snapshot_of_a_list_has_no_base_and_derives_nothing(store):
    """Even after an earlier EMPTY snapshot of that list, and with the other list already holding snapshots."""
    await deliver_list(store, INACTIVE, [asset('id-9', 'ZZZ', False)], DAY_0)
    await deliver_list(store, ACTIVE, [], DAY_0 + timedelta(hours=1))
    first = await deliver_list(store, ACTIVE, [asset('id-1', 'AAA')], DAY_0 + timedelta(hours=2))

    assert await events_of(store, first) == []
    assert await all_events(store) == []


async def test_appeared_and_vanished_carry_the_id_the_symbol_and_both_instants_exactly(store):
    base = await deliver_list(store, ACTIVE, [asset('id-1', 'AAA'), asset('id-2', 'BBB')], DAY_0)
    later = DAY_0 + timedelta(days=1)
    current = await deliver_list(store, ACTIVE, [asset('id-1', 'AAA'), asset('id-3', 'CCC')], later)

    base_snapshot = await snapshot_of(store, base)
    assert await events_of(store, current) == [
        (APPEARED, 'id-3', 'CCC', None, base_snapshot.id, DAY_0),
        (VANISHED, 'id-2', 'BBB', None, base_snapshot.id, DAY_0),
    ]


async def test_an_id_kept_with_a_new_symbol_derives_no_event(store):
    """No rename events (the shrunk description): a same-id symbol change is visible in the raw rows only."""
    await deliver_list(store, ACTIVE, [asset('id-1', 'OLD')], DAY_0)
    current = await deliver_list(store, ACTIVE, [asset('id-1', 'NEW')], DAY_0 + timedelta(days=1))

    assert await events_of(store, current) == []


async def test_a_status_flip_with_a_rename_on_the_same_id_is_one_event_carrying_both_symbols(store):
    """The 78500B403 shape: an id leaves the active list and arrives on the inactive one under another symbol."""
    active_1 = await deliver_list(store, ACTIVE, [asset('id-1', 'AAA'), asset('id-svb', 'SIVB')], DAY_0)
    await deliver_list(store, INACTIVE, [asset('id-9', 'ZZZ', False)], DAY_0 + timedelta(hours=1))
    inactive_2 = await deliver_list(
        store, INACTIVE, [asset('id-9', 'ZZZ', False), asset('id-svb', '78500B403', False)], DAY_0 + timedelta(days=1)
    )

    held_by = await snapshot_of(store, active_1)
    assert await events_of(store, inactive_2) == [(FLIP, 'id-svb', '78500B403', 'SIVB', held_by.id, DAY_0)]


async def test_the_flip_back_inactive_to_active_is_a_status_flip_too(store):
    inactive = await deliver_list(store, INACTIVE, [asset('id-1', 'AAA', False)], DAY_0)
    await deliver_list(store, ACTIVE, [asset('id-2', 'BBB')], DAY_0 + timedelta(hours=1))
    active_2 = await deliver_list(
        store, ACTIVE, [asset('id-2', 'BBB'), asset('id-1', 'AAA')], DAY_0 + timedelta(days=1)
    )

    held_by = await snapshot_of(store, inactive)
    assert await events_of(store, active_2) == [(FLIP, 'id-1', 'AAA', 'AAA', held_by.id, DAY_0)]


async def test_interleaved_lists_never_serve_as_each_others_diff_base(store):
    """The validator case (e).

    An active snapshot diffs only against the earlier active non-empty one, and an inactive one only against the
    earlier inactive one, whatever arrived between them.
    """
    active_1 = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB')], DAY_0)
    inactive_1 = await deliver_list(
        store, INACTIVE, [asset('x', 'XXX', False), asset('y', 'YYY', False)], DAY_0 + timedelta(hours=1)
    )
    active_2 = await deliver_list(
        store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB'), asset('c', 'CCC')], DAY_0 + timedelta(days=1)
    )
    inactive_2 = await deliver_list(store, INACTIVE, [asset('x', 'XXX', False)], DAY_0 + timedelta(days=1, hours=1))

    assert await events_of(store, active_2) == [
        (APPEARED, 'c', 'CCC', None, (await snapshot_of(store, active_1)).id, DAY_0)
    ]
    assert await events_of(store, inactive_2) == [
        (VANISHED, 'y', 'YYY', None, (await snapshot_of(store, inactive_1)).id, DAY_0 + timedelta(hours=1))
    ]


async def test_an_empty_snapshot_derives_nothing_and_the_next_one_diffs_against_the_earlier_non_empty_one(store):
    """The 14:31 UTC validator case.

    An empty active snapshot after a non-empty one commits, writes no event, and the next non-empty snapshot diffs
    against the earlier non-empty one, so a glitch never reads as everything vanishing.
    """
    first = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB')], DAY_0)
    empty = await deliver_list(store, ACTIVE, [], DAY_0 + timedelta(days=1))
    third = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=2))

    assert await events_of(store, empty) == []
    assert (await store.request_row(empty)).state is IngestRequestState.EMPTY
    assert await events_of(store, third) == [(VANISHED, 'b', 'BBB', None, (await snapshot_of(store, first)).id, DAY_0)]


async def test_lists_of_another_source_are_never_a_base(store):
    await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0, source=DataSource.IB_API)
    first = await deliver_list(store, ACTIVE, [asset('b', 'BBB')], DAY_0 + timedelta(days=1))

    assert await events_of(store, first) == []


async def test_a_replayed_delivery_writes_nothing_twice_and_repeats_its_answer(store):
    """The acceptance: idempotent on (request_id, attempt_no), answered from the stored row before the arm runs."""
    await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB')], DAY_0)
    request_id = await store.request(IngestRequestKind.ASSET_LIST)
    attempt = runner_report(request_id, [asset('a', 'AAA'), asset('c', 'CCC')], DAY_0 + timedelta(days=1))
    first = await store.deliver(attempt)
    before = await store.snapshot()

    again = await store.deliver(attempt)

    assert again == first
    assert await store.snapshot() == before
    assert len(await all_events(store)) == 2


async def test_the_event_insert_does_nothing_on_its_unique_key_so_a_repeat_cannot_double_an_event():
    """The second line of defence the crud names: ON CONFLICT (snapshot_id, vendor_asset_id, kind) DO NOTHING."""
    session = MagicMock()
    statements = []

    async def execute(statement):
        statements.append(statement)
        return MagicMock(rowcount=1)

    session.execute = execute
    event = crud.NewEvent(APPEARED, 'a', 'AAA', None, uuid.uuid4(), DAY_0)

    await crud.insert_events(
        session, source=DataSource.ALPACA_API, status=ACTIVE, snapshot_id=uuid.uuid4(), as_of=DAY_0, events=[event]
    )

    (statement,) = statements
    sql = ' '.join(str(statement.compile(dialect=postgresql.dialect())).split())
    assert sql.endswith('ON CONFLICT (snapshot_id, vendor_asset_id, kind) DO NOTHING')


# --------------------------------------------------------------------------------------------- ruled at the P17 gate


async def test_ruled_flip_rule_any_earlier_snapshot_of_the_other_list_counts_not_only_its_latest(store):
    """FLIP RULE, ruled (addendum 18 a).

    x was in an OLD inactive snapshot and is in no later one (purged); when x then shows up on the active list it is a
    status_flip naming that old snapshot, not an appearance.
    """
    old_inactive = await deliver_list(store, INACTIVE, [asset('x', 'XOLD', False), asset('y', 'YYY', False)], DAY_0)
    await deliver_list(store, INACTIVE, [asset('y', 'YYY', False)], DAY_0 + timedelta(days=1))
    await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=2))
    active_2 = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('x', 'XNEW')], DAY_0 + timedelta(days=3))

    assert await events_of(store, active_2) == [
        (FLIP, 'x', 'XNEW', 'XOLD', (await snapshot_of(store, old_inactive)).id, DAY_0)
    ]


async def test_ruled_flip_rule_names_the_latest_earlier_sighting_and_ignores_a_later_one(store):
    """The sighting is the newest one taken STRICTLY BEFORE this snapshot's instant.

    One taken after it (a delivery that arrives out of order) is not a sighting.
    """
    await deliver_list(store, INACTIVE, [asset('x', 'X1', False)], DAY_0)
    newer = await deliver_list(store, INACTIVE, [asset('x', 'X2', False)], DAY_0 + timedelta(days=1))
    await deliver_list(store, INACTIVE, [asset('x', 'X3', False)], DAY_0 + timedelta(days=5))
    await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=2))
    active_2 = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('x', 'XA')], DAY_0 + timedelta(days=3))
    active_3_base = await snapshot_of(store, active_2)
    active_3 = await deliver_list(
        store, ACTIVE, [asset('a', 'AAA'), asset('x', 'XA'), asset('z', 'ZZZ')], DAY_0 + timedelta(days=4)
    )

    assert await events_of(store, active_2) == [
        (FLIP, 'x', 'XA', 'X2', (await snapshot_of(store, newer)).id, DAY_0 + timedelta(days=1))
    ]
    assert await events_of(store, active_3) == [
        (APPEARED, 'z', 'ZZZ', None, active_3_base.id, DAY_0 + timedelta(days=3))
    ]


async def test_ruled_no_suppression_inactive_first_reports_a_status_flip_then_a_vanished(store):
    """NO VANISHED SUPPRESSION, ruled (addendum 18 b): inactive first.

    The move reaches the inactive list first (a flip naming the active snapshot that held x); the next active snapshot
    loses x and reports it vanished against its base, although the inactive list's newest snapshot holds it. The same
    two events as the active-first order below.
    """
    active_1 = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('x', 'XXX')], DAY_0)
    await deliver_list(store, INACTIVE, [asset('i', 'III', False)], DAY_0 + timedelta(hours=1))
    inactive_2 = await deliver_list(
        store, INACTIVE, [asset('i', 'III', False), asset('x', 'XXX', False)], DAY_0 + timedelta(days=1)
    )
    active_2 = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=1, hours=1))

    base = await snapshot_of(store, active_1)
    assert await events_of(store, inactive_2) == [(FLIP, 'x', 'XXX', 'XXX', base.id, DAY_0)]
    assert await events_of(store, active_2) == [(VANISHED, 'x', 'XXX', None, base.id, DAY_0)]
    assert sorted((AssetListEventKind(e.kind), e.vendor_asset_id) for e in await all_events(store)) == [
        (FLIP, 'x'),
        (VANISHED, 'x'),
    ]


async def test_ruled_no_suppression_active_first_reports_a_vanished_then_a_flip(store):
    """The same move with the active list arriving first (addendum 18 b).

    A vanished on the active snapshot, then a status_flip on the inactive one: the same two events as inactive first.
    """
    active_1 = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('x', 'XXX')], DAY_0)
    await deliver_list(store, INACTIVE, [asset('i', 'III', False)], DAY_0 + timedelta(hours=1))
    active_2 = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=1))
    inactive_2 = await deliver_list(
        store, INACTIVE, [asset('i', 'III', False), asset('x', 'XXX', False)], DAY_0 + timedelta(days=1, hours=1)
    )

    base = await snapshot_of(store, active_1)
    assert await events_of(store, active_2) == [(VANISHED, 'x', 'XXX', None, base.id, DAY_0)]
    assert await events_of(store, inactive_2) == [(FLIP, 'x', 'XXX', 'XXX', base.id, DAY_0)]


async def test_ruled_no_suppression_whatever_the_other_lists_newest_snapshots_hold(store):
    """Addendum 18 b: no read of the other list decides a vanished.

    The inactive list's newest non-empty snapshot holds x, and an EMPTY inactive snapshot follows it: the active
    snapshot that loses x reports it vanished all the same, against its own base.
    """
    active_1 = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('x', 'XXX')], DAY_0)
    await deliver_list(store, INACTIVE, [asset('i', 'III', False)], DAY_0 + timedelta(hours=1))
    await deliver_list(store, INACTIVE, [asset('i', 'III', False), asset('x', 'XXX', False)], DAY_0 + timedelta(days=1))
    await deliver_list(store, INACTIVE, [], DAY_0 + timedelta(days=1, hours=1))
    active_2 = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=2))

    assert await events_of(store, active_2) == [
        (VANISHED, 'x', 'XXX', None, (await snapshot_of(store, active_1)).id, DAY_0)
    ]


async def test_ruled_a_re_delisting_against_a_stale_inactive_snapshot_is_reported(store):
    """The I1 A5 A6 case of addendum 18 b, which suppression lost outright.

    x is inactive in I1, reactivated in A5 (a flip naming I1), and delisted again in A6 before the next inactive
    snapshot: A6 reports x vanished against A5, although I1, still the inactive list's newest snapshot, holds it. I2
    then holds x again, which is not new to the inactive list, so it derives nothing: A6's vanished is the only record.
    """
    inactive_1 = await deliver_list(store, INACTIVE, [asset('x', 'XXX', False), asset('i', 'III', False)], DAY_0)
    await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=3))
    active_5 = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('x', 'XXX')], DAY_0 + timedelta(days=4))
    active_6 = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=5))
    inactive_2 = await deliver_list(
        store, INACTIVE, [asset('x', 'XXX', False), asset('i', 'III', False)], DAY_0 + timedelta(days=6)
    )

    assert await events_of(store, active_5) == [
        (FLIP, 'x', 'XXX', 'XXX', (await snapshot_of(store, inactive_1)).id, DAY_0)
    ]
    assert await events_of(store, active_6) == [
        (VANISHED, 'x', 'XXX', None, (await snapshot_of(store, active_5)).id, DAY_0 + timedelta(days=4))
    ]
    assert await events_of(store, inactive_2) == []


# --------------------------------------------------------------------------------------------- a late snapshot (18 d)

LATE_SENTENCE = 'is not newer than the newest snapshot of its list; stored raw, no event derived'


def _late_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in _warnings(caplog) if LATE_SENTENCE in record.getMessage()]


def _assert_one_late_warning(
    caplog: pytest.LogCaptureFixture, request_id: uuid.UUID, as_of: datetime, newest: datetime, *hidden: str
) -> None:
    """ONE WARNING: the fixed sentence, the request id and both instants, and no symbol or vendor id of the list."""
    (warning,) = _late_warnings(caplog)
    message = warning.getMessage()
    assert str(request_id) in message
    assert f'as_of {as_of.isoformat()}' in message and f'newest as_of {newest.isoformat()}' in message
    for value in hidden:
        assert value not in message, value


@pytest.mark.parametrize('behind', [timedelta(days=1), timedelta(0)], ids=['earlier', 'equal'])
async def test_ruled_a_late_attempt_of_a_finished_request_is_stored_raw_derives_nothing_and_warns_once(
    store, caplog, behind
):
    """Addendum 18 d, a late attempt of a terminal request; EQUAL instants are late too (not after: >=).

    R1 succeeded at T1; R2 then delivered T2 (the newest of the list). A late attempt 2 of R1 taken at T2 minus
    `behind` is stored with its rows, derives no event, adds none to the list, and logs one WARNING.
    """
    caplog.set_level(logging.WARNING)
    t2 = DAY_0 + timedelta(days=2)
    first = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB')], DAY_0)
    second = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('c', 'CCC')], t2)
    events_before = len(await all_events(store))
    late_records = [asset('a', 'AAA'), asset('id-secret', SECRET_SYMBOL)]

    answer = await store.deliver(runner_report(first, late_records, t2 - behind, attempt_no=2))

    assert answer.verdict is DeliveryVerdict.FINAL
    async with AsyncSession(store.engine) as db:
        snapshots = (
            await db.execute(
                sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == first).order_by(SNAPSHOTS.c.attempt_no)
            )
        ).all()
    late = snapshots[-1]
    assert (late.attempt_no, late.row_count, late.as_of, late.asset_list) == (2, 2, t2 - behind, ACTIVE)
    assert [(row.vendor_asset_id, row.symbol) for row in await rows_of(store, late.id)] == [
        ('a', 'AAA'),
        ('id-secret', SECRET_SYMBOL),
    ]
    assert len(await all_events(store)) == events_before == 2
    assert await _all(store, sa.select(EVENTS).where(EVENTS.c.snapshot_id == late.id)) == []
    _assert_one_late_warning(caplog, first, t2 - behind, t2, SECRET_SYMBOL, 'id-secret', 'AAA')
    assert len(await events_of(store, second)) == 2


async def test_ruled_a_superseded_instances_report_after_the_recovered_attempt_derives_nothing(store, caplog):
    """Addendum 18 d, the superseded-instance case.

    Instance A held R1 and was superseded by B; B's recovered attempt 2 (taken later) delivered first and diffed; A's
    attempt 1, taken earlier, then lands: stored raw, answered FINAL, no event, one WARNING.
    """
    caplog.set_level(logging.WARNING)
    await store.register(INSTANCE_A)
    await store.register(INSTANCE_B)
    base = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB')], DAY_0)
    request_id = await store.request(IngestRequestKind.ASSET_LIST)
    recovered = runner_report(request_id, [asset('a', 'AAA'), asset('c', 'CCC')], DAY_0 + timedelta(days=2))
    await store.deliver(with_header(recovered, instance_id=INSTANCE_B, attempt_no=2))
    stale = runner_report(request_id, [asset('id-secret', SECRET_SYMBOL)], DAY_0 + timedelta(days=1), attempt_no=1)

    answer = await store.deliver(stale)

    assert answer.verdict is DeliveryVerdict.FINAL
    async with AsyncSession(store.engine) as db:
        stored = {
            row.attempt_no: row
            for row in (await db.execute(sa.select(SNAPSHOTS).where(SNAPSHOTS.c.request_id == request_id))).all()
        }
    assert (stored[1].row_count, stored[1].as_of) == (1, DAY_0 + timedelta(days=1))
    assert await _all(store, sa.select(EVENTS).where(EVENTS.c.snapshot_id == stored[1].id)) == []
    base_id = (await snapshot_of(store, base)).id
    assert sorted(
        (AssetListEventKind(e.kind), e.vendor_asset_id, e.snapshot_id, e.previous_snapshot_id)
        for e in await all_events(store)
    ) == [(APPEARED, 'c', stored[2].id, base_id), (VANISHED, 'b', stored[2].id, base_id)]
    _assert_one_late_warning(
        caplog, request_id, DAY_0 + timedelta(days=1), DAY_0 + timedelta(days=2), SECRET_SYMBOL, 'id-secret'
    )


async def test_ruled_a_late_snapshot_is_never_a_base_the_next_in_order_one_diffs_against_the_newest(store, caplog):
    """After a late snapshot, the next in-order snapshot diffs against the newest earlier non-empty one, not the late one."""
    caplog.set_level(logging.WARNING)
    newest = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB')], DAY_0 + timedelta(days=1))
    await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('l', 'LLL')], DAY_0)
    following = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=2))

    assert await events_of(store, following) == [
        (VANISHED, 'b', 'BBB', None, (await snapshot_of(store, newest)).id, DAY_0 + timedelta(days=1))
    ]
    assert len(_late_warnings(caplog)) == 1


async def test_ruled_the_late_rule_reads_only_its_own_list(store, caplog):
    """A newer snapshot of the OTHER list never makes a snapshot late: the in-order one diffs and nothing warns."""
    caplog.set_level(logging.WARNING)
    base = await deliver_list(store, ACTIVE, [asset('a', 'AAA'), asset('b', 'BBB')], DAY_0)
    await deliver_list(store, INACTIVE, [asset('i', 'III', False)], DAY_0 + timedelta(days=5))
    current = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=1))

    assert await events_of(store, current) == [(VANISHED, 'b', 'BBB', None, (await snapshot_of(store, base)).id, DAY_0)]
    assert _late_warnings(caplog) == []


async def test_ruled_the_first_snapshot_of_a_list_is_unchanged_no_base_no_event_no_late_warning(store, caplog):
    """The first non-empty snapshot has no base: no event and no late WARNING, even when an EMPTY one is newer."""
    caplog.set_level(logging.WARNING)
    await deliver_list(store, ACTIVE, [], DAY_0 + timedelta(days=3))
    first = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0)

    assert await events_of(store, first) == []
    assert await all_events(store) == []
    assert _warnings(caplog) == []


async def test_judgement_call_no_mass_vanish_threshold_a_partial_list_reports_every_missing_id(store):
    """No mass-vanish threshold was built (the 14:31 UTC note leaves it unruled until s3 sizes churn).

    A list that comes back with one of five ids reports four vanished, and nothing marks the delivery as suspect.
    """
    await deliver_list(store, ACTIVE, [asset(f'id-{n}', f'S{n}') for n in range(5)], DAY_0)
    partial = await deliver_list(store, ACTIVE, [asset('id-0', 'S0')], DAY_0 + timedelta(days=1))

    assert [(kind, vid) for kind, vid, *_ in await events_of(store, partial)] == [
        (VANISHED, f'id-{n}') for n in range(1, 5)
    ]


# --------------------------------------------------------------------------------------------- the reads and the lock


async def test_newest_snapshot_is_non_empty_only_by_default_and_include_empty_sees_the_empty_one(store):
    non_empty = await deliver_list(store, INACTIVE, [asset('a', 'AAA', False)], DAY_0)
    empty = await deliver_list(store, INACTIVE, [], DAY_0 + timedelta(days=1))
    await deliver_list(store, ACTIVE, [asset('b', 'BBB')], DAY_0 + timedelta(days=2))

    async with AsyncSession(store.engine) as db:
        default = await crud.newest_snapshot(db, DataSource.ALPACA_API, INACTIVE)
        with_empty = await crud.newest_snapshot(db, DataSource.ALPACA_API, INACTIVE, include_empty=True)
        other_source = await crud.newest_snapshot(db, DataSource.IB_API, INACTIVE)

    assert (default.request_id, default.row_count, default.asset_list) == (non_empty, 1, INACTIVE)
    assert (with_empty.request_id, with_empty.row_count) == (empty, 0)
    assert other_source is None


async def test_newest_snapshot_orders_by_the_snapshot_instant_not_by_arrival(store):
    newer = await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0 + timedelta(days=3))
    await deliver_list(store, ACTIVE, [asset('a', 'AAA')], DAY_0)

    async with AsyncSession(store.engine) as db:
        assert (await crud.newest_snapshot(db, DataSource.ALPACA_API, ACTIVE)).request_id == newer


async def test_newest_snapshot_is_none_for_a_list_with_only_an_empty_snapshot(store):
    await deliver_list(store, ACTIVE, [], DAY_0)

    async with AsyncSession(store.engine) as db:
        assert await crud.newest_snapshot(db, DataSource.ALPACA_API, ACTIVE) is None
        assert (await crud.newest_snapshot(db, DataSource.ALPACA_API, ACTIVE, include_empty=True)).row_count == 0


@pytest.mark.parametrize('status', list(AssetListStatus), ids=lambda status: status.value)
async def test_lock_list_takes_a_transaction_advisory_lock_keyed_by_source_and_list_on_postgres(status):
    """The statement and its key; that it SERIALISES two deliveries is tests/system/test_asset_list_pg.py's."""
    session = MagicMock()
    session.get_bind.return_value.dialect.name = 'postgresql'
    executed = []

    async def execute(statement, params=None):
        executed.append((str(statement), params))

    session.execute = execute

    await crud.lock_list(session, DataSource.ALPACA_API, status)

    assert executed == [
        ('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))', {'key': f'asset_list:ALPACA:{status.value}'})
    ]


async def test_the_arm_takes_the_list_lock_before_it_reads_the_base(store, monkeypatch):
    """The base read and the insert must not interleave with another delivery of the same list."""
    calls = []
    real_lock, real_newest = crud.lock_list, crud.newest_snapshot

    async def lock(db, source, status):
        calls.append(('lock', status))
        await real_lock(db, source, status)

    async def newest(db, source, status, **kwargs):
        calls.append(('base', status))
        return await real_newest(db, source, status, **kwargs)

    monkeypatch.setattr(crud, 'lock_list', lock)
    monkeypatch.setattr(crud, 'newest_snapshot', newest)

    await deliver_list(store, INACTIVE, [asset('a', 'AAA', False)], DAY_0)

    assert calls[0] == ('lock', INACTIVE)
    assert ('base', INACTIVE) in calls


def test_other_list_is_the_vendors_other_list():
    assert {status: world_snapshot.other_list(status) for status in AssetListStatus} == {
        ACTIVE: INACTIVE,
        INACTIVE: ACTIVE,
    }


async def test_the_arm_refuses_with_invalid_request_and_never_a_value(store, monkeypatch):
    """The refusal detail is a fixed sentence: no id, no symbol."""
    request_id = await store.request(IngestRequestKind.ASSET_LIST)
    real_lock = delivery_intake.lock_request

    async def lock_without(db, wanted):
        return replace(await real_lock(db, wanted), asset_list_status=None)

    monkeypatch.setattr(delivery_intake, 'lock_request', lock_without)

    refusal = await store.refused(runner_report(request_id, [asset('id-secret', SECRET_SYMBOL)], DAY_0))

    assert refusal.reason is Reason.INVALID_REQUEST
    assert (
        SECRET_SYMBOL not in refusal.detail
        and 'id-secret' not in refusal.detail
        and str(request_id) not in refusal.detail
    )
    assert isinstance(refusal, InvalidRequestError)
