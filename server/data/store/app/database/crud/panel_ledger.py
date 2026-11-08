"""The panel ledger repository: passes, member transitions, member state, the boundary read (tj-7zjov1.177, P20c).

EVERY FUNCTION TAKES THE CALLER'S SESSION AND NEVER COMMITS. The panel manager writes a pass, its transitions and the
request rows they name inside its own transactions, so a rolled-back write leaves no ledger row and a ledger row never
exists for a write that did not happen.

APPEND-ONLY. The writes are INSERTs, plus the one ruled exception: finish_pass sets finished_at and counts on a pass's
own row once, from NULL, guarded by finished_at IS NULL. There is no other update and no delete here, and none may be
added.

READS IGNORE DRY-RUN PASSES (Q10 default): a dry-run pass writes flagged rows that every read here leaves out.

WHEN A TRANSITION'S as_of IS A SNAPSHOT INSTANT, NOT THE PASS CLOCK (the as_of rule, note 14:02 UTC on tj-7zjov1.34).
A transition driven by snapshot evidence is backdated to the first snapshot showing the change, so its TRUE time lies in
(the previous snapshot's as_of, as_of]. Every read that picks "the latest" therefore orders by as_of, then the pass's
started_at, then the pass, then the append order (seq), never by pass alone.

A CREATED ROW NAMES ITS REQUEST ONLY (tj-7zjov1.216, P20d). It names the bars request it wrote and no dataset: the dataset
is ingest_request.dataset_id of that request, NULL until the delivery intake's first delivered attempt sets it, since the
intake mints the dataset entry with the resolved feed. Other kinds (EXITED, DRAINING, DRAINED) still name the dataset
when they have one.

KIND RULE. A RELEASE pass only creates members the last MEMBERSHIP pass deferred and decides no membership. The member
state reads (latest_state, members_at) take transitions of EITHER kind, since a release writes CREATED rows that
supersede a DEFERRED one. The effective-from of a settings version is the first non-dry-run pass of EITHER kind naming
it. P22's due rule, which reads the latest non-dry-run MEMBERSHIP pass, is not in this module.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.enums.data_stock import DataSource
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction
from data.store.app.database.models.ingest_request import IngestRequest
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


_PANEL = Panel.__table__
_SETTINGS = PanelSettingsVersion.__table__
_SOURCE_SETTINGS = PanelSourceSettingsVersion.__table__
_PASS = PanelPass.__table__
_TRANSITION = PanelMemberTransition.__table__
_OVERRIDE = DatasetOverrideLog.__table__
_REQUEST = IngestRequest.__table__

# asyncpg allows 32,767 bind parameters per statement; a transition binds 10 (pass_id, panel_id, vendor_asset_id, symbol,
# transition, dataset_id, request_id, cause, as_of, seq), so 2,000 rows x 10 = 20,000 < 32,767 stays well inside.
ROW_CHUNK: Final = 2000

# The keys finish_pass writes into panel_pass.counts: one per transition kind, the number of rows of that kind the pass
# appended. The column is free JSONB; the ledger rows are the truth and counts a summary. Every key is always present
# (0 when the pass wrote none), so a reader never tells "none" from "not counted".
COUNT_KEYS: Final = tuple(transition.value.lower() for transition in PanelTransition)

# The transitions that decide membership: a member is in from ENTERED until EXITED or DRAINED. CREATED, DEFERRED,
# REFUSED and DRAINING are process facts and do not end it.
MEMBERSHIP_TRANSITIONS: Final = (PanelTransition.ENTERED, PanelTransition.EXITED, PanelTransition.DRAINED)


class PanelPassMismatch(ValueError):
    """Raised when a pass names a settings version of another panel or a source settings version of another source.

    The foreign keys reach only the version tables' ids, not their owner, so nothing in the schema stops this and the
    boundary read would attribute a period to the wrong panel. The detail names ids only, never a value.
    """


@dataclass(frozen=True)
class NewTransition:
    """One member change, as it is written.

    as_of is the snapshot instant the pass decided on (see the module docstring), timezone-aware. CREATED must name the
    bars request it wrote and must NOT name a dataset: the dataset is ingest_request.dataset_id of that request, NULL
    until the delivery intake's first delivered attempt sets it.
    """

    vendor_asset_id: str
    symbol: str
    transition: PanelTransition
    as_of: datetime
    dataset_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    cause: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.as_of, 'as_of')
        if self.transition is PanelTransition.CREATED:
            if self.request_id is None:
                raise ValueError('a CREATED transition must name its request_id')
            if self.dataset_id is not None:
                raise ValueError('a CREATED transition must not name a dataset_id; its request names the dataset')


@dataclass(frozen=True)
class MemberTransition:
    """One panel_member_transition row, detached, with the started_at and kind of the pass that wrote it."""

    id: uuid.UUID
    pass_id: uuid.UUID
    pass_kind: PanelPassKind
    pass_started_at: datetime
    vendor_asset_id: str
    symbol: str
    transition: PanelTransition
    dataset_id: uuid.UUID | None
    request_id: uuid.UUID | None
    cause: str | None
    as_of: datetime
    recorded_at: datetime
    seq: int


@dataclass(frozen=True)
class MemberAt:
    """A member on a date: the vendor asset, its symbol then, and the as_of of the ENTERED that made it a member."""

    vendor_asset_id: str
    symbol: str
    member_since: datetime


@dataclass(frozen=True)
class SettingsBoundary:
    """One panel settings version and when it took effect.

    effective_from is the started_at of the first non-dry-run pass (either kind) of the panel naming the version, None
    while none has. actor, reason and saved_at are the version's own marker: who changed the settings, why (None when no reason was given), when.
    """

    version_id: uuid.UUID
    version_no: int
    enabled: bool
    cadence: PanelCadence
    saved_at: datetime
    actor: str
    reason: str | None
    effective_pass_id: uuid.UUID | None
    effective_from: datetime | None


@dataclass(frozen=True)
class SourceSettingsBoundary:
    """One settings version of the panel's SOURCE and when it took effect for this panel.

    effective_from is the started_at of the first non-dry-run pass of THIS panel naming the version.
    """

    version_id: uuid.UUID
    version_no: int
    ramp_datasets_per_hour: int | None
    request_cap_per_hour: int | None
    corporate_action_window_pre_days: int
    corporate_action_window_post_days: int
    saved_at: datetime
    actor: str
    reason: str | None
    effective_pass_id: uuid.UUID | None
    effective_from: datetime | None


@dataclass(frozen=True)
class CollectStop:
    """An operator SET_COLLECTING override on a dataset the panel's ledger names. The owner is never read."""

    dataset_id: uuid.UUID
    new_value: str | None
    actor: str
    reason: str
    at: datetime


