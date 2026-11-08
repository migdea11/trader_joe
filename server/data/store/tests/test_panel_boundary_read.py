"""The panel ledger's reads (tj-7zjov1.177, P20c), over SQLite: latest_state, members_at, created_in_window, boundary_read.

THE DESIGN: the P20c bead (BUILD (4) to (6)), the architect notes of 14:02 UTC 2026-10-27 on it and on P22 tj-7zjov1.34
(as_of is the snapshot instant the change became true; never by pass alone; members_at answers membership, ENTERED through EXITED or DRAINED; CREATED, DEFERRED and REFUSED are process
facts), the 14:16 UTC rate design on tj-7zjov1.33 (kind; effective-from is the first non-dry-run pass of EITHER kind
naming a version; created_in_window counts CREATED on the source's panels with recorded_at at or after since), ADR
tj-26jt9w addendum 20, the Q1 and Q10 defaults (source versions read per panel; dry-run passes ignored by every read),
and the architect's RULING of 01:26 UTC 2026-10-30 on tj-7zjov1.177: newest is by as_of, then the pass's started_at,
then pass_id, then seq (the per-pass append ordinal), never by the random id.

  R1 latest_state: per vendor asset id the newest transition by as_of, then started_at, then pass_id, then seq; both
     pass kinds count, so a RELEASE's CREATED supersedes the DEFERRED it released; dry-run passes and other panels are
     ignored; STRICT within one pass: a new member's ENTERED then CREATED (or DEFERRED), equal in every other key, reads
     back CREATED (DEFERRED) for EVERY member, whatever the ids (the escalation of tj-7zjov1.179);
  R2 members_at: ENTERED, EXITED, DRAINED decide, newest wins at or before the instant (inclusive); DRAINING stays a
     member until DRAINED; CREATED, DEFERRED, REFUSED neither make nor end a member; across a settings change; dry-run
     ignored; a naive instant refused; ordered by vendor asset id;
  R3 created_in_window: CREATED only, the source's panels only, recorded_at >= since (inclusive), dry-run ignored;
  R4 boundary_read: every settings version of the panel with its effective pass (first non-dry-run pass of either kind,
     None when none), actor, reason, saved_at; every source settings version of the panel's source, effective by THIS
     panel's passes; the SET_COLLECTING override rows, with new_value, on datasets the non-dry-run ledger names; empty
     for an unknown panel.
"""

import random
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from data.store.app.database.crud.panel_ledger import (
    BoundaryRead,
    CollectStop,
    MemberAt,
    NewTransition,
    boundary_read,
    created_in_window,
    latest_state,
    members_at,
)
from data.store.app.database.models.dataset_override_log import OverrideAction
from data.store.app.database.models.panel import PanelCadence, PanelPassKind, PanelTransition
from data.store.tests.test_panel_ledger import (  # noqa: F401 (aware_sqlite_datetimes and ledger_world are fixtures)
    OTHER_SOURCE,
    OVERRIDES,
    PASSES,
    SOURCE,
    T0,
    TRANSITIONS,
    World,
    aware_sqlite_datetimes,
    created,
    entered,
    transition,
)
from data.store.tests.test_panel_ledger import world as ledger_world  # noqa: F401 (a fixture, by this name)


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]


@pytest.fixture
def world(request: pytest.FixtureRequest) -> World:
    """test_panel_ledger.py's world (the fixture ledger_world here): the six tables on SQLite, the recording session."""
    return request.getfixturevalue('ledger_world')


DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
E, X, C, D, R = (
    PanelTransition.ENTERED,
    PanelTransition.EXITED,
    PanelTransition.CREATED,
    PanelTransition.DEFERRED,
    PanelTransition.REFUSED,
)
DRAINING, DRAINED = PanelTransition.DRAINING, PanelTransition.DRAINED


async def _state(world: World, panel_id: uuid.UUID) -> dict[str, PanelTransition]:
    async with world.session() as db:
        return {vendor: row.transition for vendor, row in (await latest_state(db, panel_id)).items()}


async def _members(world: World, panel_id: uuid.UUID, instant: datetime) -> list[str]:
    async with world.session() as db:
        return [member.vendor_asset_id for member in await members_at(db, panel_id, instant)]


