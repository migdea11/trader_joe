"""The panel manager's passes on SQLite (validator, tj-7zjov1.180, P22v): app/panel_manager.py over the real crud.

THE DESIGN checked against: P22 tj-7zjov1.34, its CONSOLIDATED CREATION RULING of 2026-10-31 (option D: CREATED names
its bars request, never a dataset; per member, on the pass's session P: existing and in-flight checks, budget at two
requests per creation, commit P, the clamp on its OWN session, check_own_overlap on P with the clamped start, ONE commit
of ENTERED + CREATED + create_request, the submit after it without awaiting; DEFERRED clamp_unavailable; REFUSED
own_overlap and vendor_refused; re-creation; the late mint), the 13:45 UTC panel rulings (owner 'panel', shared owner,
one pass per due panel naming both versions), the 14:02 UTC as_of rule, the 14:16 UTC rate design (one 5 minute tick,
release, the windows), the 03:29 UTC consolidation (no source settings, no pass), the owner ruling of 2026-10-31 (the
manager requests no daily bars), P47v item 8 (no asset-list request), ADR tj-26jt9w addenda 20 and 21, and the pins on
tj-7zjov1.180 (its description (1) to (9), the 14:02, 14:16, 18:31 and 03:30 notes, and (4) and (10) to (16) of the
creation ruling).

  P1  one MEMBERSHIP pass per due panel, naming its settings version, the source's current settings version and both
      snapshots, finished with counts; ENTERED then CREATED per member in symbol order up to the size;
  P2  CREATED names its POST-shaped bars request (dataset_id NULL on both); every panel request carries client_label
      'panel' and ready_by NULL; trigger REQUEST; the declared start, end None, DAILY 1day, collect true;
  P3  the clamp runs on a session that is NOT the pass session, after P committed; its start is the bars request's and
      the own-overlap key's, and check_own_overlap runs on P;
  P4  ENTERED, CREATED and the request row commit together, before the submit, or not at all; a rollback is not retried
      within the pass and the next pass re-decides; a refused or failed submit leaves the rows;
  P5  DEFERRED clamp_unavailable, no bars request, and a repeat writes no row; REFUSED own_overlap, then nothing while
      the member stays in; a finished request with no dataset is re-created, a PERMANENT one REFUSED vendor_refused;
  P6  the windows: ramp_datasets_per_hour and request_cap_per_hour over the trailing hour, two requests per creation,
      shared per source, DEFERRED ramp_rate or request_rate, a repeat writes nothing, a restart creates nothing more;
  P7  RELEASE: names the deciding pass's settings version and snapshots and the CURRENT source settings version, writes
      only CREATED, and none runs under another settings version or a disabled panel; nothing to do writes no pass;
      the pass row opens lazily (tj-7zjov1.228): in the create_request commit, never before the clamp, reset by a
      rollback; a repeat clamp failure writes no pass row but still writes the clamp's own request;
  P8  the in-flight skip: an open panel BARS-without-dataset or CORPORATE_ACTIONS request for the symbol sends nothing;
  P9  exits: a filter exit EXITED, collect false only when no enabled panel on the source selects the symbol; a
      vanished member EXITED with no entry, DRAINING with one, DRAINED after a fetch, collect false; every manager stop
      (filter exit, DRAINED, the late mint) writes stopped_cause PANEL_MEMBER_LEFT (P22r), and an entry the vendor-id
      rule stopped is never requested or set collecting; the late mint stops collecting;
  P10 the as_of rule: an exit is dated by the first snapshot showing it under the same settings version, by this pass's
      snapshot under a new one; a row-attribute flip is dated from the rows; ENTERED likewise, an in-flight-skipped
      one included (16c), except a size-cut entrant, dated by this pass's snapshot (16a); CREATED keeps the pass's;
  P11 due and gate: one pass per due time, a restart before due runs none, MONTHLY waits for its month, an older
      snapshot is waited for until the next close plus settle; no source settings, a disabled panel: no pass;
  P12 dry run: the pass row flagged, nothing requested or stopped, and the real first pass still due after it;
  P13 crash points of the pass's own multi-write sequences (the 18:31 owner ruling); after a crash before finish_pass
      (tj-7zjov1.229) only FINISHED passes satisfy the due rule and give the as_of baseline, an unfinished one holds the
      panel off for UNFINISHED_PASS_RETRY (not its release), one WARNING per unfinished pass, id only;
  P14 the manager requests no asset list and no daily bars, and never names the awaiting or committing helpers;
  P15 re-entry restart (P22r tj-7zjov1.264, ADR tj-26jt9w addendum 23 rule A, owner ruling 2026-11-02; validator
      P22rv tj-7zjov1.265): in a MEMBERSHIP pass a member decided ENTERED whose symbol's panel entry is collect false
      AND stopped_cause PANEL_MEMBER_LEFT AND whose last accepted vendor asset id is empty or the member's is resumed
      (collect true, cause NULL) in the commit of its ENTERED row, which names the entry with cause dataset_resumed;
      RESUMED is published after that commit and never on a rollback; whatever the previous transition (none, EXITED,
      DRAINED), a first entry into a second panel included. NOT resumed: a different vendor id, an operator stop (cause
      NULL, also when it came before the manager's), a vendor stop (VENDOR_ASSET_CHANGED), an operator write racing
      the pass (plain ENTERED), a RELEASE pass, a dry run (the decision recorded, nothing changed or published). The
      guarded UPDATE itself is pinned directly on resume_panel_entry.

NOT HERE (system tier): the same statements on Postgres (asyncpg, ARRAY and JSONB, enum casts), database time on the
windows under real concurrency, the real first_request_clamp and submit_record against ingest.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from common.enums.data_stock import Granularity, UpdateType
from data.store.app import panel_manager
from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.crud.dataset_override import AuditUnavailable
from data.store.app.database.crud.panel_ledger import NewTransition
from data.store.app.database.crud.panel_manager_reads import latest_pass, resume_panel_entry, stop_collecting
from data.store.app.database.crud.stock.store_dataset_entry import OwnOverlapConflict
from data.store.app.database.models.ingest_request import (
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRunState, IngestRunTrigger
from data.store.app.database.models.panel import PanelCadence, PanelPassKind, PanelTransition
from data.store.app.database.models.store_dataset_entry import DatasetStopCause
from data.store.app.events import DatasetChange
from data.store.app.panel_manager import (
    CAUSE_CLAMP_UNAVAILABLE,
    CAUSE_DATASET_RESUMED,
    CAUSE_OWN_OVERLAP,
    CAUSE_RAMP_RATE,
    CAUSE_REQUEST_RATE,
    CAUSE_VENDOR_REFUSED,
    UNFINISHED_PASS_RETRY,
    membership_due_at,
    panel_dataset_start,
)
from data.store.tests.panel_manager_harness import (
    ENTRIES,
    PASSES,
    SETTLE,
    SNAPSHOTS,
    Clock,
    EveryDayCalendar,
    kinds,
    make_world,
    manager,
)
from schemas.data_ingest.submit import AssetListStatus


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

ENTERED, CREATED, DEFERRED, REFUSED = (
    PanelTransition.ENTERED,
    PanelTransition.CREATED,
    PanelTransition.DEFERRED,
    PanelTransition.REFUSED,
)
EXITED, DRAINING, DRAINED = PanelTransition.EXITED, PanelTransition.DRAINING, PanelTransition.DRAINED
BARS, CORPORATE = IngestRequestKind.BARS, IngestRequestKind.CORPORATE_ACTIONS
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
# The stop marks, as the strings the column stores (tj-7zjov1.264).
LEFT = 'PANEL_MEMBER_LEFT'
VENDOR_STOP = 'VENDOR_ASSET_CHANGED'
# The declared start of an ALPACA_API panel dataset, by hand (tj-7zjov1.239): 1990-01-01 at New York midnight (EST).
DECLARED_START = datetime(1990, 1, 1, 5, tzinfo=UTC)


@pytest_asyncio.fixture
async def world(monkeypatch):
    built = await make_world(monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


@pytest.fixture
def clock():
    return Clock()


def now() -> datetime:
    return datetime.now(UTC)


# The vendor ids the scenarios use and the symbols the snapshots list them under.
SYMBOLS = {'v1': 'AAA', 'v2': 'BBB', 'v3': 'CCC', 'v4': 'DDD', 'v9': 'ZZZ', 'vx': 'XXX'}


def t(vendor: str, kind: PanelTransition, as_of: datetime, **kw) -> NewTransition:
    return NewTransition(vendor, kw.pop('symbol', SYMBOLS.get(vendor, vendor.upper())), kind, as_of, **kw)


async def due_again(world, seeded, by: timedelta = 2 * DAY) -> None:
    """Move the panel's passes back so the next membership pass is due (the snapshots stay where they are)."""
    for row in await world.passes(seeded):
        await world.put(PASSES.update().where(PASSES.c.id == row.id).values(started_at=row.started_at - by))


async def basic(world, *, size: int = 10, ramp: int = 10, cap: int = 100, assets=None, **settings):
    """One source settings version, one panel created two days ago, and one active snapshot an hour ago."""
    source_settings_id = await world.source_settings(ramp=ramp, cap=cap)
    seeded = await world.panel(now() - 2 * DAY, size=size, **settings)
    assets = assets if assets is not None else [('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC')]
    snapshot_id = await world.snapshot(now() - HOUR, assets)
    return seeded, source_settings_id, snapshot_id


async def snapshot_as_of(world, snapshot_id):
    (row,) = await world.rows(SNAPSHOTS, SNAPSHOTS.c.id == snapshot_id)
    return row.as_of


# ===================================================================================== P1 / P2 the first pass


async def test_the_first_pass_names_both_versions_and_snapshots_and_creates_in_symbol_order_to_size(world, clock):
    source_settings_id = await world.source_settings()
    seeded = await world.panel(now() - 2 * DAY, size=2)
    inactive = await world.snapshot(now() - 3 * HOUR, [('x1', 'OLD')], status=AssetListStatus.INACTIVE)
    active = await world.snapshot(now() - HOUR, [('v3', 'CCC'), ('v1', 'AAA'), ('v2', 'BBB')])

    await manager(clock).tick()

    (row,) = await world.passes(seeded)
    assert row.kind is PanelPassKind.MEMBERSHIP and row.dry_run is False
    assert (row.settings_version_id, row.source_settings_version_id) == (seeded.settings_id, source_settings_id)
    assert (row.active_snapshot_id, row.inactive_snapshot_id) == (active, inactive)
    assert row.finished_at is not None
    assert row.counts['entered'] == 2 and row.counts['created'] == 2 and row.counts['deferred'] == 0
    assert kinds(await world.transitions(seeded)) == [
        ('v1', 'ENTERED'),
        ('v1', 'CREATED'),
        ('v2', 'ENTERED'),
        ('v2', 'CREATED'),
    ]


