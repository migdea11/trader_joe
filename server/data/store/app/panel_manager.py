"""THE PANEL MANAGER (tj-7zjov1.34, P22): decides which assets a panel holds and creates their datasets.

Design: ADR tj-26jt9w addenda 20 and 21, the owner rulings of 2026-10-27 to 2026-10-31 (default cadence MONTHLY, the
first pass at creation then an interval from the last pass, the daily bars of members are the DAILY refresh's, never
this module's), the rate design of 14:16 UTC on tj-7zjov1.33 and the consolidated creation ruling on tj-7zjov1.34.

ONE LOOP, NOTHING STORED. run_periodic ticks every 5 minutes (a constant, not a setting) and every tick works out from the
rows what is due, so a crash or restart resets nothing. A long Every interval is never used: it would fire on every
restart.

TWO KINDS OF PASS per enabled panel, both written to the membership ledger (crud/panel_ledger.py) in the pass's own session.
  MEMBERSHIP  when membership_due_at says so, and an active snapshot at or after the due time exists (or the wait for one
              ended at the next close, when the older snapshot is used and the pass says so in the log). Reads the newest
              active snapshot, applies the panel's filters, decides ENTERED, EXITED, DRAINING, DRAINED, REFUSED and, for
              the members it creates, CREATED, or DEFERRED when a rate window is full.
  RELEASE     between membership passes, only while the panel's settings version is the deciding pass's: creates members
              whose latest transition is DEFERRED while both hourly windows have room. It decides no membership.

THE RATE is per source and per hour, read from the rows each time: created_in_window (CREATED rows) against
ramp_datasets_per_hour and requests_in_window (every request the panel owner wrote, kind and state aside) against
request_cap_per_hour. A creation costs TWO requests (the clamp query and the bars request). It applies to creation work
only; the daily bars of members are the DAILY refresh's.

CREATING A MEMBER'S DATASET (ruling D). CREATED names the BARS REQUEST written in the same commit as the row, never a
dataset: the delivery intake mints the entry on the first delivered attempt, as for every POST. Per member, on the pass's
session P: existing and in-flight checks, budget, commit P, the clamp on its OWN session (never P), the own-overlap check on
P with the clamped start, then ONE commit of ENTERED + CREATED + create_request, and the submit after it. The bars verdict
is never awaited. A rollback in that commit loses that member's ENTERED and CREATED; the next pass re-decides, and nothing
is retried within the pass. P never carries a pending row across the clamp.

ONE WRITER PER PASS: a pass is written from open_pass to finish_pass by one session of one task. The clamp's own session
writes ingest_request only.

NOTHING IN A LOG NAMES AN OWNER OR A SYMBOL: ids, counts, sources and exception types only.
"""

import asyncio
import contextlib
import uuid
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType
from common.environment import get_env_var
from common.errors.vocabulary import TraderJoeError
from common.logging import get_logger
from common.rpc.clients.ingest_submit import IngestSubmitClient
from common.scheduling import Every, SessionCalendar, add_calendar_months, run_periodic
from data.store.app.client_label import PANEL_OWNER
from data.store.app.daily_refresh import settle_delay
from data.store.app.database.crud.asset_list import (
    SnapshotRecord,
    SnapshotRow,
    newest_snapshot,
    rows_of_ids,
    snapshot_by_id,
    snapshot_rows,
    snapshots_between,
)
from data.store.app.database.crud.ingest_request import (
    IngestRequestRecord,
    create_request,
    fetch_finished_since,
    requests_by_ids,
    requests_in_window,
    symbols_with_open_requests,
)
from data.store.app.database.crud.ingest_run import last_accepted_vendor_asset_id
from data.store.app.database.crud.panel_ledger import (
    MemberTransition,
    NewTransition,
    append_transitions,
    created_in_window,
    finish_pass,
    latest_state,
    open_pass,
)
from data.store.app.database.crud.panel_manager_reads import (
    PanelEntry,
    PassRecord,
    created_requests,
    latest_pass,
    panel_entries,
    resume_panel_entry,
    stop_collecting,
)
from data.store.app.database.crud.panel_settings import (
    EnabledPanel,
    PanelSettings,
    SourceSettingsRecord,
    current_source_settings,
    enabled_panels_on_source,
)
from data.store.app.database.crud.stock.store_dataset_entry import OverlapKey, OwnOverlapConflict, check_own_overlap
from data.store.app.database.database import own_session
from data.store.app.database.models.ingest_request import IngestRequestFailureCause
from data.store.app.database.models.ingest_run import IngestRunTrigger
from data.store.app.database.models.panel import PanelCadence, PanelPassKind, PanelTransition
from data.store.app.database.models.store_dataset_entry import DatasetStopCause
from data.store.app.events import DatasetChange, DatasetChanged, publish
from data.store.app.ingest.data_action_request import (
    StartClamp,
    first_request_clamp,
    new_bars_request,
    submit_record,
    verdict_deadline_seconds,
)
from data.store.app.trading_calendar import (
    CALENDAR_START,
    SOURCE_CALENDARS,
    TradingCalendar,
    UnknownCalendarError,
    calendar_for_source,
    local_midnight,
)
from schemas.data_ingest.submit import AssetListStatus
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


log = get_logger(__name__)

