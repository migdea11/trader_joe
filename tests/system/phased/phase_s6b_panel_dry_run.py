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

THE RATES (W1 on tj-7zjov1.38; tj-7zjov1.321). The panel runs under LIVE_RATES (ramp 1, cap 2; panels.py), so a dry
run that took the create path would CREATE, and 'applies nothing' can go red; under rates of 0 it deferred like the
clean path. The recreated data_store's loop ticks at its start, so the dry pass may run BEFORE this phase does: the
rates that decide it are the version phase A appended, current since then. This phase appends one more LIVE_RATES
version before it waits, so a later tick of this phase's wait runs under rates above 0 too; it is deleted with the rest.
The reachability guard runs in phase A only: here the pass may already have written what the guard looks for.

Reds on: M7b (the dry-run setting inverted, data/store/app/panel_manager.py:1091) at the dry_run flag assertion (every
pass dry_run); M6c (a dry-run pass takes the create path, panel_manager.py:721) at the no-CREATED assertion, the
assertions before it holding (the pass is a finished dry MEMBERSHIP pass, its ENTERED for SYMBOL is recorded with
the CREATED). Inert here: M6k (no PANEL_MANAGER_ENABLED override is set for this phase), M1s.
"""

import asyncio
import time

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from data.store.app.client_label import OPERATOR_PRINCIPAL, PANEL_OWNER
from data.store.app.database.crud.panel_settings import append_source_settings_version
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.panel import PanelPassKind, PanelTransition
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.app.panel_manager import TICK_INTERVAL
from tests.system.phased.panels import (
    LIVE_RATES,
    REASON_PREFIX,
    SOURCE,
    SYMBOL,
    delete_s6_rows,
    panels,
    passes_of,
    transitions_of,
)


pytestmark = pytest.mark.data_store

REQUESTS = IngestRequest.__table__
ENTRIES = StoreDatasetEntry.__table__
PHASE_A = 'tests/system/phased/phase_s6a_panel_kill_switch.py'
DRY_RUN_WAIT_S = TICK_INTERVAL.total_seconds() + 30.0
POLL_S = 5.0


@pytest.mark.asyncio
async def test_s6b_a_dry_run_records_its_decisions_and_applies_nothing(
    pg_engine, pg_async_engine, run_identity, data_store
) -> None:
    found = panels(pg_engine)
    if not found:
        pytest.fail(f'no S6 panel: phase A never ran (or failed and cleaned up). Run {PHASE_A} first.', pytrace=False)
    try:
        assert len(found) == 1, f'{len(found)} S6 panels are present; rerun phase A'
        panel = found[0]
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            await append_source_settings_version(
                db,
                source=SOURCE,
                settings=LIVE_RATES,
                actor=OPERATOR_PRINCIPAL,
                reason=f'{REASON_PREFIX} {run_identity.run_id} phase B',
            )

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
            await asyncio.sleep(POLL_S)

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
