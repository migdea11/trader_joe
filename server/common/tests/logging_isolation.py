"""Save and restore the process-wide logging state that common/logging.py's scrub and sink change.

install_scrub() wraps the formatters of handlers it finds, and configure_broker_sink() attaches a
handler to BROKER_LOGGERS and sets propagate False on them, once per process (common.logging._sink).
Under pytest all of that would outlive the test that did it: a later test capturing on root would
stop seeing httpx or httpcore records and pass vacuously (tj-7zjov1.75). isolated_logging() records
handlers, formatters, levels and propagate on every logger the module touches, clears the
once-per-process sink for the duration, and puts everything back afterwards, closing any sink the
test created. The secret registry has no reset and needs none: tests use a fresh made-up value.
"""

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import common.logging as logging_module
from common.logging import BROKER_LOGGERS, ScrubFormatter


# Every logger common/logging.py configures or wraps, plus the children the tests log from.
TOUCHED = (
    '',
    'uvicorn',
    'uvicorn.error',
    'uvicorn.access',
    *BROKER_LOGGERS,
    'urllib3.connectionpool',
    'httpcore.http11',
    'httpcore.connection',
)


@contextmanager
def isolated_logging() -> Iterator[None]:
    """Run the body against a clean sink; restore every touched logger's state afterwards."""
    loggers = {name: logging.getLogger(name) for name in TOUCHED}
    saved = {name: (list(lg.handlers), lg.propagate, lg.level) for name, lg in loggers.items()}
    formatters = {id(h): h.formatter for lg in loggers.values() for h in lg.handlers}
    previous_sink = logging_module._sink
    logging_module._sink = None
    try:
        yield
    finally:
        created_sink = logging_module._sink
        logging_module._sink = previous_sink
        for name, lg in loggers.items():
            handlers, propagate, level = saved[name]
            if name:
                lg.handlers[:] = handlers
            # Root keeps pytest's own per-phase handlers; only formatters are put back there.
            for handler in lg.handlers:
                if id(handler) in formatters:
                    handler.setFormatter(formatters[id(handler)])
                elif isinstance(handler.formatter, ScrubFormatter):
                    handler.setFormatter(handler.formatter.inner)
            lg.propagate = propagate
            lg.setLevel(level)
        if created_sink is not None and created_sink is not previous_sink:
            created_sink.close()


def logging_snapshot() -> dict[str, object]:
    """Every touched logger's propagate, level and handlers, root's formatters by handler, and the sink.

    Root's handler LIST is not compared: pytest swaps its own capture handlers there per phase, so
    only the formatter of each root handler present is recorded, keyed by that handler.
    """
    state: dict[str, object] = {'module sink': logging_module._sink}
    for name in TOUCHED:
        logger = logging.getLogger(name)
        state[f'{name or "root"} propagate'] = logger.propagate
        state[f'{name or "root"} level'] = logger.level
        if name:
            state[f'{name} handlers'] = tuple(logger.handlers)
        else:
            state.update({f'root formatter of {handler!r}': handler.formatter for handler in logger.handlers})
    return state


def leaked_state(before: dict[str, object], after: dict[str, object]) -> dict[str, tuple[object, object]]:
    """The keys both snapshots hold whose values differ, as (before, after); keys in one only are ignored."""
    return {
        key: (before[key], after[key])
        for key in before.keys() & after.keys()
        if before[key] is not after[key] and before[key] != after[key]
    }


@contextmanager
def guarded_isolated_logging() -> Iterator[None]:
    """isolated_logging, then CHECK the restore held: fail when any snapshotted state differs.

    The data suites' autouse restore_logging_state fixtures (tj-7zjov1.92) run each test inside this,
    so a test whose logging state survives the restore fails its own teardown, which pytest reports
    under that test's name, rather than a later test going vacuous.
    """
    before = logging_snapshot()
    with isolated_logging():
        yield
    leaked = leaked_state(before, logging_snapshot())
    assert not leaked, f'logging state leaked past this test (before, after): {leaked}'


class Captured:
    """A stream handler on one logger whose FORMATTED output the test reads (never caplog)."""

    def __init__(self, logger_name: str, formatter: logging.Formatter | None = None) -> None:
        """Attach to logger_name at DEBUG with the given formatter (plain message by default)."""
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(formatter or logging.Formatter('%(name)s %(message)s'))
        self.logger = logging.getLogger(logger_name)
        self.logger.addHandler(self.handler)

    @property
    def text(self) -> str:
        """Everything the handler wrote, formatted."""
        return self.stream.getvalue()

    def close(self) -> None:
        """Detach the handler."""
        self.logger.removeHandler(self.handler)
