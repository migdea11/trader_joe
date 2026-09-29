from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from common.logging import get_logger


log = get_logger(__name__)


@asynccontextmanager
async def write_transaction(db: AsyncSession, operation: str) -> AsyncIterator[None]:
    """One transaction boundary for a crud write, replacing the copy-pasted try/except per function.

    Usage: `async with write_transaction(db, '<operation>'): ...` around the reads, checks and
    writes that make up one write. Normal exit -- a return inside the block included, since a
    context manager's __aexit__ still runs on the way out -- commits. A SQLAlchemyError raised by
    any statement in the block, OR by the commit itself, rolls back once, logs once at ERROR
    through this module's logger with exc_info (so the traceback, SQL and parameters travel to the
    log the way tj-vhboky.41 Addendum 1 asks for), and RE-RAISES THE ORIGINAL ERROR UNCHANGED: no
    wrapper type, no message to keep clean. Any other exception -- a domain rejection such as
    EntryNotFound or OwnOverlapConflict -- rolls back once and re-raises unchanged too, but is not
    logged at ERROR: it is an expected outcome, not a database failure (tj-vhboky.41 S3 revised).

    WHY db.commit()/db.rollback() DIRECTLY, NOT session.begin(): session.begin() refuses when
    autobegin has already opened a transaction on the session, which every read the caller performs
    before its first write does; and the fakes this module is tested against
    (test_dataset_entry_identity.py) implement execute/commit/rollback only, not begin. Calling
    commit and rollback explicitly is also what the four functions already did before this helper
    existed, so this changes where the calls live, not what they do.

    WHY NO TRANSLATED ERROR TYPE YET: tj-fa1rpu (the project's error-handling ADR, covering the
    exception hierarchy every caller would need to adopt) is proposed and unruled. Introducing a
    StoreDatabaseError now would be a type built to be replaced the moment that ADR lands, and the
    2026-09-28 ruling on tj-76u8ip named "forwarding errors" -- not "the caller sees SQLAlchemyError"
    -- as the smell to remove. Re-raising the original keeps the full error available to the caller
    and to this log record, at the cost of database errors being visible only as SQLAlchemyError
    (or a subclass) until that ADR is ruled. When it lands, translating here is the one edit every
    caller of this helper gets at once.
    """
    try:
        yield
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        log.error(f'{operation} failed', exc_info=True)
        raise
    except Exception:
        await db.rollback()
        raise