@dataclass(frozen=True)
class BoundaryRead:
    """What the UI shows of where a panel's rules changed: settings versions, source versions and operator stops."""

    settings_versions: tuple[SettingsBoundary, ...]
    source_settings_versions: tuple[SourceSettingsBoundary, ...]
    collect_stops: tuple[CollectStop, ...]


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'{name} must be timezone-aware')


async def open_pass(
    db: AsyncSession,
    *,
    panel_id: uuid.UUID,
    kind: PanelPassKind,
    settings_version_id: uuid.UUID,
    source_settings_version_id: uuid.UUID,
    active_snapshot_id: uuid.UUID,
    inactive_snapshot_id: uuid.UUID | None,
    dry_run: bool,
) -> uuid.UUID:
    """Append a pass row, inside the caller's transaction, after checking whose versions it names.

    Args:
        db: The session, inside the caller's transaction.
        panel_id: The panel the pass runs over.
        kind: MEMBERSHIP or RELEASE.
        settings_version_id: The panel settings version the pass runs under; it must belong to the panel.
        source_settings_version_id: The source settings version; it must belong to the panel's source.
        active_snapshot_id: The active asset-list snapshot read.
        inactive_snapshot_id: The inactive snapshot read, if any.
        dry_run: Whether the pass is a dry run (flagged rows every read ignores).

    Returns:
        uuid.UUID: The pass id; started_at is database time.

    Raises:
        PanelPassMismatch: The panel is unknown, the settings version is another panel's, or the source settings version
            is another source's.
    """
    source = (await db.execute(select(_PANEL.c.source).where(_PANEL.c.id == panel_id))).scalar_one_or_none()
    if source is None:
        raise PanelPassMismatch(f'panel {panel_id} does not exist')
    owner = (
        await db.execute(select(_SETTINGS.c.panel_id).where(_SETTINGS.c.id == settings_version_id))
    ).scalar_one_or_none()
    if owner != panel_id:
        raise PanelPassMismatch(f'settings version {settings_version_id} does not belong to panel {panel_id}')
    version_source = (
        await db.execute(select(_SOURCE_SETTINGS.c.source).where(_SOURCE_SETTINGS.c.id == source_settings_version_id))
    ).scalar_one_or_none()
    if version_source != source:
        raise PanelPassMismatch(
            f'source settings version {source_settings_version_id} does not belong to the source of panel {panel_id}'
        )
    return (
        await db.execute(
            _PASS.insert()
            .values(
                panel_id=panel_id,
                kind=kind,
                settings_version_id=settings_version_id,
                source_settings_version_id=source_settings_version_id,
                active_snapshot_id=active_snapshot_id,
                inactive_snapshot_id=inactive_snapshot_id,
                dry_run=dry_run,
            )
            .returning(_PASS.c.id)
        )
    ).scalar_one()