def ordered(n: int) -> uuid.UUID:
    """An id that sorts by n (the same order in SQLite's hex text and in Postgres's uuid bytes)."""
    return uuid.UUID(f'a{n:031x}')


async def _transition_rows(world: World, *rows: dict) -> None:
    """Rows written with chosen ids and seqs (test set-up): the ids are aimed AGAINST the seq order."""
    async with AsyncSession(world.engine) as db:
        await db.execute(sa.insert(TRANSITIONS), list(rows))
        await db.commit()


# --------------------------------------------------------------------------------------------- R1 latest_state


async def test_latest_state_is_empty_for_a_panel_with_no_transitions(world):
    seeded = await world.panel()
    assert await _state(world, seeded.panel_id) == {}
    assert await _state(world, uuid.uuid4()) == {}


async def test_latest_state_orders_by_as_of_before_the_pass(world):
    """A later pass whose transition is backdated to an earlier snapshot does not supersede a newer as_of."""
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(entered('a', T0 + 2 * DAY),))
    await world.open(seeded, started_at=T0 + 10 * DAY, rows=(transition('a', X, T0 + DAY),))
    await world.open(seeded, started_at=T0 + 11 * DAY, rows=(transition('b', X, T0 + 3 * DAY),))
    await world.open(seeded, started_at=T0 + 1 * DAY, rows=(entered('b', T0 + 4 * DAY),))

    assert await _state(world, seeded.panel_id) == {'a': E, 'b': E}


async def test_latest_state_breaks_an_as_of_tie_by_the_later_pass(world):
    seeded = await world.panel()
    await world.open(seeded, started_at=T0 + 5 * HOUR, rows=(transition('a', X, T0),))
    await world.open(seeded, started_at=T0 + HOUR, rows=(entered('a', T0),))
    assert await _state(world, seeded.panel_id) == {'a': X}


async def test_a_release_created_supersedes_the_deferred_it_released(world):
    seeded = await world.panel()
    deciding = await world.open(
        seeded, started_at=T0, rows=(entered('a', T0 - DAY), transition('a', D, T0, cause='ramp_rate'))
    )
    assert await _state(world, seeded.panel_id) == {'a': D}

    release = await world.open(seeded, kind=PanelPassKind.RELEASE, started_at=T0 + HOUR, rows=(created('a', T0),))

    async with world.session() as db:
        state = await latest_state(db, seeded.panel_id)
    assert state['a'].transition is C and state['a'].pass_id == release and state['a'].pass_id != deciding
    assert state['a'].pass_kind is PanelPassKind.RELEASE and state['a'].pass_started_at == T0 + HOUR


async def test_latest_state_breaks_a_same_pass_tie_by_the_greater_seq_never_the_id(world):
    """Equal as_of, one pass: the later append (greater seq) wins, and the id, aimed the other way, never decides."""
    seeded = await world.panel()
    pass_id = await world.open(seeded, started_at=T0)
    base = {'pass_id': pass_id, 'panel_id': seeded.panel_id, 'symbol': 'A', 'as_of': T0}
    await _transition_rows(
        world,
        base | {'id': ordered(2), 'seq': 1, 'vendor_asset_id': 'a', 'transition': E},
        base | {'id': ordered(1), 'seq': 2, 'vendor_asset_id': 'a', 'transition': X},
        base | {'id': ordered(4), 'seq': 3, 'vendor_asset_id': 'b', 'transition': X},
        base | {'id': ordered(3), 'seq': 4, 'vendor_asset_id': 'b', 'transition': E},
    )
    assert await _state(world, seeded.panel_id) == {'a': X, 'b': E}
    assert await _members(world, seeded.panel_id, T0) == ['b']


async def test_two_passes_with_one_started_at_are_split_by_pass_id_before_seq(world):
    """Seq is pass-local: across two passes sharing a started_at the greater pass_id wins whatever the seqs say."""
    seeded = await world.panel()
    first = await world.open(seeded, started_at=T0)
    second = await world.open(seeded, started_at=T0)
    low, high = sorted((first, second))
    base = {'panel_id': seeded.panel_id, 'symbol': 'A', 'as_of': T0}
    await _transition_rows(
        world,
        base | {'pass_id': high, 'id': ordered(1), 'seq': 1, 'vendor_asset_id': 'a', 'transition': E},
        base | {'pass_id': low, 'id': ordered(2), 'seq': 9, 'vendor_asset_id': 'a', 'transition': X},
        base | {'pass_id': low, 'id': ordered(3), 'seq': 5, 'vendor_asset_id': 'b', 'transition': E},
        base | {'pass_id': high, 'id': ordered(4), 'seq': 2, 'vendor_asset_id': 'b', 'transition': X},
    )
    async with world.session() as db:
        state = await latest_state(db, seeded.panel_id)
    assert {v: (r.transition, r.pass_id) for v, r in state.items()} == {'a': (E, high), 'b': (X, high)}
    assert await _members(world, seeded.panel_id, T0) == ['a']


