"""P26 S6, phase A: with PANEL_MANAGER_ENABLED false, the RUNNING data_store writes no panel pass (tj-7zjov1.286).

THE SCENARIO (P26 tj-7zjov1.38 acceptance: "the kill switch and the dry run"; note 02:35 UTC 2026-11-02: test_env
data_store {PANEL_MANAGER_ENABLED: false} then a run, no pass row written, the disabled log line via logs).
Before this phase the orchestrator recreates data_store with PANEL_MANAGER_ENABLED false. This phase checks that the
create path is reachable (panels.assert_create_path_reachable), creates one ENABLED ALPACA_API panel (panels.py: one
symbol of the fake's active list, under a source settings version of its own with LIVE_RATES, ramp 1 and cap 2, so a
loop that ran would CREATE the member, not defer it), then watches it for KILL_WAIT_S and fails at once if any pass
row appears for it, or any panel-owned ingest request is written.

Reds on: M6k (the kill switch ignores its value, data/store/app/panel_manager.py:177 'not in _FALSE') at the pass-row
assertion in the wait loop; with the rates above 0 the panel-owned request count after it can go red too, but the
pass-row assertion fires first (the pass row opens before the create commits). Inert here: M6c and M7b (they act
only on a running loop), M7a (the override is explicit).

THE BOUNDED WAIT, AND WHY IT IS THIS LONG. A running panel loop ticks every TICK_INTERVAL (5 minutes,
data/store/app/panel_manager.py, a constant) from its start, and a new enabled panel is due at its creation, with its
first pass running on the newest snapshot without waiting. So a loop that was running would write this panel's pass
at its next tick, at most TICK_INTERVAL after the panel was created. KILL_WAIT_S is TICK_INTERVAL plus 30 s for the
pass itself. Phase B (the dry run) is the control: the same panel, untouched, gets its pass within the same bound once
a loop runs.

The panel is LEFT for phase B (phase_s6b_panel_dry_run.py), which deletes it. On a failure here it is deleted at once.

ORCHESTRATOR STEPS (the hand-back of tj-7zjov1.286 has the whole list):
  0. test_env service data_store overrides {"PANEL_MANAGER_ENABLED": "false"}
  1. logs service data_store tail 400: expect INFO 'The panel manager is off (PANEL_MANAGER_ENABLED); no panel pass
     runs' once, after the recreate
  2. run_system_tests paths ['tests/system/phased/phase_s6a_panel_kill_switch.py']  -> 1 passed (takes about 330 s)
  3. test_env service data_store overrides {"PANEL_MANAGER_DRY_RUN": "true"} (REPLACES the set: the manager is on again)
  4. run_system_tests paths ['tests/system/phased/phase_s6b_panel_dry_run.py']  -> 1 passed
  5. test_env service data_store overrides {}  (clears every override)
"""

import asyncio
import time

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from data.store.app.client_label import OPERATOR_PRINCIPAL, PANEL_OWNER
from data.store.app.database.crud.panel_settings import append_source_settings_version, create_panel
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.panel_manager import TICK_INTERVAL
from tests.system.phased.panels import (
    LIVE_RATES,
    NAME_PREFIX,
    REASON_PREFIX,
    SETTINGS_V1,
    SOURCE,
    assert_active_snapshot_holds_symbol,
    assert_create_path_reachable,
    delete_s6_rows,
    panels,
    passes_of,
)


pytestmark = pytest.mark.data_store

REQUESTS = IngestRequest.__table__
KILL_WAIT_S = TICK_INTERVAL.total_seconds() + 30.0
POLL_S = 10.0


def _panel_requests_since(pg_engine, since) -> int:
    with pg_engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.count())
            .select_from(REQUESTS)
            .where(REQUESTS.c.client_label == PANEL_OWNER, REQUESTS.c.created_at >= since)
        ).scalar_one()


@pytest.mark.asyncio
async def test_s6a_the_kill_switch_holds_an_enabled_panel_off_for_a_whole_tick(
    pg_engine, pg_async_engine, run_identity, data_store
) -> None:
    delete_s6_rows(pg_engine)
    assert_active_snapshot_holds_symbol(pg_engine)
    assert_create_path_reachable(pg_engine)

    try:
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            await append_source_settings_version(
                db,
                source=SOURCE,
                settings=LIVE_RATES,
                actor=OPERATOR_PRINCIPAL,
                reason=f'{REASON_PREFIX} {run_identity.run_id}',
            )
        async with AsyncSession(pg_async_engine, expire_on_commit=False) as db:
            panel, version = await create_panel(
                db,
                name=f'{NAME_PREFIX}{run_identity.run_id}',
                source=SOURCE,
                settings=SETTINGS_V1,
                actor=OPERATOR_PRINCIPAL,
                reason='P26 S6 kill switch and dry run',
            )
        assert version.settings.enabled, 'the control: the panel must be enabled for the kill switch to mean anything'
        assert [row.id for row in panels(pg_engine)] == [panel.id]

        deadline = time.monotonic() + KILL_WAIT_S
        while True:
            written = passes_of(pg_engine, panel.id)
            assert written == [], (
                f'{len(written)} pass row(s) were written for an enabled panel while PANEL_MANAGER_ENABLED was to be '
                'false: the kill switch did not hold (or the orchestrator did not set it)'
            )
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        requests = _panel_requests_since(pg_engine, panel.created_at)
        assert requests == 0, f'{requests} panel-owned ingest request(s) were written while the manager was off'
    except BaseException:
        delete_s6_rows(pg_engine)
        raise
