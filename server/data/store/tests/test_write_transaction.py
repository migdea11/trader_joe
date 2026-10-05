"""write_transaction, driven directly: the helper's contract without any crud function in the way.

WHY THIS FILE EXISTS (validator, tj-76u8ip). d43582f added data/store/app/database/transaction.py
and routed the four dataset-entry writes through it; its coverage was reported as `none`.
test_dataset_entry_identity.py pins the helper THROUGH those four writes. This file pins what no
write can show from the outside: that a non-database exception leaves as the very object that was
raised, that a return inside the block still commits, and that task cancellation is not rolled
back (tj-vhboky.41 S1: it catches Exception, not BaseException, and does not add rollback on
cancellation).

DESIGN: tj-vhboky.41 S1 and Addendum 1 (D1 interim, S3 revised). There is no database here; the
session is a recording fake with only the three methods the helper calls.
"""

import asyncio
import logging

import pytest
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)

from common.errors.vocabulary import ExogenousError, Reason
from data.store.app.database.transaction import write_transaction


pytestmark = pytest.mark.data_store

_LOGGER = 'data.store.app.database.transaction'

# The two SQLSTATEs Postgres reports for a lost race: 40001 serialization_failure and 40P01
# deadlock_detected. Spelled out here rather than imported from the module under test, so a
# constant edited there reds rather than moving the expectation with it.
_SERIALIZATION_FAILURE = '40001'
_DEADLOCK_DETECTED = '40P01'


class _Orig(Exception):
    """A DBAPI exception carrying a SQLSTATE, the way asyncpg's do.

    ``write_transaction._sqlstate`` reads ``error.orig.sqlstate`` (asyncpg) or ``.pgcode``
    (psycopg). A SQLAlchemyError built with a plain Exception as its ``orig`` has neither, which is
    the no-SQLSTATE case; this is the one that has one.
    """

    def __init__(self, sqlstate: str):
        super().__init__(f'the driver reported SQLSTATE {sqlstate}')
        self.sqlstate = sqlstate


def _with_sqlstate(error_class: type[DBAPIError], sqlstate: str) -> DBAPIError:
    """A DBAPIError of the given class whose driver exception reports the given SQLSTATE.

    Args:
        error_class: The SQLAlchemy error class to build.
        sqlstate: The five-character code the driver reports.

    Returns:
        DBAPIError: The error, ready to raise inside a transaction.
    """
    return error_class('UPDATE ...', {}, _Orig(sqlstate))


class _Session:
    """Counts commits and rollbacks; the commit raises `commit_error` when one is given."""

    def __init__(self, commit_error: BaseException | None = None):
        self.commit_error = commit_error
        self.commit_attempts = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commit_attempts += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollbacks += 1


