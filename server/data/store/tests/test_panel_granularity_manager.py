"""The panel manager at the panel's own Bar Interval, on SQLite (validator, tj-7zjov1.365.7).

THE DESIGN checked against: ADR tj-rwoasb D1 (one granularity per panel) and its CONSEQUENCES (every cross-panel check keys
on (source, owner, symbol, DAILY, granularity): the held set counts only panels of the same source AND granularity,
panel_entries and the in-flight open BARS check filter by granularity, a corporate-actions request stays per symbol),
and the acceptance of beads tj-7zjov1.365.4 (a) to (c). Pinned:

  G1 a panel's pass mints a DAILY dataset at the PANEL'S granularity, owner PANEL_OWNER, with the point-in-time start,
     and the own-overlap key it checks names that granularity;
  G2 two panels of DIFFERENT granularities selecting one symbol each mint their own dataset (neither refused as own
     overlap, neither skipped as in flight); two of the SAME granularity share one;
  G3 symbols_with_open_requests: an open BARS creation request counts only at its own granularity, an open corporate-
     actions request counts for every panel; the manager follows it;
  G4 panel_entries returns only entries at the asked granularity; a 1 Minute panel ignores a 1 Day entry and keeps a
     1 Minute one;
  G5 exits: a member's exit is held by another enabled panel of the same granularity only, and stops only the exiting
     panel's own-granularity dataset (the other granularity's dataset of the same symbol keeps collecting).

THE CLAMP DOUBLE. The harness's first_request_clamp leaves its corporate-actions request open, but the real clamp AWAITS
that request's verdict before it returns (the name_change clamp fails closed), so by the time a second panel runs its
check the first panel's clamp request is finished. The awaited_clamp fixture finishes it, as the real one would, so the
corporate-actions arm of the in-flight check does not mask the granularity arm under test.
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from common.enums.data_stock import Granularity, UpdateType
from data.store.app import panel_manager
from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.crud.ingest_request import symbols_with_open_requests
from data.store.app.database.crud.panel_ledger import NewTransition
from data.store.app.database.crud.panel_manager_reads import panel_entries
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.database.models.panel import PanelTransition
from data.store.tests.panel_manager_harness import REQUESTS, SNAPSHOTS, SOURCE, Clock, kinds, make_world, manager


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

BARS, CORPORATE = IngestRequestKind.BARS, IngestRequestKind.CORPORATE_ACTIONS
ENTERED, CREATED, EXITED = PanelTransition.ENTERED, PanelTransition.CREATED, PanelTransition.EXITED
MINUTE, HOUR_BARS, DAY_BARS = Granularity.ONE_MINUTE, Granularity.ONE_HOUR, Granularity.ONE_DAY
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
NEW_YORK = ZoneInfo('America/New_York')
LEFT = 'PANEL_MEMBER_LEFT'


def now() -> datetime:
    return datetime.now(UTC)


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


@pytest.fixture
def awaited_clamp(world, monkeypatch):
    """The harness clamp double, then its corporate-actions request finished, as the real clamp's awaited verdict."""
    double = panel_manager.first_request_clamp

    async def awaited(path, body, db, client, *, deadline_s):
        answer = await double(path, body, db, client, deadline_s=deadline_s)
        await db.execute(
            REQUESTS.update()
            .where(REQUESTS.c.id == answer[1])
            .values(state=IngestRequestState.SUCCEEDED, finished_at=now())
        )
        await db.commit()
        return answer

    monkeypatch.setattr(panel_manager, 'first_request_clamp', awaited)


async def snapshot_as_of(world, snapshot_id):
    (row,) = await world.rows(SNAPSHOTS, SNAPSHOTS.c.id == snapshot_id)
    return row.as_of


def declared_start(entered_as_of: datetime) -> datetime:
    return datetime.combine(entered_as_of.astimezone(NEW_YORK).date(), time.min, tzinfo=NEW_YORK)


def by_name(values):
    return sorted(values, key=lambda g: g.name)


# ===================================================================================== G1 the panel's own granularity


