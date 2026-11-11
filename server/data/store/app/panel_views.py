"""What the panel routes serve, composed from the panel reads, the ledger and the health rules (tj-7zjov1.36, P24).

Design: ADR tj-26jt9w addenda 20, 21 and 22, the bead's READ RULES. The routes call this module and map its results to
messages; it knows no HTTP and no protobuf. Statements live in database/crud/panel_reads.py (and the ledger's own reads);
nothing here writes.

A CONSTANT NUMBER OF STATEMENTS PER REQUEST, NEVER ONE PER PANEL, MEMBER OR VERSION. The clock and the calendar are
arguments or looked up once per request, so the rules below are pure over what the reads returned.

THE MEMBER IS THE LEDGER'S LATEST TRANSITION (crud/panel_ledger.py latest_state). Its dataset is the transition's own
dataset_id when it names one, else the dataset its newest CREATED transition's request minted (empty until that request's
first delivery). Health, last bar and the collecting count come from that dataset, judged by the catalog's own evaluation
(dataset_catalog.evaluate_views) at the request's clock. Every vendor asset id with a latest transition is listed,
EXITED and DRAINED included: the status says which.

COUNTS. members: latest status ENTERED, CREATED, DEFERRED, REFUSED or DRAINING. collecting: those of ENTERED, CREATED or
DRAINING whose dataset is collecting (never DEFERRED or REFUSED). draining: DRAINING. lagging: members whose dataset's
health is LATE or OVERDUE.

A DAY is the source-calendar date of the instant it derives from. appeared_on is the day of the member's latest APPEARED
event on the source's ACTIVE list; vanished_on the day of its latest VANISHED event on that list when that event is later
than the APPEARED one or there is no APPEARED one (the first snapshot of a list derives no events, so an original member's
first event can be its vanishing). A source with no trading calendar has no source-calendar day: both stay empty.

CURSORS are opaque, URL-safe base64 of a small JSON document naming the route and the thing paged (the panel, source or
dataset id) as well as the last item's key, so a cursor made for one route or one parent is a 422 on any other.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource
from common.errors.vocabulary import InvalidRequestError, Reason
from data.store.app.database.crud import panel_reads
from data.store.app.database.crud.panel_ledger import MemberTransition, boundary_read, latest_state
from data.store.app.database.crud.panel_reads import ActionRow, PanelListRow, SettingsVersionRow, SourceVersionRow
from data.store.app.database.crud.panel_settings import PanelRecord
from data.store.app.database.models.panel import PanelTransition
from data.store.app.dataset_catalog import _decode_cursor, _encode_cursor, evaluate_views, invalid_cursor, is_collecting
from data.store.app.health import DatasetHealthStatus, HealthConfig, settle_margin_from_env
from data.store.app.trading_calendar import (
    CalendarRangeError,
    TradingCalendar,
    UnknownCalendarError,
    calendar_for_source,
    local_midnight,
)


# The most days one asset-list history spans.
MAX_HISTORY_DAYS: Final = 366

# The latest statuses that make a vendor asset a member now (the rest, EXITED and DRAINED, are listed but not counted).
_MEMBER_NOW: Final = frozenset(
    {
        PanelTransition.ENTERED,
        PanelTransition.CREATED,
        PanelTransition.DEFERRED,
        PanelTransition.REFUSED,
        PanelTransition.DRAINING,
    }
)
# Of those, the ones that can have a collecting dataset: a deferred or refused member has no dataset of its own to count.
_COLLECTING_STATUSES: Final = frozenset({PanelTransition.ENTERED, PanelTransition.CREATED, PanelTransition.DRAINING})
_LAGGING: Final = frozenset({DatasetHealthStatus.LATE, DatasetHealthStatus.OVERDUE})
_CAUSE_STATUSES: Final = frozenset({PanelTransition.DEFERRED, PanelTransition.REFUSED})

_SETTINGS_VERSIONS_ROUTE: Final = 'panel-settings-versions'
_SOURCE_VERSIONS_ROUTE: Final = 'panel-source-settings-versions'
_MEMBERS_ROUTE: Final = 'panel-members'
_ACTIONS_ROUTE: Final = 'corporate-actions'


class MarkerKind(StrEnum):
    """What a boundary marker marks. The names are PanelMarkerKind's members without the prefix."""

    PANEL_SETTINGS = 'PANEL_SETTINGS'
    SOURCE_SETTINGS = 'SOURCE_SETTINGS'
    COLLECT_STOP = 'COLLECT_STOP'
    COLLECT_RESUME = 'COLLECT_RESUME'


