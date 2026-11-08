"""The S6 scenario's panel: one enabled ALPACA_API panel the RUNNING data_store's own loop would tick (tj-7zjov1.286).

WHY ALPACA_API AND THE RUNNING LOOP. S6 is about the running data_store's kill switch and dry run
(PANEL_MANAGER_ENABLED, PANEL_MANAGER_DRY_RUN, read when its lifespan starts), so the panel must be one that loop
ticks: its loop runs over SOURCE_CALENDARS, which is ALPACA_API only (test_panel_manager_pg.py uses MANUAL_ENTRY to stay
OUT of that loop; this module is the opposite case).

WHY IT CANNOT CREATE ANYTHING EVEN IF A SWITCH FAILED. The panel selects one symbol of the fake's active list
(SYMBOL), and S6 appends its own ALPACA_API source settings version, newest so current, with ramp_datasets_per_hour
0 and request_cap_per_hour 0: a LIVE pass would write its pass row and an ENTERED and a DEFERRED (ramp_rate), and no
request and no dataset. So a broken kill switch shows as a pass row, and never as a real panel dataset left in the
shared database. The version is deleted with the panel, so the source's previous version is current again.

THE TAG. The panel is named NAME_PREFIX plus phase A's run id, and the source version's reason starts with
REASON_PREFIX, so phase B and a later phase A find them without a file. Every row is deleted by id at the end (a
test-only delete: the panel tables are append-only for the service), passes and transitions first.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from common.enums.data_stock import DataSource
from data.store.app.database.crud.panel_settings import PanelSettings, SourceSettings
from data.store.app.database.models.asset_list import AssetListRow, AssetListSnapshot
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelMemberTransition,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from schemas.data_ingest.submit import AssetListStatus
from tests.fakes.market_data import FAKE_LISTED_SYMBOLS


PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
SNAPSHOTS = AssetListSnapshot.__table__
SNAPSHOT_ROWS = AssetListRow.__table__

SOURCE = DataSource.ALPACA_API
SYMBOL = FAKE_LISTED_SYMBOLS[0]
NAME_PREFIX = 'p26-phase-s6-'
REASON_PREFIX = 'p26-phase-s6 rates'
SETTINGS_V1 = PanelSettings(enabled=True, size=1, cadence=PanelCadence.DAILY, allow_symbols=(SYMBOL,))
PAUSED_RATES = SourceSettings(ramp_datasets_per_hour=0, request_cap_per_hour=0)


def panels(pg_engine: Engine) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(PANELS).where(PANELS.c.name.startswith(NAME_PREFIX, autoescape=True))))


def source_versions(pg_engine: Engine) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(SOURCE_SETTINGS).where(
                    SOURCE_SETTINGS.c.source == SOURCE,
                    SOURCE_SETTINGS.c.reason.startswith(REASON_PREFIX, autoescape=True),
                )
            )
        )


def passes_of(pg_engine: Engine, panel_id: uuid.UUID) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(PASSES).where(PASSES.c.panel_id == panel_id).order_by(PASSES.c.started_at)))


def transitions_of(pg_engine: Engine, panel_id: uuid.UUID) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(TRANSITIONS)
                .where(TRANSITIONS.c.panel_id == panel_id)
                .order_by(TRANSITIONS.c.pass_id, TRANSITIONS.c.seq)
            )
        )


def assert_active_snapshot_holds_symbol(pg_engine: Engine) -> None:
    """The newest non-empty ACTIVE snapshot of the source lists SYMBOL, so a pass, were one to run, has a member."""
    with pg_engine.connect() as conn:
        newest = conn.execute(
            sa.select(SNAPSHOTS.c.id)
            .where(
                SNAPSHOTS.c.source == SOURCE,
                SNAPSHOTS.c.asset_list == AssetListStatus.ACTIVE,
                SNAPSHOTS.c.row_count > 0,
            )
            .order_by(SNAPSHOTS.c.as_of.desc())
            .limit(1)
        ).scalar_one_or_none()
        if newest is None:
            pytest.fail(
                f'no active {SOURCE.value} asset-list snapshot: a pass would wait, so S6 would prove nothing. The '
                'store requests one when an ingest instance starts (ASSET_SNAPSHOT_ENABLED).',
                pytrace=False,
            )
        listed = conn.execute(
            sa.select(sa.func.count())
            .select_from(SNAPSHOT_ROWS)
            .where(SNAPSHOT_ROWS.c.snapshot_id == newest, SNAPSHOT_ROWS.c.symbol == SYMBOL)
        ).scalar_one()
    assert listed, f'the newest active snapshot does not list {SYMBOL}, so the panel would select nothing'


def delete_s6_rows(pg_engine: Engine) -> None:
    """Delete every S6 panel (transitions, passes, versions, panel), then every S6 source version, by id."""
    panel_ids = [row.id for row in panels(pg_engine)]
    version_ids = [row.id for row in source_versions(pg_engine)]
    with pg_engine.begin() as conn:
        if panel_ids:
            conn.execute(sa.delete(TRANSITIONS).where(TRANSITIONS.c.panel_id.in_(panel_ids)))
            conn.execute(sa.delete(PASSES).where(PASSES.c.panel_id.in_(panel_ids)))
            conn.execute(sa.delete(SETTINGS).where(SETTINGS.c.panel_id.in_(panel_ids)))
            conn.execute(sa.delete(PANELS).where(PANELS.c.id.in_(panel_ids)))
    if not version_ids:
        return
    try:
        with pg_engine.begin() as conn:
            conn.execute(sa.delete(SOURCE_SETTINGS).where(SOURCE_SETTINGS.c.id.in_(version_ids)))
    except IntegrityError:
        pytest.fail(
            f'the S6 source settings version(s) {version_ids} are named by a pass of a panel S6 did not create (an '
            'enabled ALPACA_API panel another module left behind, ticked under them); they were not deleted',
            pytrace=False,
        )
