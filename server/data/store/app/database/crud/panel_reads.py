"""The panel reads the /ui/v1 routes serve (tj-7zjov1.36, P24; ADR tj-26jt9w addenda 20 and 22).

READ-ONLY. Nothing here writes and nothing commits; every function takes the request's session and only selects. The
writes are crud/panel_settings.py's and the ledger is crud/panel_ledger.py's: this module reads what they wrote and
changes neither. Composition (health, counts, history days, cursors) is app/panel_views.py's.

A CONSTANT NUMBER OF STATEMENTS PER CALL, NEVER ONE PER PANEL, MEMBER OR VERSION (the catalog's rule). The two reads that
take a list of ids (catalog_rows, dataset_labels) are given a page's worth at most, and chunk past ROW_CHUNK so a
statement never binds more than asyncpg allows.

THE SQL IS PORTABLE (no ARRAY, no timezone function): per-day grouping is done by the caller in the source calendar's
zone, so the SQLite tier reads these statements the way Postgres does.

EFFECTIVE-FROM HAS ONE DEFINITION, crud/panel_ledger.py's: the started_at of the first non-dry-run pass naming the
version. The pass rows are read here only to carry the pass's id and KIND next to a version (EffectivePass).
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, Granularity
from data.store.app.database.crud.panel_settings import (
    PanelRecord,
    PanelSettings,
    PanelSettingsRecord,
    SourceSettings,
    SourceSettingsRecord,
)
from data.store.app.database.crud.stock.dataset_catalog import CatalogRow, Entry, _last_bar, _last_bar_written_at, _row
from data.store.app.database.models.asset_list import AssetListEvent, AssetListEventKind, AssetListSnapshot
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.panel import (
    Panel,
    PanelMemberTransition,
    PanelPass,
    PanelPassKind,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
    PanelTransition,
)
from schemas.data_ingest.submit import AssetListStatus


_PANEL = Panel.__table__
_SETTINGS = PanelSettingsVersion.__table__
_SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
_PASS = PanelPass.__table__
_TRANSITION = PanelMemberTransition.__table__
_REQUEST = IngestRequest.__table__
_SNAPSHOT = AssetListSnapshot.__table__
_EVENT = AssetListEvent.__table__
_ACTION = CorporateAction.__table__

# The most ids one statement binds. asyncpg allows 32,767 bind parameters per statement.
ROW_CHUNK: Final = 5000


@dataclass(frozen=True)
class PassRef:
    """The first non-dry-run pass naming a version: its id, kind and start (the version's effective-from)."""

    pass_id: uuid.UUID
    kind: PanelPassKind
    started_at: datetime


@dataclass(frozen=True)
class PanelListRow:
    """One panel with the enabled flag of its latest settings version."""

    panel: PanelRecord
    enabled: bool


@dataclass(frozen=True)
class SettingsVersionRow:
    """One panel settings version with its effective pass, None while no pass has run under it."""

    record: PanelSettingsRecord
    effective: PassRef | None


@dataclass(frozen=True)
class SourceVersionRow:
    """One source settings version with its effective pass, None while no pass has run under it."""

    record: SourceSettingsRecord
    effective: PassRef | None


@dataclass(frozen=True)
class AssetEventAt:
    """One asset-list event reduced to what the history counts: the vendor asset id and the snapshot instant."""

    vendor_asset_id: str
    as_of: datetime


@dataclass(frozen=True)
class MemberAssetEvents:
    """The latest APPEARED and VANISHED instants of a vendor asset on the source's ACTIVE list, None when none."""

    appeared_at: datetime | None
    vanished_at: datetime | None


@dataclass(frozen=True)
class ActionRow:
    """One corporate-action observation as the event view shows it. The raw payload is never read."""

    id: uuid.UUID
    vendor_action_id: str
    action_type: str
    ex_date: datetime | None
    record_date: datetime | None
    payable_date: datetime | None
    process_date: datetime | None
    effective_date: datetime | None
    as_of: datetime


def _chunks(items: Sequence) -> list[Sequence]:
    return [items[start : start + ROW_CHUNK] for start in range(0, len(items), ROW_CHUNK)]


def _panel_record(row) -> PanelRecord:
    return PanelRecord(id=row.id, name=row.name, source=row.source, created_at=row.created_at)


def _settings_record(row) -> PanelSettingsRecord:
    return PanelSettingsRecord(
        id=row.id,
        panel_id=row.panel_id,
        version_no=row.version_no,
        settings=PanelSettings(
            enabled=row.enabled,
            size=row.size,
            cadence=row.cadence,
            allow_symbols=tuple(row.allow_symbols),
            deny_symbols=tuple(row.deny_symbols),
            asset_class=row.asset_class,
            exchange=row.exchange,
            active=row.active,
            tradable=row.tradable,
        ),
        saved_at=row.saved_at,
        actor=row.actor,
        reason=row.reason,
    )


def _source_record(row) -> SourceSettingsRecord:
    return SourceSettingsRecord(
        id=row.id,
        source=row.source,
        version_no=row.version_no,
        settings=SourceSettings(
            ramp_datasets_per_hour=row.ramp_datasets_per_hour,
            request_cap_per_hour=row.request_cap_per_hour,
            corporate_action_window_pre_days=row.corporate_action_window_pre_days,
            corporate_action_window_post_days=row.corporate_action_window_post_days,
        ),
        saved_at=row.saved_at,
        actor=row.actor,
        reason=row.reason,
    )


async def list_panels(db: AsyncSession) -> list[PanelListRow]:
    """Every panel by name then id, with the enabled flag of its latest settings version. One statement.

    Args:
        db: The request's session.

    Returns:
        list[PanelListRow]: Each panel and whether its latest version is enabled.
    """
    newest = (
        select(func.max(_SETTINGS.c.version_no))
        .where(_SETTINGS.c.panel_id == _PANEL.c.id)
        .correlate(_PANEL)
        .scalar_subquery()
    )
    result = await db.execute(
        select(_PANEL.c.id, _PANEL.c.name, _PANEL.c.source, _PANEL.c.created_at, _SETTINGS.c.enabled)
        .join(_SETTINGS, and_(_SETTINGS.c.panel_id == _PANEL.c.id, _SETTINGS.c.version_no == newest))
        .order_by(_PANEL.c.name, _PANEL.c.id)
    )
    return [PanelListRow(panel=_panel_record(row), enabled=row.enabled) for row in result.all()]


async def get_panel(db: AsyncSession, panel_id: uuid.UUID) -> PanelRecord | None:
    """One panel, or None when no such panel exists."""
    row = (
        await db.execute(
            select(_PANEL.c.id, _PANEL.c.name, _PANEL.c.source, _PANEL.c.created_at).where(_PANEL.c.id == panel_id)
        )
    ).first()
    return None if row is None else _panel_record(row)


async def _first_passes(
    db: AsyncSession, version_column, version_ids: Collection[uuid.UUID], panel_id: uuid.UUID | None
) -> dict[uuid.UUID, PassRef]:
    """Per version id, its first non-dry-run pass (the panel's own when panel_id is given, else any panel's)."""
    if not version_ids:
        return {}
    found: dict[uuid.UUID, PassRef] = {}
    for chunk in _chunks(list(version_ids)):
        rank = (
            func.row_number().over(partition_by=version_column, order_by=(_PASS.c.started_at, _PASS.c.id)).label('rank')
        )
        stmt = select(version_column.label('version_id'), _PASS.c.id, _PASS.c.kind, _PASS.c.started_at, rank).where(
            version_column.in_(chunk), _PASS.c.dry_run.is_(False)
        )
        if panel_id is not None:
            stmt = stmt.where(_PASS.c.panel_id == panel_id)
        ranked = stmt.subquery()
        for row in (await db.execute(select(ranked).where(ranked.c.rank == 1))).all():
            found[row.version_id] = PassRef(pass_id=row.id, kind=row.kind, started_at=row.started_at)
    return found


async def settings_versions_page(
    db: AsyncSession, panel_id: uuid.UUID, *, before: int | None, limit: int
) -> list[SettingsVersionRow]:
    """A panel's settings versions newest first, the full settings and the effective pass of each. Two statements.

    Args:
        db: The request's session.
        panel_id: The panel.
        before: Keyset: only versions numbered below this; None starts at the newest.
        limit: Rows to return at most (the caller asks for one more than its page to learn whether another follows).

    Returns:
        list[SettingsVersionRow]: Newest first. Empty for an unknown panel.
    """
    stmt = select(*_SETTINGS.c).where(_SETTINGS.c.panel_id == panel_id)
    if before is not None:
        stmt = stmt.where(_SETTINGS.c.version_no < before)
    rows = (await db.execute(stmt.order_by(_SETTINGS.c.version_no.desc()).limit(limit))).all()
    passes = await _first_passes(db, _PASS.c.settings_version_id, [row.id for row in rows], panel_id)
    return [SettingsVersionRow(record=_settings_record(row), effective=passes.get(row.id)) for row in rows]


async def source_settings_versions_page(
    db: AsyncSession, source: DataSource, *, before: int | None, limit: int, panel_id: uuid.UUID | None = None
) -> list[SourceVersionRow]:
    """A source's settings versions newest first with the effective pass of each. Two statements.

    The effective pass is the first non-dry-run pass naming the version of the given panel, or, with no panel, of ANY
    panel on the source (a source version belongs to the source, not to a panel).

    Args:
        db: The request's session.
        source: The source.
        before: Keyset: only versions numbered below this; None starts at the newest.
        limit: Rows to return at most.
        panel_id: Read the effective pass from this panel's passes only.

    Returns:
        list[SourceVersionRow]: Newest first. Empty for a source with no version.
    """
    stmt = select(*_SOURCE_SETTINGS.c).where(_SOURCE_SETTINGS.c.source == source)
    if before is not None:
        stmt = stmt.where(_SOURCE_SETTINGS.c.version_no < before)
    rows = (await db.execute(stmt.order_by(_SOURCE_SETTINGS.c.version_no.desc()).limit(limit))).all()
    passes = await _first_passes(db, _PASS.c.source_settings_version_id, [row.id for row in rows], panel_id)
    return [SourceVersionRow(record=_source_record(row), effective=passes.get(row.id)) for row in rows]


async def created_request_datasets(db: AsyncSession, panel_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Per vendor asset id, the dataset its newest CREATED transition's request minted. One statement.

    A CREATED row names its request, never a dataset: the dataset is ingest_request.dataset_id, empty until the
    delivery intake's first delivered attempt, so a member whose request has not been delivered has no entry here.
    Dry-run passes are ignored, as in every ledger read.

    Args:
        db: The request's session.
        panel_id: The panel.

    Returns:
        dict[str, uuid.UUID]: Vendor asset id to dataset id, only where the request has one.
    """
    rank = (
        func.row_number()
        .over(
            partition_by=_TRANSITION.c.vendor_asset_id,
            order_by=(
                _TRANSITION.c.as_of.desc(),
                _PASS.c.started_at.desc(),
                _TRANSITION.c.pass_id.desc(),
                _TRANSITION.c.seq.desc(),
            ),
        )
        .label('rank')
    )
    ranked = (
        select(_TRANSITION.c.vendor_asset_id, _REQUEST.c.dataset_id, rank)
        .join(_PASS, _PASS.c.id == _TRANSITION.c.pass_id)
        .join(_REQUEST, _REQUEST.c.id == _TRANSITION.c.request_id)
        .where(
            _TRANSITION.c.panel_id == panel_id,
            _TRANSITION.c.transition == PanelTransition.CREATED,
            _PASS.c.dry_run.is_(False),
        )
        .subquery()
    )
    rows = (await db.execute(select(ranked.c.vendor_asset_id, ranked.c.dataset_id).where(ranked.c.rank == 1))).all()
    return {row.vendor_asset_id: row.dataset_id for row in rows if row.dataset_id is not None}


def _catalog_select():
    return select(Entry, _last_bar().label('last_bar'), _last_bar_written_at().label('last_bar_written_at'))


async def catalog_rows(db: AsyncSession, dataset_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, CatalogRow]:
    """The catalog rows (entry and newest real bar) of the given datasets; an id with no entry is left out.

    Args:
        db: The request's session.
        dataset_ids: The datasets; a page's worth at most (chunked at ROW_CHUNK all the same).

    Returns:
        dict[uuid.UUID, CatalogRow]: By dataset id.
    """
    found: dict[uuid.UUID, CatalogRow] = {}
    for chunk in _chunks(sorted(set(dataset_ids))):
        result = await db.execute(_catalog_select().where(Entry.id.in_(chunk)))
        for entry, newest, written in result.all():
            found[entry.id] = _row(entry, newest, written)
    return found


async def panel_catalog_rows(db: AsyncSession, panel_id: uuid.UUID) -> dict[uuid.UUID, CatalogRow]:
    """The catalog rows of every dataset the panel's non-dry-run ledger names, directly or by a CREATED request. One statement.

    The panel-wide read for the counts: the ids are a subquery, so a panel of any size binds no id.

    Args:
        db: The request's session.
        panel_id: The panel.

    Returns:
        dict[uuid.UUID, CatalogRow]: By dataset id.
    """
    named_directly = (
        select(_TRANSITION.c.dataset_id.label('dataset_id'))
        .join(_PASS, _PASS.c.id == _TRANSITION.c.pass_id)
        .where(_TRANSITION.c.panel_id == panel_id, _TRANSITION.c.dataset_id.is_not(None), _PASS.c.dry_run.is_(False))
    )
    named_by_request = (
        select(_REQUEST.c.dataset_id.label('dataset_id'))
        .join(_TRANSITION, _TRANSITION.c.request_id == _REQUEST.c.id)
        .join(_PASS, _PASS.c.id == _TRANSITION.c.pass_id)
        .where(
            _TRANSITION.c.panel_id == panel_id,
            _TRANSITION.c.transition == PanelTransition.CREATED,
            _REQUEST.c.dataset_id.is_not(None),
            _PASS.c.dry_run.is_(False),
        )
    )
    named = named_directly.union(named_by_request).subquery()
    result = await db.execute(_catalog_select().where(Entry.id.in_(select(named.c.dataset_id))))
    return {entry.id: _row(entry, newest, written) for entry, newest, written in result.all()}


async def member_events(
    db: AsyncSession, source: DataSource, vendor_asset_ids: Collection[str]
) -> dict[str, MemberAssetEvents]:
    """Per vendor asset id, the latest APPEARED and VANISHED instants on the source's ACTIVE list. One statement per chunk.

    Args:
        db: The request's session.
        source: The panel's source.
        vendor_asset_ids: The members of a page.

    Returns:
        dict[str, MemberAssetEvents]: Only ids that have at least one such event.
    """
    latest: dict[str, dict[AssetListEventKind, datetime]] = {}
    for chunk in _chunks(sorted(set(vendor_asset_ids))):
        result = await db.execute(
            select(_EVENT.c.vendor_asset_id, _EVENT.c.kind, func.max(_EVENT.c.as_of).label('at'))
            .where(
                _EVENT.c.source == source,
                _EVENT.c.asset_list == AssetListStatus.ACTIVE,
                _EVENT.c.kind.in_((AssetListEventKind.APPEARED, AssetListEventKind.VANISHED)),
                _EVENT.c.vendor_asset_id.in_(chunk),
            )
            .group_by(_EVENT.c.vendor_asset_id, _EVENT.c.kind)
        )
        for row in result.all():
            latest.setdefault(row.vendor_asset_id, {})[row.kind] = row.at
    return {
        vendor_id: MemberAssetEvents(
            appeared_at=kinds.get(AssetListEventKind.APPEARED), vanished_at=kinds.get(AssetListEventKind.VANISHED)
        )
        for vendor_id, kinds in latest.items()
    }


async def active_snapshots(
    db: AsyncSession, source: DataSource, since: datetime, until: datetime
) -> list[tuple[datetime, int]]:
    """The source's ACTIVE-list snapshots with as_of in [since, until): (as_of, row_count), oldest first. One statement.

    Args:
        db: The request's session.
        source: The source.
        since: Inclusive.
        until: Exclusive.

    Returns:
        list[tuple[datetime, int]]: One entry per snapshot, empty ones included (row_count 0).
    """
    result = await db.execute(
        select(_SNAPSHOT.c.as_of, _SNAPSHOT.c.row_count)
        .where(
            _SNAPSHOT.c.source == source,
            _SNAPSHOT.c.asset_list == AssetListStatus.ACTIVE,
            _SNAPSHOT.c.as_of >= since,
            _SNAPSHOT.c.as_of < until,
        )
        .order_by(_SNAPSHOT.c.as_of)
    )
    return [(row.as_of, row.row_count) for row in result.all()]


async def snapshot_events(db: AsyncSession, source: DataSource, since: datetime, until: datetime) -> list[AssetEventAt]:
    """The distinct (vendor asset id, snapshot instant) of the source's events, EITHER list, with as_of in [since, until). One statement.

    Args:
        db: The request's session.
        source: The source.
        since: Inclusive.
        until: Exclusive.

    Returns:
        list[AssetEventAt]: Of any kind (APPEARED, VANISHED, STATUS_FLIP), a vendor asset once per snapshot instant.
    """
    result = await db.execute(
        select(_EVENT.c.vendor_asset_id, _EVENT.c.as_of)
        .where(_EVENT.c.source == source, _EVENT.c.as_of >= since, _EVENT.c.as_of < until)
        .distinct()
    )
    return [AssetEventAt(vendor_asset_id=row.vendor_asset_id, as_of=row.as_of) for row in result.all()]


async def dataset_labels(db: AsyncSession, dataset_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The display label (symbol and granularity) of each dataset that still exists.

    Args:
        db: The request's session.
        dataset_ids: The datasets, a few (the stopped ones of a window).

    Returns:
        dict[uuid.UUID, str]: 'SYMBOL 1day'; a deleted dataset is left out.
    """
    labels: dict[uuid.UUID, str] = {}
    for chunk in _chunks(sorted(set(dataset_ids))):
        result = await db.execute(select(Entry.id, Entry.asset_symbol, Entry.granularity).where(Entry.id.in_(chunk)))
        for dataset_id, symbol, granularity in result.all():
            labels[dataset_id] = f'{symbol} {Granularity(granularity).value}'
    return labels


async def dataset_identity(db: AsyncSession, dataset_id: uuid.UUID) -> tuple[DataSource, str] | None:
    """A dataset's (source, symbol), or None when no such dataset exists."""
    row = (await db.execute(select(Entry.source, Entry.asset_symbol).where(Entry.id == dataset_id))).first()
    return None if row is None else (row.source, row.asset_symbol)


async def corporate_action_page(
    db: AsyncSession, source: DataSource, symbol: str, *, after: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[ActionRow]:
    """Corporate-action observations for a source and requesting symbol, newest as_of first. One statement.

    EVERY observation with the source and a requesting symbol equal to the symbol, whichever request delivered it,
    and NOT filtered by any window (ADR tj-26jt9w addendum 22). A vendor revision is its own row. The raw payload is
    never selected.

    Args:
        db: The request's session.
        source: The dataset's source.
        symbol: The dataset's symbol.
        after: Keyset: the (as_of, id) of the last row of the previous page; rows strictly after it in the order.
        limit: Rows to return at most.

    Returns:
        list[ActionRow]: Ordered by as_of then id, both descending.
    """
    stmt = select(
        _ACTION.c.id,
        _ACTION.c.vendor_action_id,
        _ACTION.c.action_type,
        _ACTION.c.ex_date,
        _ACTION.c.record_date,
        _ACTION.c.payable_date,
        _ACTION.c.process_date,
        _ACTION.c.effective_date,
        _ACTION.c.as_of,
    ).where(_ACTION.c.source == source, _ACTION.c.requesting_symbol == symbol)
    if after is not None:
        as_of, action_id = after
        stmt = stmt.where(or_(_ACTION.c.as_of < as_of, and_(_ACTION.c.as_of == as_of, _ACTION.c.id < action_id)))
    rows = (await db.execute(stmt.order_by(_ACTION.c.as_of.desc(), _ACTION.c.id.desc()).limit(limit))).all()
    return [
        ActionRow(
            id=row.id,
            vendor_action_id=row.vendor_action_id,
            action_type=row.action_type,
            ex_date=row.ex_date,
            record_date=row.record_date,
            payable_date=row.payable_date,
            process_date=row.process_date,
            effective_date=row.effective_date,
            as_of=row.as_of,
        )
        for row in rows
    ]


__all__ = [
    'ROW_CHUNK',
    'ActionRow',
    'AssetEventAt',
    'MemberAssetEvents',
    'PanelListRow',
    'PassRef',
    'SettingsVersionRow',
    'SourceVersionRow',
    'active_snapshots',
    'catalog_rows',
    'corporate_action_page',
    'created_request_datasets',
    'dataset_identity',
    'dataset_labels',
    'get_panel',
    'list_panels',
    'member_events',
    'panel_catalog_rows',
    'settings_versions_page',
    'snapshot_events',
    'source_settings_versions_page',
]