async def finish_pass(db: AsyncSession, pass_id: uuid.UUID, counts: Mapping[str, int]) -> bool:
    """Set finished_at (database time) and counts on a pass, once. The one UPDATE in this module.

    Guarded by finished_at IS NULL, so a second call changes nothing.

    Args:
        db: The session, inside the caller's transaction.
        pass_id: The pass.
        counts: Rows appended per transition kind, keyed by COUNT_KEYS; a missing key is written as 0.

    Returns:
        bool: True when this call finished the pass; False when it was already finished.

    Raises:
        PanelPassMismatch: The pass does not exist.
        ValueError: A key outside COUNT_KEYS, or a count that is negative or not an integer.
    """
    unknown = set(counts) - set(COUNT_KEYS)
    if unknown:
        raise ValueError(f'unknown count keys {sorted(unknown)}; allowed {list(COUNT_KEYS)}')
    for key, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'count {key} must be a non-negative integer')
    exists = (await db.execute(select(_PASS.c.id).where(_PASS.c.id == pass_id))).scalar_one_or_none()
    if exists is None:
        raise PanelPassMismatch(f'pass {pass_id} does not exist')
    result = await db.execute(
        _PASS.update()
        .where(_PASS.c.id == pass_id, _PASS.c.finished_at.is_(None))
        .values(finished_at=func.now(), counts={key: int(counts.get(key, 0)) for key in COUNT_KEYS})
    )
    return result.rowcount == 1


async def append_transitions(db: AsyncSession, pass_id: uuid.UUID, rows: Sequence[NewTransition]) -> int:
    """Append member transitions under a pass, inside the caller's transaction.

    The panel is the pass's own, read from the pass, so a row can never name another panel's. recorded_at is database
    time. A repeat of a deferral with the same cause is the caller's to skip (no new row). Each row gets seq, its append
    ordinal within the pass: the pass's current maximum plus one plus the row's index in the list, so the list order is
    the append order. One manager writes a pass; a concurrent second writer hits the unique (pass_id, seq).

    Args:
        db: The session, inside the caller's transaction.
        pass_id: The pass the rows belong to.
        rows: The transitions.

    Returns:
        int: How many rows were appended.

    Raises:
        PanelPassMismatch: The pass does not exist or is finished.
    """
    if not rows:
        return 0
    found = (await db.execute(select(_PASS.c.panel_id, _PASS.c.finished_at).where(_PASS.c.id == pass_id))).one_or_none()
    if found is None:
        raise PanelPassMismatch(f'pass {pass_id} does not exist')
    panel_id, finished_at = found
    if finished_at is not None:
        raise PanelPassMismatch(f'pass {pass_id} is finished')
    base = (
        await db.execute(select(func.coalesce(func.max(_TRANSITION.c.seq), 0)).where(_TRANSITION.c.pass_id == pass_id))
    ).scalar_one()
    for start in range(0, len(rows), ROW_CHUNK):
        await db.execute(
            _TRANSITION.insert(),
            [
                {
                    'pass_id': pass_id,
                    'panel_id': panel_id,
                    'vendor_asset_id': row.vendor_asset_id,
                    'symbol': row.symbol,
                    'transition': row.transition,
                    'dataset_id': row.dataset_id,
                    'request_id': row.request_id,
                    'cause': row.cause,
                    'as_of': row.as_of,
                    'seq': base + 1 + index,
                }
                for index, row in enumerate(rows[start : start + ROW_CHUNK], start)
            ],
        )
    return len(rows)


def _ranked(panel_id: uuid.UUID, transitions: Sequence[PanelTransition] | None, until: datetime | None) -> Select:
    """The panel's non-dry-run transitions, ranked newest first per vendor asset by as_of, started_at, pass_id, seq."""
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
    stmt = (
        select(
            _TRANSITION.c.id,
            _TRANSITION.c.pass_id,
            _PASS.c.kind.label('pass_kind'),
            _PASS.c.started_at.label('pass_started_at'),
            _TRANSITION.c.vendor_asset_id,
            _TRANSITION.c.symbol,
            _TRANSITION.c.transition,
            _TRANSITION.c.dataset_id,
            _TRANSITION.c.request_id,
            _TRANSITION.c.cause,
            _TRANSITION.c.as_of,
            _TRANSITION.c.recorded_at,
            _TRANSITION.c.seq,
            rank,
        )
        .join(_PASS, _PASS.c.id == _TRANSITION.c.pass_id)
        .where(_TRANSITION.c.panel_id == panel_id, _PASS.c.dry_run.is_(False))
    )
    if transitions is not None:
        stmt = stmt.where(_TRANSITION.c.transition.in_(transitions))
    if until is not None:
        stmt = stmt.where(_TRANSITION.c.as_of <= until)
    return stmt