async def test_created_names_its_post_shaped_bars_request_owned_by_panel(world, clock):
    seeded, _, snapshot_id = await basic(world)
    as_of = await snapshot_as_of(world, snapshot_id)

    await manager(clock).tick()

    created = [row for row in await world.transitions(seeded) if row.transition is CREATED]
    bars = {row.id: row for row in await world.requests(BARS)}
    assert len(created) == 3 and len(bars) == 3
    for row in created:
        assert row.dataset_id is None, 'CREATED never names a dataset (ruling D)'
        assert row.as_of == as_of
        request = bars[row.request_id]
        assert request.dataset_id is None, 'the bars request is POST-shaped'
        assert request.asset_symbol == row.symbol
        assert request.client_label == PANEL_OWNER == 'panel'
        assert request.ready_by is None
        assert request.trigger is IngestRunTrigger.REQUEST
        assert request.requested_start == DECLARED_START == panel_dataset_start(request.source)
        assert request.requested_end is None
        assert (request.granularity, request.update_type) == (Granularity.ONE_DAY, UpdateType.DAILY.value)
        assert request.collect is True
    for request in await world.requests():
        assert request.client_label == 'panel' and request.ready_by is None
    assert [call.request_id for call in world.submits] == [row.request_id for row in created]


async def test_each_creation_writes_two_panel_requests_the_clamp_and_the_bars(world, clock):
    await basic(world)
    await manager(clock).tick()
    corporate = await world.requests(CORPORATE)
    bars = await world.requests(BARS)
    assert sorted(r.asset_symbol for r in corporate) == sorted(r.asset_symbol for r in bars) == ['AAA', 'BBB', 'CCC']
    for call in world.clamps:
        assert call.body.owner == PANEL_OWNER and call.body.ready_by is None and call.body.end is None
        assert call.body.start == DECLARED_START and call.body.collect is True


# ===================================================================================== P3 the clamp


async def test_the_clamp_runs_on_its_own_session_after_the_pass_session_committed(world, clock):
    await basic(world)
    await manager(clock).tick()
    (pass_session,) = set(world.pass_sessions)
    assert len(world.clamps) == 3
    for call in world.clamps:
        assert call.session is not pass_session
        assert call.pass_session_in_transaction is False, 'P carried an open transaction across the clamp'


async def test_the_clamped_start_is_the_bars_request_start_and_the_overlap_key_checked_on_p(world, clock, monkeypatch):
    await basic(world, assets=[('v1', 'AAA')])
    clamped = datetime(2022, 6, 9, 4, tzinfo=UTC)
    world.clamp_starts['AAA'] = clamped
    checked = []
    real = panel_manager.check_own_overlap

    async def recording(db, key):
        checked.append((db, key))
        return await real(db, key)

    monkeypatch.setattr(panel_manager, 'check_own_overlap', recording)

    await manager(clock).tick()

    ((db, key),) = checked
    assert db is world.pass_sessions[-1] and db is not world.clamps[0].session
    assert key.start == clamped and key.end is None and key.owner == PANEL_OWNER
    (bars,) = await world.requests(BARS)
    assert bars.requested_start == clamped


# ===================================================================================== P4 one commit


async def test_the_request_row_and_its_created_row_are_committed_before_the_submit(world, clock):
    await basic(world)
    await manager(clock).tick()
    assert len(world.submits) == 3
    for call in world.submits:
        assert call.kind is BARS
        assert call.request_row_visible and call.created_row_visible


async def test_a_failed_request_write_rolls_back_the_members_entered_and_created_and_is_not_retried(
    world, clock, monkeypatch
):
    seeded, _, _ = await basic(world)
    real = panel_manager.create_request
    calls = []

    async def failing_for_bbb(db, request):
        calls.append(request.asset_symbol)
        if request.asset_symbol == 'BBB':
            raise RuntimeError('the insert failed')
        return await real(db, request)

    monkeypatch.setattr(panel_manager, 'create_request', failing_for_bbb)

    await manager(clock).tick()

    assert calls == ['AAA', 'BBB', 'CCC'], 'BBB was tried once in the pass'
    assert kinds(await world.transitions(seeded)) == [
        ('v1', 'ENTERED'),
        ('v1', 'CREATED'),
        ('v3', 'ENTERED'),
        ('v3', 'CREATED'),
    ]
    assert sorted(r.asset_symbol for r in await world.requests(BARS)) == ['AAA', 'CCC']
    (row,) = await world.passes(seeded)
    assert row.finished_at is not None and row.counts['created'] == 2 and row.counts['entered'] == 2

    # The next pass re-decides BBB (once its clamp request has finished: the in-flight check waits for it).
    monkeypatch.setattr(panel_manager, 'create_request', real)
    for request in await world.requests(CORPORATE):
        await world.set_request(request.id, state=IngestRequestState.SUCCEEDED, finished_at=now())
    await due_again(world, seeded)
    await manager(clock).tick()
    latest = (await world.transitions(seeded))[-2:]
    assert kinds(latest) == [('v2', 'ENTERED'), ('v2', 'CREATED')]


async def test_a_failed_submit_leaves_the_committed_rows(world, clock):
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA')])
    world.submit_error = AuditUnavailable()
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED'), ('v1', 'CREATED')]
    (bars,) = await world.requests(BARS)
    assert bars.state is IngestRequestState.PENDING


# ===================================================================================== P5 deferrals and refusals


async def test_a_failing_clamp_defers_clamp_unavailable_sends_no_bars_and_a_repeat_writes_nothing(world, clock):
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA')])
    world.clamp_errors['AAA'] = AuditUnavailable()

    await manager(clock).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows) == [('v1', 'ENTERED'), ('v1', 'DEFERRED')]
    assert rows[-1].cause == CAUSE_CLAMP_UNAVAILABLE
    assert await world.requests(BARS) == []
    assert world.submits == []

    # Again, after the clamp request finished and the window emptied: a release tick, the same failure, no new row.
    for request in await world.requests(CORPORATE):
        await world.set_request(request.id, state=IngestRequestState.FAILED, finished_at=now())
    await world.age_window()
    await manager(clock).tick()
    assert len(world.clamps) == 2, 'the release tried again'
    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED'), ('v1', 'DEFERRED')]
    assert await world.requests(BARS) == []


async def test_own_overlap_refuses_the_member_and_it_stays_refused(world, clock, monkeypatch):
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA')])

    async def overlapping(db, key):
        raise OwnOverlapConflict([uuid.uuid4()])

    monkeypatch.setattr(panel_manager, 'check_own_overlap', overlapping)
    await manager(clock).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows) == [('v1', 'ENTERED'), ('v1', 'REFUSED')]
    assert rows[-1].cause == CAUSE_OWN_OVERLAP
    assert await world.requests(BARS) == []

    for request in await world.requests(CORPORATE):
        await world.set_request(request.id, state=IngestRequestState.SUCCEEDED, finished_at=now())
    await due_again(world, seeded)
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 2
    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED'), ('v1', 'REFUSED')]
    assert len(world.clamps) == 1


async def test_pin_16b_a_clamp_window_refusal_writes_refused_clamp_window_and_no_bars_request(
    world, clock, monkeypatch
):
    """Unreachable while panel datasets are open-ended (end None): the pure rule is forced to answer, to pin its wiring."""
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    world.clamp_starts['AAA'] = datetime(2022, 6, 9, 4, tzinfo=UTC)
    seen = []
    checked = []

    def refusing(clamp, end):
        seen.append((clamp, end))
        return panel_manager.CAUSE_CLAMP_WINDOW if clamp is not None else None

    real_overlap = panel_manager.check_own_overlap

    async def recording_overlap(db, key):
        checked.append(key.asset_symbol)
        return await real_overlap(db, key)

    monkeypatch.setattr(panel_manager, 'clamp_refusal', refusing)
    monkeypatch.setattr(panel_manager, 'check_own_overlap', recording_overlap)

    await manager(clock).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows) == [('v1', 'ENTERED'), ('v1', 'REFUSED'), ('v2', 'ENTERED'), ('v2', 'CREATED')]
    assert rows[1].cause == panel_manager.CAUSE_CLAMP_WINDOW
    assert [r.asset_symbol for r in await world.requests(BARS)] == ['BBB']
    assert checked == ['BBB'], 'the window refusal comes before the own-overlap check'
    assert [end for _, end in seen] == [None, None], "the body's end is what the rule judges"

    # REFUSED stays refused while the member stays in.
    for request in await world.requests(CORPORATE):
        await world.set_request(request.id, state=IngestRequestState.SUCCEEDED, finished_at=now())
    await due_again(world, seeded)
    await manager(clock).tick()
    assert len(await world.transitions(seeded)) == 4


async def _created_earlier(world, seeded, snapshot_id, state, failure_cause=None, dataset_id=None):
    request_id = await world.request(
        'AAA', state=state, failure_cause=failure_cause, dataset_id=dataset_id, finished_at=now() - DAY
    )
    as_of = await snapshot_as_of(world, snapshot_id)
    await world.ledger_pass(
        seeded,
        snapshot_id,
        [t('v1', ENTERED, as_of, symbol='AAA'), t('v1', CREATED, as_of, symbol='AAA', request_id=request_id)],
        started_at=now() - 2 * DAY,
    )
    await world.age_window()
    return request_id


@pytest.mark.parametrize(
    'cause', [IngestRequestFailureCause.EXPIRED.value, IngestRequestFailureCause.ATTEMPTS_EXHAUSTED.value]
)
async def test_a_finished_request_with_no_dataset_is_created_again(world, clock, cause):
    seeded, _, snapshot_id = await basic(world, assets=[('v1', 'AAA')])
    first = await _created_earlier(world, seeded, snapshot_id, IngestRequestState.FAILED, cause)

    await manager(clock).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows) == [('v1', 'ENTERED'), ('v1', 'CREATED'), ('v1', 'CREATED')]
    assert rows[-1].request_id != first
    assert len(await world.requests(BARS)) == 2


