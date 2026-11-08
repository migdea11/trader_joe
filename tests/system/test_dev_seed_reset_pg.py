"""The preserving dev RESET and LOAD on the real, migrated Postgres (validator, tj-7zjov1.297 P56v; built by .296).

Part of the whole tests/system tier, and safe there: every row this file writes, every TRUNCATE the reset runs and every
COPY the load runs lands in a SCRATCH DATABASE this test creates on the test server, migrates to the checkout head with
alembic and drops WITH (FORCE) at its end (test_usage_record.scratch_db, which test_reaper_race.py reuses the same way;
the scratch-database exception tj-vhboky.62). The stack's own database receives nothing but CREATE DATABASE and DROP
DATABASE, so the TRUNCATE of all eighteen tables can never reach another test's rows, and nothing else connects to the
scratch database while the reset holds its locks. To run it alone: run_system_tests with paths
['tests/system/test_dev_seed_reset_pg.py'] after test_up and migrate, or
`make test-system SYSTEM_TEST_DISPOSABLE_DB=1 SYSTEM_PATHS=tests/system/test_dev_seed_reset_pg.py`.

It imports tools/dev_datasets (request.py and dev_seed.py), which the test_client container mounts read-only at
/code/tools (docker-compose.test-client.yaml, tj-7zjov1.313). Without that mount the import FAILS collection.

THE DESIGN: decision tj-7zjov1.291 D1, D3, R2, R3 and addendum 1; the rulings on tj-7zjov1.292 (O1 to O11, corporate
actions 2 x the cutoff); the owner's ruling of 2026-11-03 (no dev guard). WHAT THIS PROVES that the double-driven tests
in tools/dev_datasets/tests cannot: that Postgres runs the dev reset's whole transaction as built (the ACCESS EXCLUSIVE
lock, the export's COPY ... TO under that lock at READ COMMITTED, the one TRUNCATE ... RESTART IDENTITY of the eighteen
with their foreign keys, the COPY ... FROM of the files just written, parents first, the setval), and that afterwards:
  - every table holds EXACTLY the rows the trim rules keep, by id (rows on both sides of the cutoff in all eighteen; the
    five wiped tables empty), the straddling entry's start clipped to the session cutoff;
  - ONLY TERMINAL REQUESTS come back (tj-7zjov1.334; owner ruling on tj-7zjov1.318, decision tj-7zjov1.291 addendum
    2026-11-04, built by tj-7zjov1.333): SUCCEEDED, EMPTY and FAILED of the window are reloaded, a NOT_DELIVERED one
    (in the window and before it) is not, nor the attempt row naming it, nor a panel CREATED row naming it nor that
    row's own pass, while the member's ENTERED row is; and nothing dangles: every request an ingest_run or
    panel_member_transition row names was reloaded with it. PENDING and QUEUED are left to the reset's own refusal;
  - the panels, their settings versions and the source settings are intact, and a new pass and transition can be
    written against them (the foreign keys are live: one that names no panel is refused);
  - the bar id sequence was set to max(id) of the KEPT bars: an INSERT without an id gets max + 1 (a dropped bar had
    the highest id before, so neither RESTART IDENTITY alone nor the old sequence value could give that);
  - LOAD of that seed into the eighteen tables, emptied by the test label's plain wipe, first refuses it (exit 3,
    before the lock: its manifest says 'dev' and the target is the test stack), then, with the manifest's stack set to
    'test' (the second adaptation, below), restores the same ids and the same sequence;
  - a failure after the TRUNCATE rolls EVERYTHING back on Postgres, every id of every table and the bar sequence as
    before, the export on disk: a file changed between its check and its COPY (caught by the hash of what the server
    read, with the tables before it already reloaded), and a statement failing right after the setval (setval is not
    transactional in general; here it acts on the sequence the TRUNCATE ... RESTART IDENTITY reset in the same
    transaction, and this is what shows that Postgres undoes it).

HOW IT DRIVES THE TOOL. run_reset and run_load are called directly, with the real database rail
(checked_connect(..., 'test'): the server's cluster_name is 'test' from its command line), and run_load with the real
HTTP rail against this stack's data_store, which answers deploymentLabel 'test'. run_reset is given label 'dev' so
it takes the preserving branch (on the test label it is the plain wipe, which the load leg uses). The quiet-stack wait is
skipped (nothing else writes to a scratch database). ONE ADAPTATION, said plainly: run_reset and run_load read the
checkout head with dev_seed.head_revision() and its DEFAULT directory, server/data/store/migrations/versions under the
repository root, which test_client does not mount (it mounts the migrations at /code/data/store/migrations). This file
therefore points dev_seed.head_revision at the mounted directory for the duration of each test (monkeypatch), and first
checks that it names the same head as alembic's ScriptDirectory. The comparison logic itself is unchanged. Without the
patch both would refuse with "expected exactly one migration head ... found 0" (reported on tj-7zjov1.297). A SECOND
ADAPTATION (tj-7zjov1.328): the load's stack rule refuses a seed exported under another label than the target's, and the
seed here is exported under 'dev' into a test cluster; after pinning that refusal the test sets the manifest's stack to
'test' and loads (the .copy files are unchanged and still verified).

WHAT STAYS UNPROVEN HERE: the dev stack itself (its cluster_name 'dev', its real volumes, a live data_ingest writing
during the wait: the NOT CLEAN path), the stale-seed refusals and the size and sha256 checks on Postgres (pure Python,
pinned against the double), and the lock timeout firing against a real long reader.
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

import data.store.app as store_app
from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from data.store.app.database.models.asset_list import (
    AssetListEvent,
    AssetListEventKind,
    AssetListRow,
    AssetListSnapshot,
)
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.dataset_read import DatasetRead, DatasetReadKind, ReadClientKind
from data.store.app.database.models.dataset_read_daily import DatasetReadDaily
from data.store.app.database.models.ingest_instance import IngestInstance
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
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_ingest.submit import AssetListStatus
from tests.system import test_usage_record
from tests.system.scratch_db import MIGRATIONS_DIR, ScratchDb


try:
    from tools.dev_datasets import dev_seed
    from tools.dev_datasets import request as tool
except ImportError as exc:  # FAIL, NEVER SKIP: the reset and the load are the code under test.
    pytest.fail(
        f'tools/dev_datasets is not importable here ({exc}): ./tools/__init__.py and ./tools/dev_datasets must be '
        'mounted read-only under /code/tools (docker-compose.test-client.yaml, tj-7zjov1.313).',
        pytrace=False,
    )


# The marker test_dev_seed_export_pg.py and tools/dev_datasets/tests carry: it drives the tool, not a service.
pytestmark = [pytest.mark.build_infra]

scratch_db = test_usage_record.scratch_db

ENTRIES = StoreDatasetEntry.__table__
BARS = StockMarketActivity.__table__
RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
READS = DatasetRead.__table__
DAILY = DatasetReadDaily.__table__
OVERRIDES = DatasetOverrideLog.__table__
DELETIONS = DatasetDeletion.__table__
SNAPSHOTS = AssetListSnapshot.__table__
ROWS = AssetListRow.__table__
EVENTS = AssetListEvent.__table__
ACTIONS = CorporateAction.__table__
PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
# Every table by name, and the key each is identified by (ingest_instance has no id column).
TABLES = {
    table.name: table
    for table in (
        ENTRIES,
        BARS,
        RUNS,
        REQUESTS,
        INSTANCES,
        READS,
        DAILY,
        OVERRIDES,
        DELETIONS,
        SNAPSHOTS,
        ROWS,
        EVENTS,
        ACTIONS,
        PANELS,
        SETTINGS,
        SOURCE_SETTINGS,
        PASSES,
        TRANSITIONS,
    )
}
WIPED = ('ingest_instance', 'dataset_read', 'dataset_read_daily', 'dataset_override_log', 'dataset_deletion')

# The reset instant; the database is ours alone, so any fixed instant serves. Every cutoff comes from it.
MOMENT = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
CUTS = dev_seed.cutoffs_for(MOMENT)
CUTOFF = CUTS.cutoff
SESSION = CUTS.session_cutoff
DAY = timedelta(days=1)
LIST_SOURCE = DataSource.IB_API
LIST = AssetListStatus.ACTIVE
PANEL_SOURCE = DataSource.MANUAL_ENTRY
STAMP = MOMENT.strftime(dev_seed.STAMP_FORMAT)


def key(table: sa.Table) -> sa.Column:
    return table.c.instance_id if table.name == 'ingest_instance' else table.c.id


def test_the_instants_are_the_ruled_ones():
    # 90 days; the first New York midnight at or after it (EST until 8 March 2026: 05:00 UTC); 180 days for actions.
    assert datetime(2026, 3, 3, 15, 0, tzinfo=UTC) == CUTOFF
    assert datetime(2026, 3, 4, 5, 0, tzinfo=UTC) == SESSION
    assert CUTS.corporate_cutoff == datetime(2025, 12, 3, 15, 0, tzinfo=UTC)


# ---- the head, as test_client lays it out ----------------------------------------------------------------------------


def _mounted_versions() -> Path:
    # server/data/store/migrations on the host, /code/data/store/migrations in test_client.
    return Path(store_app.__file__).resolve().parents[1] / 'migrations' / 'versions'


def _alembic_head() -> str:
    (head,) = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()
    return head


@pytest.fixture
def head(monkeypatch: pytest.MonkeyPatch) -> str:
    """The checkout head, and dev_seed.head_revision pointed at the mounted versions (see the docstring)."""
    original = dev_seed.head_revision
    versions = _mounted_versions()
    found = original(versions)
    assert found == _alembic_head(), f'dev_seed reads head {found}, alembic {_alembic_head()}'
    monkeypatch.setattr(dev_seed, 'head_revision', lambda versions_dir=versions: original(versions_dir))
    return found


@pytest.fixture
def migrated(scratch_db: ScratchDb, head: str) -> ScratchDb:
    scratch_db.must('upgrade', head)
    return scratch_db


# ---- the rows: both sides of the cutoff in all eighteen tables --------------------------------------------------------


class Seeded:
    """The ids written per table, and which of them the trim rules keep."""

    def __init__(self) -> None:
        self.kept: dict[str, set[str]] = {name: set() for name in TABLES}
        self.dropped: dict[str, set[str]] = {name: set() for name in TABLES}
        self.clipped_entry = ''
        self.panel = ''
        self.settings = ''
        self.source_settings = ''
        self.kept_bar_ids: list[int] = []
        self.highest_bar_before = 0
        self.open_request = ''


def _insert(conn: sa.Connection, table: sa.Table, **values: Any) -> Any:
    return conn.execute(sa.insert(table).values(**values).returning(key(table))).scalar_one()


def _seed(db: ScratchDb, entry_values: Callable[..., dict[str, Any]], bar_values, run_id: str) -> Seeded:
    s = Seeded()

    def keep(table: sa.Table, value: Any) -> Any:
        s.kept[table.name].add(str(value))
        return value

    def drop(table: sa.Table, value: Any) -> Any:
        s.dropped[table.name].add(str(value))
        return value

    with db.engine.begin() as conn:
        # Entries (O4): one straddling the session cutoff (kept, start clipped), one ending exactly at it (dropped).
        straddling = conn.execute(
            sa.insert(ENTRIES)
            .values(entry_values(asset_symbol=f'ZZDSRS{run_id}A', start=SESSION - 3 * DAY, end=MOMENT - 10 * DAY))
            .returning(*ENTRIES.c)
        ).one()
        ended = conn.execute(
            sa.insert(ENTRIES)
            .values(entry_values(asset_symbol=f'ZZDSRS{run_id}B', start=CUTOFF - 30 * DAY, end=SESSION))
            .returning(*ENTRIES.c)
        ).one()
        keep(ENTRIES, straddling.id)
        drop(ENTRIES, ended.id)
        s.clipped_entry = str(straddling.id)

        def bar(entry: sa.Row, at: datetime) -> int:
            return _insert(conn, BARS, **bar_values(entry, timestamp=at))

        drop(BARS, bar(straddling, SESSION - timedelta(minutes=1)))
        s.kept_bar_ids = [keep(BARS, bar(straddling, SESSION)), keep(BARS, bar(straddling, SESSION + DAY))]
        # The highest id of all is a DROPPED bar: the sequence after the reset must come from the kept ones.
        s.highest_bar_before = drop(BARS, bar(ended, SESSION - DAY))

        # Runs (O1): the kept dataset's latest accepted run from before the cutoff, and the window's.
        def run(entry: sa.Row, state: IngestRunState, started_at: datetime, request=None, attempt_no=None) -> uuid.UUID:
            return _insert(
                conn,
                RUNS,
                dataset_id=entry.id,
                request_id=request,
                attempt_no=attempt_no,
                trigger=IngestRunTrigger.REQUEST,
                actor='system-test',
                source=entry.source,
                asset_symbol=entry.asset_symbol,
                granularity=entry.granularity,
                vendor_asset_id='dsreset-vendor-asset',
                state=state,
                started_at=started_at,
                finished_at=started_at + timedelta(minutes=1),
            )

        keep(RUNS, run(straddling, IngestRunState.EMPTY, CUTOFF - 10 * DAY))
        keep(RUNS, run(straddling, IngestRunState.FAILED, CUTOFF + 2 * DAY))
        drop(RUNS, run(straddling, IngestRunState.SUCCEEDED, CUTOFF - 30 * DAY))
        drop(RUNS, run(ended, IngestRunState.SUCCEEDED, CUTOFF - 10 * DAY))

        # Requests (O1 as amended, tj-7zjov1.333): the three terminal states in the window kept, a terminal one before
        # the cutoff dropped by age, and NOT_DELIVERED (open) dropped in the window and before it. PENDING and QUEUED
        # are not seeded: the reset's in-flight guard refuses them (pinned in tools/dev_datasets/tests/test_reset.py),
        # and NOT_DELIVERED is the open state a dev reset meets (decision tj-7zjov1.291 addendum 2026-11-04).
        def request(created_at: datetime, state: IngestRequestState = IngestRequestState.SUCCEEDED) -> uuid.UUID:
            is_open = state is IngestRequestState.NOT_DELIVERED
            return _insert(
                conn,
                REQUESTS,
                kind=IngestRequestKind.BARS,
                source=DataSource.ALPACA_API,
                client_label='system-test',
                state=state,
                created_at=created_at,
                finished_at=None if is_open else created_at + DAY,
            )

        terminal = keep(REQUESTS, request(CUTOFF + DAY))
        keep(REQUESTS, request(CUTOFF, IngestRequestState.EMPTY))  # the cutoff is inclusive
        keep(REQUESTS, request(CUTOFF + 2 * DAY, IngestRequestState.FAILED))
        drop(REQUESTS, request(CUTOFF - DAY))
        not_delivered = drop(REQUESTS, request(CUTOFF + DAY, IngestRequestState.NOT_DELIVERED))
        drop(REQUESTS, request(CUTOFF - 3 * DAY, IngestRequestState.NOT_DELIVERED))
        s.open_request = str(not_delivered)
        # Attempt rows in the window: one naming the terminal request (kept), one naming the open one (DROPPED, not
        # nulled: an attempt row always names its request).
        keep(RUNS, run(straddling, IngestRunState.FAILED, CUTOFF + 3 * DAY, terminal, 1))
        drop(RUNS, run(straddling, IngestRunState.FAILED, CUTOFF + 4 * DAY, not_delivered, 1))

        # The five wiped tables (O1, O3): a row each, none kept.
        drop(INSTANCES, _insert(conn, INSTANCES, instance_id=f'dsreset-{run_id}'))
        drop(
            READS,
            _insert(
                conn,
                READS,
                dataset_id=straddling.id,
                client_id='system-test',
                client_kind=ReadClientKind.SDK,
                kind=DatasetReadKind.READ,
                route='/internal/asset-data',
                read_at=MOMENT - DAY,
            ),
        )
        drop(
            DAILY,
            _insert(
                conn,
                DAILY,
                dataset_id=straddling.id,
                client_id='system-test',
                client_kind=ReadClientKind.SDK,
                day=date(2026, 5, 1),
                last_read_at=MOMENT - 31 * DAY,
            ),
        )
        drop(
            OVERRIDES,
            _insert(
                conn,
                OVERRIDES,
                actor='operator',
                dataset_id=straddling.id,
                owner='system-test',
                action=OverrideAction.SET_EXPIRY,
                reason='dev seed reset system test',
            ),
        )
        drop(
            DELETIONS,
            _insert(
                conn,
                DELETIONS,
                dataset_id=uuid.uuid4(),
                owner='system-test',
                asset_symbol='ZZDSGONE',
                source='ALPACA_API',
                granularity='1min',
                update_type='STATIC',
                range_start=CUTOFF,
                cause='OPERATOR',
                actor='operator',
            ),
        )

        # Asset lists (O6): an older snapshot, the diff base (newest NON-EMPTY before the cutoff), an empty one after
        # it, and the window's; rows and events follow their snapshot.
        def snapshot(as_of: datetime, rows: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
            sid = _insert(
                conn,
                SNAPSHOTS,
                source=LIST_SOURCE,
                asset_type=AssetType.STOCK,
                asset_list=LIST,
                request_id=uuid.uuid4(),
                attempt_no=1,
                as_of=as_of,
                row_count=rows,
                received_at=as_of + timedelta(hours=1),
            )
            row_ids = [
                _insert(
                    conn,
                    ROWS,
                    snapshot_id=sid,
                    vendor_asset_id=f'dsreset-{run_id}-asset-{n}',
                    symbol=f'ZZDSRS{run_id}{n}',
                    name=f'dev seed reset asset {n}',
                    exchange='NYSE',
                    active=True,
                    tradable=True,
                    raw_payload=b'{}',
                )
                for n in range(rows)
            ]
            return sid, row_ids

        def event(sid: uuid.UUID, previous: uuid.UUID, as_of: datetime) -> uuid.UUID:
            return _insert(
                conn,
                EVENTS,
                source=LIST_SOURCE,
                asset_list=LIST,
                kind=AssetListEventKind.APPEARED,
                vendor_asset_id=f'dsreset-{run_id}-asset-0',
                symbol=f'ZZDSRS{run_id}0',
                snapshot_id=sid,
                previous_snapshot_id=previous,
                as_of=as_of,
                previous_as_of=as_of - DAY,
            )

        older, older_rows = snapshot(CUTOFF - 40 * DAY, 1)
        base, base_rows = snapshot(CUTOFF - 20 * DAY, 2)
        empty, _ = snapshot(CUTOFF - 5 * DAY, 0)
        recent, recent_rows = snapshot(CUTOFF + 5 * DAY, 1)
        for sid in (base, recent):
            keep(SNAPSHOTS, sid)
        for sid in (older, empty):
            drop(SNAPSHOTS, sid)
        for rid in (*base_rows, *recent_rows):
            keep(ROWS, rid)
        for rid in older_rows:
            drop(ROWS, rid)
        keep(EVENTS, event(base, older, CUTOFF - 20 * DAY))
        keep(EVENTS, event(recent, base, CUTOFF + 5 * DAY))
        drop(EVENTS, event(older, older, CUTOFF - 40 * DAY))

        # Corporate actions (O2 ruling): 179 days kept, 181 dropped.
        def action(days: int) -> uuid.UUID:
            return _insert(
                conn,
                ACTIONS,
                source=LIST_SOURCE,
                vendor_action_id=f'dsreset-{run_id}-{days}',
                action_type='forward_split',
                raw_payload=b'{}',
                payload_hash=f'dsreset-{run_id}-{days}',
                request_id=uuid.uuid4(),
                attempt_no=1,
                as_of=MOMENT - days * DAY,
            )

        keep(ACTIONS, action(179))
        drop(ACTIONS, action(181))

        # Panels: every version row kept whole; passes and transitions by O5.
        source_settings = keep(
            SOURCE_SETTINGS,
            _insert(
                conn,
                SOURCE_SETTINGS,
                source=PANEL_SOURCE,
                version_no=1,
                # ADR tj-2ayia2: a cap of 1 is refused by request_cap_min now; NULL ramp and cap 2 carry both shapes.
                ramp_datasets_per_hour=None,
                request_cap_per_hour=2,
                corporate_action_window_pre_days=0,
                corporate_action_window_post_days=0,
                actor='operator',
                reason='dev seed reset system test',
            ),
        )
        panel = keep(PANELS, _insert(conn, PANELS, name=f'system-test-{run_id}-dsreset', source=PANEL_SOURCE))
        settings = keep(
            SETTINGS,
            _insert(
                conn,
                SETTINGS,
                panel_id=panel,
                version_no=1,
                enabled=False,
                cadence=PanelCadence.DAILY,
                actor='operator',
                reason='dev seed reset system test panel',
            ),
        )
        s.panel, s.settings, s.source_settings = str(panel), str(settings), str(source_settings)

        def open_pass(started_at: datetime) -> uuid.UUID:
            return _insert(
                conn,
                PASSES,
                panel_id=panel,
                kind=PanelPassKind.MEMBERSHIP,
                settings_version_id=settings,
                source_settings_version_id=source_settings,
                active_snapshot_id=uuid.uuid4(),
                started_at=started_at,
                finished_at=started_at + timedelta(minutes=5),
                dry_run=False,
            )

        def row(
            pass_id: uuid.UUID, member: str, kind: PanelTransition, as_of: datetime, seq: int, request=None
        ) -> uuid.UUID:
            return _insert(
                conn,
                TRANSITIONS,
                pass_id=pass_id,
                panel_id=panel,
                vendor_asset_id=f'dsreset-{run_id}-{member}',
                symbol=f'ZZDSRS{run_id}{member.upper()}',
                transition=kind,
                request_id=request,
                as_of=as_of,
                recorded_at=as_of,
                seq=seq,
            )

        old_pass = open_pass(CUTOFF - 20 * DAY)
        exit_pass = open_pass(CUTOFF - 15 * DAY)
        window_pass = open_pass(CUTOFF + DAY)
        keep(PASSES, old_pass)  # by the closure: it holds a kept member's ENTERED
        keep(PASSES, window_pass)
        drop(PASSES, exit_pass)
        keep(TRANSITIONS, row(old_pass, 'm1', PanelTransition.ENTERED, CUTOFF - 20 * DAY, 1))  # still a member
        drop(TRANSITIONS, row(old_pass, 'm2', PanelTransition.ENTERED, CUTOFF - 20 * DAY, 2))  # then exited
        drop(TRANSITIONS, row(exit_pass, 'm2', PanelTransition.EXITED, CUTOFF - 15 * DAY, 1))
        keep(TRANSITIONS, row(window_pass, 'm5', PanelTransition.ENTERED, CUTOFF + DAY, 1))
        # tj-7zjov1.333: m3 entered before the cutoff, then its CREATED (alone in its own pass) names the OPEN request:
        # the ENTERED is reloaded, the CREATED and its pass are not, so m3's latest row is ENTERED and the next pass
        # creates its dataset again. m6, in the window, has a CREATED naming a terminal request: both kept.
        created_pass = open_pass(CUTOFF - 18 * DAY)
        drop(PASSES, created_pass)
        keep(TRANSITIONS, row(old_pass, 'm3', PanelTransition.ENTERED, CUTOFF - 20 * DAY, 3))
        drop(TRANSITIONS, row(created_pass, 'm3', PanelTransition.CREATED, CUTOFF - 18 * DAY, 1, not_delivered))
        keep(TRANSITIONS, row(window_pass, 'm6', PanelTransition.ENTERED, CUTOFF + DAY, 2))
        keep(TRANSITIONS, row(window_pass, 'm6', PanelTransition.CREATED, CUTOFF + DAY, 3, terminal))
    for name in TABLES:
        assert s.kept[name] or s.dropped[name], f'{name}: no row written, so the test proves nothing for it'
    return s


# ---- driving the tool ------------------------------------------------------------------------------------------------


def _raw_connect(db: ScratchDb, pg_settings: Any) -> Callable[[], Any]:
    def connect() -> Any:
        return psycopg2.connect(
            host=pg_settings.host,
            port=pg_settings.port,
            user=pg_settings.user,
            password=pg_settings.password,
            dbname=db.name,
            connect_timeout=pg_settings.connect_timeout,
        )

    return connect


def _reset(connect: Callable[[], Any], root: Path, label: str) -> list[str]:
    lines: list[str] = []
    tool.run_reset(
        tool.checked_connect(connect, 'test'),
        tool.Reporter(lines.append),
        lambda _seconds: None,
        label=label,
        moment=MOMENT,
        seed_root=root,
    )
    return lines


def _ids(db: ScratchDb) -> dict[str, set[str]]:
    with db.engine.connect() as conn:
        return {name: {str(v) for v in conn.execute(sa.select(key(table))).scalars()} for name, table in TABLES.items()}


def _sequence(db: ScratchDb) -> tuple[int, bool]:
    with db.engine.connect() as conn:
        name = conn.execute(sa.text("SELECT pg_get_serial_sequence('stock_market_activity', 'id')")).scalar_one()
        value, called = conn.execute(sa.text(f'SELECT last_value, is_called FROM {name}')).one()  # nosec B608
    return int(value), bool(called)


def _next_bar_id(db: ScratchDb, bar_values, entry_id: str, at: datetime) -> int:
    with db.engine.begin() as conn:
        entry = conn.execute(sa.select(*ENTRIES.c).where(ENTRIES.c.id == uuid.UUID(entry_id))).one()
        return conn.execute(sa.insert(BARS).values(bar_values(entry, timestamp=at)).returning(BARS.c.id)).scalar_one()


def _revision(db: ScratchDb) -> str | None:
    return db.revision()


def _assert_exactly_the_kept_rows(db: ScratchDb, seeded: Seeded) -> None:
    found = _ids(db)
    for name in TABLES:
        assert found[name] == seeded.kept[name], (
            name,
            sorted(seeded.kept[name] - found[name]),  # kept rows missing
            sorted(found[name] - seeded.kept[name]),  # rows that should not be there
        )
    for name in WIPED:
        assert found[name] == set(), name
    with db.engine.connect() as conn:
        start = conn.execute(
            sa.select(ENTRIES.c.start).where(ENTRIES.c.id == uuid.UUID(seeded.clipped_entry))
        ).scalar_one()
    assert start == SESSION
    _assert_terminal_only_and_nothing_dangles(db, seeded)


def _assert_terminal_only_and_nothing_dangles(db: ScratchDb, seeded: Seeded) -> None:
    """tj-7zjov1.333 on the reloaded tables (the scratch database is ours alone, so this is the whole of it)."""
    with db.engine.connect() as conn:
        states = set(conn.execute(sa.select(REQUESTS.c.state)).scalars())
        requests = {str(v) for v in conn.execute(sa.select(REQUESTS.c.id)).scalars()}
        named = {
            str(v)
            for table in (RUNS, TRANSITIONS)
            for v in conn.execute(sa.select(table.c.request_id).where(table.c.request_id.is_not(None))).scalars()
        }
    # Only the three terminal states came back; the open request did not.
    assert {state.value for state in states} == {'SUCCEEDED', 'EMPTY', 'FAILED'}, states
    assert seeded.open_request not in requests
    # Nothing dangles: every request an attempt row or a CREATED row names was reloaded with it.
    assert named, 'no row names a request, so the dangle check proves nothing'
    assert named <= requests, sorted(named - requests)
    assert seeded.open_request not in named


# ---- the round trip ----------------------------------------------------------------------------------------------------


def test_the_dev_reset_keeps_exactly_the_ruled_rows_and_load_restores_them(
    migrated, head, pg_settings, entry_values, bar_values, run_identity, data_store_url, tmp_path
):
    db = migrated
    seeded = _seed(db, entry_values, bar_values, run_identity.run_id.upper()[:6])
    before = _ids(db)
    assert before == {name: seeded.kept[name] | seeded.dropped[name] for name in TABLES}
    assert _sequence(db) == (seeded.highest_bar_before, True)
    connect = _raw_connect(db, pg_settings)
    root = tmp_path / 'seed'

    # THE PRESERVING RESET.
    lines = _reset(connect, root, 'dev')
    assert lines[-1] == (
        f'reset: verified clean (all eighteen tables as loaded, {sum(map(len, seeded.kept.values()))} rows in all)'
    )
    _assert_exactly_the_kept_rows(db, seeded)
    assert _revision(db) == head  # alembic_version untouched
    directory = root / head / STAMP
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['revision'] == manifest['checkout_head'] == head
    assert {t: e['rows'] for t, e in manifest['tables'].items()} == {n: len(seeded.kept[n]) for n in TABLES}

    # R3: the sequence is at max(id) of the kept bars, so the next INSERT without an id gets max + 1.
    top = max(seeded.kept_bar_ids)
    assert top < seeded.highest_bar_before
    assert _sequence(db) == (top, True)
    assert _next_bar_id(db, bar_values, seeded.clipped_entry, SESSION + 2 * DAY) == top + 1

    # The panel tables are live: a new pass and a transition under the kept panel and versions, and a pass naming
    # no panel is refused by the foreign key.
    with db.engine.begin() as conn:
        new_pass = _insert(
            conn,
            PASSES,
            panel_id=uuid.UUID(seeded.panel),
            kind=PanelPassKind.MEMBERSHIP,
            settings_version_id=uuid.UUID(seeded.settings),
            source_settings_version_id=uuid.UUID(seeded.source_settings),
            active_snapshot_id=uuid.uuid4(),
            started_at=MOMENT,
            dry_run=False,
        )
        _insert(
            conn,
            TRANSITIONS,
            pass_id=new_pass,
            panel_id=uuid.UUID(seeded.panel),
            vendor_asset_id='dsreset-after',
            symbol='ZZDSAFTER',
            transition=PanelTransition.ENTERED,
            as_of=MOMENT,
            recorded_at=MOMENT,
            seq=1,
        )
    with pytest.raises(sa.exc.IntegrityError), db.engine.begin() as conn:
        _insert(
            conn,
            PASSES,
            panel_id=uuid.uuid4(),
            kind=PanelPassKind.MEMBERSHIP,
            settings_version_id=uuid.UUID(seeded.settings),
            source_settings_version_id=uuid.UUID(seeded.source_settings),
            active_snapshot_id=uuid.uuid4(),
            started_at=MOMENT,
            dry_run=False,
        )

    # THE LOAD: the test label's plain wipe empties all eighteen, then the seed goes back in.
    wipe = _reset(connect, tmp_path / 'unused', 'test')
    assert wipe[-1] == 'reset: verified clean (all eighteen tables 0)'
    assert all(not ids for ids in _ids(db).values())
    assert not (tmp_path / 'unused').exists()

    # THE STACK RULE (tj-7zjov1.291 addendum 2026-11-04, P58 tj-7zjov1.327): the reset above ran with label 'dev', so
    # the manifest says 'dev', and this target is the test stack. The load refuses with exit 3 naming both, before the
    # lock: nothing reaches Postgres but the cluster_name proof, and the tables stay empty.
    with pytest.raises(tool.RevisionRefused) as refused:
        tool.run_load(
            tool.load_environment({}),
            tool.HttpClient(data_store_url),
            connect,
            directory.parent,
            dry=False,
            out=tool.Reporter([].append),
        )
    assert str(refused.value) == (
        "refused: this seed was exported from the 'dev' stack and the target is the 'test' stack; nothing was written"
    )
    assert all(not ids for ids in _ids(db).values())
    # THE SECOND ADAPTATION, said plainly: a test-label reset keeps no seed, so the seed under test is the dev-label one
    # with its manifest's stack set to 'test' (the manifest is not covered by any file hash; every .copy file is
    # unchanged and still checked by size and sha256).
    manifest['stack'] = 'test'
    (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    lines = []
    tool.run_load(
        tool.load_environment({}),
        tool.HttpClient(data_store_url),
        connect,
        directory.parent,  # the revision directory: its newest complete stamp
        dry=False,
        out=tool.Reporter(lines.append),
    )
    assert lines[-1] == f'load: committed from {directory}'
    _assert_exactly_the_kept_rows(db, seeded)
    assert _sequence(db) == (top, True)
    assert _next_bar_id(db, bar_values, seeded.clipped_entry, SESSION + 3 * DAY) == top + 1


class _FaultyConnection:
    """A psycopg2 connection whose cursors run a hook before each COPY and each execute (the tool's own statements)."""

    def __init__(self, inner: Any, on_copy: Callable[[str], None], on_execute: Callable[[str], None]) -> None:
        self._inner, self._on_copy, self._on_execute = inner, on_copy, on_execute

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return _FaultyCursor(self._inner.cursor(*args, **kwargs), self._on_copy, self._on_execute)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _FaultyCursor:
    def __init__(self, inner: Any, on_copy: Callable[[str], None], on_execute: Callable[[str], None]) -> None:
        self._inner, self._on_copy, self._on_execute = inner, on_copy, on_execute

    def __enter__(self) -> '_FaultyCursor':
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)

    def copy_expert(self, sql: str, file: Any, *args: Any, **kwargs: Any) -> Any:
        self._on_copy(sql)
        return self._inner.copy_expert(sql, file, *args, **kwargs)

    def execute(self, sql: Any, *args: Any, **kwargs: Any) -> Any:
        self._on_execute(sql)
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _faulty_reset(db: ScratchDb, pg_settings: Any, root: Path, on_copy, on_execute) -> tuple[str, list[str]]:
    raw = _raw_connect(db, pg_settings)
    lines: list[str] = []
    with pytest.raises(tool.DevDatasetsError) as caught:
        tool.run_reset(
            tool.checked_connect(lambda: _FaultyConnection(raw(), on_copy, on_execute), 'test'),
            tool.Reporter(lines.append),
            lambda _seconds: None,
            label='dev',
            moment=MOMENT,
            seed_root=root,
        )
    return str(caught.value), lines