@pytest.mark.parametrize('granularity', [MINUTE, HOUR_BARS, DAY_BARS], ids=lambda g: g.name)
async def test_a_panel_mints_a_daily_dataset_at_its_own_granularity_and_checks_overlap_there(
    world, clock, monkeypatch, granularity
):
    await world.source_settings()
    seeded = await world.panel(now() - 2 * DAY, granularity=granularity)
    snapshot_id = await world.snapshot(now() - HOUR, [('v1', 'AAA')])
    as_of = await snapshot_as_of(world, snapshot_id)
    checked = []
    real = panel_manager.check_own_overlap

    async def recording(db, key):
        checked.append(key)
        return await real(db, key)

    monkeypatch.setattr(panel_manager, 'check_own_overlap', recording)

    await manager(clock).tick()

    assert kinds(await world.transitions(seeded)) == [('v1', 'ENTERED'), ('v1', 'CREATED')]
    (bars,) = await world.requests(BARS)
    assert (bars.granularity, bars.update_type) == (granularity, UpdateType.DAILY.value)
    assert bars.client_label == PANEL_OWNER and bars.dataset_id is None and bars.collect is True
    assert bars.requested_start == declared_start(as_of) and bars.requested_end is None
    (key,) = checked
    assert key.granularity is granularity and key.update_type is UpdateType.DAILY and key.owner == PANEL_OWNER


# ===================================================================================== G2 two panels, one symbol


@pytest.mark.parametrize(
    ('first', 'second'),
    [(MINUTE, DAY_BARS), (DAY_BARS, MINUTE), (HOUR_BARS, MINUTE)],
    ids=['1min-then-1day', '1day-then-1min', '1hour-then-1min'],
)
async def test_two_panels_of_different_granularities_on_one_symbol_each_mint_their_own_dataset(
    world, clock, awaited_clamp, first, second
):
    await world.source_settings()
    one = await world.panel(now() - 3 * DAY, allow_symbols=['XXX'], granularity=first)
    two = await world.panel(now() - 2 * DAY, allow_symbols=['XXX'], granularity=second)
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])

    await manager(clock).tick()

    for seeded in (one, two):
        assert kinds(await world.transitions(seeded)) == [('vx', 'ENTERED'), ('vx', 'CREATED')], 'neither is blocked'
    bars = await world.requests(BARS)
    assert by_name(row.granularity for row in bars) == by_name([first, second])
    assert {row.asset_symbol for row in bars} == {'XXX'}


@pytest.mark.parametrize('granularity', [MINUTE, DAY_BARS], ids=lambda g: g.name)
async def test_two_panels_of_the_same_granularity_on_one_symbol_share_one_dataset_request(
    world, clock, awaited_clamp, granularity
):
    """With the clamp request finished, only the open BARS request at the same granularity holds the second panel."""
    await world.source_settings()
    one = await world.panel(now() - 3 * DAY, allow_symbols=['XXX'], granularity=granularity)
    two = await world.panel(now() - 2 * DAY, allow_symbols=['XXX'], granularity=granularity)
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])

    await manager(clock).tick()

    (bars,) = await world.requests(BARS)
    assert bars.granularity is granularity
    assert kinds(await world.transitions(one)) == [('vx', 'ENTERED'), ('vx', 'CREATED')]
    assert CREATED not in {row.transition for row in await world.transitions(two)}


# ===================================================================================== G3 the in-flight check


async def test_symbols_with_open_requests_counts_bars_only_at_their_granularity_and_corporate_actions_always(world):
    await world.request('AAA', granularity=DAY_BARS)
    await world.request('BBB', granularity=MINUTE, state=IngestRequestState.PENDING)
    await world.request('CCC', kind=CORPORATE)
    await world.request('DDD', granularity=MINUTE, dataset_id=uuid.uuid4())  # a refresh: not counted
    await world.request('EEE', granularity=MINUTE, client_label='someone')  # another label: not counted

    async with world.own_session() as db:
        assert await symbols_with_open_requests(db, SOURCE, MINUTE, PANEL_OWNER) == {'BBB', 'CCC'}
        assert await symbols_with_open_requests(db, SOURCE, DAY_BARS, PANEL_OWNER) == {'AAA', 'CCC'}
        assert await symbols_with_open_requests(db, SOURCE, HOUR_BARS, PANEL_OWNER) == {'CCC'}


async def test_a_pending_1day_bars_request_does_not_block_a_1min_panel(world, clock):
    await world.source_settings()
    seeded = await world.panel(now() - 2 * DAY, granularity=MINUTE)
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])
    await world.request('XXX', granularity=DAY_BARS, state=IngestRequestState.PENDING)

    await manager(clock).tick()

    assert kinds(await world.transitions(seeded)) == [('vx', 'ENTERED'), ('vx', 'CREATED')]
    assert by_name(row.granularity for row in await world.requests(BARS)) == by_name([DAY_BARS, MINUTE])