async def test_a_permanent_failure_is_refused_vendor_refused_and_not_retried(world, clock):
    seeded, _, snapshot_id = await basic(world, assets=[('v1', 'AAA')])
    await _created_earlier(
        world, seeded, snapshot_id, IngestRequestState.FAILED, IngestRequestFailureCause.PERMANENT.value
    )

    await manager(clock).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows)[-1] == ('v1', 'REFUSED') and rows[-1].cause == CAUSE_VENDOR_REFUSED
    assert len(await world.requests(BARS)) == 1 and world.clamps == []

    await due_again(world, seeded)
    await manager(clock).tick()
    assert len(await world.transitions(seeded)) == len(rows)


async def test_a_created_member_whose_request_is_open_or_minted_gets_nothing(world, clock):
    seeded, _, snapshot_id = await basic(world, assets=[('v1', 'AAA')])
    await _created_earlier(world, seeded, snapshot_id, IngestRequestState.QUEUED)
    await manager(clock).tick()
    assert len(await world.transitions(seeded)) == 2 and world.clamps == []


# ===================================================================================== P6 the windows


async def test_the_ramp_window_defers_the_rest_ramp_rate_and_a_restart_creates_nothing_more(world, clock):
    seeded, _, _ = await basic(world, ramp=2, assets=[('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC'), ('v4', 'DDD')])

    await manager(clock).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows) == [
        ('v1', 'ENTERED'),
        ('v1', 'CREATED'),
        ('v2', 'ENTERED'),
        ('v2', 'CREATED'),
        ('v3', 'ENTERED'),
        ('v3', 'DEFERRED'),
        ('v4', 'ENTERED'),
        ('v4', 'DEFERRED'),
    ]
    assert {row.cause for row in rows if row.transition is DEFERRED} == {CAUSE_RAMP_RATE}

    # (6b) a fresh manager at the same clock: the window is read from rows, not memory.
    await manager(clock).tick()
    assert len(await world.transitions(seeded)) == 8
    assert len(await world.passes(seeded)) == 1
    assert len(await world.requests(BARS)) == 2


async def test_a_repeat_deferral_with_the_same_cause_writes_no_row(world, clock):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()
    before = await world.transitions(seeded)
    assert kinds(before)[-1] == ('v2', 'DEFERRED')

    await due_again(world, seeded)
    await manager(clock).tick()

    assert len(await world.passes(seeded)) == 2
    assert len(await world.transitions(seeded)) == len(before), 'the second deferral repeated a row'


async def test_the_request_window_counts_two_requests_per_creation(world, clock):
    seeded, _, _ = await basic(world, cap=5, assets=[('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC')])

    await manager(clock).tick()

    rows = await world.transitions(seeded)
    assert kinds(rows) == [
        ('v1', 'ENTERED'),
        ('v1', 'CREATED'),
        ('v2', 'ENTERED'),
        ('v2', 'CREATED'),
        ('v3', 'ENTERED'),
        ('v3', 'DEFERRED'),
    ]
    assert rows[-1].cause == CAUSE_REQUEST_RATE
    assert len(await world.requests()) == 4


async def test_requests_of_another_label_do_not_count(world, clock):
    seeded, _, _ = await basic(world, cap=2, assets=[('v1', 'AAA')])
    for _ in range(5):
        await world.request('ZZZ', client_label='scheduler', dataset_id=uuid.uuid4())
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'CREATED')


async def test_the_windows_are_shared_by_the_panels_of_a_source_in_creation_order(world, clock):
    await world.source_settings(ramp=2)
    first = await world.panel(now() - 3 * DAY, allow_symbols=['AAA', 'BBB'])
    second = await world.panel(now() - 2 * DAY, allow_symbols=['CCC', 'DDD'])
    await world.snapshot(now() - HOUR, [('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC'), ('v4', 'DDD')])

    await manager(clock).tick()

    assert [k for k in kinds(await world.transitions(first)) if k[1] != 'ENTERED'] == [
        ('v1', 'CREATED'),
        ('v2', 'CREATED'),
    ]
    later = await world.transitions(second)
    assert [k for k in kinds(later) if k[1] != 'ENTERED'] == [('v3', 'DEFERRED'), ('v4', 'DEFERRED')]
    assert {row.cause for row in later if row.transition is DEFERRED} == {CAUSE_RAMP_RATE}


async def test_creations_already_in_the_window_leave_ramp_minus_n(world, clock):
    seeded, _, snapshot_id = await basic(world, ramp=2, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    other = await world.panel(now() - 3 * DAY, name='other')
    as_of = await snapshot_as_of(world, snapshot_id)
    request_id = await world.request('ZZZ')
    await world.ledger_pass(
        other, snapshot_id, [t('z', ENTERED, as_of), t('z', CREATED, as_of, request_id=request_id)], started_at=now()
    )
    await world.settings(other.panel_id, enabled=False)

    await manager(clock).tick()

    assert [k for k in kinds(await world.transitions(seeded)) if k[1] != 'ENTERED'] == [
        ('v1', 'CREATED'),
        ('v2', 'DEFERRED'),
    ]


# ===================================================================================== P7 release


async def test_a_release_names_the_deciding_pass_and_the_current_source_settings_and_writes_only_created(world, clock):
    seeded, _, snapshot_id = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC')])
    await manager(clock).tick()
    (deciding,) = await world.passes(seeded)
    newer_rates = await world.source_settings(ramp=1)
    await world.age_window()

    await manager(clock).tick()

    passes = await world.passes(seeded)
    # SQLite's CURRENT_TIMESTAMP is whole seconds: the two passes may share started_at, so they are told by kind.
    assert sorted(row.kind.value for row in passes) == ['MEMBERSHIP', 'RELEASE']
    (release,) = [row for row in passes if row.kind is PanelPassKind.RELEASE]
    assert release.settings_version_id == deciding.settings_version_id
    assert (release.active_snapshot_id, release.inactive_snapshot_id) == (
        deciding.active_snapshot_id,
        deciding.inactive_snapshot_id,
    )
    assert release.source_settings_version_id == newer_rates
    released = [row for row in await world.transitions(seeded) if row.pass_id == release.id]
    assert kinds(released) == [('v2', 'CREATED')], 'a release decides no membership and keeps the ramp'
    assert released[0].as_of == await snapshot_as_of(world, snapshot_id)
    assert release.finished_at is not None and release.counts['created'] == 1


async def test_no_release_under_another_settings_version(world, clock):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()
    await world.settings(seeded.panel_id, size=10)
    await world.age_window()
    await manager(clock).tick()
    assert [row.kind for row in await world.passes(seeded)] == [PanelPassKind.MEMBERSHIP]


async def test_a_disabled_panel_runs_no_pass_of_either_kind(world, clock):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()
    await world.settings(seeded.panel_id, enabled=False)
    await world.age_window()
    await due_again(world, seeded)
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 1


async def test_a_release_with_a_full_window_or_nothing_to_create_writes_no_pass(world, clock):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()
    await manager(clock).tick()  # the window is still full
    assert len(await world.passes(seeded)) == 1
    await world.request('BBB', kind=CORPORATE)  # in flight: nothing to create
    await world.age_window()
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 1


async def _close_corporate_requests(world) -> None:
    """Finish every clamp request, so its symbol is no longer in flight, and age the windows."""
    for request in await world.requests(CORPORATE):
        await world.set_request(request.id, state=IngestRequestState.FAILED, finished_at=now())
    await world.age_window()


async def test_a_release_whose_clamp_fails_again_writes_no_pass_then_a_later_one_writes_one_with_created(world, clock):
    """tj-7zjov1.228: a RELEASE pass row opens lazily, in the create_request commit; nothing to append, no row."""
    seeded, _, _ = await basic(world, ramp=2, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    world.clamp_errors['BBB'] = AuditUnavailable()
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded))[-1] == ('v2', 'DEFERRED')
    before = await world.transitions(seeded)
    await _close_corporate_requests(world)
    clamps_before = len(await world.requests(CORPORATE))

    await manager(clock).tick()  # release: the clamp fails again, a repeat deferral

    assert [row.kind for row in await world.passes(seeded)] == [PanelPassKind.MEMBERSHIP], 'no empty RELEASE row'
    assert len(await world.transitions(seeded)) == len(before)
    assert len(await world.requests(CORPORATE)) == clamps_before + 1, "the clamp's own request is still written"

    del world.clamp_errors['BBB']
    await _close_corporate_requests(world)
    await manager(clock).tick()

    passes = await world.passes(seeded)
    (release,) = [row for row in passes if row.kind is PanelPassKind.RELEASE]
    assert len(passes) == 2 and release.finished_at is not None and release.counts['created'] == 1
    released = [row for row in await world.transitions(seeded) if row.pass_id == release.id]
    assert kinds(released) == [('v2', 'CREATED')]
    assert world.clamps[-1].symbol == 'BBB' and world.clamps[-1].passes_visible == 1, (
        'the RELEASE pass row does not exist yet at the clamp'
    )
    submit = world.submits[-1]
    assert submit.request_id == released[0].request_id
    assert submit.request_row_visible and submit.created_row_visible and submit.pass_row_visible


async def test_a_release_creation_that_rolls_back_leaves_no_pass_row_and_the_next_member_opens_its_own(
    world, clock, monkeypatch
):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC')])
    await manager(clock).tick()  # v1 CREATED; v2 and v3 DEFERRED ramp_rate
    await world.age_window()
    real = panel_manager.create_request

    async def failing_for_bbb(db, request):
        if request.asset_symbol == 'BBB':
            raise RuntimeError('write failed')
        return await real(db, request)

    monkeypatch.setattr(panel_manager, 'create_request', failing_for_bbb)
    await manager(clock).tick()  # release: BBB rolls back, then CCC is created

    passes = {row.id: row for row in await world.passes(seeded)}
    (release,) = [row for row in passes.values() if row.kind is PanelPassKind.RELEASE]
    assert release.finished_at is not None and release.counts['created'] == 1
    rows = await world.transitions(seeded)
    assert all(row.pass_id in passes for row in rows), 'a transition names a pass row that was rolled back'
    assert kinds([row for row in rows if row.pass_id == release.id]) == [('v3', 'CREATED')]
    assert 'BBB' not in {row.asset_symbol for row in await world.requests(BARS)}