N_STRICT = 40


@pytest.mark.parametrize('shuffle', range(5))
async def test_strict_same_pass_entered_then_created_or_deferred_reads_the_later_for_every_member(world, shuffle):
    """THE STRICT PIN (tj-7zjov1.179's escalation, ruled 2026-10-30): all N, never a rate.

    One pass, one as_of: N new members written ENTERED then CREATED, N written ENTERED then DEFERRED, interleaved in a
    shuffled member order with random vendor ids. Under the old order (the random id) about half read back ENTERED, so
    P22 would create them again and a RELEASE would miss them. Every one must read CREATED or DEFERRED. members_at, which
    reads ENTERED, EXITED and DRAINED only, has every one a member.
    """
    rng = random.Random(shuffle)  # nosec B311 -- a test shuffle, not a secret
    creating = [f'n-{uuid.UUID(int=rng.getrandbits(128))}' for _ in range(N_STRICT)]
    deferring = [f'd-{uuid.UUID(int=rng.getrandbits(128))}' for _ in range(N_STRICT)]
    pairs = [(v, created(v, T0)) for v in creating] + [(v, transition(v, D, T0, cause='ramp_rate')) for v in deferring]
    rng.shuffle(pairs)
    rows = tuple(row for vendor, second in pairs for row in (entered(vendor, T0), second))
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=rows)

    state = await _state(world, seeded.panel_id)
    wrong = sorted(v for v in creating if state[v] is not C) + sorted(v for v in deferring if state[v] is not D)
    assert wrong == [], f'{len(wrong)} of {2 * N_STRICT} members read back their ENTERED'
    assert sorted(await _members(world, seeded.panel_id, T0)) == sorted(creating + deferring)


async def test_strict_same_pass_pin_holds_with_every_id_aimed_against_the_append_order(world):
    """The deterministic half: the ids rewritten so ENTERED holds the GREATER id of each pair (id order is wrong)."""
    seeded = await world.panel()
    rows = []
    for index in range(N_STRICT):
        rows += [entered(f'n{index}', T0), created(f'n{index}', T0)]
        rows += [entered(f'd{index}', T0), transition(f'd{index}', D, T0, cause='ramp_rate')]
    pass_id = await world.open(seeded, started_at=T0, rows=tuple(rows))
    stored = sorted(await world.rows(TRANSITIONS, TRANSITIONS.c.pass_id == pass_id), key=lambda r: r.seq)
    assert [r.seq for r in stored] == list(range(1, 4 * N_STRICT + 1))
    total = len(stored)
    for row in stored:  # the later the append, the smaller the id
        await world.put(TRANSITIONS.update().where(TRANSITIONS.c.id == row.id).values(id=ordered(total - row.seq)))

    state = await _state(world, seeded.panel_id)
    assert {state[f'n{i}'] for i in range(N_STRICT)} == {C}
    assert {state[f'd{i}'] for i in range(N_STRICT)} == {D}


async def test_a_same_pass_exit_after_an_entry_ends_membership_and_an_entry_after_an_exit_starts_it(world):
    """members_at takes the same order: within one pass and one as_of the later append decides."""
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(entered('a'), transition('a', X), transition('b', X), entered('b')))
    assert await _members(world, seeded.panel_id, T0) == ['b']
    assert await _state(world, seeded.panel_id) == {'a': X, 'b': E}


async def test_latest_state_ignores_dry_run_passes(world):
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(entered('a', T0),))
    await world.open(seeded, dry_run=True, started_at=T0 + DAY, rows=(transition('a', X, T0 + DAY), entered('z')))
    assert await _state(world, seeded.panel_id) == {'a': E}


