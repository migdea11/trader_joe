"""The operator's audit trail: the override log and the deletion record (tj-grna9p.86).

BOTH TABLES ARE APPEND-ONLY. The only writes here are INSERTs; there is no UPDATE and no DELETE, and none may
be added.

THE FUNCTIONS TAKE THE CALLER'S SESSION AND NEVER COMMIT. An override row and a deletion row are written in
the transaction of the write they record (store_dataset_entry.operator_set_expiry / operator_delete_entry), so
a rolled-back write leaves no row and a row never exists for a write that did not happen.

AN UNMIGRATED DATABASE REFUSES THE AUDITED WRITES. Unlike the usage recorder (telemetry, best effort), these
rows are the audit of an act: audit_tables_exist is asked before the transaction, warns once naming
`make migrate` when the tables are missing, and a write that would need a row (an overriding PATCH, every
DELETE) is then refused with AuditUnavailable, after the 404 and 409 checks and before any write.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors.vocabulary import ExogenousError, InvalidRequestError, Reason
from common.logging import get_logger
from data.store.app.database.models.dataset_deletion import DatasetDeletion
from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction


log = get_logger(__name__)

_OVERRIDE = DatasetOverrideLog.__table__
_DELETION = DatasetDeletion.__table__

_warned_missing = False


@dataclass(frozen=True)
class OperatorOverrideInput:
    """The override a request carried, as plain values: the reason as sent and whether it was confirmed."""

    reason: str = field(repr=False)
    confirmed: bool


class OverrideRequired(InvalidRequestError):
    """Raised when the operator acts on another owner's dataset without a confirmed override with a reason.

    THE DETAIL AND THE METADATA NEVER CARRY THE OWNER. The UI already shows DatasetSummary.owner, and an error
    body is not a read endpoint (the OWNER_MISMATCH rule).
    """

    def __init__(self) -> None:
        super().__init__(
            Reason.OVERRIDE_REQUIRED,
            'The dataset belongs to another owner: send override with a non-blank reason and confirmed true.',
        )


class AuditUnavailable(ExogenousError):
    """Raised when a write that must be audited meets a database without the audit tables.

    DATABASE_UNAVAILABLE: operator-paging, retry once `make migrate` has run. The detail carries no owner and no
    metadata.
    """

    def __init__(self) -> None:
        super().__init__(
            Reason.DATABASE_UNAVAILABLE,
            'The database has not been migrated: the operator audit tables are missing. Run make migrate.',
        )


def overriding_reason(owner: str, operator: str, override: OperatorOverrideInput | None) -> str | None:
    """Decide whether a write is an override, and refuse one that is not properly made.

    Args:
        owner: The dataset's owner.
        operator: The operator principal.
        override: The override the request carried, if any.

    Returns:
        str | None: The trimmed reason when the dataset is another owner's and the override is valid; None when
        the operator owns it, in which case any override sent is ignored.

    Raises:
        OverrideRequired: The dataset is another owner's and the override is missing, unconfirmed or blank.
    """
    if owner == operator:
        return None
    reason = override.reason.strip() if override is not None and override.confirmed else ''
    if not reason:
        raise OverrideRequired
    return reason


async def audit_tables_exist(db: AsyncSession) -> bool:
    """Whether both audit tables exist, by one catalogue read that does not open a write transaction.

    Warns once, naming `make migrate`, when they do not.

    Args:
        db: The session the write will run on.

    Returns:
        bool: True when both tables resolve on the session's search path.
    """
    global _warned_missing
    result = await db.execute(
        select(
            func.to_regclass(DatasetOverrideLog.__tablename__).is_not(None),
            func.to_regclass(DatasetDeletion.__tablename__).is_not(None),
        )
    )
    first, second = result.one()
    ready = bool(first and second)
    if not ready and not _warned_missing:
        _warned_missing = True
        log.warning(
            f'Audited operator writes are being refused: table {DatasetOverrideLog.__tablename__} or '
            f'{DatasetDeletion.__tablename__} does not exist; the database has not been migrated, run make migrate'
        )
    return ready


async def append_override(
    db: AsyncSession, *, actor: str, dataset_id: uuid.UUID, owner: str, action: OverrideAction, reason: str
) -> None:
    """Append one override-log row, inside the caller's transaction.

    Args:
        db: The session, inside the caller's transaction.
        actor: The operator principal.
        dataset_id: The dataset acted on.
        owner: The dataset's owner.
        action: What was done.
        reason: The reason, already trimmed.
    """
    await db.execute(
        _OVERRIDE.insert().values(actor=actor, dataset_id=dataset_id, owner=owner, action=action, reason=reason)
    )


async def append_deletion(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    owner: str,
    asset_symbol: str,
    source: str,
    granularity: str,
    update_type: str,
    range_start: datetime,
    range_end: datetime | None,
    bars_deleted: int,
    cause: str,
    actor: str,
) -> None:
    """Append one deletion-record row, inside the caller's transaction.

    Args:
        db: The session, inside the caller's transaction.
        dataset_id: The dataset deleted.
        owner: The dataset's owner.
        asset_symbol: The dataset's symbol.
        source: The data source's member name.
        granularity: The granularity's member name.
        update_type: The update type's member name.
        range_start: The declared start.
        range_end: The declared end, None for an open-ended dataset.
        bars_deleted: How many bars went with the entry.
        cause: Why: DatasetDeletion.CAUSE_OPERATOR for an operator delete.
        actor: Who deleted it.
    """
    await db.execute(
        _DELETION.insert().values(
            dataset_id=dataset_id,
            owner=owner,
            asset_symbol=asset_symbol,
            source=source,
            granularity=granularity,
            update_type=update_type,
            range_start=range_start,
            range_end=range_end,
            bars_deleted=bars_deleted,
            cause=cause,
            actor=actor,
        )
    )