class DayState(StrEnum):
    """How the history reads one expected day. The names are AssetListDayState's members without the prefix."""

    SAVED = 'SAVED'
    MISSING = 'MISSING'
    EMPTY = 'EMPTY'
    NOT_DUE = 'NOT_DUE'


@dataclass(frozen=True)
class MemberCounts:
    members: int = 0
    collecting: int = 0
    draining: int = 0
    lagging: int = 0


@dataclass(frozen=True)
class PanelDetailView:
    """A panel with its current settings version, its source's current version for it, and its counts."""

    panel: PanelRecord
    current_version: SettingsVersionRow
    current_source_version: SourceVersionRow | None
    counts: MemberCounts


@dataclass(frozen=True)
class SettingsVersionPageView:
    items: list[SettingsVersionRow]
    next_cursor: str


@dataclass(frozen=True)
class SourceVersionPageView:
    items: list[SourceVersionRow]
    next_cursor: str


@dataclass(frozen=True)
class MemberView:
    """One member as the list shows it. health is None with no dataset, or when the source has no calendar."""

    vendor_asset_id: str
    symbol: str
    status: PanelTransition
    cause: str
    dataset_id: uuid.UUID | None
    appeared_on: str
    vanished_on: str
    last_bar: datetime | None
    health: DatasetHealthStatus | None
    as_of: datetime
    recorded_at: datetime


@dataclass(frozen=True)
class MemberPageView:
    items: list[MemberView]
    next_cursor: str


@dataclass(frozen=True)
class MarkerView:
    kind: MarkerKind
    at: datetime
    version_no: int = 0
    actor: str = ''
    # Empty when none was given (a settings version's reason is optional).
    reason: str = ''
    dataset_id: uuid.UUID | None = None
    label: str = ''


@dataclass(frozen=True)
class BoundariesView:
    start: datetime
    end: datetime
    markers: list[MarkerView]


@dataclass(frozen=True)
class HistoryDayView:
    day: str
    state: DayState
    changed_count: int


@dataclass(frozen=True)
class HistoryView:
    start: datetime
    end: datetime
    days: list[HistoryDayView]
    as_of: datetime


@dataclass(frozen=True)
class ActionPageView:
    items: list[ActionRow]
    next_cursor: str


def _refuse(detail: str) -> InvalidRequestError:
    return InvalidRequestError(Reason.INVALID_REQUEST, detail)


def _cursor(route: str, scope: str, key: list[Any]) -> str:
    return _encode_cursor({'o': route, 's': scope, 'k': key})


def _cursor_key(cursor: str, route: str, scope: str, width: int) -> list[Any]:
    payload = _decode_cursor(cursor)
    key = payload.get('k')
    if payload.get('o') != route or payload.get('s') != scope or not isinstance(key, list) or len(key) != width:
        raise invalid_cursor()
    return key


def _version_cursor_key(cursor: str | None, route: str, scope: str) -> int | None:
    if cursor is None:
        return None
    (version_no,) = _cursor_key(cursor, route, scope, 1)
    if not isinstance(version_no, int) or isinstance(version_no, bool):
        raise invalid_cursor()
    return version_no


async def list_panels(db: AsyncSession) -> list[PanelListRow]:
    """Every panel by name then id, with the enabled flag of its latest settings version."""
    return await panel_reads.list_panels(db)


async def panel_detail(
    db: AsyncSession, panel_id: uuid.UUID, now: datetime, config: HealthConfig | None = None
) -> PanelDetailView | None:
    """A panel, its current settings version and source version (each with its effective pass) and its counts.

    The source version's effective pass is read from THIS panel's passes (the boundary read's rule). A panel always has a
    settings version (create_panel writes version 1 with it) and a source version (create_panel refuses a source without
    one), so a missing one is a database that was edited by hand and reads as no panel.

    Args:
        db: The request's session.
        panel_id: The panel.
        now: The instant health is judged at; aware.
        config: The health settings; the environment's when omitted.

    Returns:
        PanelDetailView | None: The detail, or None for an unknown panel.
    """
    panel = await panel_reads.get_panel(db, panel_id)
    if panel is None:
        return None
    versions = await panel_reads.settings_versions_page(db, panel_id, before=None, limit=1)
    if not versions:
        return None
    source_versions = await panel_reads.source_settings_versions_page(
        db, panel.source, before=None, limit=1, panel_id=panel_id
    )
    counts = await _counts(db, panel_id, now, config)
    return PanelDetailView(
        panel=panel,
        current_version=versions[0],
        current_source_version=source_versions[0] if source_versions else None,
        counts=counts,
    )