async def test_latest_state_reads_one_panel_only(world):
    mine = await world.panel()
    theirs = await world.panel()
    await world.open(mine, rows=(entered('a'),))
    await world.open(theirs, started_at=T0 + DAY, rows=(transition('a', X, T0 + DAY), entered('b')))
    assert await _state(world, mine.panel_id) == {'a': E}


async def test_latest_state_returns_the_whole_row(world):
    seeded = await world.panel()
    dataset_id = uuid.uuid4()
    pass_id = await world.open(seeded, started_at=T0 + HOUR, rows=(created('a', T0, dataset_id=dataset_id),))
    async with world.session() as db:
        row = (await latest_state(db, seeded.panel_id))['a']
    assert (row.pass_id, row.pass_kind, row.pass_started_at) == (pass_id, PanelPassKind.MEMBERSHIP, T0 + HOUR)
    assert (row.vendor_asset_id, row.symbol, row.transition, row.dataset_id) == ('a', 'A', C, dataset_id)
    assert row.request_id is not None and row.as_of == T0 and row.recorded_at is not None and row.cause is None


# --------------------------------------------------------------------------------------------- R2 members_at


async def test_members_at_follows_entered_exited_and_re_entered_by_as_of(world):
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(entered('a', T0), entered('b', T0)))
    await world.open(seeded, started_at=T0 + 2 * DAY, rows=(transition('a', X, T0 + 2 * DAY),))
    await world.open(seeded, started_at=T0 + 4 * DAY, rows=(entered('a', T0 + 4 * DAY),))

    assert await _members(world, seeded.panel_id, T0 - HOUR) == []
    assert await _members(world, seeded.panel_id, T0) == ['a', 'b'], 'as_of equal to the instant counts'
    assert await _members(world, seeded.panel_id, T0 + 2 * DAY - HOUR) == ['a', 'b']
    assert await _members(world, seeded.panel_id, T0 + 2 * DAY) == ['b']
    assert await _members(world, seeded.panel_id, T0 + 3 * DAY) == ['b']
    assert await _members(world, seeded.panel_id, T0 + 4 * DAY) == ['a', 'b']


async def test_a_draining_member_stays_a_member_until_drained(world):
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(entered('a', T0),))
    await world.open(seeded, started_at=T0 + DAY, rows=(transition('a', DRAINING, T0 + DAY, cause='delisted'),))
    await world.open(seeded, started_at=T0 + 3 * DAY, rows=(transition('a', DRAINED, T0 + 3 * DAY),))

    assert await _members(world, seeded.panel_id, T0 + DAY) == ['a'], 'DRAINING does not end membership'
    assert await _members(world, seeded.panel_id, T0 + 2 * DAY) == ['a']
    assert await _members(world, seeded.panel_id, T0 + 3 * DAY) == [], 'DRAINED does'


@pytest.mark.parametrize('process', [C, D, R, DRAINING])
async def test_a_process_fact_neither_makes_nor_ends_a_member(world, process):
    seeded = await world.panel()
    fact = created('p', T0 + DAY) if process is C else transition('p', process, T0 + DAY, cause='c')
    await world.open(seeded, started_at=T0, rows=(entered('a', T0),))
    await world.open(
        seeded,
        started_at=T0 + DAY,
        rows=(created('a', T0 + DAY) if process is C else transition('a', process, T0 + DAY, cause='c'), fact),
    )
    assert await _members(world, seeded.panel_id, T0 + 2 * DAY) == ['a']


async def test_members_at_orders_by_as_of_not_by_pass(world):
    """A later pass that backdates an ENTERED before an earlier pass's EXITED does not make the member current."""
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(transition('a', X, T0 + 2 * DAY),))
    await world.open(seeded, started_at=T0 + 9 * DAY, rows=(entered('a', T0 + DAY),))
    assert await _members(world, seeded.panel_id, T0 + DAY) == ['a']
    assert await _members(world, seeded.panel_id, T0 + 5 * DAY) == []


async def test_members_at_across_a_settings_change(world):
    """A new settings version narrows the panel at a pass: members before it are the old set, after it the new one."""
    seeded = await world.panel()
    v2 = await world.settings_version(seeded.panel_id, size=1)
    await world.open(seeded, started_at=T0, rows=(entered('a', T0), entered('b', T0)))
    await world.open(seeded, started_at=T0 + 5 * DAY, settings_id=v2, rows=(transition('b', X, T0 + 5 * DAY),))
    assert await _members(world, seeded.panel_id, T0 + 4 * DAY) == ['a', 'b']
    assert await _members(world, seeded.panel_id, T0 + 5 * DAY) == ['a']


