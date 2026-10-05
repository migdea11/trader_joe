from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Final

from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors.vocabulary import ExogenousError, Reason, new_error_id
from common.logging import get_logger


log = get_logger(__name__)

# The two SQLSTATEs that mean "this write lost a race and can be retried as it stands": 40001
# serialization_failure and 40P01 deadlock_detected.
_CONFLICT_SQLSTATES: Final = frozenset({'40001', '40P01'})

# THE DETAIL IS A FIXED SENTENCE PER REASON, AND NEVER str() OF THE SQLAlchemyError (ADR tj-fa1rpu
# D8). A SQLAlchemyError's text carries the statement and its bound parameters, so putting it on the
# wire would publish an owner, a symbol and every other bound value to whoever got the error -- the
# leak D8 exists to prevent. The class name and the SQLSTATE are diagnostic, so they go to the ERROR
# line below, under the same error_id the caller is handed, and nowhere else.
_DETAILS: Final[Mapping[Reason, str]] = MappingProxyType(
    {
        Reason.DATABASE_UNAVAILABLE: 'The database could not be reached, or it dropped the connection mid-write.',
        Reason.DATABASE_CONFLICT: 'The write collided with a concurrent transaction and was rolled back.',
        Reason.DATABASE_INTEGRITY: 'The database refused the write with a constraint violation.',
    }
)


def _sqlstate(error: SQLAlchemyError) -> str | None:
    """The five-character SQLSTATE the driver reported, or None where there is none.

    Read off the wrapped DBAPI exception under both spellings in use: asyncpg names it `sqlstate`
    and psycopg names it `pgcode`. Diagnostic only -- it reaches the log, never the wire (D8).

    Args:
        error: The error raised inside the transaction.

    Returns:
        str | None: The SQLSTATE, or None for an error that carries none.
    """
    orig = getattr(error, 'orig', None)
    code = getattr(orig, 'sqlstate', None) or getattr(orig, 'pgcode', None)
    return code if isinstance(code, str) else None


def _reason_for(error: SQLAlchemyError) -> Reason | None:
    """Classify a SQLAlchemyError as one of the three DATABASE_* reasons, or as a bug.

    ADR tj-fa1rpu D5, as the TE-6 bead spells the table out. None means THE ERROR IS A BUG OF OURS
    -- a ProgrammingError or a DataError, say -- and the caller re-raises it unchanged rather than
    dressing it as an exogenous failure, which would blame the database for our own defect.

    THE SQLSTATE TEST RUNS FIRST, and the order is load-bearing rather than stylistic. Postgres
    reports both 40001 and 40P01 as an OperationalError (asyncpg's SerializationError and
    DeadlockDetectedError alike), so testing OperationalError first would classify every lost race
    as DATABASE_UNAVAILABLE and leave DATABASE_CONFLICT unreachable -- a row the vocabulary
    describes as "a serialization failure or deadlock ... through write_transaction". Asking the
    SQLSTATE first is what makes both rules true at once.

    Args:
        error: The error raised inside the transaction.

    Returns:
        Reason | None: The reason to raise, or None when the error is a bug.
    """
    if isinstance(error, DBAPIError) and _sqlstate(error) in _CONFLICT_SQLSTATES:
        return Reason.DATABASE_CONFLICT
    # A disconnect SQLAlchemy recognised invalidates the connection whatever class it arrived as,
    # so it is asked for by name beside the two classes that always mean "the database is not there".
    if isinstance(error, OperationalError | InterfaceError) or (
        isinstance(error, DBAPIError) and error.connection_invalidated
    ):
        return Reason.DATABASE_UNAVAILABLE
    if isinstance(error, IntegrityError):
        return Reason.DATABASE_INTEGRITY
    return None


