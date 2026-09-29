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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from data.store.app.database.transaction import write_transaction


pytestmark = pytest.mark.data_store

_LOGGER = 'data.store.app.database.transaction'


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
    """A SQLAlchemyError SUBCLASS, so the branch is shown to catch the family, not the base alone."""
    db = _Session()
    error = IntegrityError('INSERT ...', {}, Exception('duplicate key'))
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()

    with pytest.raises(IntegrityError) as raised:
        async with write_transaction(db, 'the operation'):
            raise error

    assert raised.value is error
    assert raised.value.__cause__ is None
    assert (db.commit_attempts, db.rollbacks) == (0, 1)
    (record,) = _error_records(caplog)
    assert record.name == _LOGGER
    assert record.exc_info is not None and record.exc_info[1] is error
    assert record.getMessage() == 'the operation failed'
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