async def latest_state(db: AsyncSession, panel_id: uuid.UUID) -> dict[str, MemberTransition]:
    """Per vendor asset id, the newest transition of the panel: the member's current state.

    Newest is by as_of, then the pass's started_at, then the pass, then the append order (seq). Transitions of both
    pass kinds count, so a RELEASE's CREATED supersedes the DEFERRED it released. A transition's true time lies in
    (the previous snapshot's as_of, as_of]. Dry-run passes are ignored.

    Args:
        db: A session.
        panel_id: The panel.

    Returns:
        dict[str, MemberTransition]: Keyed by vendor asset id.
    """
    ranked = _ranked(panel_id, None, None).subquery()
    rows = (await db.execute(select(ranked).where(ranked.c.rank == 1))).all()
    return {row.vendor_asset_id: _member_transition(row) for row in rows}


async def members_at(db: AsyncSession, panel_id: uuid.UUID, instant: datetime) -> list[MemberAt]:
    """Who was a member of the panel at an instant, read by as_of from the transitions alone.

    A vendor asset is a member when its newest MEMBERSHIP-deciding transition (ENTERED, EXITED, DRAINED) with as_of at or
    before the instant is ENTERED. CREATED, DEFERRED, REFUSED and DRAINING are process facts and do not end membership,
    so a member being drained is still a member until DRAINED. as_of is a snapshot instant: a transition's true time
    lies in (the previous snapshot's as_of, as_of], so membership is exact to the snapshot cadence, not to the second.
    For a date D, pass the end of D. Dry-run passes are ignored.

    Args:
        db: A session.
        panel_id: The panel.
        instant: The instant, timezone-aware.

    Returns:
        list[MemberAt]: The members, ordered by vendor asset id.

    Raises:
        ValueError: The instant is naive.
    """
    _require_aware(instant, 'instant')
    ranked = _ranked(panel_id, MEMBERSHIP_TRANSITIONS, instant).subquery()
    rows = (
        await db.execute(
            select(ranked.c.vendor_asset_id, ranked.c.symbol, ranked.c.as_of)
            .where(ranked.c.rank == 1, ranked.c.transition == PanelTransition.ENTERED)
            .order_by(ranked.c.vendor_asset_id)
        )
    ).all()
    return [MemberAt(vendor_asset_id=r.vendor_asset_id, symbol=r.symbol, member_since=r.as_of) for r in rows]


async def created_in_window(db: AsyncSession, source: DataSource, since: datetime) -> int:
    """How many CREATED transitions the panels of a source wrote at or after an instant, by recorded_at (database time).

    The rate window's count: the caller passes now minus 60 minutes. Dry-run passes are ignored, since they create
    nothing. Filtered on the transition first, which the (transition, recorded_at) index serves.

    Args:
        db: A session.
        source: The data source.
        since: The window start, timezone-aware, inclusive.

    Returns:
        int: The count.

    Raises:
        ValueError: since is naive.
    """
    _require_aware(since, 'since')
    return (
        await db.execute(
            select(func.count())
            .select_from(_TRANSITION)
            .join(_PANEL, _PANEL.c.id == _TRANSITION.c.panel_id)
            .join(_PASS, _PASS.c.id == _TRANSITION.c.pass_id)
            .where(
                _TRANSITION.c.transition == PanelTransition.CREATED,
                _TRANSITION.c.recorded_at >= since,
                _PANEL.c.source == source,
                _PASS.c.dry_run.is_(False),
            )
        )
    ).scalar_one()


async def _first_pass_per_version(
    db: AsyncSession, panel_id: uuid.UUID, version_column
) -> dict[uuid.UUID, tuple[uuid.UUID, datetime]]:
    """Per version id, the (pass id, started_at) of the panel's first non-dry-run pass naming it, either kind."""
    rank = func.row_number().over(partition_by=version_column, order_by=(_PASS.c.started_at, _PASS.c.id)).label('rank')
    ranked = (
        select(version_column.label('version_id'), _PASS.c.id, _PASS.c.started_at, rank)
        .where(_PASS.c.panel_id == panel_id, _PASS.c.dry_run.is_(False))
        .subquery()
    )
    rows = (await db.execute(select(ranked).where(ranked.c.rank == 1))).all()
    return {row.version_id: (row.id, row.started_at) for row in rows}