async def test_a_release_that_only_defers_opens_its_pass_and_commits_it_at_the_finish(world, clock):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()  # v2 DEFERRED ramp_rate
    world.clamp_errors['BBB'] = AuditUnavailable()
    await world.age_window()

    await manager(clock).tick()  # release: the clamp fails, a NEW cause, so a DEFERRED row

    (release,) = [row for row in await world.passes(seeded) if row.kind is PanelPassKind.RELEASE]
    assert release.finished_at is not None and release.counts['deferred'] == 1
    released = [row for row in await world.transitions(seeded) if row.pass_id == release.id]
    assert [(row.vendor_asset_id, row.transition, row.cause) for row in released] == [
        ('v2', DEFERRED, CAUSE_CLAMP_UNAVAILABLE)
    ]
    assert world.clamps[-1].passes_visible == 1, 'not opened before the clamp'


async def test_no_release_runs_while_the_window_is_full_whatever_the_member_was_deferred_for(world, clock):
    seeded, _, snapshot_id = await basic(world, ramp=2, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    world.clamp_errors['BBB'] = AuditUnavailable()
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded))[-1] == ('v2', 'DEFERRED')
    for request in await world.requests(CORPORATE):
        await world.set_request(request.id, state=IngestRequestState.FAILED, finished_at=now())
    # Another panel of the source fills the ramp: two CREATED rows in the trailing hour.
    other = await world.panel(now() - 3 * DAY, name='other', enabled=False)
    as_of = await snapshot_as_of(world, snapshot_id)
    await world.ledger_pass(
        other, snapshot_id, [t('z', CREATED, as_of, request_id=await world.request('ZZZ'))], started_at=now()
    )

    await manager(clock).tick()

    assert [row.kind for row in await world.passes(seeded)] == [PanelPassKind.MEMBERSHIP]
    assert len(world.clamps) == 2


async def test_a_dry_run_manager_runs_no_release(world, clock):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()
    await world.age_window()
    await manager(clock, dry_run=True).tick()
    assert [row.kind for row in await world.passes(seeded) if not row.dry_run] == [PanelPassKind.MEMBERSHIP]
    assert not [row for row in await world.passes(seeded) if row.kind is PanelPassKind.RELEASE]


# ===================================================================================== P8 in flight


@pytest.mark.parametrize(
    'open_request',
    [
        {'kind': CORPORATE},
        {'kind': BARS, 'dataset_id': None},
        {'kind': BARS, 'state': IngestRequestState.PENDING},
        {'kind': BARS, 'state': IngestRequestState.NOT_DELIVERED},
    ],
    ids=['corporate-actions', 'bars-no-dataset', 'pending', 'not-delivered'],
)
async def test_an_open_panel_request_for_the_symbol_sends_nothing_and_writes_no_row(world, clock, open_request):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()  # v2 DEFERRED
    before = await world.transitions(seeded)
    await world.request('BBB', **open_request)
    await world.age_window()
    requests_before = len(await world.requests())

    await manager(clock).tick()  # release
    await due_again(world, seeded)
    await manager(clock).tick()  # membership

    assert len(await world.transitions(seeded)) == len(before)
    assert len(await world.requests()) == requests_before
    assert [call.symbol for call in world.clamps] == ['AAA']


@pytest.mark.parametrize(
    'other',
    [
        {'kind': BARS, 'dataset_id': uuid.uuid4()},
        {'kind': CORPORATE, 'client_label': 'someone'},
        {'kind': CORPORATE, 'state': IngestRequestState.SUCCEEDED},
    ],
    ids=['a-refresh', 'another-label', 'finished'],
)
async def test_requests_that_are_not_in_flight_creations_do_not_block(world, clock, other):
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA')])
    await world.request('AAA', **other)
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'CREATED')


async def test_two_panels_selecting_one_symbol_make_one_dataset_request(world, clock):
    await world.source_settings()
    first = await world.panel(now() - 3 * DAY, allow_symbols=['XXX'])
    second = await world.panel(now() - 2 * DAY, allow_symbols=['XXX'])
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])

    await manager(clock).tick()

    assert len(await world.requests(BARS)) == 1 and len(await world.requests(CORPORATE)) == 1
    assert kinds(await world.transitions(first)) == [('vx', 'ENTERED'), ('vx', 'CREATED')]
    assert CREATED not in {row.transition for row in await world.transitions(second)}


# ===================================================================================== P9 exits, drain, collect


async def _member_with_entry(world, seeded, snapshot_id, vendor='v1', symbol='AAA', **entry):
    entry_id = await world.entry(symbol, **entry)
    request_id = await world.request(
        symbol, state=IngestRequestState.SUCCEEDED, dataset_id=entry_id, finished_at=now() - 3 * DAY
    )
    as_of = await snapshot_as_of(world, snapshot_id)
    rows = [t(vendor, ENTERED, as_of, symbol=symbol), t(vendor, CREATED, as_of, symbol=symbol, request_id=request_id)]
    return entry_id, rows


async def test_shared_owner_a_filter_exit_stops_collecting_only_when_no_enabled_panel_selects_the_symbol(world, clock):
    await world.source_settings()
    first = await world.panel(now() - 3 * DAY)
    second = await world.panel(now() - 2 * DAY)
    old = await world.snapshot(now() - 3 * DAY, [('vx', 'XXX')])
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])
    entry_id, rows = await _member_with_entry(world, first, old, 'vx', 'XXX')
    for seeded in (first, second):
        await world.ledger_pass(seeded, old, rows, started_at=now() - 3 * DAY)

    await world.settings(first.panel_id, deny_symbols=['XXX'])
    await world.age_window()
    await manager(clock).tick()

    assert kinds(await world.transitions(first))[-1] == ('vx', 'EXITED')
    assert (await world.entry_row(entry_id)).collect is True, 'the second panel still selects it'
    assert not world.published

    await world.settings(second.panel_id, deny_symbols=['XXX'])
    await due_again(world, second)
    await manager(clock).tick()

    assert kinds(await world.transitions(second))[-1] == ('vx', 'EXITED')
    entry = await world.entry_row(entry_id)
    # A manager stop is marked PANEL_MEMBER_LEFT in the same UPDATE (P22r, addendum 23 rule A (1)).
    assert (entry.collect, entry.stopped_cause) == (False, LEFT)
    assert [(e.id, e.change) for e in world.published] == [(entry_id, DatasetChange.RETIRED)]


async def test_a_disabled_panel_does_not_hold_a_symbol(world, clock):
    await world.source_settings()
    first = await world.panel(now() - 3 * DAY)
    second = await world.panel(now() - 2 * DAY, enabled=False)
    old = await world.snapshot(now() - 3 * DAY, [('vx', 'XXX')])
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])
    entry_id, rows = await _member_with_entry(world, first, old, 'vx', 'XXX')
    await world.ledger_pass(first, old, rows, started_at=now() - 3 * DAY)
    assert second
    await world.settings(first.panel_id, deny_symbols=['XXX'])
    await world.age_window()

    await manager(clock).tick()

    assert (await world.entry_row(entry_id)).collect is False


async def test_a_vanished_member_with_no_entry_exits(world, clock):
    seeded, _, _ = await basic(world, assets=[('v9', 'ZZZ')])
    old = await world.snapshot(now() - 3 * DAY, [('v1', 'AAA'), ('v9', 'ZZZ')])
    as_of = await snapshot_as_of(world, old)
    request_id = await world.request('AAA', state=IngestRequestState.QUEUED)
    keep = await world.request('ZZZ', state=IngestRequestState.QUEUED)
    await world.ledger_pass(
        seeded,
        old,
        [
            t('v1', ENTERED, as_of),
            t('v1', CREATED, as_of, request_id=request_id),
            t('v9', ENTERED, as_of, symbol='ZZZ'),
            t('v9', CREATED, as_of, symbol='ZZZ', request_id=keep),
        ],
        started_at=now() - 3 * DAY,
    )
    await world.age_window()

    await manager(clock).tick()

    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'EXITED')


async def test_a_vanished_member_with_an_entry_drains_then_is_drained_after_a_fetch(world, clock):
    seeded, _, newest = await basic(world, assets=[('v9', 'ZZZ')])
    old = await world.snapshot(now() - 3 * DAY, [('v1', 'AAA'), ('v9', 'ZZZ')])
    entry_id, rows = await _member_with_entry(world, seeded, old)
    _, keep_rows = await _member_with_entry(world, seeded, old, 'v9', 'ZZZ')
    await world.ledger_pass(seeded, old, [*rows, *keep_rows], started_at=now() - 3 * DAY)
    await world.age_window()

    await manager(clock).tick()
    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'DRAINING')
    assert (await world.entry_row(entry_id)).collect is True

    await due_again(world, seeded)
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'DRAINING'), 'no fetch since: still draining'

    await world.request('AAA', state=IngestRequestState.SUCCEEDED, dataset_id=entry_id, finished_at=now())
    await due_again(world, seeded)
    await manager(clock).tick()

    last = (await world.transitions(seeded))[-1]
    assert (last.vendor_asset_id, last.transition) == ('v1', DRAINED)
    assert last.as_of == await snapshot_as_of(world, newest)
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, LEFT)
    assert [(e.id, e.change) for e in world.published] == [(entry_id, DatasetChange.RETIRED)]


async def test_an_entry_the_store_stopped_is_never_requested_or_set_collecting(world, clock):
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA')])
    entry_id = await world.entry('AAA', collect=False, stopped_cause='VENDOR_ASSET_CHANGED')

    await manager(clock).tick()

    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED')]
    assert await world.requests() == [] and world.clamps == []
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, 'VENDOR_ASSET_CHANGED')


async def test_an_existing_panel_entry_whatever_its_start_is_not_created_again(world, clock):
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA')])
    await world.entry('AAA', start=datetime(2022, 6, 9, 4, tzinfo=UTC))
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED')]
    assert await world.requests() == []


@pytest.mark.parametrize(
    'other',
    [
        {'owner': 'someone-else'},
        {'granularity': Granularity.ONE_HOUR},
        {'update_type': UpdateType.STATIC, 'collect': None},
    ],
    ids=['another-owner', 'another-granularity', 'static'],
)
async def test_only_a_panel_owned_daily_1day_entry_counts_as_existing(world, clock, other):
    seeded, _, _ = await basic(world, assets=[('v1', 'AAA')])
    await world.entry('AAA', **other)
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED'), ('v1', 'CREATED')]


