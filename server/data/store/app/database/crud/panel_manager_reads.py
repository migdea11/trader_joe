"""The reads and the one stop the panel manager needs besides the ledger and the settings (tj-7zjov1.34, P22).

THE PANEL TABLES ARE ONLY READ HERE. The one write is stop_collecting, an UPDATE of a dataset entry's collect intent;
it never commits, so it lands in the pass's transaction at the next commit. Nothing here touches stopped_cause.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource, Granularity, UpdateType
from data.store.app.database.models.panel import PanelMemberTransition, PanelPass, PanelPassKind, PanelTransition
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


_ENTRY = StoreDatasetEntry.__table__
_PASS = PanelPass.__table__
_TRANSITION = PanelMemberTransition.__table__

# Ids per IN clause (the driver allows 32,767 bind parameters a statement).
ID_CHUNK = 5000


@dataclass(frozen=True)
class PanelEntry:
    """A panel-owned DAILY 1day dataset entry, as the existing check and the exit rule see it."""

    id: uuid.UUID
    symbol: str
    collect: bool | None


@dataclass(frozen=True)
class PassRecord:
    """One panel_pass row, detached."""

    id: uuid.UUID
    kind: PanelPassKind
    settings_version_id: uuid.UUID
    source_settings_version_id: uuid.UUID
    active_snapshot_id: uuid.UUID
    inactive_snapshot_id: uuid.UUID | None
    started_at: datetime
    dry_run: bool


async def panel_entries(
    db: AsyncSession, source: DataSource, owner: str, symbols: Collection[str]
) -> dict[str, PanelEntry]:
    """The owner's DAILY 1day entries of a source for these symbols, by symbol, whatever their declared start.

    The existing check must not key on the declared start (the name_change clamp can move it), so this reads by
    (source, owner, symbol, DAILY, 1day) alone. When a symbol has several entries the oldest wins.
    """
    found: dict[str, PanelEntry] = {}
    names = sorted(set(symbols))
    for start in range(0, len(names), ID_CHUNK):
        rows = await db.execute(
            select(_ENTRY.c.id, _ENTRY.c.asset_symbol, _ENTRY.c.collect)
            .where(
                _ENTRY.c.source == source,
                _ENTRY.c.owner == owner,
                _ENTRY.c.update_type == UpdateType.DAILY,
                _ENTRY.c.granularity == Granularity.ONE_DAY,
                _ENTRY.c.asset_symbol.in_(names[start : start + ID_CHUNK]),
            )
            .order_by(_ENTRY.c.created_at.desc(), _ENTRY.c.id.desc())
        )
        for row in rows:
            # Ordered newest first, so the last write per symbol is the oldest entry.
            found[row.asset_symbol] = PanelEntry(id=row.id, symbol=row.asset_symbol, collect=row.collect)
    return found


async def stop_collecting(db: AsyncSession, dataset_id: uuid.UUID) -> bool:
    """Set collect false on a collecting entry. Never commits and never writes stopped_cause.

    Returns:
        bool: True when the entry was collecting and is now stopped.
    """
    result = await db.execute(
        update(_ENTRY)
        .where(_ENTRY.c.id == dataset_id, _ENTRY.c.collect.is_(True))
        .values(collect=False, updated_at=func.now())
    )
    return result.rowcount == 1


async def latest_pass(
    db: AsyncSession, panel_id: uuid.UUID, kind: PanelPassKind, *, dry_run: bool, finished: bool | None = None
) -> PassRecord | None:
    """The panel's newest pass of a kind and dry-run flag, by started_at then id; None when there is none.

    Args:
        db: The session.
        panel_id: The panel.
        kind: MEMBERSHIP or RELEASE.
        dry_run: Which flag to read.
        finished: True reads only finished passes, False only passes that never finished (a crash between open_pass and
            finish_pass), None any.
    """
    query = select(_PASS).where(_PASS.c.panel_id == panel_id, _PASS.c.kind == kind, _PASS.c.dry_run.is_(dry_run))
    if finished is not None:
        query = query.where(_PASS.c.finished_at.is_not(None) if finished else _PASS.c.finished_at.is_(None))
    row = (await db.execute(query.order_by(_PASS.c.started_at.desc(), _PASS.c.id.desc()).limit(1))).one_or_none()
    if row is None:
        return None
    return PassRecord(
        id=row.id,
        kind=row.kind,
        settings_version_id=row.settings_version_id,
        source_settings_version_id=row.source_settings_version_id,
        active_snapshot_id=row.active_snapshot_id,
        inactive_snapshot_id=row.inactive_snapshot_id,
        started_at=row.started_at,
        dry_run=row.dry_run,
    )


async def created_requests(
    db: AsyncSession, panel_id: uuid.UUID, vendor_asset_ids: Collection[str]
) -> dict[str, list[uuid.UUID]]:
    """The request ids of the panel's non-dry-run CREATED rows per vendor asset, oldest first."""
    found: dict[str, list[uuid.UUID]] = {}
    ids = sorted(set(vendor_asset_ids))
    for start in range(0, len(ids), ID_CHUNK):
        rows = await db.execute(
            select(_TRANSITION.c.vendor_asset_id, _TRANSITION.c.request_id)
            .join(_PASS, _PASS.c.id == _TRANSITION.c.pass_id)
            .where(
                _TRANSITION.c.panel_id == panel_id,
                _TRANSITION.c.transition == PanelTransition.CREATED,
                _TRANSITION.c.request_id.is_not(None),
                _PASS.c.dry_run.is_(False),
                _TRANSITION.c.vendor_asset_id.in_(ids[start : start + ID_CHUNK]),
            )
            .order_by(_TRANSITION.c.recorded_at, _TRANSITION.c.seq)
        )
        for row in rows:
            found.setdefault(row.vendor_asset_id, []).append(row.request_id)
    return found
