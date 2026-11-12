"""The panel ledger repository (tj-7zjov1.177, P20c) on the real, migrated Postgres, through asyncpg.

WRITTEN, NOT YET RUN where it was written (validator tj-7zjov1.179): it runs with the rest of the suite (make
test-system, or the agent-stack run_system_tests after test_up) on a stack migrated to the alpha baseline (ADR
tj-kg521d).

THE DESIGN: the P20c bead and its architect notes (14:02 and 14:16 UTC 2026-10-27, 03:26 UTC 2026-10-28), ADR
tj-26jt9w addendum 20, and the RULING of 01:26 UTC 2026-10-30 on tj-7zjov1.177 (P20c-fix tj-7zjov1.207, these pins
tj-7zjov1.208): seq, the per-pass append ordinal, unique (pass_id, seq); newest by as_of, started_at, pass_id, seq; a
finished pass takes no rows; finish_pass raises on an unknown pass. WHAT THIS PROVES that server/data/store/tests/test_panel_ledger.py and
test_panel_boundary_read.py cannot, because SQLite has no JSONB, no Postgres enums, no row locks and other window,
timestamp and IN-subquery semantics:
  PL1 a full round runs on asyncpg: open_pass of both kinds (panel_pass_kind cast), append_transitions of all seven
      kinds (panel_transition cast) in more than one ROW_CHUNK statement, finish_pass writing JSONB counts once (the
      UPDATE's rowcount is right on asyncpg), and the caller's rollback leaves nothing;
  PL2 latest_state and members_at (row_number() OVER a join) run and order by as_of, then started_at, then pass_id,
      then seq; STRICT within one pass: ENTERED then CREATED (or DEFERRED) reads the later for EVERY member;
  PL3 created_in_window reads recorded_at, the DATABASE's clock: since at or before the write's now() counts it, since
      after it does not;
  PL4 boundary_read runs (the DISTINCT IN-subquery, the per-version first pass) with real override-log rows, its
      named set the UNION of the datasets transitions name directly and the non-NULL ingest_request.dataset_id of
      the requests the panel's non-dry-run CREATED rows name, joined on real Postgres (P20d tj-7zjov1.216, ruling
      of 2026-10-31 on tj-7zjov1.34, option D, ADR tj-26jt9w addendum 21; these pins tj-7zjov1.221);
  PL8 a CREATED row names its request only: one naming a dataset is refused before any write, and the CREATED row
      written on asyncpg stores the request id it named and a NULL dataset id;
  PL5 two concurrent finish_pass calls on one pass: exactly one returns True (the guard re-checked under the row lock);
  PL6 seq on asyncpg: 1..n in list order across real ROW_CHUNK statements and across calls; a finished pass refuses
      rows; finish_pass on an unknown pass raises;
  PL7 two concurrent append_transitions into one pass: each reads the same max(seq) (the other's rows are uncommitted
      under READ COMMITTED), so the second INSERT waits on the first's unique-index entry and, once that commits, fails
      with an IntegrityError naming uq_panel_member_transition_pass_id_seq; its whole transaction is lost, the first's
      rows stand. That is the designed guard (one manager writes a pass), not a lock.

ISOLATION. Panel names carry the run id; the source is MANUAL_ENTRY, which nothing else in the suite gives panels to
except test_panel_settings_pg.py, and counts read on it are taken RELATIVE to a reading at the test's start. Each test
deletes exactly the rows it wrote, failed or not (a test-only delete: the module itself is append-only).

REQUESTS. A CREATED row names its bars request and no dataset. The ledger has no foreign key to ingest_request
(test_store_model_invariants.py and the baseline catalogue test pin that), so a CREATED row that only needs SOME request id names a fresh uuid with no
row behind it. Where the dataset matters (PL4) the test writes the ingest_request row itself, BARS on SOURCE, in a
FINISHED state only (SUCCEEDED once the intake set its dataset, FAILED while it is NULL), so the instance watcher,
which re-sends open requests, never picks one up; the test deletes it at its end.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from common.enums.data_stock import DataSource, Granularity
from data.store.app.database.crud import panel_ledger
from data.store.app.database.crud.panel_ledger import (
    COUNT_KEYS,
    NewTransition,
    PanelPassMismatch,
    append_transitions,
    boundary_read,
    created_in_window,
    finish_pass,
    latest_state,
    members_at,
    open_pass,
)
from data.store.app.database.crud.panel_settings import (
    PanelSettings,
    SourceSettings,
    append_source_settings_version,
    create_panel,
)
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelMemberTransition,
    PanelPass,
    PanelPassKind,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
    PanelTransition,
)


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
OVERRIDES = DatasetOverrideLog.__table__
REQUESTS = IngestRequest.__table__
SOURCE = DataSource.MANUAL_ENTRY
T0 = datetime(2026, 10, 5, 20, tzinfo=UTC)
DAY = timedelta(days=1)


class Written:
    """The ids one test wrote, deleted at its end."""

    def __init__(self) -> None:
        self.panels: list[uuid.UUID] = []
        self.source_versions: list[uuid.UUID] = []
        self.datasets: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []


@pytest.fixture
def written(pg_engine: Engine) -> Iterator[Written]:
    ids = Written()
    yield ids
    with pg_engine.begin() as conn:
        if ids.requests:
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(ids.requests)))
        if ids.datasets:
            conn.execute(sa.delete(OVERRIDES).where(OVERRIDES.c.dataset_id.in_(ids.datasets)))
        if ids.panels:
            conn.execute(sa.delete(TRANSITIONS).where(TRANSITIONS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PASSES).where(PASSES.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(SETTINGS).where(SETTINGS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PANELS).where(PANELS.c.id.in_(ids.panels)))
        if ids.source_versions:
            conn.execute(sa.delete(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id.in_(ids.source_versions)))


@pytest_asyncio.fixture
async def panel(pg_async_engine: AsyncEngine, written: Written, run_identity):
    """One panel on SOURCE with version 1, on a new source settings version of ours, through P20b's crud.

    The source version is its own write FIRST (append_source_settings_version) and the panel is created WITHOUT source
    settings: since tj-7zjov1.209 create_panel refuses them for a source that already has a version, so the fixture
    works whatever versions SOURCE already holds. The cleanup (written) removes both.
    """
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        source_version = await append_source_settings_version(
            db,
            source=SOURCE,
            settings=SourceSettings(ramp_datasets_per_hour=5, request_cap_per_hour=60),
            actor='operator',
            reason=f'ledger system test {uuid.uuid4().hex[:8]}',
        )
    written.source_versions.append(source_version.id)
    async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
        record, version = await create_panel(
            db,
            name=f'system-test-{run_identity.run_id}-ledger-{uuid.uuid4().hex[:8]}',
            source=SOURCE,
            granularity=Granularity.ONE_DAY,
            settings=PanelSettings(enabled=True, cadence=PanelCadence.DAILY),
            actor='operator',
            reason='ledger system test panel',
        )
    written.panels.append(record.id)
    return record.id, version.id, source_version.id


async def _open(db: AsyncSession, panel, *, kind=PanelPassKind.MEMBERSHIP, dry_run=False) -> uuid.UUID:
    panel_id, settings_id, source_settings_id = panel
    return await open_pass(
        db,
        panel_id=panel_id,
        kind=kind,
        settings_version_id=settings_id,
        source_settings_version_id=source_settings_id,
        active_snapshot_id=uuid.uuid4(),
        inactive_snapshot_id=uuid.uuid4(),
        dry_run=dry_run,
    )


def _row(
    vendor: str,
    kind: PanelTransition,
    as_of: datetime = T0,
    *,
    request_id: uuid.UUID | None = None,
    dataset_id: uuid.UUID | None = None,
):
    """A transition of kind. CREATED names a request (a fresh id unless given) and never a dataset (P20d).

    dataset_id is for the kinds that still name one directly (EXITED, DRAINING, DRAINED); it is refused on CREATED.
    """
    if kind is PanelTransition.CREATED:
        request_id = request_id or uuid.uuid4()
    return NewTransition(
        vendor,
        vendor.upper(),
        kind,
        as_of,
        dataset_id=dataset_id,
        request_id=request_id,
        cause='c' if kind is PanelTransition.DEFERRED else None,
    )


async def _request(
    db: AsyncSession, written: Written, dataset_id: uuid.UUID | None, state: IngestRequestState
) -> uuid.UUID:
    """One BARS request row on SOURCE, POST-shaped, in a finished state; deleted by written at the test's end.

    dataset_id is what the intake set on the first delivered attempt, or None for a request that never had one.
    """
    request_id = (
        await db.execute(
            sa.insert(REQUESTS)
            .values(
                kind=IngestRequestKind.BARS,
                source=SOURCE,
                dataset_id=dataset_id,
                client_label='panel-ledger-system-test',
                state=state,
            )
            .returning(REQUESTS.c.id)
        )
    ).scalar_one()
    written.requests.append(request_id)
    return request_id


def _stop(dataset_id: uuid.UUID, reason: str):
    return sa.insert(OVERRIDES).values(
        actor='operator',
        dataset_id=dataset_id,
        owner='someone-else',
        action=OverrideAction.SET_COLLECTING,
        reason=reason,
        new_value='false',
    )


async def _set_started_at(engine: AsyncEngine, pass_id: uuid.UUID, at: datetime) -> None:
    async with AsyncSession(engine) as db:
        await db.execute(sa.update(PASSES).where(PASSES.c.id == pass_id).values(started_at=at))
        await db.commit()


# --------------------------------------------------------------------------------------------- PL1 the round


async def test_a_full_round_runs_on_asyncpg_and_finishes_once(pg_async_engine, panel, monkeypatch):
    monkeypatch.setattr(panel_ledger, 'ROW_CHUNK', 3)
    panel_id = panel[0]
    rows = tuple(_row(f'v{i}', kind) for i, kind in enumerate(PanelTransition))

    async with AsyncSession(pg_async_engine) as db:
        membership = await _open(db, panel)
        release = await _open(db, panel, kind=PanelPassKind.RELEASE)
        assert await append_transitions(db, membership, rows) == len(rows)
        assert await finish_pass(db, membership, {'entered': 1, 'created': 1}) is True
        assert await finish_pass(db, membership, {'entered': 9}) is False
        await db.commit()

    async with AsyncSession(pg_async_engine) as db:
        passes = {r.id: r for r in (await db.execute(sa.select(PASSES).where(PASSES.c.panel_id == panel_id))).all()}
        stored = (await db.execute(sa.select(TRANSITIONS).where(TRANSITIONS.c.pass_id == membership))).all()
        now = (await db.execute(sa.select(sa.func.now()))).scalar_one()
    assert passes[membership].kind is PanelPassKind.MEMBERSHIP and passes[release].kind is PanelPassKind.RELEASE
    assert passes[membership].counts == {key: {'entered': 1, 'created': 1}.get(key, 0) for key in COUNT_KEYS}
    assert passes[membership].finished_at is not None and passes[release].finished_at is None
    assert passes[membership].started_at <= passes[membership].finished_at <= now
    assert sorted(r.transition for r in stored) == sorted(PanelTransition)
    assert {r.panel_id for r in stored} == {panel_id} and all(r.as_of == T0 for r in stored)


async def test_a_caller_rollback_leaves_nothing_on_postgres(pg_async_engine, panel):
    panel_id = panel[0]
    async with AsyncSession(pg_async_engine) as db:
        pass_id = await _open(db, panel)
        await append_transitions(db, pass_id, (_row('a', PanelTransition.ENTERED),))
        await finish_pass(db, pass_id, {})
        await db.rollback()
    async with AsyncSession(pg_async_engine) as db:
        left = await db.execute(sa.select(sa.func.count()).select_from(PASSES).where(PASSES.c.panel_id == panel_id))
        assert left.scalar_one() == 0


async def test_a_large_batch_spans_real_row_chunks(pg_async_engine, panel):
    count = panel_ledger.ROW_CHUNK + 7
    async with AsyncSession(pg_async_engine) as db:
        pass_id = await _open(db, panel)
        rows = tuple(_row(f'bulk{count - i:05d}', PanelTransition.ENTERED) for i in range(count))  # against seq order
        assert await append_transitions(db, pass_id, rows) == count
        assert await append_transitions(db, pass_id, (_row('tail', PanelTransition.ENTERED),)) == 1
        await db.commit()
    async with AsyncSession(pg_async_engine) as db:
        assert len(await members_at(db, panel[0], T0)) == count + 1
        stored = (
            await db.execute(
                sa.select(TRANSITIONS.c.vendor_asset_id, TRANSITIONS.c.seq)
                .where(TRANSITIONS.c.pass_id == pass_id)
                .order_by(TRANSITIONS.c.seq)
            )
        ).all()
    assert [tuple(r) for r in stored] == [(row.vendor_asset_id, n) for n, row in enumerate(rows, start=1)] + [
        ('tail', count + 1)
    ], 'seq is the list order, continuous across the real chunks and the second call'


# --------------------------------------------------------------------------------------------- PL2 member reads


async def test_latest_state_and_members_at_order_by_as_of_then_pass_on_postgres(pg_async_engine, panel):
    panel_id = panel[0]
    async with AsyncSession(pg_async_engine) as db:
        first = await _open(db, panel)
        later = await _open(db, panel)
        dry = await _open(db, panel, dry_run=True)
        await append_transitions(
            db,
            first,
            (
                _row('a', PanelTransition.ENTERED, T0 + 2 * DAY),
                _row('b', PanelTransition.ENTERED, T0),
                _row('c', PanelTransition.ENTERED, T0),
            ),
        )
        await append_transitions(
            db,
            later,
            (
                _row('a', PanelTransition.EXITED, T0 + DAY),  # backdated before a's ENTERED: does not win
                _row('b', PanelTransition.EXITED, T0),  # as_of tie with b's ENTERED: the later pass wins
                _row('c', PanelTransition.DRAINING, T0 + DAY),  # stays a member
            ),
        )
        await append_transitions(db, dry, (_row('c', PanelTransition.DRAINED, T0 + DAY),))
        await db.commit()
    await _set_started_at(pg_async_engine, first, T0)
    await _set_started_at(pg_async_engine, later, T0 + 3 * DAY)

    async with AsyncSession(pg_async_engine) as db:
        state = await latest_state(db, panel_id)
        now_members = [m.vendor_asset_id for m in await members_at(db, panel_id, T0 + 5 * DAY)]
        early = [m.vendor_asset_id for m in await members_at(db, panel_id, T0 + DAY)]
    assert {k: v.transition for k, v in state.items()} == {
        'a': PanelTransition.ENTERED,
        'b': PanelTransition.EXITED,
        'c': PanelTransition.DRAINING,
    }
    assert state['b'].pass_id == later and state['b'].pass_started_at == T0 + 3 * DAY
    assert now_members == ['a', 'c'] and early == ['c']


# --------------------------------------------------------------------------------------------- PL3 the rate window


async def test_created_in_window_reads_the_database_clock(pg_async_engine, panel):
    async with AsyncSession(pg_async_engine) as db:
        before_write = (await db.execute(sa.select(sa.func.now()))).scalar_one()
        await db.rollback()
        baseline = await created_in_window(db, SOURCE, before_write)
        await db.rollback()
        pass_id = await _open(db, panel)
        dry = await _open(db, panel, dry_run=True)
        write_time = (await db.execute(sa.select(sa.func.now()))).scalar_one()
        await append_transitions(
            db, pass_id, (_row('a', PanelTransition.CREATED, T0 - 30 * DAY), _row('a', PanelTransition.ENTERED))
        )
        await append_transitions(db, dry, (_row('z', PanelTransition.CREATED),))
        await db.commit()

    async with AsyncSession(pg_async_engine) as db:
        recorded = await db.execute(sa.select(TRANSITIONS.c.recorded_at).where(TRANSITIONS.c.pass_id == pass_id))
        assert set(recorded.scalars()) == {write_time}, 'recorded_at is the transaction time'
        assert await created_in_window(db, SOURCE, before_write) == baseline + 1, 'one CREATED; dry run ignored'
        assert await created_in_window(db, SOURCE, write_time) == 1, 'equal to since is inside'
        assert await created_in_window(db, SOURCE, write_time + timedelta(microseconds=1)) == 0


# --------------------------------------------------------------------------------------------- PL4 boundary read


async def test_boundary_read_runs_with_real_override_rows(pg_async_engine, panel, written):
    """The CREATED row names a real ingest_request row; the dataset is reached only through that request (P20d)."""
    panel_id, settings_id, source_settings_id = panel
    named = uuid.uuid4()
    written.datasets.append(named)
    async with AsyncSession(pg_async_engine) as db:
        dry = await _open(db, panel, dry_run=True)
        pass_id = await _open(db, panel)
        request_id = await _request(db, written, named, IngestRequestState.SUCCEEDED)
        await append_transitions(db, pass_id, (_row('a', PanelTransition.CREATED, request_id=request_id),))
        await db.execute(_stop(named, 'stop it'))
        await db.commit()
    await _set_started_at(pg_async_engine, dry, T0 - DAY)
    await _set_started_at(pg_async_engine, pass_id, T0)

    async with AsyncSession(pg_async_engine) as db:
        stored = (await db.execute(sa.select(TRANSITIONS.c.dataset_id).where(TRANSITIONS.c.pass_id == pass_id))).all()
        read = await boundary_read(db, panel_id)
    assert [r.dataset_id for r in stored] == [None], 'the ledger itself names no dataset'
    (version,) = read.settings_versions
    assert (version.version_id, version.effective_pass_id, version.effective_from) == (settings_id, pass_id, T0)
    mine = {b.version_id: b for b in read.source_settings_versions}
    assert (mine[source_settings_id].effective_pass_id, mine[source_settings_id].effective_from) == (pass_id, T0)
    assert [(s.dataset_id, s.new_value, s.reason) for s in read.collect_stops] == [(named, 'false', 'stop it')]


async def test_boundary_read_unions_direct_and_request_datasets_on_postgres(
    pg_async_engine, panel, written, run_identity
):
    """The named set on real Postgres: the UNION of the direct branch and the request join, each with its filters.

    Seven stopped datasets, three reached: by a CREATED row's request, by an EXITED row directly, and by both (the
    UNION keeps it once). Four are not: a dry-run pass's CREATED request's, a non-CREATED row's request's (the join is
    CREATED's), another panel's CREATED request's, and one no row names while a request of this panel has a NULL
    dataset (a request before its first delivered attempt contributes nothing, NULL in the IN set matching no row).
    """
    panel_id = panel[0]
    via_request, direct, both, dry_ds, via_deferred, foreign, unnamed = (uuid.uuid4() for _ in range(7))
    written.datasets.extend((via_request, direct, both, dry_ds, via_deferred, foreign, unnamed))
    other_panel = await _other_panel(pg_async_engine, panel, written, run_identity.run_id)
    async with AsyncSession(pg_async_engine) as db:
        dry = await _open(db, panel, dry_run=True)
        real = await _open(db, panel)
        theirs = await _open(db, other_panel)
        done = IngestRequestState.SUCCEEDED
        await append_transitions(
            db,
            real,
            (
                _row('a', PanelTransition.CREATED, request_id=await _request(db, written, via_request, done)),
                _row('b', PanelTransition.EXITED, dataset_id=direct),
                _row('c', PanelTransition.CREATED, request_id=await _request(db, written, both, done)),
                _row('c', PanelTransition.DRAINING, dataset_id=both),
                _row(
                    'd',
                    PanelTransition.CREATED,
                    request_id=await _request(db, written, None, IngestRequestState.FAILED),
                ),
                _row('e', PanelTransition.DEFERRED, request_id=await _request(db, written, via_deferred, done)),
            ),
        )
        await append_transitions(
            db, dry, (_row('f', PanelTransition.CREATED, request_id=await _request(db, written, dry_ds, done)),)
        )
        await append_transitions(
            db, theirs, (_row('g', PanelTransition.CREATED, request_id=await _request(db, written, foreign, done)),)
        )
        for dataset in (via_request, direct, both, dry_ds, via_deferred, foreign, unnamed):
            await db.execute(_stop(dataset, 'stop it'))
        await db.commit()

    async with AsyncSession(pg_async_engine) as db:
        stops = (await boundary_read(db, panel_id)).collect_stops
    assert sorted(str(s.dataset_id) for s in stops) == sorted(str(d) for d in (via_request, direct, both))


async def _other_panel(engine: AsyncEngine, panel, written: Written, run_id: str):
    """A second panel on SOURCE sharing our source settings version: its ledger must never reach our read."""
    _, _, source_settings_id = panel
    async with AsyncSession(engine, expire_on_commit=False) as db:
        record, version = await create_panel(
            db,
            name=f'system-test-{run_id}-ledger-other-{uuid.uuid4().hex[:8]}',
            source=SOURCE,
            granularity=Granularity.ONE_DAY,
            settings=PanelSettings(enabled=True, cadence=PanelCadence.DAILY),
            actor='operator',
            reason='ledger system test other panel',
        )
    written.panels.append(record.id)
    return record.id, version.id, source_settings_id


# --------------------------------------------------------------------------------------------- PL8 CREATED's shape


async def test_a_created_row_names_its_request_only_on_postgres(pg_async_engine, panel):
    """P20d: CREATED naming a dataset is refused before anything is written; the written one stores no dataset."""
    with pytest.raises(ValueError, match='must not name a dataset_id'):
        NewTransition('a', 'A', PanelTransition.CREATED, T0, dataset_id=uuid.uuid4(), request_id=uuid.uuid4())
    with pytest.raises(ValueError, match='must name its request_id'):
        NewTransition('a', 'A', PanelTransition.CREATED, T0)

    request_id = uuid.uuid4()
    async with AsyncSession(pg_async_engine) as db:
        pass_id = await _open(db, panel)
        await append_transitions(db, pass_id, (_row('a', PanelTransition.CREATED, request_id=request_id),))
        await db.commit()
    async with AsyncSession(pg_async_engine) as db:
        stored = (
            await db.execute(
                sa.select(TRANSITIONS.c.transition, TRANSITIONS.c.dataset_id, TRANSITIONS.c.request_id).where(
                    TRANSITIONS.c.pass_id == pass_id
                )
            )
        ).all()
        state = await latest_state(db, panel[0])
    assert [tuple(r) for r in stored] == [(PanelTransition.CREATED, None, request_id)]
    assert (state['a'].dataset_id, state['a'].request_id) == (None, request_id)


# --------------------------------------------------------------------------------------------- PL5 concurrent finish


async def test_two_concurrent_finishes_of_one_pass_finish_it_once(pg_async_engine, panel):
    async with AsyncSession(pg_async_engine) as db:
        pass_id = await _open(db, panel)
        await db.commit()

    gate = asyncio.Event()

    async def finish(n: int) -> bool:
        async with AsyncSession(pg_async_engine) as db:
            await db.execute(sa.select(sa.literal(1)))
            await gate.wait()
            result = await finish_pass(db, pass_id, {'entered': n})
            await asyncio.sleep(0.2)  # hold the row lock so the other waits on it
            await db.commit()
            return result

    tasks = [asyncio.create_task(finish(n)) for n in (1, 2)]
    await asyncio.sleep(0.1)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert sorted(results) == [False, True]
    async with AsyncSession(pg_async_engine) as db:
        counts = (await db.execute(sa.select(PASSES.c.counts).where(PASSES.c.id == pass_id))).scalar_one()
    winner = 1 if results[0] else 2
    assert counts['entered'] == winner, 'the losing finish overwrote the winner'


# --------------------------------------------------------------------------------------------- PL2 the strict tie


@pytest.mark.parametrize('round_', range(3))
async def test_strict_same_pass_entered_then_created_or_deferred_reads_the_later_on_postgres(
    pg_async_engine, panel, round_
):
    """All N, never a rate: under the old id order about half of these read back ENTERED (tj-7zjov1.179)."""
    n = 40
    creating = [f'n{round_}-{uuid.uuid4()}' for _ in range(n)]
    deferring = [f'd{round_}-{uuid.uuid4()}' for _ in range(n)]
    rows = []
    for new, deferred in zip(creating, deferring, strict=True):
        rows += [_row(new, PanelTransition.ENTERED), _row(new, PanelTransition.CREATED)]
        rows += [_row(deferred, PanelTransition.ENTERED), _row(deferred, PanelTransition.DEFERRED)]
    async with AsyncSession(pg_async_engine) as db:
        await append_transitions(db, await _open(db, panel), tuple(rows))
        await db.commit()
    async with AsyncSession(pg_async_engine) as db:
        state = await latest_state(db, panel[0])
        members = {m.vendor_asset_id for m in await members_at(db, panel[0], T0)}
    wrong = [v for v in creating if state[v].transition is not PanelTransition.CREATED]
    wrong += [v for v in deferring if state[v].transition is not PanelTransition.DEFERRED]
    assert wrong == [], f'{len(wrong)} of {2 * n} members read back their ENTERED'
    assert members == set(creating) | set(deferring)


# --------------------------------------------------------------------------------------------- PL6 the pass guards


async def test_a_finished_pass_refuses_rows_and_an_unknown_finish_raises_on_postgres(pg_async_engine, panel):
    async with AsyncSession(pg_async_engine) as db:
        pass_id = await _open(db, panel)
        await append_transitions(db, pass_id, (_row('a', PanelTransition.ENTERED),))
        assert await finish_pass(db, pass_id, {'entered': 1}) is True
        await db.commit()
    async with AsyncSession(pg_async_engine) as db:
        with pytest.raises(PanelPassMismatch, match='is finished'):
            await append_transitions(db, pass_id, (_row('b', PanelTransition.ENTERED),))
        with pytest.raises(PanelPassMismatch, match='does not exist'):
            await finish_pass(db, uuid.uuid4(), {})
        assert await finish_pass(db, pass_id, {}) is False
        await db.commit()
    async with AsyncSession(pg_async_engine) as db:
        left = (
            await db.execute(sa.select(TRANSITIONS.c.vendor_asset_id).where(TRANSITIONS.c.pass_id == pass_id))
        ).all()
    assert [r.vendor_asset_id for r in left] == ['a']


# --------------------------------------------------------------------------------------------- PL7 concurrent appends


async def test_two_concurrent_appends_into_one_pass_lose_the_second_to_the_unique_seq(pg_async_engine, panel):
    async with AsyncSession(pg_async_engine) as db:
        pass_id = await _open(db, panel)
        await db.commit()

    first_inserted = asyncio.Event()

    async def first() -> None:
        async with AsyncSession(pg_async_engine) as db:
            await append_transitions(
                db, pass_id, (_row('a', PanelTransition.ENTERED), _row('b', PanelTransition.ENTERED))
            )
            first_inserted.set()
            await asyncio.sleep(0.5)  # the second reads max(seq) = 0 and blocks on this uncommitted seq 1
            await db.commit()

    async def second() -> BaseException | None:
        await first_inserted.wait()
        async with AsyncSession(pg_async_engine) as db:
            try:
                await append_transitions(db, pass_id, (_row('c', PanelTransition.ENTERED),))
                await db.commit()
            except sa.exc.IntegrityError as exc:
                await db.rollback()
                return exc
        return None

    _, refused = await asyncio.gather(first(), second())

    assert isinstance(refused, sa.exc.IntegrityError), 'the second writer of one pass was not refused'
    assert 'uq_panel_member_transition_pass_id_seq' in str(refused.orig), str(refused.orig)
    async with AsyncSession(pg_async_engine) as db:
        stored = (
            await db.execute(
                sa.select(TRANSITIONS.c.vendor_asset_id, TRANSITIONS.c.seq)
                .where(TRANSITIONS.c.pass_id == pass_id)
                .order_by(TRANSITIONS.c.seq)
            )
        ).all()
    assert [tuple(r) for r in stored] == [('a', 1), ('b', 2)], "the first writer's rows stand alone"