async def _exited_with_late_mint(world, seeded, snapshot_id, finished_at):
    entry_id = await world.entry('AAA')
    request_id = await world.request('AAA', state=IngestRequestState.QUEUED)
    as_of = await snapshot_as_of(world, snapshot_id)
    await world.ledger_pass(
        seeded,
        snapshot_id,
        [t('v1', ENTERED, as_of), t('v1', CREATED, as_of, request_id=request_id)],
        started_at=now() - 4 * DAY,
    )
    await world.ledger_pass(seeded, snapshot_id, [t('v1', EXITED, as_of)], started_at=now() - 3 * DAY)
    await world.set_request(
        request_id, state=IngestRequestState.SUCCEEDED, dataset_id=entry_id, finished_at=finished_at
    )
    await world.age_window()
    return entry_id


async def test_an_entry_minted_after_its_member_exited_stops_collecting_with_no_ledger_row(world, clock):
    seeded, _, snapshot_id = await basic(world, deny_symbols=['AAA'], assets=[('v1', 'AAA')])
    entry_id = await _exited_with_late_mint(world, seeded, snapshot_id, now() + timedelta(minutes=5))
    before = len(await world.transitions(seeded))

    await manager(clock).tick()

    entry = await world.entry_row(entry_id)
    # (u10) the late-mint stop is a manager stop too, so it carries the mark.
    assert (entry.collect, entry.stopped_cause) == (False, LEFT)
    assert len(await world.transitions(seeded)) == before
    assert [(e.id, e.change) for e in world.published] == [(entry_id, DatasetChange.RETIRED)]


async def test_an_entry_minted_before_the_exit_is_left_alone_by_the_late_mint_rule(world, clock):
    seeded, _, snapshot_id = await basic(world, deny_symbols=['AAA'], assets=[('v1', 'AAA')])
    entry_id = await _exited_with_late_mint(world, seeded, snapshot_id, now() - 5 * DAY)
    await manager(clock).tick()
    assert (await world.entry_row(entry_id)).collect is True


async def test_a_late_mint_held_by_another_enabled_panel_keeps_collecting(world, clock):
    seeded, _, snapshot_id = await basic(world, deny_symbols=['AAA'], assets=[('v1', 'AAA')])
    await world.panel(now() - DAY, name='holder', allow_symbols=['AAA'])
    entry_id = await _exited_with_late_mint(world, seeded, snapshot_id, now() + timedelta(minutes=5))
    await manager(clock).tick()
    assert (await world.entry_row(entry_id)).collect is True


# ===================================================================================== P10 as_of


async def _history(world, *, tradable_flip=False, appear=False):
    """A previous pass on S0, then S1, S2 (the change) and S3 (newest), each a day apart."""
    await world.source_settings()
    seeded = await world.panel(now() - 6 * DAY, tradable=True if tradable_flip else None)
    v1 = ('v1', 'AAA')
    gone = ('v1', 'AAA', {'tradable': False}) if tradable_flip else None

    def assets(changed: bool):
        out = [('v9', 'ZZZ')]
        if appear:
            if changed:
                out.append(v1)
        elif not changed:
            out.append(v1)
        elif gone is not None:
            out.append(gone)
        return out

    s0 = await world.snapshot(now() - 5 * DAY, assets(False))
    await world.snapshot(now() - 4 * DAY, assets(False))
    s2 = await world.snapshot(now() - 3 * DAY, assets(True))
    s3 = await world.snapshot(now() - 2 * DAY, assets(True))
    as_of = await snapshot_as_of(world, s0)
    keep = await world.request('ZZZ', state=IngestRequestState.QUEUED)
    rows = [t('v9', ENTERED, as_of, symbol='ZZZ'), t('v9', CREATED, as_of, symbol='ZZZ', request_id=keep)]
    if not appear:
        mine = await world.request('AAA', state=IngestRequestState.QUEUED)
        rows += [t('v1', ENTERED, as_of), t('v1', CREATED, as_of, request_id=mine)]
    await world.ledger_pass(seeded, s0, rows, started_at=now() - 5 * DAY + timedelta(minutes=1))
    await world.age_window()
    return seeded, await snapshot_as_of(world, s2), await snapshot_as_of(world, s3)


async def test_an_exit_is_dated_by_the_first_snapshot_showing_it(world, clock):
    seeded, changed_at, newest_at = await _history(world)
    await manager(clock).tick()
    last = (await world.transitions(seeded))[-1]
    assert (last.vendor_asset_id, last.transition) == ('v1', EXITED)
    assert last.as_of == changed_at != newest_at


async def test_a_row_attribute_flip_is_dated_from_the_rows(world, clock):
    seeded, changed_at, _ = await _history(world, tradable_flip=True)
    await manager(clock).tick()
    last = (await world.transitions(seeded))[-1]
    assert (last.vendor_asset_id, last.transition) == ('v1', EXITED)
    assert last.as_of == changed_at


async def test_under_a_new_settings_version_an_exit_is_dated_by_this_pass_snapshot(world, clock):
    seeded, _, newest_at = await _history(world)
    await world.settings(seeded.panel_id)
    await manager(clock).tick()
    last = (await world.transitions(seeded))[-1]
    assert (last.vendor_asset_id, last.transition, last.as_of) == ('v1', EXITED, newest_at)


async def _entered(world, seeded):
    (row,) = [r for r in await world.transitions(seeded) if r.vendor_asset_id == 'v1' and r.transition is ENTERED]
    return row


async def test_an_appeared_member_is_entered_as_of_the_first_snapshot_showing_it(world, clock):
    seeded, changed_at, newest_at = await _history(world, appear=True)
    await manager(clock).tick()
    assert (await _entered(world, seeded)).as_of == changed_at != newest_at
    created = [r for r in await world.transitions(seeded) if r.vendor_asset_id == 'v1' and r.transition is CREATED]
    assert [r.as_of for r in created] == [newest_at], "CREATED keeps this pass's snapshot"


async def test_an_appeared_member_with_an_existing_entry_is_entered_backdated(world, clock):
    seeded, changed_at, _ = await _history(world, appear=True)
    await world.entry('AAA')
    await manager(clock).tick()
    assert (await _entered(world, seeded)).as_of == changed_at
    assert 'AAA' not in {r.asset_symbol for r in await world.requests(BARS)}


async def test_pin_16c_an_in_flight_skipped_new_member_is_entered_backdated_with_no_created(world, clock):
    seeded, changed_at, _ = await _history(world, appear=True)
    await world.request('AAA', kind=CORPORATE)
    before = len(await world.requests())
    await manager(clock).tick()
    assert (await _entered(world, seeded)).as_of == changed_at
    v1 = [r.transition for r in await world.transitions(seeded) if r.vendor_asset_id == 'v1']
    assert v1 == [ENTERED], 'in flight: ENTERED only, no CREATED and no DEFERRED'
    assert len(await world.requests()) == before and 'AAA' not in [c.symbol for c in world.clamps]


async def test_pin_16a_a_size_cut_entrant_is_entered_as_of_this_pass_snapshot(world, clock):
    """v2 passed the filters at the previous pass's snapshot and was cut by size; it enters only because v1 left."""
    await world.source_settings()
    seeded = await world.panel(now() - 6 * DAY, size=1)
    s0 = await world.snapshot(now() - 5 * DAY, [('v1', 'AAA'), ('v2', 'BBB')])
    await world.snapshot(now() - 4 * DAY, [('v1', 'AAA'), ('v2', 'BBB')])
    s2 = await world.snapshot(now() - 3 * DAY, [('v2', 'BBB')])
    s3 = await world.snapshot(now() - 2 * DAY, [('v2', 'BBB')])
    as_of = await snapshot_as_of(world, s0)
    mine = await world.request('AAA', state=IngestRequestState.QUEUED)
    await world.ledger_pass(
        seeded,
        s0,
        [t('v1', ENTERED, as_of), t('v1', CREATED, as_of, request_id=mine)],
        started_at=now() - 5 * DAY + timedelta(minutes=1),
    )
    await world.age_window()

    await manager(clock).tick()

    rows = {(r.vendor_asset_id, r.transition): r for r in await world.transitions(seeded)}
    assert rows[('v1', EXITED)].as_of == await snapshot_as_of(world, s2), 'the exit is still backdated'
    assert rows[('v2', ENTERED)].as_of == await snapshot_as_of(world, s3), 'a size-cut entrant is not backdated'


async def test_under_a_new_settings_version_an_appeared_member_is_entered_as_of_this_pass_snapshot(world, clock):
    seeded, _, newest_at = await _history(world, appear=True)
    await world.settings(seeded.panel_id)
    await manager(clock).tick()
    (row,) = [r for r in await world.transitions(seeded) if r.vendor_asset_id == 'v1' and r.transition is ENTERED]
    assert row.as_of == newest_at


# ===================================================================================== P11 due and gate


async def test_one_pass_per_due_time_and_a_restart_before_due_runs_none(world, clock):
    seeded, _, _ = await basic(world)
    await manager(clock).tick()
    await manager(clock).tick()
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 1


async def test_a_monthly_panel_waits_for_its_month_then_for_a_snapshot_until_the_next_close(world, clock, caplog):
    seeded, _, _ = await basic(world, cadence=PanelCadence.MONTHLY, assets=[('v1', 'AAA')])
    await manager(clock).tick()
    (first,) = await world.passes(seeded)
    calendar = EveryDayCalendar()
    due = membership_due_at(PanelCadence.MONTHLY, first.started_at, first.started_at, calendar, SETTLE)
    give_up = membership_due_at(PanelCadence.DAILY, due, due, calendar, SETTLE)

    clock.offset = due - now() - timedelta(minutes=1)
    await manager(clock).tick()
    clock.offset = give_up - now() - timedelta(minutes=1)
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 1, 'ran before its month or before the wait for a snapshot ended'

    clock.offset = give_up - now() + timedelta(minutes=1)
    with caplog.at_level('INFO'):
        await manager(clock).tick()
    assert [row.kind for row in await world.passes(seeded)] == [PanelPassKind.MEMBERSHIP] * 2
    messages = [record.getMessage() for record in caplog.records]
    assert any('older than its due time' in line for line in messages)
    assert not any('first pass' in line for line in messages), 'a later pass is not a first pass'


async def test_a_snapshot_after_the_due_time_runs_the_pass_at_once(world, clock):
    seeded, _, _ = await basic(world, cadence=PanelCadence.WEEKLY, assets=[('v1', 'AAA')])
    await manager(clock).tick()
    (first,) = await world.passes(seeded)
    due = first.started_at + timedelta(days=7)
    clock.offset = due - now() + timedelta(minutes=1)
    await world.snapshot(due, [('v1', 'AAA')])
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 2


