"""A startup test leaves the process's logging as it found it (validator, tj-7zjov1.92).

The two tests run in file order, which pytest keeps: the first does what every lifespan test here does to
logging, through the production call (app_depends.startup_logs on data_ingest's app); the second, which
any later test in the run could be, logs on httpx and reads root. Without conftest.restore_logging_state
the first leaves the broker sink installed for the process: httpx stops propagating and the second fails.
The second is also correct alone, so it does not depend on the first having run.
"""

import logging

import pytest

from common.logging import BROKER_LOGGERS
from common.tests.logging_isolation import Captured
from data.ingest.app import app_depends
from data.ingest.app.main import app


pytestmark = pytest.mark.data_ingest


def test_startup_logs_installs_the_broker_sink(monkeypatch):
    """The step every lifespan test runs: after it, the sink loggers no longer reach root."""
    monkeypatch.delenv('BROKER_LOG_FILE', raising=False)
    app_depends.startup_logs(app)
    assert [name for name in BROKER_LOGGERS if logging.getLogger(name).propagate] == [], (
        'startup_logs no longer installs the broker sink, so the test after this one proves nothing'
    )


def test_a_test_after_startup_still_sees_httpx_on_root():
    """Every sink logger propagates again and carries no sink handler; an httpx record reaches root."""
    assert [name for name in BROKER_LOGGERS if not logging.getLogger(name).propagate] == []
    # requests and urllib3 carry their own NullHandler from import; anything else is the sink's.
    attached = {name: logging.getLogger(name).handlers for name in BROKER_LOGGERS}
    assert {name: hs for name, hs in attached.items() if any(type(h) is not logging.NullHandler for h in hs)} == {}
    captured = Captured('')
    httpx_logger = logging.getLogger('httpx')
    level = httpx_logger.level
    httpx_logger.setLevel(logging.INFO)
    try:
        httpx_logger.info('a made-up request line after startup')
    finally:
        httpx_logger.setLevel(level)
        captured.close()
    assert 'httpx a made-up request line after startup' in captured.text