@asynccontextmanager
async def write_transaction(db: AsyncSession, operation: str) -> AsyncIterator[None]:
    """One transaction boundary for a crud write, replacing the copy-pasted try/except per function.

    Usage: `async with write_transaction(db, '<operation>'): ...` around the reads, checks and
    writes that make up one write. Normal exit -- a return inside the block included, since a
    context manager's __aexit__ still runs on the way out -- commits. A SQLAlchemyError raised by
    any statement in the block, OR by the commit itself, rolls back once and logs once at ERROR
    through this module's logger with exc_info (so the traceback, SQL and parameters travel to the
    log the way tj-vhboky.41 Addendum 1 asks for). Any other exception -- a domain rejection such as
    EntryNotFound or OwnOverlapConflict, or any other TraderJoeError -- rolls back once and
    re-raises UNCHANGED, but is not logged at ERROR: it is an expected outcome, not a database
    failure (tj-vhboky.41 S3 revised).

    WHAT THE CALLER SEES FOR A DATABASE FAILURE (ADR tj-fa1rpu D5 and D8, TE-6). A SQLAlchemyError
    this module can classify is re-raised as an ExogenousError carrying one of the three DATABASE_*
    reasons, `from` the original, so the HTTP edge renders a declared problem+json body and the
    cause chain still reaches the log. _reason_for above holds the table and the reason the SQLSTATE
    is asked first. ANYTHING IT CANNOT CLASSIFY IS A BUG AND IS RE-RAISED UNCHANGED: a
    ProgrammingError or a DataError is our defect, and converting one into an exogenous reason would
    make our own mistake read as the database's fault, which is the failure mode D5 names.

    THE DETAIL NEVER QUOTES THE ERROR. It is a fixed sentence per reason (_DETAILS above); the
    SQLAlchemy class name and the SQLSTATE go to the ERROR line and never to the wire, because a
    SQLAlchemyError's own text carries the statement and its bound parameters (D8).

    THE error_id TIES THE TWO TOGETHER (D8, its 16:22 UTC 2026-10-02 addendum). Each typed
    conversion mints one with common/errors' new_error_id, puts it in the raised error's metadata --
    so the problem+json body carries it -- and names it in the same ERROR line, which is the one
    place the cause chain is logged. The edge then renders the body without logging the chain a
    second time. The bug path carries no id of its own; the 500 handler logs its traceback under one
    it mints, an accepted duplicate line.

    WHY db.commit()/db.rollback() DIRECTLY, NOT session.begin(): session.begin() refuses when
    autobegin has already opened a transaction on the session, which every read the caller performs
    before its first write does; and the fakes this module is tested against
    (test_dataset_entry_identity.py) implement execute/commit/rollback only, not begin. Calling
    commit and rollback explicitly is also what the four functions already did before this helper
    existed, so this changes where the calls live, not what they do.

    Args:
        db: The session this transaction is opened on.
        operation: What the write is, for the ERROR line.

    Yields:
        None: The block runs inside the transaction.

    Raises:
        ExogenousError: DATABASE_UNAVAILABLE, DATABASE_CONFLICT or DATABASE_INTEGRITY, from the
            SQLAlchemyError that caused it, carrying an error_id in its metadata.
        SQLAlchemyError: Unchanged, for an error this module does not classify: it is a bug.
    """
    try:
        yield
        await db.commit()
    except SQLAlchemyError as error:
        await db.rollback()
        reason = _reason_for(error)
        if reason is None:
            # A bug: logged with its chain and re-raised as it is, so the 500 handler renders it as
            # one and nothing presents it to the caller as a condition of the database's.
            log.error(f'{operation} failed: {type(error).__name__}, SQLSTATE {_sqlstate(error)}', exc_info=True)
            raise
        error_id = new_error_id()
        log.error(
            f'{operation} failed: {type(error).__name__}, SQLSTATE {_sqlstate(error)}, '
            f'reported as {reason}; error_id {error_id}',
            exc_info=True,
        )
        raise ExogenousError(reason, _DETAILS[reason], metadata={'error_id': error_id}) from error
    except Exception:
        await db.rollback()
        raise