def info_lines(monkeypatch) -> list[str]:
    lines: list[str] = []
    real = panel_manager.log.info

    def record(message, *args, **kwargs):
        lines.append(str(message))
        return real(message, *args, **kwargs)

    monkeypatch.setattr(panel_manager.log, 'info', record)
    return lines


async def test_a_panel_created_after_the_snapshot_runs_its_first_pass_at_once_and_says_the_snapshot_is_older(
    world, clock, monkeypatch
):
    """tj-7zjov1.230: the first pass runs at creation on the newest snapshot, however old; one INFO line says so."""
    await world.source_settings()
    seeded = await world.panel(now() - timedelta(seconds=5))
    await world.snapshot(now() - 2 * DAY, [('v1', 'AAA')])  # before the latest close plus settle, whatever the hour
    lines = info_lines(monkeypatch)

    await manager(clock).tick()

    (row,) = await world.passes(seeded)
    assert row.kind is PanelPassKind.MEMBERSHIP and row.finished_at is not None
    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED'), ('v1', 'CREATED')]
    (line,) = [line for line in lines if 'first pass' in line]
    assert str(row.id) in line and 'AAA' not in line and 'v1' not in line.split()
    assert not any('older than its due time' in line for line in lines), 'the later-pass line is not logged'


async def test_a_first_pass_on_a_current_snapshot_logs_no_older_line(world, clock, monkeypatch):
    await world.source_settings()
    seeded = await world.panel(now() - timedelta(seconds=5))
    await world.snapshot(now(), [('v1', 'AAA')])
    lines = info_lines(monkeypatch)

    await manager(clock).tick()

    assert len(await world.passes(seeded)) == 1
    assert not [line for line in lines if 'first pass' in line or 'older than' in line]


async def test_no_snapshot_no_pass(world, clock):
    await world.source_settings()
    seeded = await world.panel(now() - DAY)
    await world.snapshot(now() - HOUR, [], status=AssetListStatus.ACTIVE)  # an EMPTY snapshot is not one
    await manager(clock).tick()
    assert await world.passes(seeded) == []


async def test_a_panel_whose_source_has_no_settings_runs_no_pass_and_is_logged_once(world, clock, monkeypatch):
    seeded = await world.panel(now() - DAY)
    await world.snapshot(now() - HOUR, [('v1', 'AAA')])
    lines = []
    monkeypatch.setattr(panel_manager.log, 'info', lambda message, *a, **k: lines.append(message))
    runner = manager(clock)
    await runner.tick()
    await runner.tick()
    assert await world.passes(seeded) == []
    assert len([line for line in lines if 'not runnable' in line]) == 1


async def test_a_failing_panel_ends_its_own_turn_only(world, clock, monkeypatch):
    await world.source_settings()
    first = await world.panel(now() - 3 * DAY, allow_symbols=['AAA'])
    second = await world.panel(now() - 2 * DAY, allow_symbols=['BBB'])
    await world.snapshot(now() - HOUR, [('v1', 'AAA'), ('v2', 'BBB')])
    real = panel_manager.PanelManager._membership_pass

    async def failing_first(self, db, source, enabled, *rest):
        if enabled.panel.id == first.panel_id:
            raise RuntimeError('boom')
        return await real(self, db, source, enabled, *rest)

    monkeypatch.setattr(panel_manager.PanelManager, '_membership_pass', failing_first)
    await manager(clock).tick()
    assert await world.passes(first) == [] and len(await world.passes(second)) == 1


# ===================================================================================== P12 dry run


async def test_a_dry_run_pass_is_flagged_requests_nothing_and_leaves_the_real_first_pass_due(world, clock):
    seeded, _, _ = await basic(world)

    await manager(clock, dry_run=True).tick()

    (row,) = await world.passes(seeded)
    assert row.dry_run is True and row.finished_at is not None
    assert {k[1] for k in kinds(await world.transitions(seeded))} == {'ENTERED'}
    assert await world.requests() == [] and world.clamps == [] and world.submits == []

    await manager(clock).tick()
    real = [row for row in await world.passes(seeded) if not row.dry_run]
    assert len(real) == 1 and len(await world.requests(BARS)) == 3


async def test_a_dry_run_exit_stops_nothing(world, clock):
    seeded, _, snapshot_id = await basic(world, deny_symbols=['AAA'], assets=[('v1', 'AAA')])
    entry_id, rows = await _member_with_entry(world, seeded, snapshot_id)
    await world.ledger_pass(seeded, snapshot_id, rows, started_at=now() - 3 * DAY, dry_run=True)
    await world.ledger_pass(seeded, snapshot_id, rows, started_at=now() - 3 * DAY)
    await world.age_window()
    runner = manager(clock, dry_run=True)
    await runner.tick()
    assert (await world.entry_row(entry_id)).collect is True and not world.published


# ===================================================================================== P13 crash points


async def test_a_crash_between_the_pass_row_and_its_transitions_leaves_nothing_and_the_next_tick_runs(
    world, clock, monkeypatch
):
    # A member the filter now drops: its EXITED is the pass's first transition, appended right after open_pass.
    seeded, _, snapshot_id = await basic(world, assets=[('v1', 'AAA')], deny_symbols=['AAA'])
    as_of = await snapshot_as_of(world, snapshot_id)
    request_id = await world.request('AAA', state=IngestRequestState.QUEUED)
    await world.ledger_pass(
        seeded,
        snapshot_id,
        [t('v1', ENTERED, as_of), t('v1', CREATED, as_of, request_id=request_id)],
        started_at=now() - 3 * DAY,
    )
    await world.age_window()
    real = panel_manager.append_transitions

    async def crash(*args, **kwargs):
        raise RuntimeError('crash')

    monkeypatch.setattr(panel_manager, 'append_transitions', crash)
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 1, 'the crashed pass left its row: only the earlier pass may exist'
    assert len(await world.transitions(seeded)) == 2, 'only the earlier pass rows'

    monkeypatch.setattr(panel_manager, 'append_transitions', real)
    await manager(clock).tick()
    assert len(await world.passes(seeded)) == 2
    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'EXITED')


async def _crashing_finish(*args, **kwargs):
    raise RuntimeError('crash')


async def unfinished_pass(world, seeded, snapshot_id, *, started_at: datetime) -> uuid.UUID:
    """A MEMBERSHIP pass row that never finished (a crash between open_pass and finish_pass), with no transitions."""
    pass_id = await world.ledger_pass(seeded, snapshot_id, [], started_at=started_at)
    await world.put(PASSES.update().where(PASSES.c.id == pass_id).values(finished_at=None))
    return pass_id


def never_finished_warnings(monkeypatch) -> list[str]:
    lines: list[str] = []
    real = panel_manager.log.warning

    def record(message, *args, **kwargs):
        if 'never finished' in str(message):
            lines.append(str(message))
        return real(message, *args, **kwargs)

    monkeypatch.setattr(panel_manager.log, 'warning', record)
    return lines


async def test_a_crash_before_the_finish_holds_the_panel_off_for_an_hour_then_a_new_pass_decides_the_rest(
    world, clock, monkeypatch
):
    """tj-7zjov1.229: only a FINISHED pass satisfies the due rule; an unfinished one holds off UNFINISHED_PASS_RETRY."""
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    real = panel_manager.finish_pass
    monkeypatch.setattr(panel_manager, 'finish_pass', _crashing_finish)
    await manager(clock).tick()

    (crashed,) = await world.passes(seeded)
    assert crashed.finished_at is None
    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED'), ('v1', 'CREATED')], (
        'the creation committed with its request; the deferral after it was pending and is lost'
    )

    # Within the hour: no pass of either kind (there is no finished pass for a release to work from).
    monkeypatch.setattr(panel_manager, 'finish_pass', real)
    await world.age_window()
    await manager(clock).tick()
    assert [row.id for row in await world.passes(seeded)] == [crashed.id]

    # Once the unfinished pass is an hour old the panel is due again: a NEW membership pass decides v2.
    await due_again(world, seeded, by=HOUR + timedelta(minutes=1))
    await manager(clock).tick()
    first, second = await world.passes(seeded)
    assert first.id == crashed.id and first.finished_at is None, 'the unfinished row stays as written'
    assert second.kind is PanelPassKind.MEMBERSHIP and second.finished_at is not None
    decided = [row for row in await world.transitions(seeded) if row.pass_id == second.id]
    assert kinds(decided) == [('v2', 'ENTERED'), ('v2', 'CREATED')]


async def test_a_pass_that_crashes_every_time_leaves_at_most_one_pass_row_per_hour_and_one_warning_each(
    world, clock, monkeypatch
):
    seeded, _, _ = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB'), ('v3', 'CCC'), ('v4', 'DDD')])
    monkeypatch.setattr(panel_manager, 'finish_pass', _crashing_finish)
    warnings = never_finished_warnings(monkeypatch)
    runner = manager(clock)

    async def attempt() -> None:
        # Every attempt can commit one creation (so its pass row is written) once the ramp window is aged.
        await world.age_window()
        await runner.tick()

    await attempt()
    for _ in range(3):
        await attempt()
    assert len(await world.passes(seeded)) == 1, 'a crash loop at the tick rate'

    await due_again(world, seeded, by=HOUR + timedelta(minutes=1))
    await attempt()
    assert len(await world.passes(seeded)) == 2
    await attempt()
    await attempt()
    passes = await world.passes(seeded)
    assert len(passes) == 2 and all(row.finished_at is None for row in passes)

    # One WARNING per unfinished pass seen, naming the pass id and nothing else (no symbol).
    assert len(warnings) == 2
    for line, row in zip(warnings, passes, strict=True):
        assert str(row.id) in line
        assert not {'AAA', 'BBB', 'CCC', 'DDD', PANEL_OWNER} & set(line.replace('.', ' ').split())


async def test_the_as_of_baseline_after_a_crash_is_the_previous_finished_pass_snapshot(world, clock):
    seeded, changed_at, newest_at = await _history(world)
    newest = max(await world.rows(SNAPSHOTS), key=lambda row: row.as_of)
    assert newest.as_of == newest_at
    # A crashed pass on the newest snapshot, older than the retry floor: were it the baseline, the exit would be dated
    # by the newest snapshot (nothing between it and the pass's own snapshot).
    await unfinished_pass(world, seeded, newest.id, started_at=now() - 2 * HOUR)

    await manager(clock).tick()

    last = (await world.transitions(seeded))[-1]
    assert (last.vendor_asset_id, last.transition) == ('v1', EXITED)
    assert last.as_of == changed_at != newest_at


