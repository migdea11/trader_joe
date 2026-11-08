"""The dev seed EXPORT on the real, migrated Postgres: one round trip (validator, tj-7zjov1.295; export tj-7zjov1.294).

Part of the whole tests/system tier. To run it alone: run_system_tests with paths
['tests/system/test_dev_seed_export_pg.py'] after test_up and migrate, or
`make test-system SYSTEM_TEST_DISPOSABLE_DB=1 SYSTEM_PATHS=tests/system/test_dev_seed_export_pg.py`.

It imports tools/dev_datasets/dev_seed.py, which the test_client container mounts read-only at /code/tools (exactly
./tools/__init__.py and ./tools/dev_datasets; docker-compose.test-client.yaml, tj-7zjov1.313). Without that mount the
import FAILS collection (never skips) with a message naming it.

THE DESIGN: decision tj-7zjov1.291 D2, D3, D5 and addendum 1; the rulings on tj-7zjov1.292 (O1 to O11, O2: corporate
actions kept 2 x the cutoff = 180 days); the architect's RE on tj-7zjov1.294 (F1: membership judged as members_at and
latest_state judge it; F2: the diff base as newest_snapshot picks it; F3: a revision refusal exits 3) and its RE on
tj-7zjov1.295, which lists the cases seeded here. WHAT THIS PROVES that tools/dev_datasets/tests/test_export.py cannot
(it pins the statements as text against a double): that every trim statement parses, plans and runs on Postgres inside
one REPEATABLE READ READ ONLY transaction that takes no transaction id (writes nothing), and keeps exactly these rows:
  ledger    M1 real ENTERED + CREATED tied in one pass before the cutoff, then a DRY-RUN EXITED: both real rows kept,
            the dry-run row not. M2 ENTERED then a real EXITED before the cutoff: neither. M3 ENTERED and EXITED at
            one as_of in two passes, the EXITED's pass started later but recorded EARLIER: neither (the order is
            started_at, never recorded_at). M4 ENTERED then DRAINING (still a member): both, and DRAINING's pass by the
            closure. M5 an ENTERED in the window: kept. M6 a member only in a dry-run pass: nothing. Passes: those the
            kept rows name plus the window's; the dry-run pass and M3's are not kept.
  lists     one (source, list) whose newest pre-cutoff snapshot is EMPTY, with an older non-empty base whose twin at
            the same as_of arrived earlier (received_at decides, against the id order) and an older snapshot of
            ANOTHER asset type (not a key): only the base and the window's snapshot, with their rows and events.
  entries   an entry straddling the session cutoff: kept with start clipped to it, its bars from the session cutoff
            inclusive; an entry ending exactly at the session cutoff: dropped with its bars and runs.
  runs      the kept dataset's latest accepted run (EMPTY) from BEFORE the cutoff (no accepted run in the window) and
            its window run; an older accepted run and a later failed one are not kept.
  actions   a corporate action 179 days old kept, one 181 days old not.
  requests  (tj-7zjov1.334; the owner's ruling on tj-7zjov1.318, decision tj-7zjov1.291 addendum 2026-11-04, built by
            tj-7zjov1.333) SUCCEEDED, EMPTY and FAILED in the window kept; QUEUED and NOT_DELIVERED in the window, a
            NOT_DELIVERED before the cutoff and a SUCCEEDED before it (the age rule) not. PENDING is NOT seeded here:
            the stack's instance watcher re-sends every PENDING row older than 30 s (list_pending_before) and would
            submit it to the live ingest; tools/dev_datasets/tests/test_export.py pins PENDING on rows (SQLite) and as
            text. The two open rows name an instance id no ingest_instance row has, so the watcher never takes them
            (list_abandoned needs a superseded instance row, or a NULL instance on NOT_DELIVERED).
  drops     a window attempt row naming the open QUEUED request, and a kept dataset's latest accepted run (before the
            cutoff) naming an open NOT_DELIVERED one: both dropped (the closure included). A window attempt row naming
            a terminal request kept, and one naming the age-trimmed request kept (R2: it may dangle). M7: ENTERED
            before the cutoff, then a CREATED naming an open request in a pass of its own: the ENTERED kept, the
            CREATED and its pass not (the passes follow). M8: in the window, a CREATED naming an open request dropped,
            its ENTERED kept. M9: in the window, a CREATED naming a terminal request kept with its ENTERED. Nothing
            ours dangles but the references to the age-trimmed request, and no exported request is in an open state.

ISOLATION. The export reads the whole test database, so every assertion is made on the intersection of what it wrote
with the ids this test inserted, and each test deletes exactly what it inserted, failed or not (entries through the
suite's entry_registry, whose cascade takes their bars). The rows are dated around an export instant in 1996, before
any instant another part of the suite writes, so no foreign row can sit in the pre-cutoff ranking of a list this test
uses; that is CHECKED before the export (a foreign pre-cutoff snapshot of the list fails the test, it never silently
moves the base). Asset lists and corporate actions use the source IB_API, which no service writes (as
test_asset_list_pg.py, which also clears IB_API leftovers at its start); the panel is on MANUAL_ENTRY, DISABLED, with a
source settings version of its own (as test_panel_ledger_pg.py); vendor ids, symbols and names carry the run id. No
PENDING ingest_request row is written (the watcher would re-send it; see requests above), and no entry carries a past expiry (the
reaper would delete it and write an audit row this test does not own). The seed files go to pytest's tmp_path.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extensions
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

import data.store.app as store_app
from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Granularity
from data.store.app.database.models.asset_list import (
    AssetListEvent,
    AssetListEventKind,
    AssetListRow,
    AssetListSnapshot,
)
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
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
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.seeds.manifest import head_revision as store_head_revision
from schemas.data_ingest.submit import AssetListStatus


try:
    from tools.dev_datasets import dev_seed
except ImportError as exc:  # FAIL, NEVER SKIP: the export is the code under test and must be reachable.
    pytest.fail(
        f'tools/dev_datasets is not importable here ({exc}): ./tools/__init__.py and ./tools/dev_datasets must be '
        'mounted read-only under /code/tools (docker-compose.test-client.yaml, tj-7zjov1.313).',
        pytrace=False,
    )


# The marker test_export.py carries for the same tool (it drives tools/dev_datasets, not a service's interface).
pytestmark = [pytest.mark.build_infra]

SNAPSHOTS = AssetListSnapshot.__table__
ROWS = AssetListRow.__table__
EVENTS = AssetListEvent.__table__
ACTIONS = CorporateAction.__table__
RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
BARS = StockMarketActivity.__table__
PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__

# The export instant. 1996: before any instant the rest of the suite writes (its fixed instants start in 2001).
MOMENT = datetime(1996, 6, 14, 15, 0, tzinfo=UTC)
CUTS = dev_seed.cutoffs_for(MOMENT)
CUTOFF = CUTS.cutoff
SESSION = CUTS.session_cutoff
DAY = timedelta(days=1)
LIST_SOURCE = DataSource.IB_API
LIST = AssetListStatus.INACTIVE
PANEL_SOURCE = DataSource.MANUAL_ENTRY


def test_the_instants_are_the_ruled_ones():
    # Written out, so the cases below mean what they say: 90 days, the first New York midnight at or after (EST in
    # March 1996: 05:00 UTC), and 180 days for corporate actions.
    assert datetime(1996, 3, 16, 15, 0, tzinfo=UTC) == CUTOFF
    assert datetime(1996, 3, 17, 5, 0, tzinfo=UTC) == SESSION
    assert CUTS.corporate_cutoff == datetime(1995, 12, 17, 15, 0, tzinfo=UTC)


@dataclass
class Written:
    """The ids this test inserted, per table, deleted at its end in foreign-key order."""

    ids: dict[str, list[Any]] = field(default_factory=dict)

    def add(self, table: sa.Table, row_id: Any) -> Any:
        self.ids.setdefault(table.name, []).append(row_id)
        return row_id

    def of(self, table: sa.Table) -> set[str]:
        return {str(row_id) for row_id in self.ids.get(table.name, [])}


DELETE_ORDER = (
    TRANSITIONS,
    PASSES,
    SETTINGS,
    PANELS,
    SOURCE_SETTINGS,
    EVENTS,
    ROWS,
    SNAPSHOTS,
    ACTIONS,
    RUNS,
    REQUESTS,
)
# Written out from the design (the store's OPEN_STATES; tools/dev_datasets/tests/test_export.py pins the two equal).
OPEN_STATES = ('PENDING', 'QUEUED', 'NOT_DELIVERED')
TERMINAL_STATES = ('SUCCEEDED', 'EMPTY', 'FAILED')


@pytest.fixture
def written(pg_engine: Engine) -> Iterator[Written]:
    record = Written()
    yield record
    with pg_engine.begin() as conn:
        for table in DELETE_ORDER:
            ids = record.ids.get(table.name)
            if ids:
                conn.execute(sa.delete(table).where(table.c.id.in_(ids)))


def _insert(conn: sa.Connection, written: Written, table: sa.Table, **values: Any) -> Any:
    return written.add(table, conn.execute(sa.insert(table).values(**values).returning(table.c.id)).scalar_one())


# ---- the seed -------------------------------------------------------------------------------------------------------


@dataclass
class Seeded:
    kept: dict[str, set[str]]
    dropped: dict[str, set[str]]
    clipped_entry: str
    entry_start: datetime
    age_trimmed_request: str


def _seed_ledger(
    conn: sa.Connection, written: Written, run_id: str, kept: dict, dropped: dict, requests: dict[str, uuid.UUID]
) -> None:
    version_no = conn.execute(
        sa.select(sa.func.coalesce(sa.func.max(SOURCE_SETTINGS.c.version_no), 0) + 1).where(
            SOURCE_SETTINGS.c.source == PANEL_SOURCE
        )
    ).scalar_one()
    source_version = _insert(
        conn,
        written,
        SOURCE_SETTINGS,
        source=PANEL_SOURCE,
        version_no=version_no,
        # ADR tj-2ayia2: a cap of 1 is refused by request_cap_min now; a NULL ramp (no limit) and the minimum cap 2
        # carry both shapes of a rate through the export.
        ramp_datasets_per_hour=None,
        request_cap_per_hour=2,
        corporate_action_window_pre_days=0,
        corporate_action_window_post_days=0,
        actor='operator',
        reason=f'dev seed export system test {run_id}',
    )
    panel = _insert(conn, written, PANELS, name=f'system-test-{run_id}-dev-seed', source=PANEL_SOURCE)
    settings = _insert(
        conn,
        written,
        SETTINGS,
        panel_id=panel,
        version_no=1,
        enabled=False,  # never a pass of the stack's own panel loop
        cadence=PanelCadence.DAILY,
        actor='operator',
        reason='dev seed export system test panel',
    )

    def open_pass(started_at: datetime, *, dry_run: bool = False) -> uuid.UUID:
        return _insert(
            conn,
            written,
            PASSES,
            panel_id=panel,
            kind=PanelPassKind.MEMBERSHIP,
            settings_version_id=settings,
            source_settings_version_id=source_version,
            active_snapshot_id=uuid.uuid4(),
            started_at=started_at,
            finished_at=started_at + timedelta(minutes=5),
            dry_run=dry_run,
        )

    def row(
        pass_id, member: str, kind: PanelTransition, as_of: datetime, seq: int, recorded_at=None, request=None
    ) -> uuid.UUID:
        # A CREATED row names a request: by default the terminal request trimmed by age, so every request this test's
        # rows name exists in the database and the only dangle left is the one R2 allows.
        if kind is PanelTransition.CREATED and request is None:
            request = requests['age_trimmed']
        values = {
            'pass_id': pass_id,
            'panel_id': panel,
            'vendor_asset_id': f'devseed-{run_id}-{member}',
            'symbol': f'ZZDS{run_id.upper()}{member.upper()}',
            'transition': kind,
            'request_id': request,
            'as_of': as_of,
            'recorded_at': recorded_at if recorded_at is not None else as_of,
            'seq': seq,
        }
        return _insert(conn, written, TRANSITIONS, **values)

    t1 = CUTOFF - 20 * DAY
    real_1 = open_pass(t1)
    real_2 = open_pass(CUTOFF - 15 * DAY)
    dry = open_pass(CUTOFF - 10 * DAY, dry_run=True)
    pass_a = open_pass(CUTOFF - 30 * DAY)
    pass_b = open_pass(CUTOFF - 29 * DAY)
    window = open_pass(CUTOFF + DAY)
    open_created = open_pass(CUTOFF - 12 * DAY)  # holds only M7's CREATED, which names an open request

    keep = kept.setdefault(TRANSITIONS.name, set())
    drop = dropped.setdefault(TRANSITIONS.name, set())
    # M1: ENTERED and CREATED tied (same pass, same as_of; CREATED has the higher seq), then a DRY-RUN EXITED.
    keep.add(str(row(real_1, 'm1', PanelTransition.ENTERED, t1, 1)))
    keep.add(str(row(real_1, 'm1', PanelTransition.CREATED, t1, 2)))
    drop.add(str(row(dry, 'm1', PanelTransition.EXITED, CUTOFF - 10 * DAY, 1)))
    # M2: entered, then really exited before the cutoff.
    drop.add(str(row(real_1, 'm2', PanelTransition.ENTERED, t1, 3)))
    drop.add(str(row(real_2, 'm2', PanelTransition.EXITED, CUTOFF - 15 * DAY, 1)))
    # M3: one as_of; EXITED's pass started later, but EXITED was RECORDED earlier. The store says EXITED (not a member).
    t3 = CUTOFF - 28 * DAY
    drop.add(str(row(pass_a, 'm3', PanelTransition.ENTERED, t3, 1, recorded_at=CUTOFF - DAY)))
    drop.add(str(row(pass_b, 'm3', PanelTransition.EXITED, t3, 1, recorded_at=t3 - DAY)))
    # M4: entered, then DRAINING: still a member; the newest row of any kind is the DRAINING.
    keep.add(str(row(real_1, 'm4', PanelTransition.ENTERED, t1, 4)))
    keep.add(str(row(real_2, 'm4', PanelTransition.DRAINING, CUTOFF - 15 * DAY, 2)))
    # M5: in the window.
    keep.add(str(row(window, 'm5', PanelTransition.ENTERED, CUTOFF + DAY, 1)))
    # M6: a member of a dry-run pass only.
    drop.add(str(row(dry, 'm6', PanelTransition.ENTERED, CUTOFF - 10 * DAY, 2)))
    # M7 (tj-7zjov1.333): a member before the cutoff whose newest row is a CREATED naming an OPEN request, alone in its
    # pass. The old rules kept both rows and the pass; now the CREATED is dropped, the ENTERED stays, the pass goes.
    keep.add(str(row(real_1, 'm7', PanelTransition.ENTERED, t1, 5)))
    drop.add(str(row(open_created, 'm7', PanelTransition.CREATED, CUTOFF - 12 * DAY, 1, request=requests['queued'])))
    # M8: in the window, a CREATED naming an open request: dropped whatever kept it; its ENTERED stays.
    keep.add(str(row(window, 'm8', PanelTransition.ENTERED, CUTOFF + DAY, 2)))
    drop.add(str(row(window, 'm8', PanelTransition.CREATED, CUTOFF + DAY, 3, request=requests['not_delivered'])))
    # M9: in the window, a CREATED naming a terminal request: both kept.
    keep.add(str(row(window, 'm9', PanelTransition.ENTERED, CUTOFF + DAY, 4)))
    keep.add(str(row(window, 'm9', PanelTransition.CREATED, CUTOFF + DAY, 5, request=requests['succeeded'])))

    kept[PASSES.name] = {str(real_1), str(real_2), str(window)}
    dropped[PASSES.name] = {str(dry), str(pass_a), str(pass_b), str(open_created)}


def _seed_lists(conn: sa.Connection, written: Written, run_id: str, kept: dict, dropped: dict) -> None:
    def snapshot(as_of: datetime, rows: int, *, received_at=None, asset_type=AssetType.STOCK, snapshot_id=None):
        values = {
            'source': LIST_SOURCE,
            'asset_type': asset_type,
            'asset_list': LIST,
            'request_id': uuid.uuid4(),
            'attempt_no': 1,
            'as_of': as_of,
            'row_count': rows,
            'received_at': received_at if received_at is not None else as_of + timedelta(hours=1),
        }
        if snapshot_id is not None:
            values['id'] = snapshot_id
        sid = _insert(conn, written, SNAPSHOTS, **values)
        row_ids = [
            str(
                _insert(
                    conn,
                    written,
                    ROWS,
                    snapshot_id=sid,
                    vendor_asset_id=f'devseed-{run_id}-asset-{n}',
                    symbol=f'ZZDS{run_id.upper()}{n}',
                    name=f'dev seed asset {n}',
                    exchange='NYSE',
                    active=False,
                    tradable=False,
                    raw_payload=b'{}',
                )
            )
            for n in range(rows)
        ]
        return str(sid), row_ids

    def event(snapshot_id: str, previous_id: str, as_of: datetime) -> str:
        return str(
            _insert(
                conn,
                written,
                EVENTS,
                source=LIST_SOURCE,
                asset_list=LIST,
                kind=AssetListEventKind.APPEARED,
                vendor_asset_id=f'devseed-{run_id}-asset-0',
                symbol=f'ZZDS{run_id.upper()}0',
                snapshot_id=snapshot_id,
                previous_snapshot_id=previous_id,
                as_of=as_of,
                previous_as_of=as_of - DAY,
            )
        )

    # The base and its twin share an as_of; the base arrived LATER but gets the LOWER id, so received_at, not id,
    # must decide.
    low, high = sorted((uuid.uuid4(), uuid.uuid4()))
    base_as_of = CUTOFF - 20 * DAY
    older, older_rows = snapshot(CUTOFF - 40 * DAY, 1)
    other_type, other_rows = snapshot(CUTOFF - 25 * DAY, 1, asset_type=AssetType.CRYPTO)
    base, base_rows = snapshot(base_as_of, 2, received_at=base_as_of + timedelta(hours=2), snapshot_id=low)
    twin, twin_rows = snapshot(base_as_of, 1, received_at=base_as_of + timedelta(hours=1), snapshot_id=high)
    empty, _ = snapshot(CUTOFF - 5 * DAY, 0)
    recent, recent_rows = snapshot(CUTOFF + 5 * DAY, 1)
    kept[SNAPSHOTS.name] = {base, recent}
    dropped[SNAPSHOTS.name] = {older, other_type, twin, empty}
    kept[ROWS.name] = set(base_rows) | set(recent_rows)
    dropped[ROWS.name] = set(older_rows) | set(other_rows) | set(twin_rows)
    kept[EVENTS.name] = {event(base, older, base_as_of), event(recent, base, CUTOFF + 5 * DAY)}
    dropped[EVENTS.name] = {event(older, older, CUTOFF - 40 * DAY)}


def _seed_actions(conn: sa.Connection, written: Written, run_id: str, kept: dict, dropped: dict) -> None:
    def action(days: int) -> str:
        return str(
            _insert(
                conn,
                written,
                ACTIONS,
                source=LIST_SOURCE,
                vendor_action_id=f'devseed-{run_id}-{days}',
                action_type='forward_split',
                raw_payload=b'{}',
                payload_hash=f'devseed-{run_id}-{days}',
                request_id=uuid.uuid4(),
                attempt_no=1,
                as_of=MOMENT - days * DAY,
            )
        )

    kept[ACTIONS.name] = {action(179)}
    dropped[ACTIONS.name] = {action(181)}


def _run(
    conn, written: Written, entry, state: IngestRunState, started_at: datetime, request=None, attempt_no=None
) -> str:
    return str(
        _insert(
            conn,
            written,
            RUNS,
            dataset_id=entry.id,
            request_id=request,
            attempt_no=attempt_no,
            trigger=IngestRunTrigger.REQUEST,
            actor='system-test',
            source=entry.source,
            asset_symbol=entry.asset_symbol,
            granularity=entry.granularity,
            vendor_asset_id='devseed-vendor-asset',
            state=state,
            started_at=started_at,
            finished_at=started_at + timedelta(minutes=1),
        )
    )


@pytest.fixture
def seeded(pg_engine: Engine, written: Written, insert_entry, bar_values, run_identity) -> Seeded:
    kept: dict[str, set[str]] = {}
    dropped: dict[str, set[str]] = {}
    run_id = run_identity.run_id
    # Entries first, each committed by insert_entry and registered for the session's cleanup.
    straddling = insert_entry(
        asset_symbol=run_identity.alt_symbol('DSKEEP'),
        granularity=Granularity.ONE_MINUTE,
        start=SESSION - 3 * DAY,
        end=MOMENT - 10 * DAY,
    )
    ended = insert_entry(
        asset_symbol=run_identity.alt_symbol('DSDROP'), start=CUTOFF - 30 * DAY, end=SESSION
    )  # "end > session cutoff" is strict: ending AT it is dropped
    # A second kept entry whose only accepted run names an OPEN request (tj-7zjov1.333: the closure is dropped too).
    open_based = insert_entry(
        asset_symbol=run_identity.alt_symbol('DSOPEN'),
        granularity=Granularity.ONE_MINUTE,
        start=SESSION - 3 * DAY,
        end=MOMENT - 10 * DAY,
    )
    with pg_engine.begin() as conn:

        def request(state: IngestRequestState, created_at: datetime) -> uuid.UUID:
            # IB_API, which no service writes; never PENDING (see the module docstring). An open row names an instance
            # id no ingest_instance row has, so the stack's watcher never re-sends it, and no dataset id, so no dataset
            # reads Running.
            is_open = state.value in OPEN_STATES
            return _insert(
                conn,
                written,
                REQUESTS,
                kind=IngestRequestKind.BARS,
                source=LIST_SOURCE,
                client_label='system-test',
                asset_symbol=run_identity.alt_symbol('DSREQ'),
                state=state,
                instance_id=f'devseed-{run_id}-no-such-instance' if is_open else None,
                created_at=created_at,
                finished_at=None if is_open else created_at + timedelta(minutes=5),
            )

        requests = {
            'succeeded': request(IngestRequestState.SUCCEEDED, CUTOFF + DAY),
            'empty': request(IngestRequestState.EMPTY, CUTOFF),  # the cutoff is inclusive
            'failed': request(IngestRequestState.FAILED, CUTOFF + 3 * DAY),
            'queued': request(IngestRequestState.QUEUED, CUTOFF + DAY),
            'not_delivered': request(IngestRequestState.NOT_DELIVERED, CUTOFF + DAY),
            'not_delivered_old': request(IngestRequestState.NOT_DELIVERED, CUTOFF - 3 * DAY),
            'age_trimmed': request(IngestRequestState.SUCCEEDED, CUTOFF - DAY),
        }
        kept[REQUESTS.name] = {str(requests[k]) for k in ('succeeded', 'empty', 'failed')}
        dropped[REQUESTS.name] = {
            str(requests[k]) for k in ('queued', 'not_delivered', 'not_delivered_old', 'age_trimmed')
        }

        def bar(entry, at: datetime) -> str:
            return str(
                conn.execute(sa.insert(BARS).values(bar_values(entry, timestamp=at)).returning(BARS.c.id)).scalar_one()
            )

        kept[BARS.name] = {bar(straddling, SESSION), bar(straddling, SESSION + DAY)}
        dropped[BARS.name] = {bar(straddling, SESSION - timedelta(minutes=1)), bar(ended, SESSION - DAY)}
        kept['store_dataset_entry'] = {str(straddling.id)}
        dropped['store_dataset_entry'] = {str(ended.id)}
        kept[RUNS.name] = {
            _run(conn, written, straddling, IngestRunState.EMPTY, CUTOFF - 10 * DAY),  # the latest accepted
            _run(conn, written, straddling, IngestRunState.FAILED, CUTOFF + 2 * DAY),  # the window's
        }
        dropped[RUNS.name] = {
            _run(conn, written, straddling, IngestRunState.SUCCEEDED, CUTOFF - 30 * DAY),  # an older accepted one
            _run(conn, written, straddling, IngestRunState.FAILED, CUTOFF - 5 * DAY),  # newer, but not accepted
            _run(conn, written, ended, IngestRunState.SUCCEEDED, CUTOFF - 10 * DAY),  # of a dropped dataset
        }
        # tj-7zjov1.333: attempt rows (request_id and attempt_no both set, as the store writes them).
        kept[RUNS.name] |= {
            _run(conn, written, straddling, IngestRunState.FAILED, CUTOFF + 3 * DAY, requests['succeeded'], 1),
            # R2: a request trimmed by AGE may dangle; its attempt row is in the window and stays.
            _run(conn, written, straddling, IngestRunState.FAILED, CUTOFF + 4 * DAY, requests['age_trimmed'], 2),
        }
        dropped[RUNS.name] |= {
            _run(conn, written, straddling, IngestRunState.FAILED, CUTOFF + 5 * DAY, requests['queued'], 1),
            # The closure: open_based's latest (and only) accepted run, which names an open request.
            _run(conn, written, open_based, IngestRunState.EMPTY, CUTOFF - 10 * DAY, requests['not_delivered_old'], 1),
        }
        kept['store_dataset_entry'].add(str(open_based.id))
        _seed_ledger(conn, written, run_id, kept, dropped, requests)
        _seed_lists(conn, written, run_id, kept, dropped)
        _seed_actions(conn, written, run_id, kept, dropped)
    return Seeded(
        kept=kept,
        dropped=dropped,
        clipped_entry=str(straddling.id),
        entry_start=straddling.start,
        age_trimmed_request=str(requests['age_trimmed']),
    )


# ---- the export -----------------------------------------------------------------------------------------------------


class WatchedConnection:
    """The export's psycopg2 connection, probed at each rollback that ends a transaction.

    The probe reads txid_current_if_assigned() (NULL unless the transaction wrote something: Postgres assigns an xid
    only on a write), transaction_read_only and transaction_isolation, in the export's own transaction, before it is
    rolled back. commit() is counted and refused.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.probes: list[tuple] = []

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.cursor(*args, **kwargs)

    def commit(self) -> None:
        raise AssertionError('the export committed')

    def rollback(self) -> None:
        if self.inner.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS:
            with self.inner.cursor() as cursor:
                cursor.execute(
                    'SELECT txid_current_if_assigned(), current_setting(%s), current_setting(%s)',
                    ('transaction_read_only', 'transaction_isolation'),
                )
                self.probes.append(cursor.fetchone())
        self.inner.rollback()