ENABLED_ENV: Final = 'PANEL_MANAGER_ENABLED'
DRY_RUN_ENV: Final = 'PANEL_MANAGER_DRY_RUN'
_FALSE: Final = ('false', '0', 'no', 'off')
_TRUE: Final = ('true', '1', 'yes', 'on')

# The tick: constant, not a setting. Everything due is derived from the rows at each tick.
TICK_INTERVAL: Final = timedelta(minutes=5)
WEEKLY_INTERVAL: Final = timedelta(days=7)
# The trailing window of both hourly rates.
RATE_WINDOW: Final = timedelta(minutes=60)
# A membership pass that crashed (never finished) holds off the next one this long, so a pass that crashes every time
# retries at most once an hour, never at the tick rate.
UNFINISHED_PASS_RETRY: Final = timedelta(hours=1)
# A creation writes a corporate-actions request (the clamp) and a bars request.
REQUESTS_PER_CREATION: Final = 2


def panel_dataset_start(source: DataSource) -> datetime:
    """The declared start of a panel dataset of `source`, before the name_change clamp moves it.

    The source calendar's local midnight of CALENDAR_START, the widest start the store's calendars cover (UTC midnight
    would fall on the previous local date, before the calendar's first day, and the dataset would get no health and
    never be owed by the DAILY refresh). The vendor answers from the first bar it holds. Raises UnknownCalendarError for
    a source without a calendar; never falls back to UTC.
    """
    return local_midnight(CALENDAR_START, calendar_for_source(source))


# Ledger causes (no CHECK in the schema, by design): pinned by name.
CAUSE_RAMP_RATE: Final = 'ramp_rate'
CAUSE_REQUEST_RATE: Final = 'request_rate'
CAUSE_CLAMP_UNAVAILABLE: Final = 'clamp_unavailable'
CAUSE_OWN_OVERLAP: Final = 'own_overlap'
# Unreachable while panel datasets are open-ended (end None); the check stays.
CAUSE_CLAMP_WINDOW: Final = 'clamp_window'
CAUSE_VENDOR_REFUSED: Final = 'vendor_refused'
# On ENTERED: the member's manager-stopped dataset was restarted in the same commit (addendum 23).
CAUSE_DATASET_RESUMED: Final = 'dataset_resumed'

# A member no longer in the panel: its latest transition is one of these.
_NOT_MEMBER: Final = (PanelTransition.EXITED, PanelTransition.DRAINED)


def is_enabled() -> bool:
    """Whether the loop runs. Anything but an explicit false (`false`, `0`, `no`, `off`) leaves it on.

    Every panel is also off until a settings version enables it, so this is the process-wide switch only.
    """
    return get_env_var(ENABLED_ENV, default='true', cast_type=str).strip().lower() not in _FALSE


def is_dry_run() -> bool:
    """Whether passes are dry runs: flagged ledger rows, nothing applied. Only an explicit true turns it on."""
    return get_env_var(DRY_RUN_ENV, default='false', cast_type=str).strip().lower() in _TRUE


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('a naive datetime names no instant')
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------------------------------------------------
# Pure rules: no database, the clock and the calendar are arguments.
# ---------------------------------------------------------------------------------------------------------------------


def membership_due_at(
    cadence: PanelCadence,
    last_membership_started_at: datetime | None,
    created_at: datetime,
    calendar: SessionCalendar,
    settle: timedelta,
) -> datetime:
    """The instant a panel's next MEMBERSHIP pass is due, from its last non-dry-run MEMBERSHIP pass.

    No pass yet: due at the panel's creation (the first pass runs at creation). Otherwise an interval from the last pass,
    not a calendar boundary: MONTHLY is one calendar month later (UTC, day clamped to the month's end, via
    add_calendar_months), WEEKLY 7 days later, DAILY the first session close after the last pass plus the settle margin
    on the source's calendar (the instant the asset-list schedule requests that day's list).

    Args:
        cadence: The panel's cadence from its current settings version.
        last_membership_started_at: started_at of the panel's latest non-dry-run MEMBERSHIP pass, or None.
        created_at: When the panel was created.
        calendar: The source's trading calendar (read for DAILY only).
        settle: The wait after a close, DAILY_REFRESH_SETTLE_MINUTES.

    Returns:
        datetime: The due instant, aware UTC. A caller runs the pass when now is at or after it.

    Raises:
        ValueError: A naive datetime.
    """
    if last_membership_started_at is None:
        return _aware(created_at)
    last = _aware(last_membership_started_at)
    if cadence is PanelCadence.MONTHLY:
        return add_calendar_months(last, 1)
    if cadence is PanelCadence.WEEKLY:
        return last + WEEKLY_INTERVAL
    # DAILY: the latest session whose close plus settle is at or before the last pass, then the session after it. Its
    # close plus settle is strictly after the last pass, so a pass never repeats within one session.
    session = calendar.last_completed_session(last - settle)
    following = calendar.next_session(session if session is not None else (last - settle).date() - timedelta(days=1))
    return calendar.session_close(following).astimezone(UTC) + settle


