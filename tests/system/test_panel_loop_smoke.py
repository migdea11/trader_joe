"""The RUNNING data_store's own panel loop writes a finished pass (P26 S7, tj-7zjov1.287): the one wiring smoke.

Design: ADR tj-26jt9w addenda 20 and 21 and server/data/store/CLAUDE.md "Panels and the panel manager": one loop,
run_periodic(Every(TICK_INTERVAL)), TICK_INTERVAL 5 minutes, the first tick at startup; the first MEMBERSHIP pass of a
panel runs at its creation's first tick, on the newest active snapshot, without waiting (owner ruling 2026-10-27 (c)).

WHAT ONLY THIS CAN REACH. P26a (test_panel_manager_pg.py) drives PanelManager.tick in process against the stack's
Postgres; nothing there proves the loop the lifespan starts is running, ticking ALPACA_API (the one source with a
trading calendar the fake stack serves), and committing through the image's own engine. Here the test only writes the
panel through the REST write (W1, the instance secret) and reads the pass row from the database.

THE PANEL SELECTS NOTHING ON PURPOSE. Its allow list names one symbol of this run, which no asset list holds, so the
pass is written and finished with no member: no ENTERED, no CREATED, no request, no dataset, nothing to clean up but
the panel's own rows. A pass that selected the fake's listed symbols would mint panel-owned datasets for VFV, XEQT and
AAPL, which nothing in this suite could remove by run identity. Membership decisions are P26a's (PM1 to PM8).

THE WAIT IS BOUNDED BY THE TICK: at most TICK_INTERVAL from the loop's last fire, plus the pass, plus a margin
(PASS_BOUND_SECONDS, 330 s). Every measures from the start of the last run, so the panel's first pass comes at the
first tick after the create, wherever in the interval the create lands. That makes this the slowest test of the tier,
up to five and a half minutes, by design: there is no knob to tick sooner (TICK_INTERVAL is a constant) and none is
added for a test.

PRECONDITIONS, failed with a message rather than skipped: the boot snapshot (P47: the active list is requested when an
ingest instance starts) exists for ALPACA_API; the stack is not in dry run (PANEL_MANAGER_DRY_RUN would write a dry-run
pass, which the assertion names). If ALPACA_API has no source settings version, the create gives the first one and the
test deletes it at the end; otherwise the existing one is used and left alone.

CLEANUP, by id, a test-only delete (the tables are append-only): the pass's transitions (none expected), the panel's
passes and settings versions, the panel, and the source settings version this test created.

Reds on (P26 mutant plan, tj-7zjov1.38; tj-7zjov1.321): M7a (the loop off by default, data/store/app/
panel_manager.py:177 default 'false') at 'no finished pass', after the full wait; M7b (the dry-run setting inverted,
panel_manager.py:1091) at 'the pass is a dry run'.
Covered by, without a mutant (architect ruling 00:28 UTC 2026-11-04 on tj-7zjov1.38): the pass kind, its settings
version, its active snapshot and its zero transitions restate P26a PM1 (tests/system/test_panel_manager_pg.py), whose
planted mutants in the tj-7zjov1.262 mutation pass (m1, m4, m6, m7) red PM1.
"""

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_stock import DataSource
from data.store.app.database.models.asset_list import AssetListSnapshot
from data.store.app.database.models.panel import (
    Panel,
    PanelMemberTransition,
    PanelPass,
    PanelPassKind,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.app.panel_manager import TICK_INTERVAL
from schemas.data_ingest.submit import AssetListStatus


pytestmark = pytest.mark.data_store

PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
SNAPSHOTS = AssetListSnapshot.__table__
SOURCE = 'DATA_SOURCE_ALPACA_API'
RATES = {'rampDatasetsPerHour': 1, 'requestCapPerHour': 2}
PASS_BOUND_SECONDS = TICK_INTERVAL.total_seconds() + 30.0
POLL_SECONDS = 2.0


@dataclass
class Written:
    panels: list[uuid.UUID] = field(default_factory=list)
    source_versions: list[uuid.UUID] = field(default_factory=list)


@pytest.fixture
def written(pg_engine: Engine) -> Iterator[Written]:
    ids = Written()
    yield ids
    with pg_engine.begin() as conn:
        if ids.panels:
            conn.execute(sa.delete(TRANSITIONS).where(TRANSITIONS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PASSES).where(PASSES.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(SETTINGS).where(SETTINGS.c.panel_id.in_(ids.panels)))
            conn.execute(sa.delete(PANELS).where(PANELS.c.id.in_(ids.panels)))
        if ids.source_versions:
            conn.execute(sa.delete(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id.in_(ids.source_versions)))


def _active_snapshots(pg_engine: Engine) -> set[uuid.UUID]:
    with pg_engine.connect() as conn:
        return set(
            conn.execute(
                sa.select(SNAPSHOTS.c.id).where(
                    SNAPSHOTS.c.source == DataSource.ALPACA_API, SNAPSHOTS.c.asset_list == AssetListStatus.ACTIVE
                )
            ).scalars()
        )


def _finished_passes(pg_engine: Engine, panel_id: uuid.UUID) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(PASSES)
                .where(PASSES.c.panel_id == panel_id, PASSES.c.finished_at.is_not(None))
                .order_by(PASSES.c.started_at)
            )
        )