def _dataset_of(state: MemberTransition, minted: dict[str, uuid.UUID]) -> uuid.UUID | None:
    return state.dataset_id if state.dataset_id is not None else minted.get(state.vendor_asset_id)


async def _counts(db: AsyncSession, panel_id: uuid.UUID, now: datetime, config: HealthConfig | None) -> MemberCounts:
    states = await latest_state(db, panel_id)
    members = {vendor_id: state for vendor_id, state in states.items() if state.transition in _MEMBER_NOW}
    if not members:
        return MemberCounts()
    minted = await panel_reads.created_request_datasets(db, panel_id)
    rows = await panel_reads.panel_catalog_rows(db, panel_id)
    dataset_of = {vendor_id: _dataset_of(state, minted) for vendor_id, state in members.items()}
    judged = [rows[dataset_id] for dataset_id in set(dataset_of.values()) if dataset_id in rows]
    health = {view.row.id: view.health for view in await evaluate_views(db, judged, now, config)}
    collecting = draining = lagging = 0
    for vendor_id, state in members.items():
        row = rows.get(dataset_of[vendor_id]) if dataset_of[vendor_id] is not None else None
        if state.transition is PanelTransition.DRAINING:
            draining += 1
        if state.transition in _COLLECTING_STATUSES and row is not None and is_collecting(row, False):
            collecting += 1
        verdict = health.get(row.id) if row is not None else None
        if verdict is not None and verdict.status in _LAGGING:
            lagging += 1
    return MemberCounts(members=len(members), collecting=collecting, draining=draining, lagging=lagging)


async def settings_version_page(
    db: AsyncSession, panel_id: uuid.UUID, *, cursor: str | None, limit: int
) -> SettingsVersionPageView | None:
    """One page of a panel's settings versions, newest first, or None for an unknown panel.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a cursor made for another route or panel.
    """
    scope = str(panel_id)
    before = _version_cursor_key(cursor, _SETTINGS_VERSIONS_ROUTE, scope)
    if await panel_reads.get_panel(db, panel_id) is None:
        return None
    rows = await panel_reads.settings_versions_page(db, panel_id, before=before, limit=limit + 1)
    page = rows[:limit]
    more = len(rows) > limit
    return SettingsVersionPageView(
        items=page, next_cursor=_cursor(_SETTINGS_VERSIONS_ROUTE, scope, [page[-1].record.version_no]) if more else ''
    )


async def source_version_page(
    db: AsyncSession, source: DataSource, *, cursor: str | None, limit: int
) -> SourceVersionPageView:
    """One page of a source's settings versions, newest first. A source with no version is an empty page.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a cursor made for another route or source.
    """
    scope = source.name
    before = _version_cursor_key(cursor, _SOURCE_VERSIONS_ROUTE, scope)
    rows = await panel_reads.source_settings_versions_page(db, source, before=before, limit=limit + 1)
    page = rows[:limit]
    more = len(rows) > limit
    return SourceVersionPageView(
        items=page, next_cursor=_cursor(_SOURCE_VERSIONS_ROUTE, scope, [page[-1].record.version_no]) if more else ''
    )


def _calendar_or_none(source: DataSource) -> TradingCalendar | None:
    try:
        return calendar_for_source(source)
    except UnknownCalendarError:
        return None


def _day_of(instant: datetime, calendar: TradingCalendar | None) -> str:
    return '' if calendar is None else instant.astimezone(calendar.tz).date().isoformat()


def member_days(events: panel_reads.MemberAssetEvents | None, calendar: TradingCalendar | None) -> tuple[str, str]:
    """A member's (appeared_on, vanished_on) days; see the module docstring for the rule."""
    if events is None:
        return '', ''
    appeared = events.appeared_at
    vanished = events.vanished_at
    vanished_counts = vanished is not None and (appeared is None or vanished > appeared)
    return (
        _day_of(appeared, calendar) if appeared is not None else '',
        _day_of(vanished, calendar) if vanished_counts and vanished is not None else '',
    )