async def test_members_at_ignores_dry_run_passes(world):
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(entered('a', T0),))
    await world.open(seeded, dry_run=True, started_at=T0 + DAY, rows=(transition('a', X, T0 + DAY), entered('z', T0)))
    assert await _members(world, seeded.panel_id, T0 + 2 * DAY) == ['a']


async def test_members_at_reads_one_panel_and_orders_by_vendor_asset_id(world):
    mine = await world.panel()
    theirs = await world.panel()
    await world.open(mine, rows=(entered('c'), entered('a'), entered('b')))
    await world.open(theirs, rows=(entered('d'),))
    assert await _members(world, mine.panel_id, T0) == ['a', 'b', 'c']


async def test_members_at_returns_the_entered_symbol_and_since(world):
    seeded = await world.panel()
    await world.open(seeded, started_at=T0, rows=(entered('a', T0 - DAY, symbol='OLD'),))
    async with world.session() as db:
        (member,) = await members_at(db, seeded.panel_id, T0)
    assert member == MemberAt(vendor_asset_id='a', symbol='OLD', member_since=T0 - DAY)


async def test_members_at_refuses_a_naive_instant(world):
    seeded = await world.panel()
    async with world.session() as db:
        with pytest.raises(ValueError, match='instant'):
            await members_at(db, seeded.panel_id, datetime(2026, 10, 5))


async def test_members_at_compares_instants_not_wall_clocks(world):
    seeded = await world.panel()
    await world.open(seeded, rows=(entered('a', T0),))
    eastern = T0.astimezone(ZoneInfo('America/New_York'))
    assert await _members(world, seeded.panel_id, eastern) == ['a']
    assert await _members(world, seeded.panel_id, eastern - timedelta(microseconds=1)) == []


# --------------------------------------------------------------------------------------------- R3 created_in_window


async def _recorded(world: World, pass_id: uuid.UUID, at: datetime) -> None:
    await world.put(sa.update(TRANSITIONS).where(TRANSITIONS.c.pass_id == pass_id).values(recorded_at=at))


async def _window(world: World, source, since: datetime) -> int:
    async with world.session() as db:
        return await created_in_window(db, source, since)


async def test_created_in_window_counts_created_at_or_after_since(world):
    seeded = await world.panel()
    since = T0
    at_since = await world.open(seeded, rows=(created('a'), created('b'), entered('a')))
    inside = await world.open(seeded, rows=(created('c'),))
    before = await world.open(seeded, rows=(created('d'),))
    await _recorded(world, at_since, since)
    await _recorded(world, inside, since + 59 * timedelta(minutes=1))
    await _recorded(world, before, since - timedelta(microseconds=1))

    assert await _window(world, SOURCE, since) == 3, 'equal to since is inside the window; ENTERED is not counted'
    assert await _window(world, SOURCE, since + timedelta(microseconds=1)) == 1
    assert await _window(world, SOURCE, since - HOUR) == 4


@pytest.mark.parametrize('kind', [E, D, R, X, DRAINING, DRAINED])
async def test_created_in_window_counts_only_created(world, kind):
    seeded = await world.panel()
    pass_id = await world.open(seeded, rows=(transition('a', kind),))
    await _recorded(world, pass_id, T0)
    assert await _window(world, SOURCE, T0 - HOUR) == 0


async def test_created_in_window_counts_every_panel_of_the_source_and_no_other_source(world):
    one = await world.panel(SOURCE)
    two = await world.panel(SOURCE)
    other = await world.panel(OTHER_SOURCE)
    for seeded, count in ((one, 1), (two, 2), (other, 4)):
        pass_id = await world.open(seeded, rows=tuple(created(f'v{i}') for i in range(count)))
        await _recorded(world, pass_id, T0)
    assert await _window(world, SOURCE, T0) == 3
    assert await _window(world, OTHER_SOURCE, T0) == 4