async def boundary_read(db: AsyncSession, panel_id: uuid.UUID) -> BoundaryRead:
    """Where a panel's rules changed, for the UI: its settings versions, its source's, and the operator's stops.

    Each version is returned with its effective pass: the first non-dry-run pass of this panel, of either kind, naming it
    (None while none has), the pass's started_at as the effective date, and the version's actor and reason. The source's
    versions are read the same way, by this panel's passes (Q1 default). The stops are the SET_COLLECTING override-log
    rows, with new_value, on every dataset the panel's non-dry-run ledger names; the owner column is never read.

    Args:
        db: A session.
        panel_id: The panel.

    Returns:
        BoundaryRead: Versions ordered by version number, stops by time then id. Empty for an unknown panel.
    """
    source = (await db.execute(select(_PANEL.c.source).where(_PANEL.c.id == panel_id))).scalar_one_or_none()
    if source is None:
        return BoundaryRead((), (), ())
    first_settings = await _first_pass_per_version(db, panel_id, _PASS.c.settings_version_id)
    first_source = await _first_pass_per_version(db, panel_id, _PASS.c.source_settings_version_id)
    settings = (
        await db.execute(select(_SETTINGS).where(_SETTINGS.c.panel_id == panel_id).order_by(_SETTINGS.c.version_no))
    ).all()
    source_settings = (
        await db.execute(
            select(_SOURCE_SETTINGS).where(_SOURCE_SETTINGS.c.source == source).order_by(_SOURCE_SETTINGS.c.version_no)
        )
    ).all()
    named_directly = (
        select(_TRANSITION.c.dataset_id.label('dataset_id'))
        .join(_PASS, _PASS.c.id == _TRANSITION.c.pass_id)
        .where(_TRANSITION.c.panel_id == panel_id, _TRANSITION.c.dataset_id.is_not(None), _PASS.c.dry_run.is_(False))
    )
    # A CREATED row names its request only; the dataset is the request's own, NULL until the first delivered attempt.
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
    named = named_directly.union(named_by_request)
    stops = (
        await db.execute(
            select(_OVERRIDE.c.dataset_id, _OVERRIDE.c.new_value, _OVERRIDE.c.actor, _OVERRIDE.c.reason, _OVERRIDE.c.at)
            .where(and_(_OVERRIDE.c.action == OverrideAction.SET_COLLECTING, _OVERRIDE.c.dataset_id.in_(named)))
            .order_by(_OVERRIDE.c.at, _OVERRIDE.c.id)
        )
    ).all()
    return BoundaryRead(
        settings_versions=tuple(
            SettingsBoundary(
                version_id=row.id,
                version_no=row.version_no,
                enabled=row.enabled,
                cadence=row.cadence,
                saved_at=row.saved_at,
                actor=row.actor,
                reason=row.reason,
                effective_pass_id=first_settings[row.id][0] if row.id in first_settings else None,
                effective_from=first_settings[row.id][1] if row.id in first_settings else None,
            )
            for row in settings
        ),
        source_settings_versions=tuple(
            SourceSettingsBoundary(
                version_id=row.id,
                version_no=row.version_no,
                ramp_datasets_per_hour=row.ramp_datasets_per_hour,
                request_cap_per_hour=row.request_cap_per_hour,
                corporate_action_window_pre_days=row.corporate_action_window_pre_days,
                corporate_action_window_post_days=row.corporate_action_window_post_days,
                saved_at=row.saved_at,
                actor=row.actor,
                reason=row.reason,
                effective_pass_id=first_source[row.id][0] if row.id in first_source else None,
                effective_from=first_source[row.id][1] if row.id in first_source else None,
            )
            for row in source_settings
        ),
        collect_stops=tuple(
            CollectStop(dataset_id=r.dataset_id, new_value=r.new_value, actor=r.actor, reason=r.reason, at=r.at)
            for r in stops
        ),
    )


def _member_transition(row) -> MemberTransition:
    return MemberTransition(
        id=row.id,
        pass_id=row.pass_id,
        pass_kind=row.pass_kind,
        pass_started_at=row.pass_started_at,
        vendor_asset_id=row.vendor_asset_id,
        symbol=row.symbol,
        transition=row.transition,
        dataset_id=row.dataset_id,
        request_id=row.request_id,
        cause=row.cause,
        as_of=row.as_of,
        recorded_at=row.recorded_at,
        seq=row.seq,
    )