async def test_a_held_off_panel_still_runs_its_release(world, clock):
    seeded, _, snapshot_id = await basic(world, ramp=1, assets=[('v1', 'AAA'), ('v2', 'BBB')])
    await manager(clock).tick()  # v2 DEFERRED ramp_rate
    await due_again(world, seeded)  # the membership pass is due again ...
    crashed = await unfinished_pass(world, seeded, snapshot_id, started_at=now() - timedelta(minutes=10))
    await world.age_window()

    await manager(clock).tick()  # ... but held off by the crash ten minutes ago

    passes = await world.passes(seeded)
    memberships = [row for row in passes if row.kind is PanelPassKind.MEMBERSHIP]
    assert len(memberships) == 2 and crashed in {row.id for row in memberships}, (
        'no new membership pass within the hour'
    )
    (release,) = [row for row in passes if row.kind is PanelPassKind.RELEASE]
    released = [row for row in await world.transitions(seeded) if row.pass_id == release.id]
    assert kinds(released) == [('v2', 'CREATED')]


async def test_unfinished_holds_off_strictly_under_the_retry_floor_and_only_when_newer_than_the_last_finished(
    world, clock, monkeypatch
):
    seeded, _, snapshot_id = await basic(world)
    warnings = never_finished_warnings(monkeypatch)
    runner = manager(clock)
    membership = PanelPassKind.MEMBERSHIP

    async def holds_off(at: datetime) -> bool:
        async with world.own_session() as db:
            last = await latest_pass(db, seeded.panel_id, membership, dry_run=False, finished=True)
            return await runner._unfinished_holds_off(db, seeded.panel_id, last, at)

    # No pass at all.
    assert await holds_off(now()) is False
    # A first pass that crashed: held off with no finished pass.
    crashed = await unfinished_pass(world, seeded, snapshot_id, started_at=now() - 3 * HOUR)
    (row,) = await world.passes(seeded)
    start = row.started_at
    assert await holds_off(start + UNFINISHED_PASS_RETRY - timedelta(seconds=1)) is True
    assert await holds_off(start + UNFINISHED_PASS_RETRY) is False, 'an hour old: due again'
    assert len(warnings) == 1 and str(crashed) in warnings[0], 'one WARNING per pass, however often it is seen'

    # A finished pass started at the same instant: the crash is not newer than it, so it holds nothing off.
    tie = await world.ledger_pass(seeded, snapshot_id, [], started_at=start)
    assert await holds_off(start + timedelta(minutes=1)) is False
    await world.put(PASSES.update().where(PASSES.c.id == tie).values(started_at=start - HOUR))

    # A finished pass after it: the crash is history, never a hold-off, and no new warning.
    await world.ledger_pass(seeded, snapshot_id, [], started_at=now() - 2 * HOUR)
    assert await holds_off(start + timedelta(minutes=1)) is False
    assert len(warnings) == 1


async def test_latest_pass_reads_finished_unfinished_or_any(world, clock):
    seeded, _, snapshot_id = await basic(world)
    finished = await world.ledger_pass(seeded, snapshot_id, [], started_at=now() - 3 * HOUR)
    unfinished = await unfinished_pass(world, seeded, snapshot_id, started_at=now() - 2 * HOUR)
    dry = await world.ledger_pass(seeded, snapshot_id, [], started_at=now() - HOUR, dry_run=True)
    await world.put(PASSES.update().where(PASSES.c.id == dry).values(finished_at=None))
    membership = PanelPassKind.MEMBERSHIP

    async with world.own_session() as db:

        async def read(**kw):
            found = await latest_pass(db, seeded.panel_id, membership, **kw)
            return None if found is None else found.id

        assert await read(dry_run=False, finished=True) == finished
        assert await read(dry_run=False, finished=False) == unfinished
        assert await read(dry_run=False) == unfinished, 'None reads any: the newest'
        assert await read(dry_run=True, finished=False) == dry
        assert await read(dry_run=True, finished=True) is None
        assert await latest_pass(db, seeded.panel_id, PanelPassKind.RELEASE, dry_run=False, finished=False) is None


# ===================================================================================== P14 nothing else requested


async def test_the_manager_requests_no_asset_list_and_no_daily_bars(world, clock):
    await basic(world, ramp=1)
    await world.entry('ZZZ')
    await world.snapshot(now() - timedelta(minutes=30), [('v1', 'AAA'), ('v2', 'BBB'), ('v9', 'ZZZ')])
    await manager(clock).tick()
    await world.age_window()
    await manager(clock).tick()
    requests = await world.requests()
    assert requests and {row.kind for row in requests} <= {BARS, CORPORATE}
    assert all(row.dataset_id is None and row.trigger is IngestRunTrigger.REQUEST for row in requests)
    assert 'ZZZ' not in {row.asset_symbol for row in requests}, "a member's daily bars are the DAILY refresh's"


# The source-level half of P14 (no asset-list request, no awaiting or committing helper) is test_panel_manager_rules.py R8.


# ===================================================================================== P15 re-entry restart (P22r)


async def test_the_stop_mark_and_the_resume_cause_are_the_designed_strings():
    assert DatasetStopCause.PANEL_MEMBER_LEFT.value == LEFT
    assert CAUSE_DATASET_RESUMED == 'dataset_resumed'


async def _re_entry(world, previous, *, collect=False, cause=LEFT):
    """A panel whose newest snapshot selects v1 (AAA) again, a panel entry for AAA, and v1's ledger ending `previous`.

    previous None: the member has no ledger row on this panel at all (its entry exists: another panel minted it).
    """
    seeded, _, snapshot_id = await basic(world, assets=[('v1', 'AAA')])
    entry_id, rows = await _member_with_entry(world, seeded, snapshot_id, collect=collect, stopped_cause=cause)
    as_of = await snapshot_as_of(world, snapshot_id)
    if previous is not None:
        await world.ledger_pass(seeded, snapshot_id, rows, started_at=now() - 4 * DAY)
        await world.ledger_pass(seeded, snapshot_id, [t('v1', previous, as_of)], started_at=now() - 3 * DAY)
    await world.age_window()
    return seeded, entry_id


async def _assert_resumed(world, seeded, entry_id):
    last = (await world.transitions(seeded))[-1]
    assert (last.vendor_asset_id, last.transition, last.dataset_id, last.cause) == (
        'v1',
        ENTERED,
        entry_id,
        CAUSE_DATASET_RESUMED,
    ), 'the ENTERED row names the restarted entry'
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (True, None), 'collect true and the mark cleared'
    assert entry.updated_at > datetime(2026, 1, 1, tzinfo=UTC)
    assert [(e.id, e.change) for e in world.published] == [(entry_id, DatasetChange.RESUMED)]
    # A restart writes no request: the gap is the DAILY refresh's (addendum 23 (4)).
    assert world.clamps == [] and len(await world.requests(BARS)) == 1 and await world.requests(CORPORATE) == []


async def _assert_not_resumed(world, seeded, entry_id, collect, cause):
    last = (await world.transitions(seeded))[-1]
    assert (last.vendor_asset_id, last.transition, last.dataset_id, last.cause) == ('v1', ENTERED, None, None)
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (collect, cause)
    assert not [e for e in world.published if e.change is DatasetChange.RESUMED]
    assert world.clamps == [] and len(await world.requests(BARS)) == 1


async def test_u1_a_filter_exit_marks_the_stop_and_the_re_entry_restarts_it_in_the_entered_commit(world, clock):
    seeded, _, snapshot_id = await basic(world, deny_symbols=['AAA'], assets=[('v1', 'AAA')])
    entry_id, rows = await _member_with_entry(world, seeded, snapshot_id)
    await world.ledger_pass(seeded, snapshot_id, rows, started_at=now() - 3 * DAY)
    await world.age_window()

    await manager(clock).tick()

    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'EXITED')
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, LEFT)

    await world.settings(seeded.panel_id)
    await due_again(world, seeded)
    await manager(clock).tick()

    last = (await world.transitions(seeded))[-1]
    assert (last.transition, last.dataset_id, last.cause) == (ENTERED, entry_id, CAUSE_DATASET_RESUMED)
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (True, None)
    assert [(e.id, e.change) for e in world.published] == [
        (entry_id, DatasetChange.RETIRED),
        (entry_id, DatasetChange.RESUMED),
    ]
    assert world.clamps == [] and len(await world.requests(BARS)) == 1


@pytest.mark.parametrize('previous', [EXITED, DRAINED, None], ids=['after-exited', 'after-drained', 'no-ledger-row'])
async def test_u4_a_manager_stopped_entry_is_resumed_whatever_the_previous_transition(world, clock, previous):
    seeded, entry_id = await _re_entry(world, previous)
    await manager(clock).tick()
    await _assert_resumed(world, seeded, entry_id)


async def test_u6_stopped_by_panel_a_s_exit_the_first_entry_into_panel_b_resumes_it(world, clock):
    await world.source_settings()
    first = await world.panel(now() - 3 * DAY, deny_symbols=['AAA'])
    old = await world.snapshot(now() - 3 * DAY, [('v1', 'AAA')])
    await world.snapshot(now() - HOUR, [('v1', 'AAA')])
    entry_id, rows = await _member_with_entry(world, first, old)
    await world.ledger_pass(first, old, rows, started_at=now() - 3 * DAY)
    await world.age_window()
    await manager(clock).tick()
    assert (await world.entry_row(entry_id)).stopped_cause == LEFT, 'panel A stopped it: no other panel selects AAA'

    second = await world.panel(now() - DAY, allow_symbols=['AAA'])
    world.published.clear()
    await manager(clock).tick()

    assert kinds(await world.transitions(second)) == [('v1', 'ENTERED')]
    await _assert_resumed(world, second, entry_id)


@pytest.mark.parametrize(
    ('collect', 'cause'),
    [(False, None), (False, VENDOR_STOP), (True, None)],
    ids=['u2-operator-stop', 'u3-vendor-stop', 'collecting'],
)
async def test_only_a_manager_stop_is_resumed(world, clock, collect, cause):
    seeded, entry_id = await _re_entry(world, EXITED, collect=collect, cause=cause)
    await manager(clock).tick()
    await _assert_not_resumed(world, seeded, entry_id, collect, cause)


