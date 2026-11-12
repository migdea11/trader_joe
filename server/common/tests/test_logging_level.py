"""The log level switch in common/logging.py (tj-7zjov1.86, tests tj-7zjov1.88).

Owner rulings on tj-7zjov1.78: the root logger defaults to INFO for every service, and the
environment variable LOG_LEVEL set to DEBUG switches it to DEBUG; the broker sink handler follows
the same switch. Any other value, unknown ones included, gives INFO.

The root level is set by get_logger()'s logging.basicConfig, which is a no-op under pytest (pytest's
handlers are already on root), so the root cases run a fresh interpreter, the way a service starts.
The sink cases run in-process inside isolated_logging(), which clears the once-per-process sink.
"""

import logging
import os
import subprocess
import sys

import pytest

from common.logging import LOG_LEVEL_VAR, configure_broker_sink
from common.tests.logging_isolation import isolated_logging
from common.tests.roots import SERVER_ROOT


pytestmark = pytest.mark.common

# None means unset. Values the code reads as DEBUG, and values it must read as INFO.
CASES = [
    (None, logging.INFO),
    ('', logging.INFO),
    ('DEBUG', logging.DEBUG),
    ('debug', logging.DEBUG),
    (' DEBUG\n', logging.DEBUG),
    ('INFO', logging.INFO),
    ('WARNING', logging.INFO),
    ('ERROR', logging.INFO),
    ('10', logging.INFO),
    ('verbose', logging.INFO),
    ('DEBUGGING', logging.INFO),
]
IDS = ['unset', 'empty', 'DEBUG', 'debug', 'padded-DEBUG', 'INFO', 'WARNING', 'ERROR', 'numeric', 'unknown', 'prefix']

_ROOT_PROBE = """
import logging
from common.logging import get_logger
get_logger('tj88.service')
print(logging.getLogger().level)
"""


def test_the_variable_is_named_log_level():
    assert LOG_LEVEL_VAR == 'LOG_LEVEL'


@pytest.mark.parametrize(('value', 'expected'), CASES, ids=IDS)
def test_the_root_level_a_service_starts_with(value: str | None, expected: int):
    env = {k: v for k, v in os.environ.items() if k not in ('PYTEST_ADDOPTS', 'LOG_LEVEL')}
    if value is not None:
        env['LOG_LEVEL'] = value
    env['PYTHONPATH'] = str(SERVER_ROOT)
    result = subprocess.run(
        [sys.executable, '-c', _ROOT_PROBE],
        cwd=SERVER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == expected


@pytest.mark.parametrize(('value', 'expected'), CASES, ids=IDS)
def test_the_broker_sink_handler_level_follows_the_same_switch(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
):
    if value is None:
        monkeypatch.delenv('LOG_LEVEL', raising=False)
    else:
        monkeypatch.setenv('LOG_LEVEL', value)
    with isolated_logging():
        sink = configure_broker_sink(None)
        assert sink.level == expected
        for name in ('alpaca', 'httpx', 'httpcore'):
            assert logging.getLogger(name).level == logging.NOTSET, 'the switch must not set levels on the loggers'
