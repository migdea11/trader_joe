"""Shared fixtures for the data_ingest suite (validator, tj-7zjov1.92).

LOGGING STATE IS PROCESS-WIDE, AND THE LIFESPAN CHANGES IT. app_depends' lifespan calls
common.app_lifecycle.startup_logs, which installs the broker sink once per process (propagate False on
alpaca, requests, urllib3, httpx and httpcore, a handler on each) and wraps root's and uvicorn's handler
formatters in ScrubFormatter. Without a restore, the first lifespan test leaves that for every later test:
one capturing httpx on root then passes vacuously, and test order decides what passes (finding F5 on
tj-7zjov1.75).

restore_logging_state runs every test inside common.tests.logging_isolation.guarded_isolated_logging,
imported and not copied: isolated_logging, and then a CHECK that the restore held. A test that leaves a
touched logger with another propagate flag, level or handler list, a new module sink, or a root handler
with a different formatter fails its teardown, naming what leaked. Root's handler LIST is not compared:
pytest swaps its own capture handlers there per phase. test_logging_state_restored.py proves the gap by
running startup_logs and then, in the next test, logging on httpx through root.
"""

from collections.abc import Iterator

import pytest

from common.tests.logging_isolation import guarded_isolated_logging


@pytest.fixture(autouse=True)
def restore_logging_state() -> Iterator[None]:
    """Run the test against a clean broker sink; fail if any logging state survives the restore."""
    with guarded_isolated_logging():
        yield