class _DomainRejection(Exception):
    """Stands in for EntryNotFound and its siblings: any non-database Exception."""


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_a_block_that_returns_from_inside_commits_once_and_never_rolls_back():
    """S1: normal exit, a return inside the block included, commits. The four writes return from inside."""
    db = _Session()

    async def write() -> str:
        async with write_transaction(db, 'the operation'):
            return 'written'

    assert await write() == 'written'
    assert (db.commit_attempts, db.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_a_database_error_in_the_block_rolls_back_logs_once_and_leaves_unchanged(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
):
    """A classified SQLAlchemyError leaves as a TYPED ExogenousError, from the original (TE-6, D5).

    REPOINTED (validator, gating tj-3mk3u5.37.8), and the inversion is the point of the task rather
    than a detail of it. This case used to assert ``raised.value is error`` -- the helper re-raised
    the SQLAlchemyError unchanged, because tj-vhboky.41's D1 was an explicit INTERIM pending the
    error-handling ADR, and this file's own docstring recorded it as such. ADR tj-fa1rpu landed and
    D5 settled it: a database failure the helper can classify becomes an ExogenousError carrying one
    of the three DATABASE_* reasons, so the HTTP edge can render a declared problem+json instead of
    a 500. Identity therefore CANNOT be the assertion any more, and what replaces it is strictly
    more than it was:

      * the reason, so the conversion is to the right row and not merely to something typed;
      * ``__cause__ is error``, which is the half identity used to give for free -- the original is
        not discarded, it is chained, so the traceback and the SQL still reach an operator;
      * the detail is the FIXED SENTENCE and is NOT str(error) (D8). That is the assertion identity
        never made and the one that matters, because a SQLAlchemyError's text carries the statement
        and its bound parameters. test_the_sql_and_its_parameters_never_reach_the_caller below
        drives that with a canary rather than by inspection;
      * the ERROR line names the same error_id the raised error carries, so the body an operator is
        quoting and the log line they are searching for are tied together (D8's 16:22 addendum).

    The rollback-once, log-once and never-to-stderr assertions are unchanged: TE-6 explicitly keeps
    them. IntegrityError is still the error raised, so the branch is still shown to catch a SUBCLASS
    and not the base alone.
    """
    db = _Session()
    error = IntegrityError('INSERT ...', {}, Exception('duplicate key'))
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()

    with pytest.raises(ExogenousError) as raised:
        async with write_transaction(db, 'the operation'):
            raise error

    assert raised.value.reason is Reason.DATABASE_INTEGRITY, (
        f'an IntegrityError was reported as {raised.value.reason}, not DATABASE_INTEGRITY'
    )
    assert raised.value.__cause__ is error, 'the original error is not chained, so its traceback is lost'
    assert raised.value.detail != str(error), 'the detail is str() of the SQLAlchemyError, which carries its SQL (D8)'
    assert (db.commit_attempts, db.rollbacks) == (0, 1)
    (record,) = _error_records(caplog)
    assert record.name == _LOGGER
    assert record.exc_info is not None and record.exc_info[1] is error
    error_id = raised.value.metadata['error_id']
    assert f'error_id {error_id}' in record.getMessage(), (
        f'the ERROR line does not name the error_id the caller was handed ({error_id}), so the body '
        f'an operator quotes cannot be found in the log: {record.getMessage()!r}'
    )
    assert capfd.readouterr().err == ''


@pytest.mark.asyncio
async def test_a_non_database_error_in_the_block_rolls_back_and_leaves_as_the_same_object_unlogged(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
):
    """tj-76u8ip MUST PIN 3 at the helper: identity, which a crud-level test cannot observe."""
    db = _Session()
    rejection = _DomainRejection('rejected')
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()

    with pytest.raises(_DomainRejection) as raised:
        async with write_transaction(db, 'the operation'):
            raise rejection

    assert raised.value is rejection
    assert raised.value.__cause__ is None
    assert (db.commit_attempts, db.rollbacks) == (0, 1)
    assert _error_records(caplog) == []
    assert capfd.readouterr().err == ''


@pytest.mark.asyncio
async def test_a_database_error_from_the_commit_rolls_back_logs_once_and_leaves_unchanged(
    caplog: pytest.LogCaptureFixture,
):
    """S1: a SQLAlchemyError raised by the commit itself takes the same branch as one from a statement."""
    error = SQLAlchemyError('commit failed')
    db = _Session(commit_error=error)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SQLAlchemyError) as raised:
        async with write_transaction(db, 'the operation'):
            pass

    assert raised.value is error
    assert (db.commit_attempts, db.rollbacks) == (1, 1)
    (record,) = _error_records(caplog)
    assert record.exc_info is not None and record.exc_info[1] is error


@pytest.mark.asyncio
async def test_a_non_database_error_from_the_commit_rolls_back_and_leaves_unlogged(caplog: pytest.LogCaptureFixture):
    """S1: the Exception branch covers the commit too, and stays unlogged there as well."""
    error = _DomainRejection('commit rejected')
    db = _Session(commit_error=error)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(_DomainRejection) as raised:
        async with write_transaction(db, 'the operation'):
            pass

    assert raised.value is error
    assert (db.commit_attempts, db.rollbacks) == (1, 1)
    assert _error_records(caplog) == []


