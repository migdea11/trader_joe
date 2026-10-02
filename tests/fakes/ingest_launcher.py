"""The TEST-ONLY data_ingest entrypoint: the production app composed around FakeRead.

Decision tj-j4wknb R4. The fake-mode compose overlay (tj-vhboky.61) runs
`tests.fakes.ingest_launcher:app` in place of `data.ingest.app.main:app`. Production code keeps
its single composition, create_app({DataSource.ALPACA_API: AlpacaRead()}), and knows nothing of
this module: the swap is which module uvicorn is pointed at, never a flag.

The app is built by production's own create_app, so the lifespan, the RPC servers and every
router are the real ones; only the handle in the ALPACA_API slot differs.

SLOW_ DELAY: read from FAKE_READ_SLOW_SECONDS here, in the test tree, never in production code.
Unset or empty means DEFAULT_SLOW_DELAY_SECONDS. A value that is not a number, or that FakeRead
refuses, stops the launcher at import rather than serving with a delay nobody asked for.

On import this logs ONE WARNING, starting with BANNER, so a log reader (and CI, tj-irhy0a.1)
can tell a fake-mode service from a real one.
"""

import os
from collections.abc import Mapping

from common.enums.data_stock import DataSource
from common.logging import get_logger
from data.ingest.app.main import create_app
from tests.fakes.market_data import DEFAULT_SLOW_DELAY_SECONDS, FakeRead


log = get_logger(__name__)

SLOW_DELAY_ENV = 'FAKE_READ_SLOW_SECONDS'

BANNER = (
    'FAKE BROKER: data_ingest is serving market data from FakeRead (tests.fakes.market_data) in the '
    'ALPACA_API slot; no vendor is called. Fake bars are labelled ALPACA and belong only in a '
    'disposable database.'
)


def slow_delay_from_env(environ: Mapping[str, str] = os.environ) -> float:
    """Read SLOW_'s delay from the environment.

    Args:
        environ (Mapping[str, str]): Environment to read.

    Returns:
        float: The configured delay in seconds, or DEFAULT_SLOW_DELAY_SECONDS when unset or empty.

    Raises:
        ValueError: If the variable is set to something that is not a number.
    """
    raw = environ.get(SLOW_DELAY_ENV, '').strip()
    return float(raw) if raw else DEFAULT_SLOW_DELAY_SECONDS


reader = FakeRead(slow_delay_seconds=slow_delay_from_env())
log.warning(f'{BANNER} SLOW_ delay: {reader.slow_delay_seconds} s.')

app = create_app({DataSource.ALPACA_API: reader})
