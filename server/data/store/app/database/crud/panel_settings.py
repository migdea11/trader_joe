"""The panel settings repository: panels, their settings versions and the source's settings versions (tj-7zjov1.176, P20b).

Design: ADR tj-26jt9w addendum 20, owner rulings 2026-10-27. EVERY TABLE HERE IS APPEND-ONLY: the only writes are INSERTs,
there is no update and no delete function, and none may be added. A change is a new version row. Enable and disable are
versions too.

A VERSION ROW IS A MARKER: saved_at (database time), actor and reason. The reason is OPTIONAL: stored trimmed, and a blank
or missing one is stored as NULL (ADR tj-46jkey, owner ruling 2026-11-06). The actor is still required. EFFECTIVE-FROM IS DERIVED and has no column, and has ONE definition, the boundary read's (crud/panel_ledger.py
boundary_read): the started_at of the first non-dry-run pass OF THIS PANEL that names the version.

A PANEL'S SOURCE NEVER CHANGES: no function takes a source for an existing panel (another source is another panel).

NO FALLBACK NUMBERS. The two hourly rates are optional and None means NO LIMIT (ADR tj-2ayia2, owner ruling
2026-11-07): there is no default value for either. A set ramp is 1 to INT32_MAX and a set cap 2 to INT32_MAX; zero is
refused. current_source_settings still answers None for a source with no version, but create_panel now ALWAYS writes a
source's first version (from the source settings it is given, else no limits and the 90-day window), so every panel's
source has one. The only defaults named are DEFAULT_PANEL_CADENCE (MONTHLY, applied by create_panel when the caller
names no cadence) and the 90-day corporate-action window on both sides. A panel has no size: it selects every asset its
filters match.

Each write commits on its own, through write_transaction, on the session the caller hands in.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.database.models.panel import (
    DEFAULT_CORPORATE_ACTION_WINDOW_DAYS,
    Panel,
    PanelCadence,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.app.database.transaction import write_transaction


# The cadence create_panel applies when the caller names none (owner ruling 2026-10-27 (c)). The schema carries no default.
DEFAULT_PANEL_CADENCE: Final = PanelCadence.MONTHLY

# The smallest ramp, 1: one creation an hour is a real limit.
MIN_RAMP_DATASETS_PER_HOUR: Final = 1
# The smallest request cap, 2. It equals panel_manager.REQUESTS_PER_CREATION (a creation costs the clamp query and the
# bars request), so a smaller cap could never admit a creation. A DB CHECK needs a literal, so a test pins this value,
# the CHECK and REQUESTS_PER_CREATION equal.
MIN_REQUEST_CAP_PER_HOUR: Final = 2
# The largest number any of the four fields takes: the int4 column and the int32 on the wire.
INT32_MAX: Final = 2147483647

_PANEL = Panel.__table__
_SETTINGS = PanelSettingsVersion.__table__
_SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__


@dataclass(frozen=True)
class PanelSettings:
    """A panel's own settings, as sent: enabled, cadence and the filters.

    cadence None means 'not named': create_panel applies DEFAULT_PANEL_CADENCE, append_settings_version refuses it
    (a new version states its cadence in full). The filters restrict nothing at their neutral value: an empty
    allow_symbols, and None for asset_class, exchange, active and tradable.
    """

    enabled: bool
    cadence: PanelCadence | None = None
    allow_symbols: Sequence[str] = ()
    deny_symbols: Sequence[str] = ()
    asset_class: AssetType | None = None
    exchange: str | None = None
    active: bool | None = None
    tradable: bool | None = None


@dataclass(frozen=True)
class SourceSettings:
    """A source's settings, as sent: the two hourly rates and the corporate-action window.

    ramp_datasets_per_hour is the most new panel datasets created per source in any trailing 60 minutes (at least 1);
    request_cap_per_hour the most ingest requests the panel manager writes per source in any trailing 60 minutes (at
    least 2). None for a rate means NO LIMIT; there is no default, so a caller says None explicitly. Zero is refused
    (a pause is enabled=false on the panel). The window defaults to the ruled 90 days before and after.
    """

    ramp_datasets_per_hour: int | None
    request_cap_per_hour: int | None
    corporate_action_window_pre_days: int = DEFAULT_CORPORATE_ACTION_WINDOW_DAYS
    corporate_action_window_post_days: int = DEFAULT_CORPORATE_ACTION_WINDOW_DAYS


@dataclass(frozen=True)
class PanelRecord:
    """One panel row, detached."""

    id: uuid.UUID
    name: str
    source: DataSource
    created_at: datetime


@dataclass(frozen=True)
class PanelSettingsRecord:
    """One panel_settings_version row, detached."""

    id: uuid.UUID
    panel_id: uuid.UUID
    version_no: int
    settings: PanelSettings
    saved_at: datetime
    actor: str
    reason: str | None


@dataclass(frozen=True)
class SourceSettingsRecord:
    """One panel_source_settings_version row, detached."""

    id: uuid.UUID
    source: DataSource
    version_no: int
    settings: SourceSettings
    saved_at: datetime
    actor: str
    reason: str | None


@dataclass(frozen=True)
class EnabledPanel:
    """An enabled panel with the settings version it is currently under."""

    panel: PanelRecord
    current: PanelSettingsRecord = field(repr=False)


def _refuse(detail: str) -> InvalidRequestError:
    return InvalidRequestError(Reason.INVALID_REQUEST, detail)


def _marker(actor: str, reason: str | None) -> tuple[str, str | None]:
    """Return the trimmed actor, refusing a blank one, and the trimmed reason, None when missing or blank."""
    trimmed_actor = actor.strip()
    if not trimmed_actor:
        raise _refuse('A settings version needs an actor.')
    trimmed_reason = reason.strip() if reason is not None else ''
    return trimmed_actor, trimmed_reason or None


def _check_panel_settings(settings: PanelSettings, cadence: PanelCadence | None) -> PanelCadence:
    if cadence is None:
        raise _refuse('A settings version needs a cadence.')
    return cadence


def _check_source_settings(settings: SourceSettings) -> None:
    rates = (
        ('ramp_datasets_per_hour', settings.ramp_datasets_per_hour, MIN_RAMP_DATASETS_PER_HOUR),
        ('request_cap_per_hour', settings.request_cap_per_hour, MIN_REQUEST_CAP_PER_HOUR),
    )
    for field_name, value, minimum in rates:
        if value is not None and not minimum <= value <= INT32_MAX:
            raise _refuse(f'{field_name} must be {minimum} to {INT32_MAX}, or unset for no limit.')
    windows = (
        ('corporate_action_window_pre_days', settings.corporate_action_window_pre_days),
        ('corporate_action_window_post_days', settings.corporate_action_window_post_days),
    )
    for field_name, value in windows:
        if not 0 <= value <= INT32_MAX:
            raise _refuse(f'{field_name} must be 0 to {INT32_MAX}.')


def _panel_settings_values(
    panel_id: uuid.UUID, version_no: int, settings: PanelSettings, cadence: PanelCadence, actor: str, reason: str | None
) -> dict:
    return {
        'panel_id': panel_id,
        'version_no': version_no,
        'enabled': settings.enabled,
        'cadence': cadence,
        'allow_symbols': list(settings.allow_symbols),
        'deny_symbols': list(settings.deny_symbols),
        'asset_class': settings.asset_class,
        'exchange': settings.exchange,
        'active': settings.active,
        'tradable': settings.tradable,
        'actor': actor,
        'reason': reason,
    }


def _source_settings_values(
    source: DataSource, version_no: int, settings: SourceSettings, actor: str, reason: str | None
) -> dict:
    return {
        'source': source,
        'version_no': version_no,
        'ramp_datasets_per_hour': settings.ramp_datasets_per_hour,
        'request_cap_per_hour': settings.request_cap_per_hour,
        'corporate_action_window_pre_days': settings.corporate_action_window_pre_days,
        'corporate_action_window_post_days': settings.corporate_action_window_post_days,
        'actor': actor,
        'reason': reason,
    }


def _panel_record(row) -> PanelRecord:
    return PanelRecord(id=row.id, name=row.name, source=row.source, created_at=row.created_at)


def _settings_record(row) -> PanelSettingsRecord:
    return PanelSettingsRecord(
        id=row.id,
        panel_id=row.panel_id,
        version_no=row.version_no,
        settings=PanelSettings(
            enabled=row.enabled,
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


def _source_settings_record(row) -> SourceSettingsRecord:
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


async def _lock_source(db: AsyncSession, source: DataSource) -> None:
    """Serialise source-version numbering: a transaction-scoped advisory lock keyed on the source."""
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f'{_SOURCE_SETTINGS.name}:{source.name}'))))


async def _lock_panel_name(db: AsyncSession, name: str) -> None:
    """Serialise creates of one panel name: a transaction-scoped advisory lock keyed on the trimmed name.

    Taken before the name check, so the second of two concurrent creates waits, then sees the first's row and gets the
    ordinary refusal instead of hitting the unique index. A hashtext collision only serialises two unrelated creates.
    """
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f'panel:name:{name}'))))


async def _next_source_version_no(db: AsyncSession, source: DataSource) -> int:
    await _lock_source(db, source)
    result = await db.execute(
        select(func.coalesce(func.max(_SOURCE_SETTINGS.c.version_no), 0)).where(_SOURCE_SETTINGS.c.source == source)
    )
    return result.scalar_one() + 1


async def _insert_source_settings(
    db: AsyncSession, source: DataSource, settings: SourceSettings, actor: str, reason: str | None
) -> SourceSettingsRecord:
    version_no = await _next_source_version_no(db, source)
    result = await db.execute(
        _SOURCE_SETTINGS.insert()
        .values(**_source_settings_values(source, version_no, settings, actor, reason))
        .returning(*_SOURCE_SETTINGS.c)
    )
    return _source_settings_record(result.one())


async def _latest_source_settings(db: AsyncSession, source: DataSource) -> SourceSettingsRecord | None:
    result = await db.execute(
        select(*_SOURCE_SETTINGS.c)
        .where(_SOURCE_SETTINGS.c.source == source)
        .order_by(_SOURCE_SETTINGS.c.version_no.desc())
        .limit(1)
    )
    row = result.first()
    return None if row is None else _source_settings_record(row)


async def create_panel(
    db: AsyncSession,
    *,
    name: str,
    source: DataSource,
    settings: PanelSettings,
    actor: str,
    reason: str | None,
    source_settings: SourceSettings | None = None,
) -> tuple[PanelRecord, PanelSettingsRecord]:
    """Create a panel and its settings version 1 in one transaction.

    The cadence is DEFAULT_PANEL_CADENCE when settings names none. On a source with NO version this ALWAYS writes that
    source's version 1 in the same transaction, before the panel's version 1, with the same actor and reason: from
    source_settings when given, else SourceSettings(None, None) (no limits, the 90-day window; ADR tj-2ayia2 D4).
    source_settings is refused (422) for a source that already has a version: a source's rates change for every panel
    on it, so they change only through append_source_settings_version, with its own actor and reason. The source lock
    is taken on EVERY create and the version is read under it, so two concurrent creates on a source with none write
    one version 1 and the second is refused only if it carried source settings.

    Args:
        db: The session.
        name: The panel's name, unique; stored trimmed.
        source: The panel's source, fixed for the panel's life.
        settings: The first settings version.
        actor: The operator principal.
        reason: Why the panel was created, optional; stored trimmed, NULL when None or blank.
        source_settings: The source's settings; accepted only when the source has no settings version yet.

    Returns:
        tuple[PanelRecord, PanelSettingsRecord]: The panel and its version 1.

    Raises:
        InvalidRequestError: INVALID_REQUEST, a blank name or actor, a rate or window out of range, a name already
            used, or source settings given for a source that already has a version. Nothing is written.
    """
    trimmed_name = name.strip()
    if not trimmed_name:
        raise _refuse('A panel needs a name.')
    trimmed_actor, trimmed_reason = _marker(actor, reason)
    cadence = _check_panel_settings(settings, settings.cadence or DEFAULT_PANEL_CADENCE)
    if source_settings is not None:
        _check_source_settings(source_settings)
    async with write_transaction(db, f'create panel {trimmed_name}'):
        # Lock order: the name lock first, then the source lock (every create), then rows. Nothing else
        # takes the name lock, so no cycle. It is held to the commit, so the loser of two concurrent creates reads the
        # winner's row below and is refused like any duplicate name.
        await _lock_panel_name(db, trimmed_name)
        taken = await db.execute(select(_PANEL.c.id).where(_PANEL.c.name == trimmed_name))
        if taken.first() is not None:
            raise _refuse('A panel with that name already exists.')
        # Then the source advisory lock, before rows (the new panel and its settings), on every create. The read below
        # is under it, so two concurrent creates cannot both see no version and write a version 1 and a version 2.
        # Nothing here takes a panel row lock and then the source lock, so no cycle: append_settings_version holds
        # only a panel row lock, append_source_settings_version only the source lock. Re-entrant in this
        # transaction, so _insert_source_settings taking it again is harmless.
        await _lock_source(db, source)
        has_version = await _latest_source_settings(db, source) is not None
        if has_version:
            if source_settings is not None:
                raise _refuse(
                    f'The source {source.name} already has settings, and creating a panel does not change them: '
                    "save the source's settings as their own write, then create the panel without source settings."
                )
        else:
            first = source_settings if source_settings is not None else SourceSettings(None, None)
            await _insert_source_settings(db, source, first, trimmed_actor, trimmed_reason)
        panel_row = (
            await db.execute(_PANEL.insert().values(name=trimmed_name, source=source).returning(*_PANEL.c))
        ).one()
        version_row = (
            await db.execute(
                _SETTINGS.insert()
                .values(**_panel_settings_values(panel_row.id, 1, settings, cadence, trimmed_actor, trimmed_reason))
                .returning(*_SETTINGS.c)
            )
        ).one()
        return _panel_record(panel_row), _settings_record(version_row)


async def append_settings_version(
    db: AsyncSession, *, panel_id: uuid.UUID, settings: PanelSettings, actor: str, reason: str | None
) -> PanelSettingsRecord:
    """Append a panel's next settings version: a full new row, version_no plus one, saved_at by the database.

    Enable and disable are versions too: send the settings with enabled changed. The panel's source cannot change.

    Args:
        db: The session.
        panel_id: The panel.
        settings: The complete new settings; its cadence must be named.
        actor: The operator principal.
        reason: Why, optional; stored trimmed, NULL when None or blank.

    Returns:
        PanelSettingsRecord: The new version.

    Raises:
        InvalidRequestError: NOT_FOUND, no such panel; INVALID_REQUEST, a blank actor or a missing cadence. Nothing is written.
    """
    trimmed_actor, trimmed_reason = _marker(actor, reason)
    cadence = _check_panel_settings(settings, settings.cadence)
    async with write_transaction(db, f'append settings version for panel {panel_id}'):
        # The panel row lock serialises the numbering of two concurrent versions of one panel.
        locked = await db.execute(select(_PANEL.c.id).where(_PANEL.c.id == panel_id).with_for_update())
        if locked.first() is None:
            raise InvalidRequestError(Reason.NOT_FOUND, 'No panel found with that id')
        latest = await db.execute(select(func.max(_SETTINGS.c.version_no)).where(_SETTINGS.c.panel_id == panel_id))
        version_no = (latest.scalar_one() or 0) + 1
        row = (
            await db.execute(
                _SETTINGS.insert()
                .values(
                    **_panel_settings_values(panel_id, version_no, settings, cadence, trimmed_actor, trimmed_reason)
                )
                .returning(*_SETTINGS.c)
            )
        ).one()
        return _settings_record(row)


async def append_source_settings_version(
    db: AsyncSession, *, source: DataSource, settings: SourceSettings, actor: str, reason: str | None
) -> SourceSettingsRecord:
    """Append a source's next settings version: a full new row, version_no plus one, saved_at by the database.

    Args:
        db: The session.
        source: The source the rates and the window apply to.
        settings: The complete new settings.
        actor: The operator principal.
        reason: Why, optional; stored trimmed, NULL when None or blank.

    Returns:
        SourceSettingsRecord: The new version.

    Raises:
        InvalidRequestError: INVALID_REQUEST, a blank actor or a rate or window out of range. Nothing is written.
    """
    trimmed_actor, trimmed_reason = _marker(actor, reason)
    _check_source_settings(settings)
    async with write_transaction(db, f'append source settings version for {source.name}'):
        return await _insert_source_settings(db, source, settings, trimmed_actor, trimmed_reason)


async def current_settings(db: AsyncSession, panel_id: uuid.UUID) -> PanelSettingsRecord | None:
    """The panel's latest settings version, or None when the panel does not exist."""
    result = await db.execute(
        select(*_SETTINGS.c).where(_SETTINGS.c.panel_id == panel_id).order_by(_SETTINGS.c.version_no.desc()).limit(1)
    )
    row = result.first()
    return None if row is None else _settings_record(row)


