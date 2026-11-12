"""The logging leak guard itself (validator, tj-7zjov1.94).

logging_snapshot, leaked_state and guarded_isolated_logging back the autouse restore_logging_state
fixtures in the data_ingest and data_store suites. These tests pin that the snapshot sees each piece of
state the guard promises to compare (a touched logger's propagate flag, level and handler list, the module
sink, a root handler's formatter), that root's handler list is not compared, and that the guard passes
when the restore holds and fails, naming the leaked key, when it does not.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import common.logging as logging_module
from common.tests import logging_isolation
from common.tests.logging_isolation import guarded_isolated_logging, isolated_logging, leaked_state, logging_snapshot


@pytest.fixture(autouse=True)
def restored() -> Iterator[None]:
    """Every change a test makes here is undone, whatever it asserts."""
    with isolated_logging():
        yield


def test_unchanged_state_reports_nothing() -> None:
    """Two snapshots with nothing done in between differ in no key."""
    assert leaked_state(logging_snapshot(), logging_snapshot()) == {}


def test_a_changed_propagate_flag_is_reported() -> None:
    """Flipping propagate on a touched logger is named by that logger."""
    logger = logging.getLogger('httpx')
    before = logging_snapshot()
    logger.propagate = not logger.propagate
    leaked = leaked_state(before, logging_snapshot())
    assert set(leaked) == {'httpx propagate'}
    assert leaked['httpx propagate'] == (not logger.propagate, logger.propagate)


def test_a_changed_level_is_reported() -> None:
    """A new level on a touched logger, root included, is named by that logger."""
    before = logging_snapshot()
    logging.getLogger('alpaca').setLevel(logging.CRITICAL + 7)
    logging.getLogger().setLevel(logging.CRITICAL + 9)
    assert set(leaked_state(before, logging_snapshot())) == {'alpaca level', 'root level'}


def test_a_changed_handler_list_is_reported() -> None:
    """A handler added to a touched, non-root logger is named by that logger's handler list."""
    before = logging_snapshot()
    logging.getLogger('urllib3').addHandler(logging.NullHandler())
    assert set(leaked_state(before, logging_snapshot())) == {'urllib3 handlers'}


def test_a_changed_module_sink_is_reported() -> None:
    """A sink set in common.logging is reported as the module sink."""
    before = logging_snapshot()
    logging_module._sink = logging.NullHandler()
    assert set(leaked_state(before, logging_snapshot())) == {'module sink'}


def test_a_changed_root_formatter_is_reported() -> None:
    """A new formatter on a root handler is named by that handler."""
    handler = logging.NullHandler()
    logging.getLogger().addHandler(handler)
    try:
        before = logging_snapshot()
        handler.setFormatter(logging.Formatter('%(message)s changed'))
        assert set(leaked_state(before, logging_snapshot())) == {f'root formatter of {handler!r}'}
    finally:
        logging.getLogger().removeHandler(handler)


def test_root_handler_list_is_not_compared() -> None:
    """A root handler added or removed is not a leak: pytest swaps its own there per phase."""
    before = logging_snapshot()
    handler = logging.NullHandler()
    logging.getLogger().addHandler(handler)
    try:
        assert leaked_state(before, logging_snapshot()) == {}
    finally:
        logging.getLogger().removeHandler(handler)


def test_guard_passes_when_the_restore_holds() -> None:
    """Every kind of change the restore undoes leaves the guard silent."""
    with guarded_isolated_logging():
        logging.getLogger('httpx').propagate = False
        logging.getLogger('httpcore').setLevel(logging.CRITICAL + 3)
        logging.getLogger('requests').addHandler(logging.NullHandler())
        logging_module._sink = None


def test_guard_fails_naming_the_leak_when_the_restore_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With isolated_logging replaced by a no-op, a flipped propagate fails the guard by key."""

    @contextmanager
    def no_restore() -> Iterator[None]:
        yield

    monkeypatch.setattr(logging_isolation, 'isolated_logging', no_restore)
    logger = logging.getLogger('httpx')
    leak = r'logging state leaked past this test \(before, after\): .*httpx propagate'
    with pytest.raises(AssertionError, match=leak), guarded_isolated_logging():
        logger.propagate = not logger.propagate