async def member_page(
    db: AsyncSession,
    panel_id: uuid.UUID,
    *,
    cursor: str | None,
    limit: int,
    now: datetime,
    config: HealthConfig | None = None,
) -> MemberPageView | None:
    """One page of a panel's members, keyset over (symbol, vendor asset id), or None for an unknown panel.

    Only the page's datasets are read and judged. Every vendor asset id with a latest transition is listed.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a cursor made for another route or panel.
    """
    scope = str(panel_id)
    after: tuple[str, str] | None = None
    if cursor is not None:
        symbol, vendor_id = _cursor_key(cursor, _MEMBERS_ROUTE, scope, 2)
        if not isinstance(symbol, str) or not isinstance(vendor_id, str):
            raise invalid_cursor()
        after = (symbol, vendor_id)
    panel = await panel_reads.get_panel(db, panel_id)
    if panel is None:
        return None
    states = await latest_state(db, panel_id)
    ordered = sorted(states.values(), key=lambda state: (state.symbol, state.vendor_asset_id))
    if after is not None:
        ordered = [state for state in ordered if (state.symbol, state.vendor_asset_id) > after]
    page = ordered[:limit]
    more = len(ordered) > limit
    if not page:
        return MemberPageView(items=[], next_cursor='')
    minted = await panel_reads.created_request_datasets(db, panel_id)
    dataset_of = {state.vendor_asset_id: _dataset_of(state, minted) for state in page}
    rows = await panel_reads.catalog_rows(db, {ds for ds in dataset_of.values() if ds is not None})
    health = {view.row.id: view.health for view in await evaluate_views(db, list(rows.values()), now, config)}
    events = await panel_reads.member_events(db, panel.source, [state.vendor_asset_id for state in page])
    calendar = _calendar_or_none(panel.source)
    items: list[MemberView] = []
    for state in page:
        dataset_id = dataset_of[state.vendor_asset_id]
        row = rows.get(dataset_id) if dataset_id is not None else None
        verdict = health.get(row.id) if row is not None else None
        appeared_on, vanished_on = member_days(events.get(state.vendor_asset_id), calendar)
        items.append(
            MemberView(
                vendor_asset_id=state.vendor_asset_id,
                symbol=state.symbol,
                status=state.transition,
                cause=(state.cause or '') if state.transition in _CAUSE_STATUSES else '',
                dataset_id=dataset_id,
                appeared_on=appeared_on,
                vanished_on=vanished_on,
                last_bar=row.last_bar if row is not None else None,
                health=verdict.status if verdict is not None else None,
                as_of=state.as_of,
                recorded_at=state.recorded_at,
            )
        )
    last = page[-1]
    return MemberPageView(
        items=items, next_cursor=_cursor(_MEMBERS_ROUTE, scope, [last.symbol, last.vendor_asset_id]) if more else ''
    )


async def boundaries(db: AsyncSession, panel_id: uuid.UUID, start: datetime, end: datetime) -> BoundariesView | None:
    """Every marker of a panel in [start, end), oldest first, or None for an unknown panel.

    The panel's and its source's settings versions come from the ledger's boundary read, as do the operator's collect
    stops and resumes on the panel's datasets (the override log's new_value 'false' is a stop, 'true' a resume; a row
    with no new_value says neither and is left out).

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) when start is not before end.
    """
    if not start < end:
        raise _refuse('start must be before end.')
    if await panel_reads.get_panel(db, panel_id) is None:
        return None
    read = await boundary_read(db, panel_id)
    markers: list[MarkerView] = [
        MarkerView(
            kind=MarkerKind.PANEL_SETTINGS,
            at=version.saved_at,
            version_no=version.version_no,
            actor=version.actor,
            reason=version.reason or '',
        )
        for version in read.settings_versions
        if start <= version.saved_at < end
    ]
    markers.extend(
        MarkerView(
            kind=MarkerKind.SOURCE_SETTINGS,
            at=version.saved_at,
            version_no=version.version_no,
            actor=version.actor,
            reason=version.reason or '',
        )
        for version in read.source_settings_versions
        if start <= version.saved_at < end
    )
    stops = [stop for stop in read.collect_stops if start <= stop.at < end and stop.new_value in ('true', 'false')]
    labels = await panel_reads.dataset_labels(db, {stop.dataset_id for stop in stops})
    markers.extend(
        MarkerView(
            kind=MarkerKind.COLLECT_STOP if stop.new_value == 'false' else MarkerKind.COLLECT_RESUME,
            at=stop.at,
            actor=stop.actor,
            reason=stop.reason,
            dataset_id=stop.dataset_id,
            label=labels.get(stop.dataset_id, ''),
        )
        for stop in stops
    )
    # Stable: equal instants keep the order panel, source, collect.
    markers.sort(key=lambda marker: marker.at)
    return BoundariesView(start=start, end=end, markers=markers)


def _history_days(start: datetime, end: datetime, calendar: TradingCalendar) -> list[date]:
    try:
        sessions = calendar.sessions_overlapping(start - timedelta(days=1), end + timedelta(days=1))
        return [day for day in sessions if start <= local_midnight(day, calendar) < end]
    except CalendarRangeError:
        raise _refuse('The range starts before the trading calendar does.') from None