def _assert_rolled_back(db: ScratchDb, before: dict[str, set[str]], sequence: tuple[int, bool], head: str, root: Path):
    assert _ids(db) == before
    assert _sequence(db) == sequence
    assert _revision(db) == head
    # The export stays on disk, complete.
    [manifest] = list(root.glob('*/*/manifest.json'))
    assert json.loads(manifest.read_text(encoding='utf-8'))['revision'] == head


def test_a_file_changed_during_the_reload_rolls_everything_back_on_postgres(
    migrated, head, pg_settings, entry_values, bar_values, run_identity, tmp_path
):
    db = migrated
    _seed(db, entry_values, bar_values, run_identity.run_id.upper()[:6])
    before, sequence = _ids(db), _sequence(db)
    root = tmp_path / 'seed'
    edited: list[str] = []

    def edit(sql: str) -> None:
        # The panel's name, one byte changed and the length kept: the COPY itself succeeds, so the refusal is the
        # hash of what the server read, after the TRUNCATE and the COPYs of every table before panel.
        if sql != dev_seed.LOAD_SQL['panel']:
            return
        [path] = root.glob('*/*/panel.copy')
        data = path.read_bytes()
        assert b'-dsreset' in data
        path.write_bytes(data.replace(b'-dsreset', b'-dsresex', 1))
        edited.append('panel')

    message, lines = _faulty_reset(db, pg_settings, root, edit, lambda _sql: None)
    assert message == 'panel.copy changed while it was loaded: refused'
    assert edited == ['panel']
    # The TRUNCATE ran and the tables before panel were reloaded, inside the transaction; Postgres undid all of it.
    assert any(line.startswith('reset: the seed is on disk') for line in lines)
    assert '  panel_settings_version: loaded' not in lines
    _assert_rolled_back(db, before, sequence, head, root)


