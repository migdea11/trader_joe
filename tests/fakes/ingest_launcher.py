"""The TEST-ONLY data_ingest entrypoint: the production app composed around FakeRead.

Decision tj-j4wknb R4. The fake-mode compose overlay (tj-vhboky.61) runs
`tests.fakes.ingest_launcher:app` in place of `data.ingest.app.main:app`. Production code keeps
its single composition (main.py: one AlpacaHandle handed to create_app) and
knows nothing of this module: the swap is which module uvicorn is pointed at, never a flag.

The app is built by production's own create_app, so the lifespan, the RPC servers and every
router are the real ones. The handle in the ALPACA_API slot is the production AlpacaHandle too
(P53, tj-7zjov1.158): its scheduler, executor, runners and intakes are production's, and only its
reader differs. The handle builds that reader through its reader_factory seam, once, with its own
scheduler, its two quota scopes and the feed it resolved, so the FakeRead is PACED on the handle's
scheduler as AlpacaRead is and serves the account's feed. Submit is the one read path: the
FetchDataset pull stream that used to read through a second readers mapping is gone (tj-7zjov1.275,
owner ruling 2026-11-02), and `reader` below is kept only as the handle's reader, for the banner and
for tests that inspect it.

SLOW_ DELAY: read from FAKE_READ_SLOW_SECONDS here, in the test tree, never in production code.
Unset or empty means DEFAULT_SLOW_DELAY_SECONDS. A value that is not a number, or that FakeRead
refuses, stops the launcher at import rather than serving with a delay nobody asked for.

On import this logs ONE WARNING, starting with BANNER, so a log reader (and CI, tj-irhy0a.1)
can tell a fake-mode service from a real one.
"""

import os
from collections.abc import Mapping

from common.enums.data_stock import DataSource, Feed
from common.logging import get_logger
from data.ingest.app.brokers.alpaca.handle import AlpacaHandle
from data.ingest.app.main import create_app
from data.ingest.app.scheduler.scheduler import Scheduler
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


class FakeReaderFactory:
    """AlpacaHandle's reader factory for fake mode: a FakeRead paced on the handle's scheduler.

    Args:
        slow_delay_seconds (float): SLOW_'s delay for the FakeRead it builds.
    """

    def __init__(self, slow_delay_seconds: float) -> None:
        self.__slow_delay_seconds = slow_delay_seconds

    def __call__(self, *, scheduler: Scheduler, scope: str, trading_scope: str, feed: Feed) -> FakeRead:
        """Build the handle's reader from what the handle resolved at build.

        Args:
            scheduler (Scheduler): The handle's scheduler, which paces every fake "vendor" call.
            scope (str): The data scope bars and corporate actions are charged to.
            trading_scope (str): The trading scope asset reads are charged to.
            feed (Feed): The feed the handle resolved; the fake serves it.

        Returns:
            FakeRead: The reader.

        Raises:
            ValueError: If FakeRead refuses the delay.
        """
        return FakeRead(
            slow_delay_seconds=self.__slow_delay_seconds,
            feed=feed,
            scheduler=scheduler,
            scope=scope,
            trading_scope=trading_scope,
        )


handle = AlpacaHandle(reader_factory=FakeReaderFactory(slow_delay_from_env()))
reader = handle.reader
log.warning(f'{BANNER} SLOW_ delay: {reader.slow_delay_seconds} s.')

app = create_app({DataSource.ALPACA_API: handle})