async def test_created_in_window_ignores_dry_run_passes(world):
    seeded = await world.panel()
    real = await world.open(seeded, rows=(created('a'),))
    dry = await world.open(seeded, dry_run=True, rows=(created('b'), created('c')))
    await _recorded(world, real, T0)
    await _recorded(world, dry, T0)
    assert await _window(world, SOURCE, T0) == 1


async def test_created_in_window_reads_recorded_at_not_as_of(world):
    """as_of is the snapshot instant (backdated); the rate window is wall time."""
    seeded = await world.panel()
    pass_id = await world.open(seeded, rows=(created('a', T0 - 30 * DAY),))
    await _recorded(world, pass_id, T0)
    assert await _window(world, SOURCE, T0 - HOUR) == 1


async def test_created_in_window_refuses_a_naive_since(world):
    async with world.session() as db:
        with pytest.raises(ValueError, match='since'):
            await created_in_window(db, SOURCE, datetime(2026, 10, 5))


# --------------------------------------------------------------------------------------------- R4 boundary_read


async def test_boundary_read_of_an_unknown_panel_is_empty(world):
    async with world.session() as db:
        assert await boundary_read(db, uuid.uuid4()) == BoundaryRead((), (), ())


async def test_boundary_read_derives_each_settings_versions_effective_pass(world):
    seeded = await world.panel()
    v1 = seeded.settings_id
    v2 = await world.settings_version(seeded.panel_id, enabled=False, actor='operator', reason='pause it')
    v3 = await world.settings_version(seeded.panel_id, reason='never run')
    await world.open(seeded, dry_run=True, started_at=T0 - 5 * DAY)  # dry run names v1 first: not effective
    first_v1 = await world.open(seeded, started_at=T0)
    await world.open(seeded, started_at=T0 + DAY)
    await world.open(seeded, dry_run=True, started_at=T0 + 2 * DAY, settings_id=v3)  # v3 only ever dry-run
    first_v2 = await world.open(seeded, kind=PanelPassKind.RELEASE, started_at=T0 + 3 * DAY, settings_id=v2)
    await world.open(seeded, started_at=T0 + 4 * DAY, settings_id=v2)

    async with world.session() as db:
        read = await boundary_read(db, seeded.panel_id)

    versions = read.settings_versions
    assert [(b.version_id, b.version_no) for b in versions] == [(v1, 1), (v2, 2), (v3, 3)]
    assert (versions[0].effective_pass_id, versions[0].effective_from) == (first_v1, T0)
    assert (versions[1].effective_pass_id, versions[1].effective_from) == (first_v2, T0 + 3 * DAY), 'either kind'
    assert (versions[2].effective_pass_id, versions[2].effective_from) == (None, None), 'a dry run never'
    assert (versions[1].enabled, versions[1].actor, versions[1].reason) == (False, 'operator', 'pause it')
    assert all(b.saved_at is not None for b in versions)
    assert (versions[0].cadence, versions[0].size) == (PanelCadence.DAILY, 10)


async def test_boundary_read_reads_another_panels_versions_never(world):
    mine = await world.panel()
    theirs = await world.panel()
    await world.open(theirs, started_at=T0)
    async with world.session() as db:
        read = await boundary_read(db, mine.panel_id)
    assert [b.version_id for b in read.settings_versions] == [mine.settings_id]
    assert read.settings_versions[0].effective_from is None


async def test_boundary_read_lists_the_sources_versions_effective_by_this_panels_passes(world):
    mine = await world.panel(SOURCE)  # writes SOURCE version 1
    neighbour = await world.panel(SOURCE)  # writes SOURCE version 2
    s3 = await world.source_version(SOURCE, ramp_datasets_per_hour=0, reason='pause creation')
    other_source = await world.panel(OTHER_SOURCE)
    await world.open(neighbour, started_at=T0 - DAY, source_settings_id=mine.source_settings_id)
    await world.open(mine, dry_run=True, started_at=T0, source_settings_id=s3)
    mine_s1 = await world.open(mine, started_at=T0 + DAY)
    mine_s3 = await world.open(mine, started_at=T0 + 2 * DAY, source_settings_id=s3)

    async with world.session() as db:
        read = await boundary_read(db, mine.panel_id)

    rows = read.source_settings_versions
    assert [b.version_id for b in rows] == [mine.source_settings_id, neighbour.source_settings_id, s3]
    assert other_source.source_settings_id not in {b.version_id for b in rows}
    assert [b.version_no for b in rows] == [1, 2, 3]
    assert (rows[0].effective_pass_id, rows[0].effective_from) == (mine_s1, T0 + DAY), (
        "the neighbour's pass is not ours"
    )
    assert (rows[1].effective_pass_id, rows[1].effective_from) == (None, None)
    assert (rows[2].effective_pass_id, rows[2].effective_from) == (mine_s3, T0 + 2 * DAY)
    assert (rows[2].ramp_datasets_per_hour, rows[2].request_cap_per_hour, rows[2].reason) == (0, 60, 'pause creation')
    assert (rows[2].corporate_action_window_pre_days, rows[2].corporate_action_window_post_days) == (90, 90)
    assert rows[2].actor == 'operator' and rows[2].saved_at is not None