def test_a_failure_after_the_setval_rolls_the_sequence_back_with_the_truncate_on_postgres(
    migrated, head, pg_settings, entry_values, bar_values, run_identity, tmp_path
):
    # setval is not transactional in general; here it acts on the sequence the TRUNCATE ... RESTART IDENTITY reset in
    # the same transaction, so a failure after it must still leave the sequence as it was (else the next bar insert
    # would collide with a restored row: the highest id before the reset is a dropped bar's).
    db = migrated
    _seed(db, entry_values, bar_values, run_identity.run_id.upper()[:6])
    before, sequence = _ids(db), _sequence(db)
    root = tmp_path / 'seed'
    seen: list[str] = []

    def fail_after_setval(sql: Any) -> None:
        text = ' '.join(str(sql).split())
        if seen and seen[-1].startswith('SELECT setval('):
            seen.append(text)
            raise RuntimeError('injected after the setval')
        seen.append(text)

    message, _ = _faulty_reset(db, pg_settings, root, lambda _sql: None, fail_after_setval)
    assert message == 'the reset transaction failed and was rolled back (RuntimeError)'
    assert seen[-2].startswith('SELECT setval(')
    _assert_rolled_back(db, before, sequence, head, root)


def test_the_scratch_database_is_never_the_stack_database(scratch_db, pg_settings):
    # The isolation claim, checked: the reset above runs on a database whose name is not the stack's.
    assert scratch_db.name != pg_settings.database
    with scratch_db.engine.connect() as conn:
        assert conn.execute(sa.text('SELECT current_database()')).scalar_one() == scratch_db.name