@pytest.mark.asyncio
async def test_cancellation_neither_commits_nor_rolls_back():
    """S1: the helper catches Exception, not BaseException, and adds no rollback on cancellation.

    CancelledError is a BaseException. A helper widened to BaseException would roll back here,
    which S1 says it deliberately does not do (today's code did not either).
    """
    db = _Session()

    with pytest.raises(asyncio.CancelledError):
        async with write_transaction(db, 'the operation'):
            raise asyncio.CancelledError

    assert (db.commit_attempts, db.rollbacks) == (0, 0)


# ---------------------------------------------------------------------------------------------
# THE CLASSIFICATION TABLE (TE-6 item 2, ADR tj-fa1rpu D5). Which SQLAlchemyError becomes which
# reason, and which is left alone as a bug of ours. Before TE-6 the helper had no table at all --
# it re-raised everything -- so none of this was reachable by any test.
# ---------------------------------------------------------------------------------------------


_CLASSIFIED: list[tuple[str, SQLAlchemyError, Reason]] = [
    (
        'OperationalError',
        OperationalError('SELECT 1', {}, Exception('server closed the connection unexpectedly')),
        Reason.DATABASE_UNAVAILABLE,
    ),
    (
        'InterfaceError',
        InterfaceError('SELECT 1', {}, Exception('connection already closed')),
        Reason.DATABASE_UNAVAILABLE,
    ),
    ('IntegrityError', IntegrityError('INSERT ...', {}, Exception('duplicate key')), Reason.DATABASE_INTEGRITY),
    ('serialization failure 40001', _with_sqlstate(OperationalError, _SERIALIZATION_FAILURE), Reason.DATABASE_CONFLICT),
    ('deadlock detected 40P01', _with_sqlstate(OperationalError, _DEADLOCK_DETECTED), Reason.DATABASE_CONFLICT),
]


@pytest.mark.parametrize(('error', 'expected'), [(e, r) for _, e, r in _CLASSIFIED], ids=[n for n, _, _ in _CLASSIFIED])
@pytest.mark.asyncio
async def test_each_classified_database_error_becomes_its_reason(
    error: SQLAlchemyError, expected: Reason, caplog: pytest.LogCaptureFixture
):
    """The whole table in one case, so a row cannot be added to the code without being ruled on here.

    Every row is reachable only through this helper -- the three reasons' vocabulary entries all say
    "through write_transaction" -- so a row that stopped working would show up nowhere else.

    Args:
        error: The error raised inside the transaction.
        expected: The reason the helper must report it as.
        caplog: Captures the single ERROR line.
    """
    db = _Session()
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ExogenousError) as raised:
        async with write_transaction(db, 'the operation'):
            raise error

    assert raised.value.reason is expected, f'{type(error).__name__} was reported as {raised.value.reason}'
    assert raised.value.__cause__ is error, 'the original error is not chained'
    assert (db.commit_attempts, db.rollbacks) == (0, 1), 'a classified failure did not roll back exactly once'
    assert len(_error_records(caplog)) == 1, 'a classified failure was not logged exactly once at ERROR'