async def asset_list_history(
    db: AsyncSession, panel_id: uuid.UUID, start: datetime, end: datetime, now: datetime
) -> HistoryView | None:
    """The asset-list history of a panel's source: one entry per session in [start, end), ascending, or None for an unknown panel.

    A day D is in the history iff D's local midnight in the source calendar's zone lies in [start, end). Its state reads
    the ACTIVE list's snapshots whose as_of falls on D in that zone: SAVED (a non-empty snapshot), EMPTY (only empty
    ones), else MISSING once D's close plus DAILY_REFRESH_SETTLE_MINUTES has passed at `now` and NOT_DUE before. The
    changed_count is the distinct vendor asset ids with an event of any kind, from either list, in a snapshot dated D, over
    the whole source list; 0 unless the day is SAVED.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) when start is not before end, the range exceeds MAX_HISTORY_DAYS, the
            source has no trading calendar or the range starts before it does.
    """
    if not start < end:
        raise _refuse('start must be before end.')
    if end - start > timedelta(days=MAX_HISTORY_DAYS):
        raise _refuse(f'The range spans more than {MAX_HISTORY_DAYS} days.')
    panel = await panel_reads.get_panel(db, panel_id)
    if panel is None:
        return None
    calendar = _calendar_or_none(panel.source)
    if calendar is None:
        raise _refuse('No trading calendar covers this panel, so its asset-list history cannot be computed.')
    days = _history_days(start, end, calendar)
    since, until = start - timedelta(days=2), end + timedelta(days=2)
    snapshots = await panel_reads.active_snapshots(db, panel.source, since, until)
    events = await panel_reads.snapshot_events(db, panel.source, since, until)
    total: dict[date, int] = {}
    non_empty: dict[date, int] = {}
    for as_of, row_count in snapshots:
        local = as_of.astimezone(calendar.tz).date()
        total[local] = total.get(local, 0) + 1
        if row_count > 0:
            non_empty[local] = non_empty.get(local, 0) + 1
    changed: dict[date, set[str]] = {}
    for event in events:
        changed.setdefault(event.as_of.astimezone(calendar.tz).date(), set()).add(event.vendor_asset_id)
    settle = settle_margin_from_env()
    out: list[HistoryDayView] = []
    for day in days:
        if non_empty.get(day):
            state = DayState.SAVED
        elif total.get(day):
            state = DayState.EMPTY
        elif now >= calendar.session_close(day) + settle:
            state = DayState.MISSING
        else:
            state = DayState.NOT_DUE
        out.append(
            HistoryDayView(
                day=day.isoformat(),
                state=state,
                changed_count=len(changed.get(day, ())) if state is DayState.SAVED else 0,
            )
        )
    return HistoryView(start=start, end=end, days=out, as_of=now)


async def corporate_action_events(
    db: AsyncSession, dataset_id: uuid.UUID, *, cursor: str | None, limit: int
) -> ActionPageView | None:
    """One page of a dataset's corporate-action observations, by the date they take effect, newest first, or None for an unknown dataset.

    The order is COALESCE(ex_date, effective_date, process_date) descending with undated rows last, then as_of, then id,
    both descending. Every stored observation is shown: no window filters the view.

    Raises:
        InvalidRequestError: INVALID_REQUEST (422) for a cursor made for another route or dataset.
    """
    scope = str(dataset_id)
    after: tuple[datetime | None, datetime, uuid.UUID] | None = None
    if cursor is not None:
        date_text, as_of_text, action_id = _cursor_key(cursor, _ACTIONS_ROUTE, scope, 3)
        try:
            sort_date = datetime.fromisoformat(date_text).astimezone(UTC) if date_text else None
            after = (sort_date, datetime.fromisoformat(as_of_text).astimezone(UTC), uuid.UUID(action_id))
        except (TypeError, ValueError):
            raise invalid_cursor() from None
    identity = await panel_reads.dataset_identity(db, dataset_id)
    if identity is None:
        return None
    source, symbol = identity
    rows = await panel_reads.corporate_action_page(db, source, symbol, after=after, limit=limit + 1)
    page = rows[:limit]
    more = len(rows) > limit
    next_cursor = ''
    if more:
        last = page[-1]
        last_date = panel_reads.action_sort_date(last)
        key = [last_date.isoformat() if last_date else '', last.as_of.isoformat(), str(last.id)]
        next_cursor = _cursor(_ACTIONS_ROUTE, scope, key)
    return ActionPageView(items=page, next_cursor=next_cursor)