def test_the_running_panel_loop_writes_one_finished_membership_pass_for_a_new_enabled_panel(
    data_store, pg_engine, written, run_identity, own_symbol
):
    assert _active_snapshots(pg_engine), (
        'no active ALPACA_API asset-list snapshot: the boot snapshot (P47) is missing, so no membership pass can run'
    )
    has_version = data_store.get(f'/ui/v1/panel-sources/{SOURCE}/settings-versions').json().get('items')
    body = {
        'name': f'system-test-{run_identity.run_id}-loop-smoke',
        'source': SOURCE,
        'enabled': True,
        # A symbol of this run: no asset list holds it, so the pass selects nothing.
        'allowSymbols': [own_symbol],
        'reason': 'system test: the running loop writes a pass',
    }
    created = data_store.post('/ui/v1/panels', json=body | ({} if has_version else RATES), auth='right')
    assert created.status_code == 201, data_store.describe(created)
    panel_id = uuid.UUID(created.json()['id'])
    written.panels.append(panel_id)
    if not has_version:
        with pg_engine.connect() as conn:
            written.source_versions += conn.execute(
                sa.select(SOURCE_SETTINGS.c.id).where(SOURCE_SETTINGS.c.source == DataSource.ALPACA_API)
            ).scalars()
    with pg_engine.connect() as conn:
        (version_id,) = conn.execute(sa.select(SETTINGS.c.id).where(SETTINGS.c.panel_id == panel_id)).scalars()

    deadline = time.monotonic() + PASS_BOUND_SECONDS
    passes: list[sa.Row] = []
    while not passes and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        passes = _finished_passes(pg_engine, panel_id)
    assert passes, (
        f'no finished pass for panel {panel_id} within {PASS_BOUND_SECONDS:.0f}s (one tick is '
        f'{TICK_INTERVAL.total_seconds():.0f}s): the running loop did not tick it. Check the data_store log for '
        "'The panel manager is off', 'is not runnable' or 'waits: no active snapshot'"
    )
    (first,) = passes
    assert first.kind == PanelPassKind.MEMBERSHIP, f'the first pass is {first.kind}'
    assert first.dry_run is False, 'the pass is a dry run: the stack runs with PANEL_MANAGER_DRY_RUN set'
    assert first.settings_version_id == version_id, 'the pass names another settings version than the panel has'
    assert first.active_snapshot_id in _active_snapshots(pg_engine), 'the pass names no active ALPACA_API snapshot'
    with pg_engine.connect() as conn:
        transitions = conn.execute(
            sa.select(sa.func.count()).select_from(TRANSITIONS).where(TRANSITIONS.c.pass_id == first.id)
        ).scalar_one()
    assert transitions == 0, f'a panel that selects nothing wrote {transitions} transitions'
