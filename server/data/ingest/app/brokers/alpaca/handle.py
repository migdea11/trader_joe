"""The Alpaca broker handle: the scheduler Alpaca's bars go through, built from the shared scheduler module.

PACING IS HERE AND NOWHERE ELSE (tj-26jt9w addendum 1 item 1; the P10 gate note of 07:41 UTC 2026-10-24). The
handle declares one quota scope for Alpaca's market-data API with the vendor's hard-coded ceiling, and sets the
limit under it. Every vendor call, from the Submit queue and from FetchDataset alike, acquires from this scope.
The environment-built RateBudget that broker_api used to hold is gone, so no vendor call is paced twice or by a
second formula.

THE SCOPE NAME starts with the vendor's DataSource value (ALPACA), because the deadline error of Scheduler.acquire
carries the scope under the metadata key vendor and that key must still name the vendor wherever it is rendered
(ADR tj-fa1rpu D8). It never carries an account identifier or a credential.

CONFIGURATION may only LOWER what is hard-coded here (limits.py): ALPACA_RATE_LIMIT_PER_MINUTE under the ceiling;
ALPACA_OUTGOING_BUFFER_SIZE, ALPACA_STALL_TIMEOUT_S and ALPACA_MAX_ATTEMPTS set the executor's buffer, its alarm and
its retry budget. Each is read when the handle is built and falls back to the default below.

CREDENTIALS. The credential provider reads ALPACA_API_KEY and ALPACA_API_SECRET when it is asked, never at import, so
the module stays importable without them (CLAUDE.md pitfall 1). The vendor client still takes its key from
broker_api.get_client(); the provider is the seam the account mapping replaces.
"""

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from common.enums.data_stock import DataSource
from common.environment import get_env_var
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork
from data.ingest.app.brokers.handle import BrokerHandle
from data.ingest.app.scheduler.credentials import Credential, StaticCredentialProvider
from data.ingest.app.scheduler.executor import AckGatedExecutor, ExecutorSettings
from data.ingest.app.scheduler.limits import Limit
from data.ingest.app.scheduler.queue import ScheduledRequest
from data.ingest.app.scheduler.retry import RetryPolicy
from data.ingest.app.scheduler.scheduler import SchedulerBuilder


# The quota scope of Alpaca's market-data API. Starts with the DataSource value; see the module docstring.
ALPACA_DATA_SCOPE = f'{DataSource.ALPACA_API.value}_DATA'

# Alpaca's documented basic-plan ceiling, in calls per minute (owner ruling Q1, tj-26jt9w).
ALPACA_CALLS_PER_MINUTE_CEILING = 200

# The most rows one page of a bars call returns, and so the page size the cost estimate assumes.
ALPACA_BARS_PAGE_SIZE = 10_000

DEFAULT_OUTGOING_BUFFER_SIZE = 64
DEFAULT_STALL_TIMEOUT_S = 300.0
DEFAULT_MAX_ATTEMPTS = 5


class _EnvCredentials:
    """Alpaca's configured key, read when asked rather than at import."""

    def credentials(self) -> Sequence[Credential]:
        """List the one configured credential.

        Returns:
            Sequence[Credential]: The key from ALPACA_API_KEY and ALPACA_API_SECRET.

        Raises:
            SchedulerConfigError: If either variable is unset or empty. The message names the variables only.
        """
        return StaticCredentialProvider.from_env(*broker_api.ALPACA_CREDENTIAL_VARS).credentials()


def estimate_bars_calls(request: ScheduledRequest, page_size: int) -> int:
    """Estimate the vendor calls a request costs: a high count of pages for its range.

    Every period of the granularity in the range is counted as a bar, which over-counts overnight and weekend
    gaps on purpose: a high estimate only makes the restart charge cautious (tj-26jt9w addendum 5).

    Args:
        request (ScheduledRequest): A request whose payload is BarsWork.
        page_size (int): Rows per vendor page.

    Returns:
        int: At least 1.
    """
    work: BarsWork = request.payload
    bars = work.request
    end = bars.end if bars.end is not None else datetime.now(UTC)
    periods = max((end - bars.start) / bars.granularity.offset, 0.0)
    return max(1, math.ceil(periods / page_size))


def executor_settings_from_env() -> ExecutorSettings:
    """Read the handle's executor settings from the environment, each falling back to its default.

    Returns:
        ExecutorSettings: The buffer size, stall timeout and retry budget in force.
    """
    return ExecutorSettings(
        buffer_size=get_env_var('ALPACA_OUTGOING_BUFFER_SIZE', DEFAULT_OUTGOING_BUFFER_SIZE, int),
        stall_timeout_s=get_env_var('ALPACA_STALL_TIMEOUT_S', DEFAULT_STALL_TIMEOUT_S, float),
        retry=RetryPolicy(max_attempts=get_env_var('ALPACA_MAX_ATTEMPTS', DEFAULT_MAX_ATTEMPTS, int)),
    )


class AlpacaHandle(BrokerHandle):
    """The handle for DataSource.ALPACA_API: its scheduler, its reader and its executor."""

    def __init__(self, settings: ExecutorSettings | None = None, calls_per_minute: int | None = None) -> None:
        """Build the handle. Nothing runs and nothing reads a credential until start().

        Args:
            settings (ExecutorSettings | None): The executor's settings, or those the environment sets.
            calls_per_minute (int | None): The per-minute limit, or ALPACA_RATE_LIMIT_PER_MINUTE, or the ceiling.

        Raises:
            SchedulerConfigError: If the limit is above Alpaca's ceiling.
        """
        if calls_per_minute is None:
            calls_per_minute = get_env_var('ALPACA_RATE_LIMIT_PER_MINUTE', ALPACA_CALLS_PER_MINUTE_CEILING, int)
        minute = timedelta(minutes=1)
        scheduler = (
            SchedulerBuilder()
            .declare_scope(ALPACA_DATA_SCOPE, [Limit(minute, ALPACA_CALLS_PER_MINUTE_CEILING)])
            .set_limit(minute, calls_per_minute, scope=ALPACA_DATA_SCOPE)
            .cost_function(estimate_bars_calls, page_size=ALPACA_BARS_PAGE_SIZE)
            .credential_provider(_EnvCredentials())
            .on_expired(self.report_expired)
            .build()
        )
        reader = AlpacaRead(scheduler=scheduler, scope=ALPACA_DATA_SCOPE)
        executor = AckGatedExecutor(
            name=ALPACA_DATA_SCOPE,
            scheduler=scheduler,
            run_attempt=BarsAttemptRunner(reader),
            settings=settings if settings is not None else executor_settings_from_env(),
            on_finished=self.finished,
        )
        super().__init__(
            source=DataSource.ALPACA_API,
            scope=ALPACA_DATA_SCOPE,
            scheduler=scheduler,
            executor=executor,
            reader=reader,
            resolve_feed=broker_api.resolve_feed,
        )