async def _override(world: World, dataset_id: uuid.UUID, action, new_value, at: datetime, row_id=None) -> uuid.UUID:
    values = {
        'actor': 'operator',
        'dataset_id': dataset_id,
        'owner': 'someone-else',
        'action': action,
        'reason': f'{action} {new_value}',
        'at': at,
        'new_value': new_value,
    }
    if row_id is not None:
        values['id'] = row_id
    return (await world.put(sa.insert(OVERRIDES).values(**values).returning(OVERRIDES.c.id))).scalar_one()


async def test_boundary_read_lists_the_collect_stops_on_datasets_the_ledger_names(world):
    seeded = await world.panel()
    theirs = await world.panel()
    named, released, only_dry, foreign, unnamed = (uuid.uuid4() for _ in range(5))
    await world.open(seeded, rows=(entered('a'), created('a', dataset_id=named)))
    await world.open(seeded, kind=PanelPassKind.RELEASE, rows=(created('b', dataset_id=released),))
    await world.open(seeded, dry_run=True, rows=(created('c', dataset_id=only_dry),))
    await world.open(theirs, rows=(created('d', dataset_id=foreign),))

    await _override(world, named, OverrideAction.SET_COLLECTING, 'false', T0 + DAY)
    await _override(world, named, OverrideAction.SET_COLLECTING, 'true', T0 + 2 * DAY)
    # Two stops at one instant: inserted high id first, read back by (at, id).
    await _override(world, released, OverrideAction.SET_COLLECTING, 'false', T0, row_id=ordered(2))
    await _override(world, released, OverrideAction.SET_COLLECTING, None, T0, row_id=ordered(1))
    await _override(world, named, OverrideAction.SET_EXPIRY, '2027-01-01T00:00:00+00:00', T0 + 3 * DAY)
    await _override(world, named, OverrideAction.DELETE, None, T0 + 4 * DAY)
    for dataset in (only_dry, foreign, unnamed):
        await _override(world, dataset, OverrideAction.SET_COLLECTING, 'false', T0)

    async with world.session() as db:
        read = await boundary_read(db, seeded.panel_id)

    assert read.collect_stops == (
        CollectStop(released, None, 'operator', 'SET_COLLECTING None', T0),
        CollectStop(released, 'false', 'operator', 'SET_COLLECTING false', T0),
        CollectStop(named, 'false', 'operator', 'SET_COLLECTING false', T0 + DAY),
        CollectStop(named, 'true', 'operator', 'SET_COLLECTING true', T0 + 2 * DAY),
    )


async def test_boundary_read_with_no_ledger_has_versions_and_no_stops(world):
    seeded = await world.panel()
    async with world.session() as db:
        read = await boundary_read(db, seeded.panel_id)
    assert len(read.settings_versions) == 1 and len(read.source_settings_versions) == 1
    assert read.collect_stops == ()
    assert isinstance(read, BoundaryRead) and read.settings_versions[0].effective_pass_id is None


async def test_reads_take_new_transition_rows_of_every_kind(world):
    """A smoke over the seven kinds: every one round-trips through append and latest_state."""
    seeded = await world.panel()
    rows = tuple(
        created(f'v{i}') if kind is C else NewTransition(f'v{i}', f'V{i}', kind, T0)
        for i, kind in enumerate(PanelTransition)
    )
    await world.open(seeded, rows=rows)
    assert await _state(world, seeded.panel_id) == {f'v{i}': kind for i, kind in enumerate(PanelTransition)}
    assert len(await world.rows(PASSES)) == 1
