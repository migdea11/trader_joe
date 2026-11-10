"""The SQLite world the panel UI tests share (P24v tj-7zjov1.241): every table the /ui/v1 panel reads and writes touch.

The panel tables, ingest_request and dataset_override_log as test_panel_ledger.py sets them up (copies with JSON for the
Postgres ARRAY and JSONB columns, sa.Uuid for UUID so a random hex id keeps TEXT affinity), plus the catalog tables
(store_dataset_entry, stock_market_activity, ingest_run), the asset-list snapshot and event tables and corporate_action.
btrim, hashtext and pg_advisory_xact_lock are registered on the connection, as test_panel_settings.py does, so
create_panel and the version appends run unchanged. Instants are stored and read as UTC (patch_aware_sqlite_datetimes).

WHAT THIS TIER CANNOT SAY: the Postgres enum types and asyncpg's binding of them, the advisory lock actually serialising
two creates, and Postgres's own uuid ordering in a keyset. Those are listed for the system tier in
tests/system/test_panel_routes_pg.py.
"""

import uuid
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from data.store.app.database.crud.panel_ledger import NewTransition, append_transitions, open_pass
from data.store.app.database.models.asset_list import AssetListEvent, AssetListEventKind, AssetListSnapshot
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind
from data.store.app.database.models.ingest_run import IngestRun
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
from data.store.tests.request_path_harness import patch_aware_sqlite_datetimes
from schemas.data_ingest.submit import AssetListStatus


PANELS = Panel.__table__
SETTINGS = PanelSettingsVersion.__table__
SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
PASSES = PanelPass.__table__
TRANSITIONS = PanelMemberTransition.__table__
OVERRIDES = DatasetOverrideLog.__table__
REQUESTS = IngestRequest.__table__
ENTRIES = StoreDatasetEntry.__table__
BARS = StockMarketActivity.__table__
RUNS = IngestRun.__table__
SNAPSHOTS = AssetListSnapshot.__table__
EVENTS = AssetListEvent.__table__
ACTIONS = CorporateAction.__table__
TABLES = (
    PANELS,
    SETTINGS,
    SOURCE_SETTINGS,
    PASSES,
    TRANSITIONS,
    OVERRIDES,
    REQUESTS,
    ENTRIES,
    BARS,
    RUNS,
    SNAPSHOTS,
    EVENTS,
    ACTIONS,
)

SOURCE = DataSource.ALPACA_API
# No trading calendar covers it (trading_calendar.SOURCE_CALENDARS names ALPACA_API alone).
NO_CALENDAR_SOURCE = DataSource.IB_API
NEW_YORK = ZoneInfo('America/New_York')
# Tue 6 Oct 2026, 22:00Z: after Tuesday's 20:00Z close plus the 30 minute settle, before Wednesday's open (the clock
# test_ui_datasets_route.py judges health at, so a dataset's status here is the catalog's for the same bars).
NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)


def ny_midnight(day: int, month: int = 10) -> datetime:
    """A session's local midnight in New York, as UTC."""
    return datetime(2026, month, day, tzinfo=NEW_YORK).astimezone(UTC)


def ordered(n: int) -> uuid.UUID:
    """An id that sorts by n, in SQLite's hex text as in Postgres's uuid bytes."""
    return uuid.UUID(f'a{n:031x}')


@dataclass(frozen=True)
class Seeded:
    """A panel with its version 1 and the source version its passes name by default."""

    panel_id: uuid.UUID
    settings_id: uuid.UUID
    source_settings_id: uuid.UUID
    source: DataSource


