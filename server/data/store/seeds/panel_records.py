"""The seed's panel cast: the five panel tables, written by the store's REAL panel crud.

Bead tj-7zjov1.196 (P20s2), ADR tj-26jt9w addendum 20. The seed held no panel row, so the migration tests said nothing
about the panel tables across an upgrade or a downgrade. This module fills them the way the panel manager does, through
crud/panel_settings.py (create_panel, append_settings_version, append_source_settings_version) and crud/panel_ledger.py
(open_pass, append_transitions, finish_pass), not with raw SQL. It runs LAST in seed_ingest_records, after the asset-list
cast, because a pass names the asset-list snapshots it read and a CREATED transition names a request of the ingest cast.

THE CAST. One panel on ALPACA_API, named PANEL_NAME, with two panel settings versions and two source settings versions.
The assets are the asset-list cast's (vendor ids seed-asset-a1 to a4, symbols ZZSEEDXA to ZZSEEDXD), the days its snapshots'.

  settings v1 (via create_panel; no cadence named, so the default MONTHLY applies), source v1 (via create_panel)
  P1 MEMBERSHIP v1/s1, finished, snapshots AL_ACTIVE_1 and AL_INACTIVE_1, as_of day 0:
       a1 ENTERED + CREATED (names a request of the ingest cast, no dataset: the same-pass tie that seq orders)
       a2 ENTERED + DEFERRED (ramp_rate)       a3 ENTERED + REFUSED (a refusal cause)
  source v2, settings v2 (the panel is DISABLED by it: a disable is a version too)
  P2 MEMBERSHIP v2/s2, finished, AL_ACTIVE_2 and AL_INACTIVE_2:
       a4 ENTERED + DEFERRED (request_rate), day 1     a3 EXITED, a2 EXITED, day 1     a2 DRAINING, day 2
  P3 MEMBERSHIP v2/s2, finished, AL_ACTIVE_EMPTY, no inactive snapshot (inactive_snapshot_id NULL):
       a2 DRAINED, day 3, naming a dataset that is absent (deleted: no foreign key keeps it)
  P4 RELEASE v2/s2, UNFINISHED (finished_at and counts NULL): a4 CREATED, day 3 (names a request of the cast, no dataset)
  P5 MEMBERSHIP v2/s2, DRY RUN, finished: a1 ENTERED, day 3 (a flagged row every read ignores)

Fourteen transitions in all, every PanelTransition at least once; a CREATED row names only a request of the
ingest cast (SUCCEEDED, EMPTY); its dataset is that request's dataset_id, the entry the cast's bars go under, which the
boundary read reaches through the request. dump.NORMALISE_SQL maps those ids onto the normalised entry ids, which is why the entry id is passed in.

NOTHING HERE READS A CLOCK OR DRAWS A RANDOM ID. Every instant is a literal (the asset-list cast's days). The instants and
ids Postgres stamps (saved_at, created_at, started_at, finished_at, recorded_at, gen_random_uuid ids) are rewritten by
dump.NORMALISE_SQL; the append ordinal seq is assigned by append_transitions from the list order.

SYNTHETIC ONLY: the panel name, the actor (OWNER_PATTERN) and the symbols (SYMBOL_PATTERN) are the scenario's own patterns.
The producer's synthetic-only refusal judges them.

The app modules are imported inside the function: they read the database environment at import, and this package must
stay importable without it.
"""

import re
import uuid
from datetime import timedelta

from sqlalchemy.engine import URL

from data.store.seeds.ingest_records import _AL_ASSETS, _AL_DAY_0, REQUEST_IDS


PANEL_NAME = 'zz-seed-panel'
# The panel names a row may carry and still be synthetic.
PANEL_NAME_PATTERN = re.compile(r'^zz-seed-panel[a-z0-9-]*$')
# An actor that passes scenario.OWNER_PATTERN, as every settings version's actor must.
PANEL_ACTOR = 'seed-owner-p'
# A dataset that does not exist (deleted): the ledger names datasets with no foreign key, so a row outlives one.
ABSENT_DATASET_ID = uuid.UUID('5eed0000-0000-4000-8000-00000000dead')

# What the seeded panel tables must come to; the producer refuses a seed that does not match. Counts only.
EXPECTED_PANELS = 1
EXPECTED_SETTINGS_VERSIONS = 2
EXPECTED_SOURCE_SETTINGS_VERSIONS = 2
EXPECTED_PASSES = 5
EXPECTED_DRY_RUN_PASSES = 1
EXPECTED_UNFINISHED_PASSES = 1
EXPECTED_PASS_KINDS = {'MEMBERSHIP': 4, 'RELEASE': 1}
EXPECTED_TRANSITIONS = {
    'ENTERED': 5,
    'CREATED': 2,
    'DEFERRED': 2,
    'REFUSED': 1,
    'EXITED': 2,
    'DRAINING': 1,
    'DRAINED': 1,
}
EXPECTED_TRANSITION_ROWS = sum(EXPECTED_TRANSITIONS.values())
# Members with an ENTERED and a CREATED in one pass at one as_of: the tie only seq orders.
EXPECTED_TIED_MEMBERS = 1