def snapshot_gate(
    now: datetime,
    due: datetime,
    newest_as_of: datetime,
    calendar: SessionCalendar,
    settle: timedelta,
    *,
    first: bool = False,
) -> tuple[bool, bool]:
    """Whether a due pass may run on the newest active snapshot, and whether that snapshot is older than it should be.

    The pass needs an active snapshot with as_of at or after the due time: it waits for one until the next close after the
    due time (plus settle), then runs on the older one, and the caller logs that it did.

    The FIRST pass of a panel (`first`) never waits (owner ruling (c) of 2026-10-27: the first pass runs at creation): it
    runs at once on the newest snapshot, and the flag says whether that snapshot is older than the latest session close
    plus settle.

    Returns:
        tuple[bool, bool]: (may run now, the snapshot is older than the due time; for a first pass, older than the latest
        close plus settle).
    """
    now, due, newest_as_of = _aware(now), _aware(due), _aware(newest_as_of)
    if now < due:
        return False, False
    if first:
        session = calendar.last_completed_session(now - settle)
        if session is None:
            return True, False
        return True, newest_as_of < calendar.session_close(session).astimezone(UTC) + settle
    if newest_as_of >= due:
        return True, False
    give_up = membership_due_at(PanelCadence.DAILY, due, due, calendar, settle)
    return now >= give_up, True


def row_selected(
    row: SnapshotRow, settings: PanelSettings, asset_type: AssetType, list_status: AssetListStatus
) -> bool:
    """Whether a snapshot row passes the panel's filters (a panel has no size, so a selected row is a member).

    An empty allow list restricts nothing, a non-empty one restricts to those symbols; the deny list removes; the asset
    class, exchange, active and tradable filters do not filter at None. A row whose active flag the vendor did not say is
    active exactly when the snapshot is the active list; a tradable flag the vendor did not say never matches a
    tradable filter.
    """
    if settings.asset_class is not None and settings.asset_class is not asset_type:
        return False
    symbol = row.symbol.upper()
    allow = {name.upper() for name in settings.allow_symbols}
    if allow and symbol not in allow:
        return False
    if symbol in {name.upper() for name in settings.deny_symbols}:
        return False
    if settings.exchange is not None and row.exchange != settings.exchange:
        return False
    if settings.active is not None:
        active = row.active if row.active is not None else list_status is AssetListStatus.ACTIVE
        if active is not settings.active:
            return False
    return settings.tradable is None or row.tradable is settings.tradable


def select_members(
    rows: Sequence[SnapshotRow], settings: PanelSettings, asset_type: AssetType, list_status: AssetListStatus
) -> list[SnapshotRow]:
    """The panel's members in a snapshot: EVERY row that passes the filters, in symbol order (no size, no cut).

    Deterministic: ordered by symbol then vendor asset id, so equal inputs give equal members.
    """
    chosen = [row for row in rows if row_selected(row, settings, asset_type, list_status)]
    chosen.sort(key=lambda row: (row.symbol, row.vendor_asset_id))
    return chosen


def evidence_as_of(
    vendor_asset_id: str,
    selected_now: bool,
    snapshots: Sequence[SnapshotRecord],
    rows: Mapping[tuple[uuid.UUID, str], SnapshotRow],
    settings: PanelSettings,
    asset_type: AssetType,
    list_status: AssetListStatus,
    default: datetime,
    baseline: SnapshotRecord | None = None,
) -> datetime:
    """The as_of of a transition driven by snapshot evidence: the first snapshot showing the new outcome.

    `snapshots` are the active snapshots after the previous pass's, oldest first, the current one last. The outcome in a
    snapshot is the row's presence and its filters. The BASELINE is the previous pass's own
    snapshot: when the outcome there already equals the new one, nothing changed it between the two snapshots, and
    the answer is `default`, the current snapshot's as_of. Otherwise the change lies in (the baseline's as_of, the
    returned as_of].
    """
    if baseline is not None:
        row = rows.get((baseline.id, vendor_asset_id))
        if (row is not None and row_selected(row, settings, asset_type, list_status)) == selected_now:
            return default
    for snapshot in snapshots:
        row = rows.get((snapshot.id, vendor_asset_id))
        outcome = row is not None and row_selected(row, settings, asset_type, list_status)
        if outcome == selected_now:
            return snapshot.as_of
    return default


def clamp_refusal(clamp: StartClamp | None, end: datetime | None) -> str | None:
    """CAUSE_CLAMP_WINDOW when the clamped start leaves no window, else None.

    The whole window belongs to an earlier holder of the symbol when the clamp moved the start to or past the declared end.
    Unreachable while panel datasets are open-ended (end None); kept so that a future end date cannot send a bars request
    for an earlier holder's window.
    """
    if clamp is not None and end is not None and clamp.clamped_start >= end:
        return CAUSE_CLAMP_WINDOW
    return None


def budget_cause(created_in_hour: int, requests_in_hour: int, ramp: int, cap: int) -> str | None:
    """The deferral cause when a creation does not fit the hourly windows, else None.

    Ramp is checked first: created_in_hour must be under ramp_datasets_per_hour; then the two requests a creation costs
    must fit under request_cap_per_hour.
    """
    if created_in_hour >= ramp:
        return CAUSE_RAMP_RATE
    if requests_in_hour + REQUESTS_PER_CREATION > cap:
        return CAUSE_REQUEST_RATE
    return None


class MemberAction(Enum):
    """What a pass does about a selected member's dataset."""

    NONE = 'none'
    CREATE = 'create'
    REFUSE_VENDOR = 'refuse_vendor'