@pytest.mark.parametrize('sqlstate', [_SERIALIZATION_FAILURE, _DEADLOCK_DETECTED])
@pytest.mark.asyncio
async def test_a_lost_race_is_a_conflict_even_though_postgres_reports_it_as_an_operational_error(sqlstate: str):
    """THE ORDER OF THE TWO TESTS IS LOAD-BEARING, and this is the case that says so.

    THE BUG THIS PREVENTS, which the TE-6 bead's own wording would have produced. The bead lists the
    rules as bullets, OperationalError first and the SQLSTATE rule after it. Implemented in that
    order the code is wrong, because Postgres does not report a lost race as some third class: both
    40001 (serialization_failure) and 40P01 (deadlock_detected) arrive AS an OperationalError --
    asyncpg raises SerializationError and DeadlockDetectedError, both of which SQLAlchemy wraps as
    OperationalError. An ``isinstance(error, OperationalError)`` test placed first therefore swallows
    every one of them, DATABASE_CONFLICT becomes unreachable from any input whatsoever, and the
    vocabulary row describing it as "a serialization failure or deadlock ... through
    write_transaction" describes something that can never happen. The builder inverted the order and
    said so; this is the test that holds the inversion in place.

    WHY NOTHING ELSE CATCHES IT. The table case above would go on passing with the order reversed
    for every row EXCEPT these two, and a reader adding a plain OperationalError case -- the obvious
    thing to write -- would see green. Only an error that satisfies BOTH rules can tell the order
    apart, which is exactly what this builds: an OperationalError that also carries a conflict
    SQLSTATE. Swapping the two ifs in ``_reason_for`` reds here and nowhere else in the repository.

    Args:
        sqlstate: The conflict SQLSTATE the driver reports, under an OperationalError.
    """
    db = _Session()
    error = _with_sqlstate(OperationalError, sqlstate)

    with pytest.raises(ExogenousError) as raised:
        async with write_transaction(db, 'the operation'):
            raise error

    assert raised.value.reason is Reason.DATABASE_CONFLICT, (
        f'an OperationalError carrying SQLSTATE {sqlstate} was reported as {raised.value.reason}. A lost '
        f'race is retryable as it stands and a database that is not there is not, so a caller told '
        f'DATABASE_UNAVAILABLE for a deadlock is told to back off from a write it should simply retry. '
        f'The SQLSTATE must be tested BEFORE the OperationalError class, or DATABASE_CONFLICT is '
        f'unreachable for every input.'
    )


_UNCLASSIFIED: list[tuple[str, SQLAlchemyError]] = [
    ('ProgrammingError', ProgrammingError('SELECT nonexistent', {}, Exception('column does not exist'))),
    ('DataError', DataError('INSERT ...', {}, Exception('value too long for type character varying(8)'))),
    ('the base SQLAlchemyError', SQLAlchemyError('something went wrong in the ORM layer')),
]


@pytest.mark.parametrize('error', [e for _, e in _UNCLASSIFIED], ids=[n for n, _ in _UNCLASSIFIED])
@pytest.mark.asyncio
async def test_an_unclassified_database_error_is_a_bug_and_leaves_exactly_as_it_arrived(
    error: SQLAlchemyError, caplog: pytest.LogCaptureFixture
):
    """D5's other half, and the one that is invisible unless tested: OUR bug is not THEIR failure.

    ProgrammingError and DataError are the two the bead names, and both mean the store sent the
    database something wrong -- a column that does not exist, a value that does not fit. Converting
    one into an ExogenousError would publish our own defect as a condition of the database's: the
    caller is handed a typed 5xx saying the database is unavailable or in conflict, is told by the
    vocabulary that it may retry, retries, and gets the identical answer forever, while the actual
    defect never surfaces as the 500-with-a-traceback that would have got it fixed.

    IDENTITY IS THE ASSERTION, not the type. A wrapper of the same class, or any re-raise that built
    a new object, loses the traceback the 500 handler logs. ``__cause__ is None`` is asserted beside
    it because a ``raise error from ...`` would also satisfy identity while inventing a chain.

    THE ERROR LINE STILL HAPPENS, and it must: a bug is logged with its chain here, under no
    error_id of its own, and the 500 handler mints one for the body. The accepted duplicate is
    recorded in write_transaction's docstring; what must NOT happen is silence.

    Args:
        error: A SQLAlchemyError the table does not classify.
        caplog: Captures the single ERROR line.
    """
    db = _Session()
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SQLAlchemyError) as raised:
        async with write_transaction(db, 'the operation'):
            raise error

    assert raised.value is error, (
        f'a {type(error).__name__} was replaced on its way out. It is a bug of ours, so it is re-raised '
        f'unchanged and rendered as a 500; dressing it as an exogenous reason tells the caller to retry '
        f'a request that can never succeed (D5).'
    )
    assert not isinstance(raised.value, ExogenousError), 'an unclassifiable error was given a reason'
    assert raised.value.__cause__ is None, 'the bug path invented a cause chain'
    assert (db.commit_attempts, db.rollbacks) == (0, 1), 'a bug did not roll back exactly once'
    assert len(_error_records(caplog)) == 1, 'a bug was not logged exactly once at ERROR'