async def seed_panel_records(url: URL, dataset_id: uuid.UUID) -> None:
    """Write the panel cast through the store's own panel crud.

    Args:
        url (URL): The asyncpg URL of the scratch database.
        dataset_id (uuid.UUID): The entry the ingest cast's bars go under. Unused since P20d: the CREATED rows reach
            it through their requests; kept so the caller's signature stays.

    Raises:
        Exception: Whatever the store's code raises for a write it refuses; nothing is caught, a seed is not produced
            from a half-written cast.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from common.enums.data_stock import DataSource
    from data.store.app.database.crud import panel_ledger, panel_settings
    from data.store.app.database.models.asset_list import AssetListSnapshot
    from data.store.app.database.models.panel import PanelCadence, PanelPassKind, PanelTransition

    engine = create_async_engine(url, connect_args={'timeout': 10})
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:

            async def snapshot(name: str) -> uuid.UUID:
                found = (
                    await db.execute(
                        select(AssetListSnapshot.id).where(AssetListSnapshot.request_id == REQUEST_IDS[name])
                    )
                ).scalar_one_or_none()
                if found is None:
                    raise RuntimeError(f'seed asset-list snapshot of request {name} is missing')
                return found

            def at(days: int):
                return _AL_DAY_0 + timedelta(days=days)

            def row(asset: str, transition: PanelTransition, days: int, **named):
                vendor_id, symbol = _AL_ASSETS[asset]
                return panel_ledger.NewTransition(
                    vendor_asset_id=vendor_id, symbol=symbol, transition=transition, as_of=at(days), **named
                )

            async def run_pass(
                kind: PanelPassKind,
                settings_id: uuid.UUID,
                source_settings_id: uuid.UUID,
                active: str,
                inactive: str | None,
                rows: list,
                *,
                dry_run: bool = False,
                finish: bool = True,
            ) -> None:
                pass_id = await panel_ledger.open_pass(
                    db,
                    panel_id=panel.id,
                    kind=kind,
                    settings_version_id=settings_id,
                    source_settings_version_id=source_settings_id,
                    active_snapshot_id=await snapshot(active),
                    inactive_snapshot_id=None if inactive is None else await snapshot(inactive),
                    dry_run=dry_run,
                )
                await panel_ledger.append_transitions(db, pass_id, rows)
                if finish:
                    counts = {
                        key: sum(1 for made in rows if made.transition.value.lower() == key)
                        for key in panel_ledger.COUNT_KEYS
                    }
                    if not await panel_ledger.finish_pass(db, pass_id, counts):
                        raise RuntimeError('the seed pass was already finished')
                # The ledger never commits: the pass, its rows and its finish are one transaction.
                await db.commit()

            membership, release = PanelPassKind.MEMBERSHIP, PanelPassKind.RELEASE
            entered, created, deferred = PanelTransition.ENTERED, PanelTransition.CREATED, PanelTransition.DEFERRED
            exited, draining = PanelTransition.EXITED, PanelTransition.DRAINING

            panel, settings_1 = await panel_settings.create_panel(
                db,
                name=PANEL_NAME,
                source=DataSource.ALPACA_API,
                settings=panel_settings.PanelSettings(enabled=True, size=3, allow_symbols=('ZZSEEDXA', 'ZZSEEDXB')),
                actor=PANEL_ACTOR,
                reason='seed: create the panel',
                source_settings=panel_settings.SourceSettings(ramp_datasets_per_hour=5, request_cap_per_hour=50),
            )
            source_1 = await panel_settings.current_source_settings(db, DataSource.ALPACA_API)
            if source_1 is None:
                raise RuntimeError('the seed panel has no source settings version')

            await run_pass(
                membership,
                settings_1.id,
                source_1.id,
                'AL_ACTIVE_1',
                'AL_INACTIVE_1',
                [
                    row('AL_A1', entered, 0),
                    row('AL_A1', created, 0, request_id=REQUEST_IDS['SUCCEEDED']),
                    row('AL_A2', entered, 0),
                    row('AL_A2', deferred, 0, cause='ramp_rate'),
                    row('AL_A3', entered, 0),
                    row('AL_A3', PanelTransition.REFUSED, 0, cause='asset_not_tradable'),
                ],
            )

            source_2 = await panel_settings.append_source_settings_version(
                db,
                source=DataSource.ALPACA_API,
                settings=panel_settings.SourceSettings(
                    ramp_datasets_per_hour=10,
                    request_cap_per_hour=100,
                    corporate_action_window_pre_days=30,
                    corporate_action_window_post_days=60,
                ),
                actor=PANEL_ACTOR,
                reason='seed: raise the rates',
            )
            settings_2 = await panel_settings.append_settings_version(
                db,
                panel_id=panel.id,
                settings=panel_settings.PanelSettings(
                    enabled=False,
                    size=2,
                    cadence=PanelCadence.WEEKLY,
                    deny_symbols=('ZZSEEDXC',),
                    exchange='SEEDX',
                    active=True,
                    tradable=True,
                ),
                actor=PANEL_ACTOR,
                reason='seed: disable the panel',
            )

            await run_pass(
                membership,
                settings_2.id,
                source_2.id,
                'AL_ACTIVE_2',
                'AL_INACTIVE_2',
                [
                    row('AL_A4', entered, 1),
                    row('AL_A4', deferred, 1, cause='request_rate'),
                    row('AL_A3', exited, 1),
                    row('AL_A2', exited, 1),
                    row('AL_A2', draining, 2),
                ],
            )
            await run_pass(
                membership,
                settings_2.id,
                source_2.id,
                'AL_ACTIVE_EMPTY',
                None,
                [row('AL_A2', PanelTransition.DRAINED, 3, dataset_id=ABSENT_DATASET_ID)],
            )
            await run_pass(
                release,
                settings_2.id,
                source_2.id,
                'AL_ACTIVE_EMPTY',
                'AL_INACTIVE_2',
                [row('AL_A4', created, 3, request_id=REQUEST_IDS['EMPTY'])],
                finish=False,
            )
            await run_pass(
                membership,
                settings_2.id,
                source_2.id,
                'AL_ACTIVE_EMPTY',
                'AL_INACTIVE_2',
                [row('AL_A1', entered, 3)],
                dry_run=True,
            )
    finally:
        await engine.dispose()