@pytest.mark.parametrize(
    'open_request',
    [{'kind': CORPORATE}, {'kind': BARS, 'granularity': MINUTE}],
    ids=['corporate-actions', 'bars-same-granularity'],
)
async def test_an_open_corporate_actions_or_same_granularity_bars_request_blocks_a_1min_panel(
    world, clock, open_request
):
    await world.source_settings()
    seeded = await world.panel(now() - 2 * DAY, granularity=MINUTE)
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])
    await world.request('XXX', **open_request)
    before = len(await world.requests())

    await manager(clock).tick()

    assert CREATED not in {row.transition for row in await world.transitions(seeded)}
    assert len(await world.requests()) == before and world.clamps == []


# ===================================================================================== G4 panel_entries


async def test_panel_entries_returns_only_the_asked_granularity(world):
    day_id = await world.entry('XXX', granularity=DAY_BARS, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    minute_id = await world.entry('XXX', granularity=MINUTE, created_at=datetime(2026, 2, 1, tzinfo=UTC))

    async with world.own_session() as db:
        assert (await panel_entries(db, SOURCE, MINUTE, PANEL_OWNER, {'XXX'}))['XXX'].id == minute_id
        assert (await panel_entries(db, SOURCE, DAY_BARS, PANEL_OWNER, {'XXX'}))['XXX'].id == day_id
        assert await panel_entries(db, SOURCE, HOUR_BARS, PANEL_OWNER, {'XXX'}) == {}


@pytest.mark.parametrize(
    ('entry_granularity', 'expected'),
    [(DAY_BARS, [('vx', 'ENTERED'), ('vx', 'CREATED')]), (MINUTE, [('vx', 'ENTERED')])],
    ids=['a-1day-entry-is-not-existing', 'a-1min-entry-is-existing'],
)
async def test_a_1min_panel_counts_only_a_1min_panel_entry_as_existing(world, clock, entry_granularity, expected):
    await world.source_settings()
    seeded = await world.panel(now() - 2 * DAY, granularity=MINUTE)
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])
    await world.entry('XXX', granularity=entry_granularity)

    await manager(clock).tick()

    assert kinds(await world.transitions(seeded)) == expected


# ===================================================================================== G5 exits and the held set


async def _member(world, seeded, snapshot_id, granularity, entry_id=None):
    """The panel's member XXX with a minted entry at its granularity (reused when given), from an earlier pass."""
    entry_id = entry_id or await world.entry('XXX', granularity=granularity)
    request_id = await world.request(
        'XXX',
        granularity=granularity,
        state=IngestRequestState.SUCCEEDED,
        dataset_id=entry_id,
        finished_at=now() - 3 * DAY,
    )
    as_of = await snapshot_as_of(world, snapshot_id)
    rows = [
        NewTransition('vx', 'XXX', ENTERED, as_of),
        NewTransition('vx', 'XXX', CREATED, as_of, request_id=request_id),
    ]
    await world.ledger_pass(seeded, snapshot_id, rows, started_at=now() - 3 * DAY)
    return entry_id


@pytest.mark.parametrize(
    ('exiting', 'other', 'held'),
    [(MINUTE, MINUTE, True), (MINUTE, DAY_BARS, False), (DAY_BARS, MINUTE, False), (DAY_BARS, DAY_BARS, True)],
    ids=['1min-held-by-1min', '1min-not-held-by-1day', '1day-not-held-by-1min', '1day-held-by-1day'],
)
async def test_a_filter_exit_is_held_only_by_an_enabled_panel_of_the_same_granularity(
    world, clock, exiting, other, held
):
    await world.source_settings()
    first = await world.panel(now() - 3 * DAY, granularity=exiting)
    second = await world.panel(now() - 2 * DAY, granularity=other)
    old = await world.snapshot(now() - 3 * DAY, [('vx', 'XXX')])
    await world.snapshot(now() - HOUR, [('vx', 'XXX')])
    exiting_entry = await _member(world, first, old, exiting)
    other_entry = await _member(world, second, old, other, exiting_entry if other is exiting else None)

    await world.settings(first.panel_id, deny_symbols=['XXX'])
    await world.age_window()
    await manager(clock).tick()

    assert kinds(await world.transitions(first))[-1] == ('vx', 'EXITED')
    stopped = await world.entry_row(exiting_entry)
    assert (stopped.collect, stopped.stopped_cause) == ((True, None) if held else (False, LEFT))
    # The other panel's dataset is never stopped by this exit, whatever its granularity.
    assert (await world.entry_row(other_entry)).collect is True
    assert EXITED not in {row.transition for row in await world.transitions(second)}