@pytest.mark.parametrize(
    ('attempts', 'resumed'),
    [
        ([], True),
        ([('v1', IngestRunState.SUCCEEDED, 5)], True),
        ([(None, IngestRunState.EMPTY, 1), ('v1', IngestRunState.SUCCEEDED, 5)], True),
        ([('v7', IngestRunState.FAILED, 1), ('v1', IngestRunState.EMPTY, 5)], True),
        ([('v7', IngestRunState.SUCCEEDED, 5)], False),
        ([('v7', IngestRunState.SUCCEEDED, 1), ('v1', IngestRunState.SUCCEEDED, 5)], False),
    ],
    ids=[
        'u-empty-no-attempt',
        'u-equal',
        'u-equal-a-dropped-delivery-stores-none',
        'u-equal-a-refused-attempt-is-no-base',
        'u5-different',
        'u5-the-newest-accepted-is-different',
    ],
)
async def test_the_last_accepted_vendor_asset_id_must_be_empty_or_the_members(world, clock, attempts, resumed):
    seeded, entry_id = await _re_entry(world, EXITED)
    for vendor, state, days_ago in attempts:
        await world.bars_attempt(entry_id, vendor, state=state, finished_at=now() - days_ago * DAY)

    await manager(clock).tick()

    if resumed:
        await _assert_resumed(world, seeded, entry_id)
    else:
        await _assert_not_resumed(world, seeded, entry_id, False, LEFT)


async def test_u8_an_operator_stop_before_the_manager_s_exit_keeps_no_mark_and_is_not_resumed(world, clock):
    seeded, _, snapshot_id = await basic(world, deny_symbols=['AAA'], assets=[('v1', 'AAA')])
    # The operator stopped it first (cause NULL); the exit then has nothing to flip.
    entry_id, rows = await _member_with_entry(world, seeded, snapshot_id, collect=False)
    await world.ledger_pass(seeded, snapshot_id, rows, started_at=now() - 3 * DAY)
    await world.age_window()

    await manager(clock).tick()

    assert kinds(await world.transitions(seeded))[-1] == ('v1', 'EXITED')
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, None)
    assert world.published == []

    await world.settings(seeded.panel_id)
    await due_again(world, seeded)
    await manager(clock).tick()

    await _assert_not_resumed(world, seeded, entry_id, False, None)


@pytest.mark.parametrize(
    ('collect', 'cause'), [(True, None), (False, None)], ids=['operator-resume', 'operator-resume-then-stop']
)
async def test_an_operator_write_racing_the_pass_wins_and_the_entered_row_is_plain(
    world, clock, monkeypatch, collect, cause
):
    """The operator writes between the pass's read of the entry and its guarded UPDATE: rowcount 0, no restart."""
    seeded, entry_id = await _re_entry(world, EXITED)
    real = panel_manager.last_accepted_vendor_asset_id

    async def operator_first(db, dataset_id):
        await world.put(ENTRIES.update().where(ENTRIES.c.id == entry_id).values(collect=collect, stopped_cause=cause))
        return await real(db, dataset_id)

    monkeypatch.setattr(panel_manager, 'last_accepted_vendor_asset_id', operator_first)

    await manager(clock).tick()

    await _assert_not_resumed(world, seeded, entry_id, collect, cause)


async def test_u7_a_dry_run_records_the_decision_and_changes_and_publishes_nothing(world, clock):
    seeded, entry_id = await _re_entry(world, EXITED)

    await manager(clock, dry_run=True).tick()

    (dry,) = [row for row in await world.passes(seeded) if row.dry_run]
    (decided,) = [row for row in await world.transitions(seeded) if row.pass_id == dry.id]
    assert (decided.transition, decided.dataset_id, decided.cause) == (ENTERED, entry_id, CAUSE_DATASET_RESUMED)
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, LEFT)
    assert world.published == [] and world.clamps == []

    await manager(clock).tick()
    await _assert_resumed(world, seeded, entry_id)


@pytest.mark.parametrize(
    ('collect', 'cause', 'vendor'),
    [(False, None, None), (False, VENDOR_STOP, None), (False, LEFT, 'v7')],
    ids=['operator-stop', 'vendor-stop', 'different-vendor-id'],
)
async def test_a_dry_run_records_a_plain_entered_for_an_entry_it_would_not_resume(world, clock, collect, cause, vendor):
    """The dry run applies no guarded UPDATE, so its decision alone must apply all three conditions."""
    seeded, entry_id = await _re_entry(world, EXITED, collect=collect, cause=cause)
    if vendor is not None:
        await world.bars_attempt(entry_id, vendor)

    await manager(clock, dry_run=True).tick()

    (dry,) = [row for row in await world.passes(seeded) if row.dry_run]
    (decided,) = [row for row in await world.transitions(seeded) if row.pass_id == dry.id]
    assert (decided.transition, decided.dataset_id, decided.cause) == (ENTERED, None, None)
    assert world.published == []


async def test_u9_a_release_pass_never_resumes(world, clock):
    seeded, _, snapshot_id = await basic(world, assets=[('v1', 'AAA')])
    entry_id, _ = await _member_with_entry(world, seeded, snapshot_id, collect=False, stopped_cause=LEFT)
    as_of = await snapshot_as_of(world, snapshot_id)
    # The last membership pass deferred v1 and is not due again: the tick runs a RELEASE only.
    await world.ledger_pass(
        seeded,
        snapshot_id,
        [t('v1', ENTERED, as_of), t('v1', DEFERRED, as_of, cause=CAUSE_RAMP_RATE)],
        started_at=now(),
    )
    await world.age_window()

    await manager(clock).tick()

    assert [row.kind for row in await world.passes(seeded)] == [PanelPassKind.MEMBERSHIP], 'no new membership pass'
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, LEFT)
    assert world.published == [] and world.clamps == []


async def test_resumed_is_published_after_the_pass_commits(world, clock, monkeypatch):
    _, entry_id = await _re_entry(world, EXITED)
    seen = []

    def recorder(event):
        pass_db = world.pass_sessions[-1]
        seen.append((event.change, pass_db.in_transaction(), world.commits.get(id(pass_db), 0)))

    monkeypatch.setattr(panel_manager, 'publish', recorder)

    await manager(clock).tick()

    assert seen == [(DatasetChange.RESUMED, False, 1)], 'published once, after the one commit of the pass session'
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (True, None)


async def test_a_rollback_loses_the_restart_and_its_entered_row_and_publishes_nothing(world, clock, monkeypatch):
    seeded, entry_id = await _re_entry(world, EXITED)
    before = len(await world.transitions(seeded))

    async def crash(*_args, **_kw):
        raise RuntimeError('crash before the commit')

    monkeypatch.setattr(panel_manager, 'finish_pass', crash)
    await manager(clock).tick()

    assert len(await world.transitions(seeded)) == before
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == (False, LEFT), 'the guarded UPDATE rolled back with the ENTERED row'
    assert world.published == []


async def test_a_resumed_entry_that_exits_again_is_marked_again_and_resumed_again(world, clock):
    seeded, entry_id = await _re_entry(world, EXITED)
    await manager(clock).tick()
    await _assert_resumed(world, seeded, entry_id)

    await world.settings(seeded.panel_id, deny_symbols=['AAA'])
    await due_again(world, seeded)
    await manager(clock).tick()
    assert (await world.entry_row(entry_id)).stopped_cause == LEFT

    await world.settings(seeded.panel_id)
    await due_again(world, seeded)
    await manager(clock).tick()
    last = (await world.transitions(seeded))[-1]
    assert (last.transition, last.dataset_id, last.cause) == (ENTERED, entry_id, CAUSE_DATASET_RESUMED)
    assert [e.change for e in world.published] == [DatasetChange.RESUMED, DatasetChange.RETIRED, DatasetChange.RESUMED]


# ------------------------------------------------------------------------------- the two crud writes, directly


@pytest.mark.parametrize(
    ('collect', 'cause', 'flips'),
    [(False, LEFT, True), (False, None, False), (False, VENDOR_STOP, False), (True, None, False)],
    ids=['manager-stopped', 'operator-stopped', 'vendor-stopped', 'collecting'],
)
async def test_resume_panel_entry_is_guarded_on_collect_false_and_the_mark(world, collect, cause, flips):
    entry_id = await world.entry('AAA', collect=collect, stopped_cause=cause)
    async with world.own_session() as db:
        assert await resume_panel_entry(db, entry_id) is flips
        await db.rollback()
    assert ((await world.entry_row(entry_id)).collect, (await world.entry_row(entry_id)).stopped_cause) == (
        collect,
        cause,
    ), 'it never commits'
    async with world.own_session() as db:
        await resume_panel_entry(db, entry_id)
        await db.commit()
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == ((True, None) if flips else (collect, cause))


@pytest.mark.parametrize(
    ('collect', 'cause', 'flips'),
    [(True, None, True), (False, None, False), (False, VENDOR_STOP, False)],
    ids=['collecting', 'operator-stopped', 'vendor-stopped'],
)
async def test_stop_collecting_marks_only_the_stop_it_makes(world, collect, cause, flips):
    entry_id = await world.entry('AAA', collect=collect, stopped_cause=cause)
    async with world.own_session() as db:
        assert await stop_collecting(db, entry_id) is flips
        await db.commit()
    entry = await world.entry_row(entry_id)
    assert (entry.collect, entry.stopped_cause) == ((False, LEFT) if flips else (collect, cause))


async def test_re_entry_a_member_with_no_entry_is_entered_and_created_again(world, clock):
    seeded, _, snapshot_id = await basic(world, assets=[('v1', 'AAA')])
    as_of = await snapshot_as_of(world, snapshot_id)
    old = await world.request('AAA', state=IngestRequestState.FAILED, finished_at=now() - 4 * DAY)
    await world.ledger_pass(
        seeded,
        snapshot_id,
        [t('v1', ENTERED, as_of), t('v1', CREATED, as_of, request_id=old)],
        started_at=now() - 4 * DAY,
    )
    await world.ledger_pass(seeded, snapshot_id, [t('v1', EXITED, as_of)], started_at=now() - 3 * DAY)
    await world.age_window()
    await manager(clock).tick()
    assert kinds(await world.transitions(seeded))[-2:] == [('v1', 'ENTERED'), ('v1', 'CREATED')]
