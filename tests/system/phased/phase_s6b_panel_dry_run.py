"""P26 S6, phase B: in a dry run the RUNNING data_store writes only flagged rows and applies nothing (tj-7zjov1.286).

Runs after phase_s6a_panel_kill_switch.py and the orchestrator's test_env data_store {PANEL_MANAGER_DRY_RUN: true}
(the step list is in that file and in the hand-back of tj-7zjov1.286). It fails loudly when phase A never ran (no S6
panel), and it deletes every S6 row at its end, passed or failed.

WHAT IT PINS (data/store/CLAUDE.md, Panels: "Dry run writes dry-run passes, whose ledger rows every read ignores: the
decisions are recorded, but no dataset is created, none is stopped and no release tick runs"):
  * a pass is written within DRY_RUN_WAIT_S: the recreated data_store's loop ticks at its start and the panel, never
    passed in a dry run, is due (the dry-run manager reads only dry-run passes for its due rule). This is also the
    control for phase A's wait: the same panel, unchanged, is ticked within the same bound once a loop runs;
  * every pass of the panel is a finished MEMBERSHIP pass flagged dry_run (no RELEASE pass: a dry run runs no release
    tick), and every transition belongs to one of them;
  * the decision is recorded: an ENTERED for the selected symbol; and nothing is applied: no CREATED, no transition
    naming a request or a dataset, no panel-owned ingest request written since the panel was created, no panel-owned
    dataset created since then and none stopped (collect false) since then.
"""

import time

import pytest
import sqlalchemy as sa

from data.store.app.client_label import PANEL_OWNER
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.panel import PanelPassKind, PanelTransition
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.panel_manager import TICK_INTERVAL
from tests.system.phased.panels import SYMBOL, delete_s6_rows, panels, passes_of, transitions_of


pytestmark = pytest.mark.data_store

REQUESTS = IngestRequest.__table__
ENTRIES = StoreDatasetEntry.__table__
PHASE_A = 'tests/system/phased/phase_s6a_panel_kill_switch.py'
DRY_RUN_WAIT_S = TICK_INTERVAL.total_seconds() + 30.0
POLL_S = 5.0


def test_s6b_a_dry_run_records_its_decisions_and_applies_nothing(pg_engine, data_store) -> None:
    found = panels(pg_engine)
    if not found:
        pytest.fail(f'no S6 panel: phase A never ran (or failed and cleaned up). Run {PHASE_A} first.', pytrace=False)
    try:
        assert len(found) == 1, f'{len(found)} S6 panels are present; rerun phase A'
        panel = found[0]

        deadline = time.monotonic() + DRY_RUN_WAIT_S
        while True:
            written = passes_of(pg_engine, panel.id)
            if any(row.finished_at is not None for row in written):
                break
            if time.monotonic() >= deadline:
                pytest.fail(
                    f'no finished pass for the S6 panel within {DRY_RUN_WAIT_S:.0f} s of this phase starting: the '
                    'dry-run loop did not tick it (is PANEL_MANAGER_DRY_RUN true and PANEL_MANAGER_ENABLED unset?)',
                    pytrace=False,
                )
            time.sleep(POLL_S)

        written = passes_of(pg_engine, panel.id)
        assert all(row.dry_run for row in written), f'{sum(not row.dry_run for row in written)} pass(es) not dry_run'
        assert {row.kind for row in written} == {PanelPassKind.MEMBERSHIP}, f'pass kinds {[r.kind for r in written]}'

        transitions = transitions_of(pg_engine, panel.id)
        pass_ids = {row.id for row in written}
        assert all(row.pass_id in pass_ids for row in transitions), 'a transition of no dry-run pass of this panel'
        kinds = [row.transition for row in transitions]
        assert PanelTransition.ENTERED in kinds and {row.symbol for row in transitions} == {SYMBOL}, (
            f'the dry run recorded {[(row.symbol, row.transition.value) for row in transitions]}, not ENTERED {SYMBOL}'
        )
        assert PanelTransition.CREATED not in kinds, 'a dry run recorded a CREATED'
        assert all(row.request_id is None and row.dataset_id is None for row in transitions), (
            'a dry-run transition names a request or a dataset'
        )

        with pg_engine.connect() as conn:
            requests = conn.execute(
                sa.select(sa.func.count())
                .select_from(REQUESTS)
                .where(REQUESTS.c.client_label == PANEL_OWNER, REQUESTS.c.created_at >= panel.created_at)
            ).scalar_one()
            created = conn.execute(
                sa.select(sa.func.count())
                .select_from(ENTRIES)
                .where(ENTRIES.c.owner == PANEL_OWNER, ENTRIES.c.created_at >= panel.created_at)
            ).scalar_one()
            stopped = conn.execute(
                sa.select(sa.func.count())
                .select_from(ENTRIES)
                .where(
                    ENTRIES.c.owner == PANEL_OWNER,
                    ENTRIES.c.collect.is_(False),
                    ENTRIES.c.updated_at >= panel.created_at,
                )
            ).scalar_one()
        assert (requests, created, stopped) == (0, 0, 0), (
            f'the dry run applied something: {requests} panel request(s), {created} panel dataset(s) created, '
            f'{stopped} stopped since the panel was created'
        )
    finally:
        delete_s6_rows(pg_engine)
