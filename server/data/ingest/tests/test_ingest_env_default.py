"""The ingest default env file lists exactly the Alpaca settings the handle reads (tj-7zjov1.57).

The file is public and is copied into every environment, so a name in it that nothing reads misleads every
reader: ALPACA_RATE_LIMIT_PER_SEC, ALPACA_RATE_BURST and ALPACA_BACKFILL_RESERVE sat there setting numbers
after the environment-built rate budget had gone. These tests pin the file against the handle in both
directions: no retired name, every live name present at the handle's own default, and each name really read.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from data.ingest.app.brokers.alpaca.handle import (
    ALPACA_CALLS_PER_MINUTE_CEILING,
    ALPACA_DATA_SCOPE,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_OUTGOING_BUFFER_SIZE,
    DEFAULT_STALL_TIMEOUT_S,
    AlpacaHandle,
)
from data.ingest.app.scheduler.limits import Limit


INGEST_ENV_DEFAULT = Path(__file__).resolve().parents[1] / '.env.default'

RETIRED = ('ALPACA_RATE_LIMIT_PER_SEC', 'ALPACA_RATE_BURST', 'ALPACA_BACKFILL_RESERVE')

# Each live setting, with the default the handle falls back to when it is unset.
LIVE = {
    'ALPACA_RATE_LIMIT_PER_MINUTE': ALPACA_CALLS_PER_MINUTE_CEILING,
    'ALPACA_OUTGOING_BUFFER_SIZE': DEFAULT_OUTGOING_BUFFER_SIZE,
    'ALPACA_STALL_TIMEOUT_S': DEFAULT_STALL_TIMEOUT_S,
    'ALPACA_MAX_ATTEMPTS': DEFAULT_MAX_ATTEMPTS,
}


def assigned() -> dict[str, str]:
    """Return the NAME=value assignments of the file, comments and blank lines skipped."""
    values = {}
    for line in INGEST_ENV_DEFAULT.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            name, _, value = stripped.partition('=')
            values[name.strip()] = value.strip()
    return values


@pytest.mark.parametrize('name', RETIRED)
def test_no_retired_rate_variable_is_named_in_the_file(name: str):
    """Not assigned and not mentioned in a comment either: the retirement is documented in CLAUDE.md."""
    assert name not in INGEST_ENV_DEFAULT.read_text(encoding='utf-8')


def test_the_alpaca_settings_assigned_are_exactly_the_ones_the_handle_reads():
    alpaca = {name for name in assigned() if name.startswith('ALPACA_')}
    assert alpaca == set(LIVE)


@pytest.mark.parametrize(('name', 'default'), list(LIVE.items()))
def test_each_live_setting_ships_at_the_handles_own_default(name: str, default: float):
    assert float(assigned()[name]) == float(default)


def test_the_handle_built_from_the_file_takes_each_value_it_lists(monkeypatch):
    """Each name is really read: the file's spelling of it, moved off the default, reaches the built handle.

    The names set below are the file's own keys, looked up through assigned(), so a misspelt key in the file
    fails here (KeyError) rather than shipping a setting nothing reads.
    """
    moved_values = {
        'ALPACA_RATE_LIMIT_PER_MINUTE': '60',
        'ALPACA_OUTGOING_BUFFER_SIZE': '7',
        'ALPACA_STALL_TIMEOUT_S': '12.5',
        'ALPACA_MAX_ATTEMPTS': '2',
    }
    for name in LIVE:
        monkeypatch.delenv(name, raising=False)
    file_names = [name for name in assigned() if name.startswith('ALPACA_')]
    for name in file_names:
        monkeypatch.setenv(name, assigned()[name])
    as_shipped = AlpacaHandle()

    for name in file_names:
        monkeypatch.setenv(name, moved_values[name])
    moved = AlpacaHandle()

    def refill(count: int) -> float:
        limit = Limit(timedelta(minutes=1), count)
        return limit.burst / limit.refill_per_sec

    assert as_shipped.scheduler.window(ALPACA_DATA_SCOPE).full_refill_seconds == pytest.approx(
        refill(ALPACA_CALLS_PER_MINUTE_CEILING)
    )
    assert moved.scheduler.window(ALPACA_DATA_SCOPE).full_refill_seconds == pytest.approx(refill(60))
    shipped, configured = as_shipped.executor.settings, moved.executor.settings
    assert (shipped.buffer_size, shipped.stall_timeout_s, shipped.retry.max_attempts) == (
        DEFAULT_OUTGOING_BUFFER_SIZE,
        DEFAULT_STALL_TIMEOUT_S,
        DEFAULT_MAX_ATTEMPTS,
    )
    assert (configured.buffer_size, configured.stall_timeout_s, configured.retry.max_attempts) == (7, 12.5, 2)
