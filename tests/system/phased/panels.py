"""The S6 scenario's panel: one enabled ALPACA_API panel the RUNNING data_store's own loop would tick (tj-7zjov1.286).

WHY ALPACA_API AND THE RUNNING LOOP. S6 is about the running data_store's kill switch and dry run
(PANEL_MANAGER_ENABLED, PANEL_MANAGER_DRY_RUN, read when its lifespan starts), so the panel must be one that loop
ticks: its loop runs over SOURCE_CALENDARS, which is ALPACA_API only (test_panel_manager_pg.py uses MANUAL_ENTRY to stay
OUT of that loop; this module is the opposite case).

WHY THE RATES ARE ABOVE ZERO (W1 on tj-7zjov1.38, ruled 00:28 UTC 2026-11-04; tj-7zjov1.321). The panel selects one
symbol of the fake's active list (SYMBOL), and each S6 phase appends its own ALPACA_API source settings version,
newest so current, with LIVE_RATES: ramp_datasets_per_hour 1 and request_cap_per_hour 2, the least that lets one
creation through (a creation costs two requests, the clamp query and the bars request; budget_cause in
data/store/app/panel_manager.py). Under rates of 0 every pass, live or dry, DEFERs the member (ramp_rate) before the
create path, so S6-B's 'applies nothing' and S6-A's 'no request' could never go red: a dry run that took the create
path (mutant M6c, panel_manager.py:721) deferred exactly as the clean one did. With these rates the create path is
reachable, so a dry run that applies, or a kill switch that does not hold, writes a CREATED and a request.

WHAT A BROKEN SWITCH NOW LEAVES, AND THE CLEANUP. A live pass on this panel (a failed kill switch, a dry run that
applies) writes one clamp request and one bars request under PANEL_OWNER for SYMBOL, and the delivery intake mints a
panel-owned DAILY dataset for SYMBOL. The size of 1 and the ramp of 1 bound that to one dataset an hour; nothing else
of the database is touched. delete_s6_rows removes it with the panel: it waits for those requests to finish (ingest
may still deliver them, and a report for a deleted request is refused and redelivered), then deletes their attempt
rows, the requests and the dataset (its bars cascade). On a clean stack there is none.

THE REACHABILITY GUARD. assert_create_path_reachable (phase A, before it creates the panel) fails, naming what it
found, when the create path could not be reached whatever the switches do: a panel-owned dataset or an open
panel-owned request for SYMBOL already exists (member_action answers NONE, or the in-flight rule holds the member),
or either hourly window of the source is not empty (with these rates a creation would DEFER). Such a run is 'did not
exercise', not a pass.

THE TAG. The panel is named NAME_PREFIX plus phase A's run id, and the source version's reason starts with
REASON_PREFIX, so phase B and a later phase A find them without a file. Every row is deleted by id at the end (a
test-only delete: the panel tables are append-only for the service), passes and transitions first.
"""

import time
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from common.enums.data_stock import DataSource
from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.crud.panel_settings import PanelSettings, SourceSettings
from data.store.app.database.models.asset_list import AssetListRow, AssetListSnapshot
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.panel import (
    Panel,
    PanelCadence,
    PanelMemberTransition,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
    PanelTransition,
)
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from schemas.data_ingest.submit import AssetListStatus
from tests.fakes.market_data import FAKE_LISTED_SYMBOLS


PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
SNAPSHOTS = AssetListSnapshot.__table__
SNAPSHOT_ROWS = AssetListRow.__table__
REQUESTS = IngestRequest.__table__
RUNS = IngestRun.__table__
ENTRIES = StoreDatasetEntry.__table__

SOURCE = DataSource.ALPACA_API
SYMBOL = FAKE_LISTED_SYMBOLS[0]
NAME_PREFIX = 'p26-phase-s6-'
REASON_PREFIX = 'p26-phase-s6 rates'
SETTINGS_V1 = PanelSettings(enabled=True, size=1, cadence=PanelCadence.DAILY, allow_symbols=(SYMBOL,))
LIVE_RATES = SourceSettings(ramp_datasets_per_hour=1, request_cap_per_hour=2)
OPEN_STATES = (IngestRequestState.PENDING, IngestRequestState.QUEUED, IngestRequestState.NOT_DELIVERED)
# A broken switch's two requests are served by FakeRead at once; this bounds the wait before their rows are deleted.
RESIDUE_WAIT_S = 180.0
RESIDUE_POLL_S = 2.0


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