def _connect(pg_settings: Any) -> Any:
    """A plain psycopg2 connection, as request.py's _connect opens one for the tool (the export's own driver)."""
    return psycopg2.connect(
        host=pg_settings.host,
        port=pg_settings.port,
        user=pg_settings.user,
        password=pg_settings.password,
        dbname=pg_settings.database,
        connect_timeout=pg_settings.connect_timeout,
    )


def _versions_dir() -> Path:
    # Where the store's migrations are in THIS process: server/data/store/migrations on the host, /code/data/store/
    # migrations in test_client. dev_seed's own default is the host layout only.
    return Path(store_app.__file__).resolve().parents[1] / 'migrations' / 'versions'


def _copy_rows(directory: Path, manifest: dict, table: str) -> list[dict[str, str]]:
    columns = manifest['tables'][table]['columns']
    data = (directory / f'{table}.copy').read_text(encoding='utf-8')
    return [dict(zip(columns, line.split('\t'), strict=True)) for line in data.splitlines()]


def _timestamp(text: str) -> datetime:
    # COPY text under the export's TimeZone UTC: '1996-03-17 05:00:00+00'.
    return datetime.fromisoformat(text + ':00' if text[-3] in '+-' else text)


def test_the_export_keeps_exactly_the_ruled_rows_and_writes_nothing(pg_engine, pg_settings, seeded, tmp_path):
    versions = _versions_dir()
    head = dev_seed.head_revision(versions)
    assert head == store_head_revision(versions)
    with pg_engine.connect() as conn:
        revision = conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one()
        # The isolation precondition: no foreign snapshot of this list before the cutoff could be the base.
        foreign = conn.execute(
            sa.select(sa.func.count())
            .select_from(SNAPSHOTS)
            .where(
                SNAPSHOTS.c.source == LIST_SOURCE,
                SNAPSHOTS.c.asset_list == LIST,
                SNAPSHOTS.c.as_of < CUTOFF,
                SNAPSHOTS.c.id.notin_(
                    [uuid.UUID(i) for i in seeded.kept[SNAPSHOTS.name] | seeded.dropped[SNAPSHOTS.name]]
                ),
            )
        ).scalar_one()
    assert revision == head, f'the test database is at {revision}, the checkout head is {head}: migrate it first'
    assert foreign == 0, (
        f'{foreign} foreign {LIST_SOURCE.value}/{LIST.value} snapshot(s) before 1996-03-16: isolation broken'
    )

    inner = _connect(pg_settings)
    watched = WatchedConnection(inner)
    lines: list[str] = []
    try:
        result = dev_seed.export_seed(
            watched, 'test', MOMENT, lines.append, root=tmp_path / 'seed', versions_dir=versions
        )
    finally:
        inner.close()

    # One transaction, read only, repeatable read, and it never took a transaction id: it wrote nothing.
    assert watched.probes == [(None, 'on', 'repeatable read')], watched.probes
    assert result.revision == head
    directory = result.directory
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['revision'] == manifest['checkout_head'] == head
    assert set(manifest['tables']) == set(dev_seed.SEED_TABLES)

    for table, expected in seeded.kept.items():
        exported = {row['id'] for row in _copy_rows(directory, manifest, table)}
        ours = expected | seeded.dropped[table]
        assert exported & ours == expected, (
            table,
            sorted(expected - exported),  # kept rows missing
            sorted(exported & seeded.dropped[table]),  # dropped rows written
        )
    # The manifest's counts are the rows written, for every table (what a reload recounts against).
    for table in dev_seed.SEED_TABLES:
        assert manifest['tables'][table]['rows'] == len(_copy_rows(directory, manifest, table)), table

    # tj-7zjov1.333: no exported request, ours or another test's, is in an open state.
    exported_requests = _copy_rows(directory, manifest, REQUESTS.name)
    assert not [r['id'] for r in exported_requests if r['state'] not in TERMINAL_STATES]
    # Nothing ours dangles but what R2 allows: every request our kept runs and transitions name is in the seed's
    # ingest_request, except the terminal request trimmed by age (created before the cutoff). '\N' is COPY's NULL.
    seed_request_ids = {r['id'] for r in exported_requests}
    named = {
        row['request_id']
        for table in (RUNS.name, TRANSITIONS.name)
        for row in _copy_rows(directory, manifest, table)
        if row['id'] in seeded.kept[table] and row['request_id'] != '\\N'
    }
    assert seeded.age_trimmed_request in named
    assert named - seed_request_ids == {seeded.age_trimmed_request}, sorted(named - seed_request_ids)

    # O4: the straddling entry's start is clipped up to the session cutoff in the FILE, and only there.
    [entry] = [r for r in _copy_rows(directory, manifest, 'store_dataset_entry') if r['id'] == seeded.clipped_entry]
    assert _timestamp(entry['start']) == SESSION
    with pg_engine.connect() as conn:
        stored = conn.execute(
            sa.text('SELECT start FROM store_dataset_entry WHERE id = :id'), {'id': seeded.clipped_entry}
        ).scalar_one()
    assert stored == seeded.entry_start == SESSION - 3 * DAY

    # The wiped tables are written empty whatever the database holds.
    for table in ('ingest_instance', 'dataset_read', 'dataset_read_daily', 'dataset_override_log', 'dataset_deletion'):
        assert manifest['tables'][table]['rows'] == 0, table


def test_a_database_not_at_the_checkout_head_is_refused_before_any_file(pg_settings, tmp_path):
    # D3 on Postgres: the revision read inside the export's transaction is compared with a head this database is not at
    # (a scratch versions directory holding one unrelated revision), and nothing is written.
    versions = tmp_path / 'versions'
    versions.mkdir()
    (versions / 'ffffffffffff_x.py').write_text(
        "revision: str = 'ffffffffffff'\ndown_revision = None\n", encoding='utf-8'
    )
    inner = _connect(pg_settings)
    watched = WatchedConnection(inner)
    try:
        with pytest.raises(dev_seed.DevSeedRefused, match='ffffffffffff'):
            dev_seed.export_seed(
                watched, 'test', MOMENT, lambda line: None, root=tmp_path / 'seed', versions_dir=versions
            )
    finally:
        inner.close()
    assert not (tmp_path / 'seed').exists()
    assert watched.probes == [(None, 'on', 'repeatable read')], watched.probes