class World:
    """The engine, every statement the request sessions sent, their commits and the advisory-lock keys taken."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.statements: list[Any] = []
        self.commits = 0
        self.locks: list[int] = []
        self._settings_versions: dict[uuid.UUID, int] = {}
        self._source_versions: dict[DataSource, int] = {}

    def session(self) -> AsyncSession:
        world = self

        class Recording(AsyncSession):
            async def execute(self, statement, *args, **kwargs):
                world.statements.append(statement)
                return await super().execute(statement, *args, **kwargs)

            async def commit(self) -> None:
                await super().commit()
                world.commits += 1

        return Recording(self.engine, expire_on_commit=False)

    async def request_session(self) -> AsyncIterator[AsyncSession]:
        """The async_db override: one recording session per request."""
        async with self.session() as db:
            yield db

    def reset_recording(self) -> None:
        self.statements.clear()

    async def rows(self, table: sa.Table, *where: Any) -> list[sa.Row]:
        async with AsyncSession(self.engine) as db:
            return list((await db.execute(sa.select(table).where(*where))).all())

    async def counts(self) -> dict[str, int]:
        return {table.name: len(await self.rows(table)) for table in TABLES}

    async def put(self, statement: Any) -> Any:
        """A write behind the modules' backs (test set-up only)."""
        async with AsyncSession(self.engine) as db:
            result = await db.execute(statement)
            await db.commit()
            return result

    # ---- panels, versions, passes and the ledger

    async def source_version(self, source: DataSource = SOURCE, **kw: Any) -> uuid.UUID:
        number = self._source_versions.get(source, 0) + 1
        self._source_versions[source] = number
        values = {
            'source': source,
            'version_no': number,
            'ramp_datasets_per_hour': 5,
            'request_cap_per_hour': 60,
            'corporate_action_window_pre_days': 90,
            'corporate_action_window_post_days': 90,
            'actor': 'operator',
            'reason': f'rates {number}',
        } | kw
        result = await self.put(SOURCE_SETTINGS.insert().values(**values).returning(SOURCE_SETTINGS.c.id))
        return result.scalar_one()

    async def settings_version(self, panel_id: uuid.UUID, **kw: Any) -> uuid.UUID:
        number = self._settings_versions.get(panel_id, 0) + 1
        self._settings_versions[panel_id] = number
        values = {
            'panel_id': panel_id,
            'version_no': number,
            'enabled': True,
            'cadence': PanelCadence.DAILY,
            'actor': 'operator',
            'reason': f'settings {number}',
            # NOT NULL with no schema default (ADR tj-ed4t8m D1): a row written behind the crud's back states it, off.
            'include_otc': False,
        } | kw
        result = await self.put(SETTINGS.insert().values(**values).returning(SETTINGS.c.id))
        return result.scalar_one()

    async def panel(
        self,
        name: str | None = None,
        source: DataSource = SOURCE,
        *,
        panel_id: uuid.UUID | None = None,
        new_source_version: bool = True,
        granularity: Granularity = Granularity.ONE_DAY,
        **settings: Any,
    ) -> Seeded:
        # panel.granularity is NOT NULL with no schema default (ADR tj-rwoasb D2): a row written behind the crud states it.
        values: dict[str, Any] = {
            'name': name or f'panel-{uuid.uuid4().hex[:8]}',
            'source': source,
            'granularity': granularity,
        }
        if panel_id is not None:
            values['id'] = panel_id
        result = await self.put(PANELS.insert().values(**values).returning(PANELS.c.id))
        new_id = result.scalar_one()
        settings_id = await self.settings_version(new_id, **settings)
        if new_source_version or source not in self._source_versions:
            source_settings_id = await self.source_version(source)
        else:
            rows = await self.rows(SOURCE_SETTINGS, SOURCE_SETTINGS.c.source == source)
            source_settings_id = max(rows, key=lambda row: row.version_no).id
        return Seeded(new_id, settings_id, source_settings_id, source)

    async def open(
        self,
        seeded: Seeded,
        *,
        kind: PanelPassKind = PanelPassKind.MEMBERSHIP,
        dry_run: bool = False,
        started_at: datetime | None = None,
        settings_id: uuid.UUID | None = None,
        source_settings_id: uuid.UUID | None = None,
        rows: tuple[NewTransition, ...] = (),
    ) -> uuid.UUID:
        """A pass opened and filled through the ledger module, committed here."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            pass_id = await open_pass(
                db,
                panel_id=seeded.panel_id,
                kind=kind,
                settings_version_id=settings_id or seeded.settings_id,
                source_settings_version_id=source_settings_id or seeded.source_settings_id,
                active_snapshot_id=uuid.uuid4(),
                inactive_snapshot_id=None,
                dry_run=dry_run,
            )
            await append_transitions(db, pass_id, rows)
            await db.commit()
        if started_at is not None:
            await self.put(PASSES.update().where(PASSES.c.id == pass_id).values(started_at=started_at))
        return pass_id

    async def request(self, dataset_id: uuid.UUID | None = None, source: DataSource = SOURCE) -> uuid.UUID:
        """A bars request row, POST-shaped: dataset_id NULL until the intake's first delivered attempt sets it."""
        values = {'kind': IngestRequestKind.BARS, 'source': source, 'dataset_id': dataset_id, 'client_label': 'panel'}
        return (await self.put(REQUESTS.insert().values(**values).returning(REQUESTS.c.id))).scalar_one()

    # ---- datasets and their bars

    async def dataset(
        self,
        symbol: str,
        *,
        bars: tuple[datetime, ...] = (),
        collect: bool = True,
        source: DataSource = SOURCE,
        start: datetime | None = None,
        dataset_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """A DAILY 1day subscription owned by the panel, started 1 Sep 2026 (a valid XNYS date), with daily bars.

        Each bar is written at NOW, after its session's close plus the settle, so it is final.
        """
        new_id = dataset_id or uuid.uuid4()
        entry = StoreDatasetEntry(
            id=new_id,
            owner='panel',
            source=source,
            feed=Feed.IEX,
            asset_symbol=symbol,
            asset_type=AssetType.STOCK,
            data_type=DataType.MARKET_ACTIVITY,
            granularity=Granularity.ONE_DAY,
            start=start or ny_midnight(1, 9),
            end=NullableDateTime.EPOCH,
            expiry=None,
            expiry_type=ExpiryType.ROLLING,
            update_type=UpdateType.DAILY,
            collect=collect,
            created_at=NOW,
            updated_at=NOW,
        )
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            db.add(entry)
            await db.flush()
            db.add_all(
                StockMarketActivity(
                    dataset_id=new_id,
                    source=source,
                    asset_symbol=symbol,
                    feed=Feed.IEX,
                    granularity=Granularity.ONE_DAY,
                    timestamp=at,
                    open=1.0,
                    high=2.0,
                    low=0.5,
                    close=1.0,
                    volume=100,
                    trade_count=10,
                    created_at=NOW,
                    updated_at=NOW,
                )
                for at in bars
            )
            await db.commit()
        return new_id

    async def override(
        self,
        dataset_id: uuid.UUID,
        *,
        at: datetime,
        new_value: str | None,
        reason: str = 'operator stop',
        action: OverrideAction = OverrideAction.SET_COLLECTING,
    ) -> None:
        await self.put(
            OVERRIDES.insert().values(
                actor='operator',
                dataset_id=dataset_id,
                owner='panel',
                action=action,
                reason=reason,
                at=at,
                new_value=new_value,
            )
        )

    # ---- asset lists

    async def snapshot(
        self,
        as_of: datetime,
        *,
        row_count: int = 3,
        asset_list: AssetListStatus = AssetListStatus.ACTIVE,
        source: DataSource = SOURCE,
    ) -> uuid.UUID:
        values = {
            'source': source,
            'asset_type': AssetType.STOCK,
            'asset_list': asset_list,
            'request_id': uuid.uuid4(),
            'attempt_no': 1,
            'as_of': as_of,
            'row_count': row_count,
        }
        return (await self.put(SNAPSHOTS.insert().values(**values).returning(SNAPSHOTS.c.id))).scalar_one()

    async def asset_event(
        self,
        kind: AssetListEventKind,
        vendor_asset_id: str,
        as_of: datetime,
        *,
        asset_list: AssetListStatus = AssetListStatus.ACTIVE,
        source: DataSource = SOURCE,
    ) -> None:
        await self.put(
            EVENTS.insert().values(
                source=source,
                asset_list=asset_list,
                kind=kind,
                vendor_asset_id=vendor_asset_id,
                symbol=vendor_asset_id.upper(),
                snapshot_id=uuid.uuid4(),
                previous_snapshot_id=uuid.uuid4(),
                as_of=as_of,
                previous_as_of=as_of - timedelta(days=1),
            )
        )

    # ---- corporate actions

    async def action(
        self,
        requesting_symbol: str | None,
        as_of: datetime,
        *,
        vendor_action_id: str = 'act-1',
        action_id: uuid.UUID | None = None,
        source: DataSource = SOURCE,
        payload: bytes | None = None,
        **dates: datetime | None,
    ) -> uuid.UUID:
        new_id = action_id or uuid.uuid4()
        body = payload or new_id.bytes
        await self.put(
            ACTIONS.insert().values(
                id=new_id,
                source=source,
                vendor_action_id=vendor_action_id,
                action_type='cash_dividend',
                identifiers={},
                raw_payload=body,
                payload_hash=zlib.crc32(body).to_bytes(4, 'big').hex() + new_id.hex,
                request_id=uuid.uuid4(),
                attempt_no=1,
                requesting_symbol=requesting_symbol,
                as_of=as_of,
                **dates,
            )
        )
        return new_id


async def make_world(monkeypatch: pytest.MonkeyPatch) -> World:
    """The world on a fresh in-memory SQLite database; the column-type patches are undone after the test."""
    patch_aware_sqlite_datetimes(monkeypatch)
    for column in (
        SETTINGS.c.allow_symbols,
        SETTINGS.c.deny_symbols,
        PASSES.c.counts,
        REQUESTS.c.data_types,
        ACTIONS.c.identifiers,
    ):
        monkeypatch.setattr(column, 'type', sa.JSON())
    metadata = sa.MetaData()
    copies = {table.name: table.to_metadata(metadata) for table in TABLES}
    for name in ('allow_symbols', 'deny_symbols'):
        copies[SETTINGS.name].c[name].server_default = sa.DefaultClause(sa.text("'[]'"))
    copies[ACTIONS.name].c.identifiers.server_default = sa.DefaultClause(sa.text("'{}'"))
    for copy in copies.values():
        for column in copy.columns:
            if isinstance(column.type, sa.UUID):
                column.type = sa.Uuid()
    engine = create_async_engine('sqlite+aiosqlite://', poolclass=StaticPool)
    world = World(engine)

    @event.listens_for(engine.sync_engine, 'connect')
    def register(dbapi_connection, _record):
        dbapi_connection.run_async(lambda c: c.create_function('gen_random_uuid', 0, lambda: uuid.uuid4().hex))
        dbapi_connection.run_async(lambda c: c.create_function('btrim', 1, lambda s: None if s is None else s.strip()))
        dbapi_connection.run_async(lambda c: c.create_function('hashtext', 1, lambda s: zlib.crc32(s.encode())))
        dbapi_connection.run_async(lambda c: c.create_function('pg_advisory_xact_lock', 1, world.locks.append))

    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    return world


def entered(vendor: str, as_of: datetime, symbol: str | None = None) -> NewTransition:
    return NewTransition(vendor, symbol or vendor.upper(), PanelTransition.ENTERED, as_of)


def created(vendor: str, as_of: datetime, request_id: uuid.UUID, symbol: str | None = None) -> NewTransition:
    return NewTransition(vendor, symbol or vendor.upper(), PanelTransition.CREATED, as_of, request_id=request_id)


def moved(
    vendor: str,
    kind: PanelTransition,
    as_of: datetime,
    *,
    cause: str | None = None,
    dataset_id: uuid.UUID | None = None,
    symbol: str | None = None,
) -> NewTransition:
    return NewTransition(vendor, symbol or vendor.upper(), kind, as_of, dataset_id=dataset_id, cause=cause)