def assert_create_path_reachable(pg_engine: Engine) -> None:
    """Fail when no pass on SYMBOL could reach the create path, whatever the switches do (module docstring)."""
    hour_ago = sa.func.now() - sa.text("interval '1 hour'")
    with pg_engine.connect() as conn:
        entries = conn.execute(
            sa.select(sa.func.count())
            .select_from(ENTRIES)
            .where(ENTRIES.c.owner == PANEL_OWNER, ENTRIES.c.source == SOURCE, ENTRIES.c.asset_symbol == SYMBOL)
        ).scalar_one()
        in_flight = conn.execute(
            sa.select(sa.func.count())
            .select_from(REQUESTS)
            .where(
                REQUESTS.c.client_label == PANEL_OWNER,
                REQUESTS.c.source == SOURCE,
                REQUESTS.c.asset_symbol == SYMBOL,
                REQUESTS.c.state.in_(OPEN_STATES),
            )
        ).scalar_one()
        created = conn.execute(
            sa.select(sa.func.count())
            .select_from(TRANSITIONS)
            .join(PANELS, PANELS.c.id == TRANSITIONS.c.panel_id)
            .join(PASSES, PASSES.c.id == TRANSITIONS.c.pass_id)
            .where(
                TRANSITIONS.c.transition == PanelTransition.CREATED,
                TRANSITIONS.c.recorded_at >= hour_ago,
                PANELS.c.source == SOURCE,
                PASSES.c.dry_run.is_(False),
            )
        ).scalar_one()
        requested = conn.execute(
            sa.select(sa.func.count())
            .select_from(REQUESTS)
            .where(
                REQUESTS.c.client_label == PANEL_OWNER, REQUESTS.c.source == SOURCE, REQUESTS.c.created_at >= hour_ago
            )
        ).scalar_one()
    found = {
        f'panel-owned {SYMBOL} dataset(s)': entries,
        f'open panel-owned {SYMBOL} request(s)': in_flight,
        f'CREATED row(s) on {SOURCE.value} in the last hour': created,
        f'panel-owned {SOURCE.value} request(s) in the last hour': requested,
    }
    blocking = {what: count for what, count in found.items() if count}
    if blocking:
        pytest.fail(
            f'S6 cannot exercise the create path, so it would prove nothing: {blocking}. A dataset or an open request '
            'means the member is never created; a non-empty hourly window means it DEFERs under LIVE_RATES. Wait out '
            'the hour, or test_reset and migrate the stack, then rerun from phase A.',
            pytrace=False,
        )


def _residue_requests(conn: sa.Connection, since: datetime) -> list[sa.Row]:
    """What a live pass on the S6 panel wrote outside the panel tables: requests for SYMBOL and its datasets'."""
    entry_ids = list(
        conn.execute(
            sa.select(ENTRIES.c.id).where(
                ENTRIES.c.owner == PANEL_OWNER,
                ENTRIES.c.source == SOURCE,
                ENTRIES.c.asset_symbol == SYMBOL,
                ENTRIES.c.created_at >= since,
            )
        ).scalars()
    )
    mine = sa.and_(
        REQUESTS.c.client_label == PANEL_OWNER,
        REQUESTS.c.source == SOURCE,
        REQUESTS.c.asset_symbol == SYMBOL,
        REQUESTS.c.created_at >= since,
    )
    where = sa.or_(mine, REQUESTS.c.dataset_id.in_(entry_ids)) if entry_ids else mine
    return list(conn.execute(sa.select(REQUESTS.c.id, REQUESTS.c.state).where(where)))


def delete_switch_residue(pg_engine: Engine, since: datetime) -> None:
    """Delete the requests, attempt rows and dataset a live pass on the S6 panel wrote, once every request finished.

    On a clean stack there are none. A request still open after RESIDUE_WAIT_S is left with everything else, and the
    phase fails naming the count: deleting it would leave ingest's report refused and redelivered for ever.
    """
    deadline = time.monotonic() + RESIDUE_WAIT_S
    while True:
        with pg_engine.connect() as conn:
            rows = _residue_requests(conn, since)
        still_open = [row for row in rows if row.state in OPEN_STATES]
        if not still_open:
            break
        if time.monotonic() >= deadline:
            pytest.fail(
                f'{len(still_open)} panel-owned {SYMBOL} request(s) written by a live S6 pass are still open after '
                f'{RESIDUE_WAIT_S:.0f} s; they and the S6 rows are left. Rerun phase A once they finish.',
                pytrace=False,
            )
        time.sleep(RESIDUE_POLL_S)
    with pg_engine.begin() as conn:
        if rows:
            ids = [row.id for row in rows]
            conn.execute(sa.delete(RUNS).where(RUNS.c.request_id.in_(ids)))
            conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.id.in_(ids)))
        conn.execute(
            sa.delete(ENTRIES).where(
                ENTRIES.c.owner == PANEL_OWNER,
                ENTRIES.c.source == SOURCE,
                ENTRIES.c.asset_symbol == SYMBOL,
                ENTRIES.c.created_at >= since,
            )
        )


def delete_s6_rows(pg_engine: Engine) -> None:
    """Delete a live pass's residue, every S6 panel (transitions, passes, versions, panel), then every S6 source version."""
    found = panels(pg_engine)
    if found:
        delete_switch_residue(pg_engine, min(row.created_at for row in found))
    panel_ids = [row.id for row in found]
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