async def current_source_settings(db: AsyncSession, source: DataSource) -> SourceSettingsRecord | None:
    """The source's latest settings version, or None when it has none yet (no panel was created on it). No fallback."""
    return await _latest_source_settings(db, source)


async def enabled_panels_on_source(db: AsyncSession, source: DataSource) -> list[EnabledPanel]:
    """The panels on a source whose latest settings version is enabled, in creation order then id (the rate-sharing order).

    Args:
        db: The session.
        source: The source.

    Returns:
        list[EnabledPanel]: Each panel with its current version.
    """
    newest = (
        select(func.max(_SETTINGS.c.version_no))
        .where(_SETTINGS.c.panel_id == _PANEL.c.id)
        .correlate(_PANEL)
        .scalar_subquery()
    )
    result = await db.execute(
        select(_PANEL.c.id.label('panel_row_id'), _PANEL.c.name, _PANEL.c.source, _PANEL.c.created_at, *_SETTINGS.c)
        .join(_SETTINGS, _SETTINGS.c.panel_id == _PANEL.c.id)
        .where(_PANEL.c.source == source, _SETTINGS.c.version_no == newest, _SETTINGS.c.enabled.is_(True))
        .order_by(_PANEL.c.created_at, _PANEL.c.id)
    )
    return [
        EnabledPanel(
            panel=PanelRecord(id=row.panel_row_id, name=row.name, source=row.source, created_at=row.created_at),
            current=_settings_record(row),
        )
        for row in result.all()
    ]