def is_member_state(state: MemberTransition | None) -> bool:
    """Whether a member's latest transition leaves it in the panel (a drain in progress included)."""
    return state is not None and state.transition not in _NOT_MEMBER


def member_action(
    state: MemberTransition | None, entry_exists: bool, created_request: IngestRequestRecord | None
) -> MemberAction:
    """What to do about a SELECTED member whose latest transition is `state`.

    A panel-owned entry for the symbol means the dataset exists: nothing to create. A member whose latest CREATED names
    a request still open waits for it. One whose request FINISHED with no dataset was not created: a PERMANENT failure
    is a vendor refusal (not retried while the member stays in), any other finish is created again. REFUSED stays
    refused while the member stays in.
    """
    if entry_exists:
        return MemberAction.NONE
    if state is None or state.transition in (*_NOT_MEMBER, PanelTransition.DRAINING, PanelTransition.ENTERED):
        return MemberAction.CREATE
    if state.transition is PanelTransition.DEFERRED:
        return MemberAction.CREATE
    if state.transition is PanelTransition.REFUSED:
        return MemberAction.NONE
    if state.transition is PanelTransition.CREATED:
        if created_request is None or created_request.state.is_open or created_request.dataset_id is not None:
            return MemberAction.NONE
        if created_request.failure_cause == IngestRequestFailureCause.PERMANENT.value:
            return MemberAction.REFUSE_VENDOR
        return MemberAction.CREATE
    return MemberAction.NONE


# ---------------------------------------------------------------------------------------------------------------------
# The passes.
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class _PassRun:
    """One pass in progress on the pass's own session: its row opens lazily (a release with nothing to do writes none)."""

    db: AsyncSession
    panel_id: uuid.UUID
    kind: PanelPassKind
    settings_version_id: uuid.UUID
    source_settings_version_id: uuid.UUID
    active_snapshot_id: uuid.UUID
    inactive_snapshot_id: uuid.UUID | None
    dry_run: bool
    pass_id: uuid.UUID | None = None
    counts: Counter = field(default_factory=Counter)
    # Set once a window is full: the windows only fill within a pass, so the rest of the members share the cause.
    blocked: str | None = None

    async def ensure_open(self) -> uuid.UUID:
        if self.pass_id is None:
            self.pass_id = await open_pass(
                self.db,
                panel_id=self.panel_id,
                kind=self.kind,
                settings_version_id=self.settings_version_id,
                source_settings_version_id=self.source_settings_version_id,
                active_snapshot_id=self.active_snapshot_id,
                inactive_snapshot_id=self.inactive_snapshot_id,
                dry_run=self.dry_run,
            )
        return self.pass_id

    async def append(self, rows: Sequence[NewTransition]) -> None:
        if not rows:
            return
        await append_transitions(self.db, await self.ensure_open(), rows)
        self.count(rows)

    def count(self, rows: Sequence[NewTransition]) -> None:
        for row in rows:
            self.counts[row.transition.value.lower()] += 1

    async def finish(self) -> None:
        if self.pass_id is not None:
            await finish_pass(self.db, self.pass_id, self.counts)
        await self.db.commit()


@dataclass(frozen=True)
class _Member:
    """A member to create a dataset for."""

    vendor_asset_id: str
    symbol: str
    new: bool
    previous: MemberTransition | None


class PanelManager:
    """Runs the panel passes over the Submit client the lifespan built.

    Args:
        client: The Submit seam, as the interface.
        calendars: The trading calendar of each source that is managed.
        clock: The current aware instant; injected so a test does not wait.
        settle: The settle margin after a close; None reads DAILY_REFRESH_SETTLE_MINUTES.
        dry_run: Whether passes are dry runs (flagged ledger rows, nothing applied).
    """

    def __init__(
        self,
        client: IngestSubmitClient,
        calendars: Mapping[DataSource, TradingCalendar],
        *,
        clock=_utc_now,
        settle: timedelta | None = None,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._calendars = dict(calendars)
        self._clock = clock
        self._settle = settle_delay() if settle is None else settle
        self._dry_run = dry_run
        # Panels already logged as not runnable (no source settings, no snapshot): once per panel.
        self._logged: set[tuple[uuid.UUID, str]] = set()

    async def tick(self) -> None:
        """One tick: for every enabled panel of every managed source, run what is due. A panel's failure ends its turn only."""
        for source, calendar in self._calendars.items():
            async with own_session() as db:
                panels = await enabled_panels_on_source(db, source)
            for enabled in panels:
                try:
                    await self._tick_panel(source, calendar, enabled, panels)
                except Exception as error:
                    log.warning(f'Panel pass for panel {enabled.panel.id} failed: {type(error).__name__}')

    def _log_once(self, panel_id: uuid.UUID, what: str, message: str) -> None:
        if (panel_id, what) not in self._logged:
            self._logged.add((panel_id, what))
            log.info(message)

    async def _tick_panel(
        self, source: DataSource, calendar: TradingCalendar, enabled: EnabledPanel, on_source: Sequence[EnabledPanel]
    ) -> None:
        panel, current = enabled.panel, enabled.current
        now = self._clock()
        async with own_session() as db:
            source_settings = await current_source_settings(db, source)
            if source_settings is None:
                self._log_once(
                    panel.id, 'source', f'Panel {panel.id} is not runnable: source {source.value} has no settings'
                )
                return
            # Only FINISHED passes satisfy the due rule; a crashed pass holds the panel off for UNFINISHED_PASS_RETRY.
            last = await latest_pass(db, panel.id, PanelPassKind.MEMBERSHIP, dry_run=self._dry_run, finished=True)
            held_off = await self._unfinished_holds_off(db, panel.id, last, now)
            due = membership_due_at(
                current.settings.cadence, last.started_at if last else None, panel.created_at, calendar, self._settle
            )
            if now >= due and not held_off:
                newest = await newest_snapshot(db, source, AssetListStatus.ACTIVE)
                if newest is None:
                    self._log_once(
                        panel.id, 'snapshot', f'Panel {panel.id} waits: no active snapshot for {source.value}'
                    )
                    return
                may_run, older = snapshot_gate(now, due, newest.as_of, calendar, self._settle, first=last is None)
                if may_run:
                    if older and last is not None:
                        log.info(f'Panel {panel.id} pass runs on a snapshot older than its due time (none arrived)')
                    stale_first = now - _aware(newest.as_of) if older and last is None else None
                    await self._membership_pass(
                        db, source, enabled, on_source, source_settings, newest, last, stale_first
                    )
                    return
            if not self._dry_run and last is not None:
                await self._release_tick(db, source, enabled, source_settings, last)

    async def _unfinished_holds_off(
        self, db: AsyncSession, panel_id: uuid.UUID, last: PassRecord | None, now: datetime
    ) -> bool:
        """Whether a crashed membership pass, newer than the last finished one, is younger than UNFINISHED_PASS_RETRY.

        The unfinished row stays as written (append-only). One WARNING per unfinished pass seen: its id only.
        """
        unfinished = await latest_pass(db, panel_id, PanelPassKind.MEMBERSHIP, dry_run=self._dry_run, finished=False)
        if unfinished is None or (last is not None and _aware(unfinished.started_at) <= _aware(last.started_at)):
            return False
        if (panel_id, f'unfinished:{unfinished.id}') not in self._logged:
            self._logged.add((panel_id, f'unfinished:{unfinished.id}'))
            log.warning(f'Panel {panel_id} membership pass {unfinished.id} never finished')
        return now - _aware(unfinished.started_at) < UNFINISHED_PASS_RETRY

    # ------------------------------------------------------------------------------------------------ membership pass

    async def _membership_pass(
        self,
        db: AsyncSession,
        source: DataSource,
        enabled: EnabledPanel,
        on_source: Sequence[EnabledPanel],
        source_settings: SourceSettingsRecord,
        newest: SnapshotRecord,
        previous: PassRecord | None,
        stale_first: timedelta | None = None,
    ) -> None:
        panel, current = enabled.panel, enabled.current
        settings = current.settings
        rows = await snapshot_rows(db, newest.id)
        present = {row.vendor_asset_id for row in rows}
        selected = select_members(rows, settings, newest.asset_type, AssetListStatus.ACTIVE)
        selected_ids = {row.vendor_asset_id for row in selected}
        held = {
            row.symbol
            for other in on_source
            for row in select_members(rows, other.current.settings, newest.asset_type, AssetListStatus.ACTIVE)
        }
        states = await latest_state(db, panel.id)
        inactive = await newest_snapshot(db, source, AssetListStatus.INACTIVE)
        run = _PassRun(
            db,
            panel.id,
            PanelPassKind.MEMBERSHIP,
            current.id,
            source_settings.id,
            newest.id,
            inactive.id if inactive else None,
            self._dry_run,
        )
        await run.ensure_open()
        if stale_first is not None:
            log.info(
                f'Panel {panel.id} first pass {run.pass_id} runs on a snapshot {stale_first} old (before the latest close)'
            )

        exiting = [s for s in states.values() if is_member_state(s) and s.vendor_asset_id not in selected_ids]
        symbols = {s.symbol for s in states.values()} | {row.symbol for row in selected}
        entries = await panel_entries(db, source, PANEL_OWNER, symbols)
        evidence = await self._evidence(db, source, current.id, previous, newest, exiting, selected)

        def as_of_of(vendor_asset_id: str, selected_now: bool) -> datetime:
            if evidence is None:
                return newest.as_of
            snapshots, lookup, baseline = evidence
            return evidence_as_of(
                vendor_asset_id,
                selected_now,
                snapshots,
                lookup,
                settings,
                newest.asset_type,
                AssetListStatus.ACTIVE,
                newest.as_of,
                baseline=baseline,
            )

        stops: list[uuid.UUID] = []
        plain: list[NewTransition] = []
        for state in exiting:
            entry = entries.get(state.symbol)
            if state.vendor_asset_id in present:
                # A filter exit: the row is still listed but no longer selected.
                plain.append(
                    NewTransition(
                        state.vendor_asset_id,
                        state.symbol,
                        PanelTransition.EXITED,
                        as_of_of(state.vendor_asset_id, False),
                    )
                )
                if entry is not None and entry.collect is True and state.symbol not in held:
                    stops.append(entry.id)
            elif state.transition is PanelTransition.DRAINING:
                if entry is None or await fetch_finished_since(db, entry.id, state.as_of):
                    plain.append(
                        NewTransition(state.vendor_asset_id, state.symbol, PanelTransition.DRAINED, newest.as_of)
                    )
                    if entry is not None and entry.collect is True:
                        stops.append(entry.id)
            elif entry is None:
                plain.append(
                    NewTransition(
                        state.vendor_asset_id,
                        state.symbol,
                        PanelTransition.EXITED,
                        as_of_of(state.vendor_asset_id, False),
                    )
                )
            else:
                plain.append(
                    NewTransition(
                        state.vendor_asset_id,
                        state.symbol,
                        PanelTransition.DRAINING,
                        as_of_of(state.vendor_asset_id, False),
                    )
                )
        if self._dry_run:
            stops = []
        else:
            stops.extend(await self._late_mint_stops(db, panel.id, states, held, entries))
            for dataset_id in stops:
                await stop_collecting(db, dataset_id)

        request_rows = await requests_by_ids(
            db, [s.request_id for s in states.values() if s.transition is PanelTransition.CREATED and s.request_id]
        )
        in_flight = await symbols_with_open_requests(db, source, PANEL_OWNER)
        to_create: list[_Member] = []
        entered_only: list[NewTransition] = []
        resumed: list[uuid.UUID] = []
        for row in selected:
            state = states.get(row.vendor_asset_id)
            new = not is_member_state(state) or state.transition is PanelTransition.DRAINING
            request = request_rows.get(state.request_id) if state is not None and state.request_id else None
            action = member_action(state, row.symbol in entries, request)
            if new and action is MemberAction.NONE:
                resume_id = await self._resume_target(db, entries.get(row.symbol), row.vendor_asset_id)
                if resume_id is not None and not self._dry_run and not await resume_panel_entry(db, resume_id):
                    # A concurrent operator write won: no restart, a plain ENTERED.
                    resume_id = None
                if resume_id is not None and not self._dry_run:
                    resumed.append(resume_id)
                entered_only.append(
                    NewTransition(
                        row.vendor_asset_id,
                        row.symbol,
                        PanelTransition.ENTERED,
                        as_of_of(row.vendor_asset_id, True),
                        dataset_id=resume_id,
                        cause=CAUSE_DATASET_RESUMED if resume_id is not None else None,
                    )
                )
            elif action is MemberAction.REFUSE_VENDOR:
                plain.append(
                    NewTransition(
                        row.vendor_asset_id,
                        row.symbol,
                        PanelTransition.REFUSED,
                        newest.as_of,
                        cause=CAUSE_VENDOR_REFUSED,
                    )
                )
            elif action is MemberAction.CREATE:
                if self._dry_run or row.symbol in in_flight:
                    if new:
                        entered_only.append(
                            NewTransition(
                                row.vendor_asset_id,
                                row.symbol,
                                PanelTransition.ENTERED,
                                as_of_of(row.vendor_asset_id, True),
                            )
                        )
                else:
                    to_create.append(_Member(row.vendor_asset_id, row.symbol, new, state))
        await run.append([*plain, *entered_only])

        for member in to_create:
            await self._create(
                run,
                source,
                newest,
                source_settings,
                member,
                newest.as_of,
                entered_as_of=as_of_of(member.vendor_asset_id, True),
            )
        await run.finish()
        for dataset_id in stops:
            publish(DatasetChanged(dataset_id, DatasetChange.RETIRED))
        for dataset_id in resumed:
            publish(DatasetChanged(dataset_id, DatasetChange.RESUMED))

    @staticmethod
    async def _resume_target(db: AsyncSession, entry: PanelEntry | None, vendor_asset_id: str) -> uuid.UUID | None:
        """The entry id a re-entering member's ENTERED restarts, or None.

        Only an entry the manager itself stopped (collect false, PANEL_MEMBER_LEFT) whose last accepted vendor asset
        id is empty or the member's: a different id is a reused symbol, and the dataset stays stopped.
        """
        if entry is None or entry.collect is not False or entry.stopped_cause != DatasetStopCause.PANEL_MEMBER_LEFT:
            return None
        last = await last_accepted_vendor_asset_id(db, entry.id)
        return entry.id if last is None or last == vendor_asset_id else None

    async def _evidence(
        self,
        db: AsyncSession,
        source: DataSource,
        settings_version_id: uuid.UUID,
        previous: PassRecord | None,
        newest: SnapshotRecord,
        exiting: Sequence[MemberTransition],
        selected: Sequence[SnapshotRow],
    ) -> tuple[list[SnapshotRecord], dict[tuple[uuid.UUID, str], SnapshotRow], SnapshotRecord] | None:
        """The snapshots since the previous pass, the rows of the changed members (the baseline's included) and the baseline.

        The baseline is the previous pass's own active snapshot. Returns None when no backdating applies.

        Backdate only when this pass runs under the same settings version as the previous non-dry-run membership pass
        (a settings-driven change is dated by this pass's snapshot). Returns None otherwise.
        """
        if previous is None or previous.settings_version_id != settings_version_id:
            return None
        before = await snapshot_by_id(db, previous.active_snapshot_id)
        if before is None:
            return None
        snapshots = await snapshots_between(db, source, AssetListStatus.ACTIVE, before.as_of, newest.as_of)
        # Only members that can change need rows: the exiting ones and the selected ones.
        changed = {s.vendor_asset_id for s in exiting} | {row.vendor_asset_id for row in selected}
        lookup = await rows_of_ids(db, [before.id, *(snapshot.id for snapshot in snapshots)], changed)
        return snapshots, lookup, before

    async def _late_mint_stops(
        self,
        db: AsyncSession,
        panel_id: uuid.UUID,
        states: Mapping[str, MemberTransition],
        held: Collection[str],
        entries: Mapping[str, PanelEntry],
    ) -> list[uuid.UUID]:
        """Entries minted after their member left the panel, still collecting, that no enabled panel selects.

        A member that EXITS or DRAINS while its bars request is open gets its entry minted afterwards with collect true.
        Only an entry whose CREATED request finished at or after the member's latest EXITED or DRAINED is stopped, so an
        operator's later Resume is left alone; no ledger row is written (stop_collecting marks the stop PANEL_MEMBER_LEFT).
        """
        gone = {vid: s for vid, s in states.items() if s.transition in _NOT_MEMBER}
        if not gone:
            return []
        created = await created_requests(db, panel_id, gone)
        request_rows = await requests_by_ids(db, [rid for ids in created.values() for rid in ids])
        symbols = {s.symbol for s in gone.values()}
        known = {symbol: entries[symbol] for symbol in symbols if symbol in entries}
        stops: list[uuid.UUID] = []
        for vid, state in gone.items():
            entry = known.get(state.symbol)
            if entry is None or entry.collect is not True or state.symbol in held:
                continue
            for rid in created.get(vid, []):
                request = request_rows.get(rid)
                if (
                    request is not None
                    and request.dataset_id == entry.id
                    and request.finished_at is not None
                    and request.finished_at >= state.recorded_at
                ):
                    stops.append(entry.id)
                    break
        return stops

    # ----------------------------------------------------------------------------------------------------- release tick

    async def _release_tick(
        self,
        db: AsyncSession,
        source: DataSource,
        enabled: EnabledPanel,
        source_settings: SourceSettingsRecord,
        last: PassRecord,
    ) -> None:
        """Create the members the last membership pass deferred, while the hourly windows have room.

        Runs only while the panel's current settings version is the deciding pass's (a new or disabling version halts
        releases until the next membership pass; a disabled panel is not listed at all). Names the deciding pass's
        settings version and snapshots and the CURRENT source settings version. Decides no membership.
        """
        panel, current = enabled.panel, enabled.current
        if current.id != last.settings_version_id:
            return
        states = await latest_state(db, panel.id)
        deferred = sorted(
            (s for s in states.values() if s.transition is PanelTransition.DEFERRED),
            key=lambda s: (s.symbol, s.vendor_asset_id),
        )
        if not deferred:
            return
        if await self._window_cause(db, source, source_settings) is not None:
            return
        snapshot = await snapshot_by_id(db, last.active_snapshot_id)
        if snapshot is None:
            return
        entries = await panel_entries(db, source, PANEL_OWNER, {s.symbol for s in deferred})
        in_flight = await symbols_with_open_requests(db, source, PANEL_OWNER)
        run = _PassRun(
            db,
            panel.id,
            PanelPassKind.RELEASE,
            last.settings_version_id,
            source_settings.id,
            last.active_snapshot_id,
            last.inactive_snapshot_id,
            False,
        )
        for state in deferred:
            if state.symbol in entries or state.symbol in in_flight:
                continue
            await self._create(
                run,
                source,
                snapshot,
                source_settings,
                _Member(state.vendor_asset_id, state.symbol, False, state),
                snapshot.as_of,
            )
        await run.finish()

    # --------------------------------------------------------------------------------------------------------- creation

    async def _window_cause(
        self, db: AsyncSession, source: DataSource, source_settings: SourceSettingsRecord
    ) -> str | None:
        since = self._clock() - RATE_WINDOW
        return budget_cause(
            await created_in_window(db, source, since),
            await requests_in_window(db, source, PANEL_OWNER, since),
            source_settings.settings.ramp_datasets_per_hour,
            source_settings.settings.request_cap_per_hour,
        )

    async def _create(
        self,
        run: _PassRun,
        source: DataSource,
        snapshot: SnapshotRecord,
        source_settings: SourceSettingsRecord,
        member: _Member,
        as_of: datetime,
        *,
        entered_as_of: datetime | None = None,
    ) -> None:
        """Create one member's dataset request, in the order of the creation ruling. See the module docstring.

        as_of dates CREATED, DEFERRED and REFUSED (the pass's snapshot); entered_as_of dates ENTERED (evidence-backdated),
        and defaults to as_of.
        """
        db = run.db
        entered = (
            [
                NewTransition(
                    member.vendor_asset_id,
                    member.symbol,
                    PanelTransition.ENTERED,
                    as_of if entered_as_of is None else entered_as_of,
                )
            ]
            if member.new
            else []
        )

        async def defer(cause: str) -> None:
            repeat = (
                member.previous is not None
                and member.previous.transition is PanelTransition.DEFERRED
                and member.previous.cause == cause
            )
            await run.append(
                [
                    *entered,
                    *(
                        []
                        if repeat
                        else [
                            NewTransition(
                                member.vendor_asset_id, member.symbol, PanelTransition.DEFERRED, as_of, cause=cause
                            )
                        ]
                    ),
                ]
            )

        # (2) budget
        cause = run.blocked or await self._window_cause(db, source, source_settings)
        if cause is not None:
            run.blocked = cause
            await defer(cause)
            return
        # (3) commit P first: no transaction is held open across a vendor wait, and no pending row crosses the clamp. A
        # MEMBERSHIP pass row is open already; a RELEASE pass row opens lazily, at its first transition.
        await db.commit()
        path = StoreAssetDatasetPath(
            asset_type=snapshot.asset_type, data_type=DataType.MARKET_ACTIVITY, asset_symbol=member.symbol
        )
        declared_start = panel_dataset_start(source)
        body = self._body(source, declared_start)
        # (4) the clamp, on its OWN session, never P.
        try:
            async with own_session() as clamp_db:
                clamp, _ = await first_request_clamp(
                    path, body, clamp_db, self._client, deadline_s=verdict_deadline_seconds()
                )
        except TraderJoeError as error:
            log.info(f'Panel member clamp unavailable: {type(error).__name__} {error.reason}')
            await defer(CAUSE_CLAMP_UNAVAILABLE)
            return
        window_cause = clamp_refusal(clamp, body.end)
        if window_cause is not None:
            # Nothing is pending after step (3): no CREATED and no bars request.
            await run.append(
                [
                    *entered,
                    NewTransition(
                        member.vendor_asset_id, member.symbol, PanelTransition.REFUSED, as_of, cause=window_cause
                    ),
                ]
            )
            return
        start = clamp.clamped_start if clamp is not None else declared_start
        clamped = self._body(source, start)
        # (5) own overlap on P with the clamped start (not _check_overlap: it commits P).
        key = OverlapKey(
            owner=PANEL_OWNER,
            asset_type=path.asset_type,
            asset_symbol=path.asset_symbol,
            data_type=path.data_type,
            source=source,
            granularity=Granularity.ONE_DAY,
            expiry_type=ExpiryType.ROLLING,
            update_type=UpdateType.DAILY,
            start=start,
            end=None,
        )
        try:
            await check_own_overlap(db, key)
        except OwnOverlapConflict:
            await db.rollback()
            await run.append(
                [
                    *entered,
                    NewTransition(
                        member.vendor_asset_id, member.symbol, PanelTransition.REFUSED, as_of, cause=CAUSE_OWN_OVERLAP
                    ),
                ]
            )
            return
        # (6) ONE COMMIT: ENTERED + CREATED + the request row.
        request_id = uuid.uuid4()
        created = NewTransition(
            member.vendor_asset_id, member.symbol, PanelTransition.CREATED, as_of, request_id=request_id
        )
        opened_here = run.pass_id is None
        try:
            await append_transitions(db, await run.ensure_open(), [*entered, created])
            record = await create_request(
                db,
                replace(
                    new_bars_request(
                        path,
                        clamped,
                        trigger=IngestRunTrigger.REQUEST,
                        client_label=PANEL_OWNER,
                        ready_by=None,
                        dataset_id=None,
                        carries_lifecycle=True,
                    ),
                    id=request_id,
                ),
            )
        except Exception as error:
            await db.rollback()
            if opened_here:
                run.pass_id = None
            log.warning(f'Panel creation request not written: {type(error).__name__}')
            return
        run.count([*entered, created])
        # (7) after the commit; the verdict is never awaited.
        try:
            await submit_record(record, self._client)
        except TraderJoeError as error:
            log.info(f'Panel creation request {record.id} not accepted yet: {type(error).__name__} {error.reason}')

    @staticmethod
    def _body(source: DataSource, start: datetime) -> StoreAssetDatasetBody:
        return StoreAssetDatasetBody(
            owner=PANEL_OWNER,
            source=source,
            granularity=Granularity.ONE_DAY,
            start=start,
            end=None,
            expiry=None,
            expiry_type=ExpiryType.ROLLING,
            update_type=UpdateType.DAILY,
            collect=True,
        )


# ---------------------------------------------------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------------------------------------------------


async def start_panel_manager_loop(client: IngestSubmitClient) -> asyncio.Task[None] | None:
    """Start the one tick loop. Call from the running loop, after the Submit client exists.

    The first tick runs at once (the catch-up), then every 5 minutes; what is due is derived from the rows each tick.

    Args:
        client: The Submit seam the requests go through.

    Returns:
        asyncio.Task[None] | None: The task, for stop_panel_manager_loop; None when PANEL_MANAGER_ENABLED is off or no
        source has a trading calendar.
    """
    if not is_enabled():
        log.info(f'The panel manager is off ({ENABLED_ENV}); no panel pass runs')
        return None
    calendars: dict[DataSource, TradingCalendar] = {}
    for source in SOURCE_CALENDARS:
        # Building a calendar is slow (cached after the first): off the event loop.
        with contextlib.suppress(UnknownCalendarError):
            calendars[source] = await asyncio.to_thread(calendar_for_source, source)
    if not calendars:
        return None
    manager = PanelManager(client, calendars, dry_run=is_dry_run())
    wake = asyncio.Event()
    wake.set()
    return run_periodic('panel-manager', Every(TICK_INTERVAL), manager.tick, wake=wake, log=log)


async def stop_panel_manager_loop(task: asyncio.Task[None] | None) -> None:
    """Cancel the loop and await it. Call before the Submit channel and the database go."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
