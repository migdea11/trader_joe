"""The Alpaca broker handle: the scheduler Alpaca's bars go through, built from the shared scheduler module.

PACING IS HERE AND NOWHERE ELSE (tj-26jt9w addendum 1 item 1; the P10 gate note of 07:41 UTC 2026-10-24). The
handle declares one quota scope for Alpaca's market-data API with the vendor's hard-coded ceiling, and sets the
limit under it. Every vendor call, from the Submit queue and from FetchDataset alike, acquires from this scope.
The environment-built RateBudget that broker_api used to hold is gone, so no vendor call is paced twice or by a
second formula.

THE SCOPE NAME starts with the vendor's DataSource value (ALPACA), because the deadline error of Scheduler.acquire
carries the scope under the metadata key vendor and that key must still name the vendor wherever it is rendered
(ADR tj-fa1rpu D8). It never carries an account identifier or a credential.

CONFIGURATION may only LOWER the one thing limits.py hard-codes: ALPACA_RATE_LIMIT_PER_MINUTE, under the ceiling.
ALPACA_OUTGOING_BUFFER_SIZE, ALPACA_STALL_TIMEOUT_S and ALPACA_MAX_ATTEMPTS are plain overrides of their defaults, with
no ceiling: they set the executor's buffer, its alarm and its retry budget, and each may be set lower or higher. Each is
read when the handle is built and falls back to the default below (the attempt default, DEFAULT_MAX_ATTEMPTS, lives in
scheduler/retry.py so a caller needing it imports no vendor SDK).

THE FEED. The handle decides the default feed, per broker and per account (owner rulings, tj-7zjov1.91). One Alpaca
account per deployment today, so the handle reads ALPACA_SIP_ENABLED once, when it is built, as that account's setting
(default false, IEX) and hands the resulting feed to its reader. Nothing reads the variable again.

CREDENTIALS. The credential provider reads ALPACA_API_KEY and ALPACA_API_SECRET when it is asked, never at import, so
the module stays importable without them (CLAUDE.md pitfall 1). The vendor client still takes its key from
broker_api.get_client(); the provider is the seam the account mapping replaces.
"""

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed
from common.environment import get_env_var
from common.errors.vocabulary import InvalidRequestError, Reason
from data.ingest.app.brokers.alpaca import broker_api
from data.ingest.app.brokers.alpaca.asset_attempt import AssetListAttemptRunner, AssetListWork
from data.ingest.app.brokers.alpaca.corporate_actions_attempt import CorporateActionsAttemptRunner, CorporateActionsWork
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.bars_attempt import BarsAttemptRunner, BarsWork
from data.ingest.app.brokers.handle import BrokerHandle, Intake, no_body
from data.ingest.app.brokers.interface import BrokerRead
from data.ingest.app.brokers.rate_budget import RequestPriority
from data.ingest.app.scheduler.credentials import Credential, StaticCredentialProvider
from data.ingest.app.scheduler.executor import AckGatedExecutor, AttemptResult, AttemptRunner, ExecutorSettings
from data.ingest.app.scheduler.limits import Limit
from data.ingest.app.scheduler.queue import ScheduledRequest
from data.ingest.app.scheduler.retry import DEFAULT_MAX_ATTEMPTS, RetryPolicy
from data.ingest.app.scheduler.scheduler import Scheduler, SchedulerBuilder
from schemas.data_ingest.submit import RequestKind, SubmitRequest


# The quota scope of Alpaca's market-data API. Starts with the DataSource value; see the module docstring.
ALPACA_DATA_SCOPE = f'{DataSource.ALPACA_API.value}_DATA'

# The quota scope of Alpaca's trading API, which serves the asset reads. Its own scope: the trading host keeps its own
# rate limit (it answered x-ratelimit-limit 200, P27a s1), so an asset read never spends from the data API's scope and
# a bars attempt's vendor_stats never counts one.
ALPACA_TRADING_SCOPE = f'{DataSource.ALPACA_API.value}_TRADING'

# Alpaca's documented basic-plan ceiling, in calls per minute (owner ruling Q1, tj-26jt9w).
ALPACA_CALLS_PER_MINUTE_CEILING = 200

# The most rows one page of a bars call returns, and so the page size the cost estimate assumes.
ALPACA_BARS_PAGE_SIZE = broker_api.BARS_PAGE_SIZE

DEFAULT_OUTGOING_BUFFER_SIZE = 64
DEFAULT_STALL_TIMEOUT_S = 300.0


class ReaderFactory(Protocol):
    """Builds the handle's reader from what the handle has resolved at build."""

    def __call__(self, *, scheduler: Scheduler, scope: str, trading_scope: str, feed: Feed) -> BrokerRead:
        """Build the reader.

        Args:
            scheduler (Scheduler): The handle's scheduler.
            scope (str): The data scope (ALPACA_DATA_SCOPE).
            trading_scope (str): The trading scope (ALPACA_TRADING_SCOPE).
            feed (Feed): The feed the handle resolved, once, at build.

        Returns:
            BrokerRead: The reader every runner and the handle use.
        """
        ...


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


def estimate_request_calls(request: ScheduledRequest, page_size: int) -> int:
    """Estimate the vendor calls any request of this handle costs.

    One for an asset list or a corporate-actions read, else a bars request's pages. A corporate-actions read is one call per 1000 records of one symbol's window, and a symbol rarely has so many in
    any window, so its estimate is the one page it nearly always is; the real count is the tally of the attempt.

    Args:
        request (ScheduledRequest): A request whose payload is BarsWork, AssetListWork or CorporateActionsWork.
        page_size (int): Rows per vendor page.

    Returns:
        int: At least 1.
    """
    if isinstance(request.payload, AssetListWork):
        # The whole list is one HTTP request, no pagination (P27a s1).
        return 1
    if isinstance(request.payload, CorporateActionsWork):
        return 1
    return estimate_bars_calls(request, page_size)


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


class _KindRunner:
    """The handle's AttemptRunner: hands each request to the runner of its kind, by its payload."""

    def __init__(self, bars: AttemptRunner, assets: AttemptRunner, actions: AttemptRunner) -> None:
        self.__bars = bars
        self.__assets = assets
        self.__actions = actions

    async def __call__(self, request: ScheduledRequest, attempt_no: int) -> AttemptResult:
        """Run one attempt with the runner of the request's kind.

        Args:
            request (ScheduledRequest): A request whose payload is BarsWork, AssetListWork or CorporateActionsWork.
            attempt_no (int): The attempt's number.

        Returns:
            AttemptResult: What that runner reports.
        """
        if isinstance(request.payload, AssetListWork):
            runner = self.__assets
        elif isinstance(request.payload, CorporateActionsWork):
            runner = self.__actions
        else:
            runner = self.__bars
        return await runner(request, attempt_no)


class AlpacaHandle(BrokerHandle):
    """The handle for DataSource.ALPACA_API: its scheduler, its reader and its executor."""

    def __init__(
        self,
        settings: ExecutorSettings | None = None,
        calls_per_minute: int | None = None,
        *,
        reader_factory: ReaderFactory | None = None,
    ) -> None:
        """Build the handle. Nothing runs and nothing reads a credential until start().

        Two quota scopes: the market-data API's and the trading API's, which serves the asset reads. Both take the
        same lowering (ALPACA_RATE_LIMIT_PER_MINUTE) under the same 200 ceiling, and neither spends from the other.

        Args:
            settings (ExecutorSettings | None): The executor's settings, or those the environment sets.
            calls_per_minute (int | None): The per-minute limit, or ALPACA_RATE_LIMIT_PER_MINUTE, or the ceiling.
            reader_factory (ReaderFactory | None): Called once, with the scheduler, the data scope, the trading scope and
                the resolved feed (all keywords), for the reader the runners and the handle use. Omitted, the handle
                builds AlpacaRead with those same arguments.

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
            .declare_scope(ALPACA_TRADING_SCOPE, [Limit(minute, ALPACA_CALLS_PER_MINUTE_CEILING)])
            .set_limit(minute, calls_per_minute, scope=ALPACA_TRADING_SCOPE)
            .cost_function(estimate_request_calls, page_size=ALPACA_BARS_PAGE_SIZE)
            .credential_provider(_EnvCredentials())
            .on_expired(self.report_expired)
            .build()
        )
        # Read once, here: the account's setting, and the only read of it.
        feed = broker_api.resolve_feed()
        build_reader = reader_factory if reader_factory is not None else AlpacaRead
        reader = build_reader(
            scheduler=scheduler, scope=ALPACA_DATA_SCOPE, trading_scope=ALPACA_TRADING_SCOPE, feed=feed
        )
        executor = AckGatedExecutor(
            name=ALPACA_DATA_SCOPE,
            scheduler=scheduler,
            run_attempt=_KindRunner(
                BarsAttemptRunner(reader), AssetListAttemptRunner(reader), CorporateActionsAttemptRunner(reader)
            ),
            settings=settings if settings is not None else executor_settings_from_env(),
            on_finished=self.finished,
        )
        super().__init__(
            source=DataSource.ALPACA_API,
            scope=ALPACA_DATA_SCOPE,
            scheduler=scheduler,
            executor=executor,
            reader=reader,
            feed=feed,
        )
        self.register_intake(RequestKind.ASSET_LIST, self.__asset_list_intake)
        self.register_intake(RequestKind.CORPORATE_ACTIONS, self.__corporate_actions_intake)

    @staticmethod
    def __asset_list_intake(request: SubmitRequest) -> Intake:
        """The asset_list kind's intake: stocks only, on the TRADING scope at BACKFILL priority, with no tape.

        SubmitAccepted.feed is None and the ScheduledRequest.feed stays None, so an EXPIRED or EXHAUSTED report of it
        is FEED_UNSPECIFIED.

        Args:
            request (SubmitRequest): An asset-list request.

        Returns:
            Intake: The trading scope, the AssetListWork, no feed.

        Raises:
            InvalidRequestError: INVALID_REQUEST if the request carries no asset list; UNSUPPORTED_ASSET_TYPE for an
                asset list of a kind other than stock.
        """
        listing = request.asset_list
        if listing is None:
            raise no_body(request.kind)
        if listing.asset_type is not AssetType.STOCK:
            raise InvalidRequestError(
                Reason.UNSUPPORTED_ASSET_TYPE, f'Alpaca lists stocks only: asset_type={listing.asset_type!r}'
            )
        return Intake(
            scope=ALPACA_TRADING_SCOPE, payload=AssetListWork(request=listing, priority=RequestPriority.BACKFILL)
        )

    @staticmethod
    def __corporate_actions_intake(request: SubmitRequest) -> Intake:
        """The corporate_actions kind's intake: on the DATA scope at BACKFILL priority, with no tape.

        The endpoint is on the market-data host, so its pages are paced on the data scope the bars use. The request has
        no asset type and Alpaca's endpoint is stocks: nothing here to refuse by type. SubmitAccepted.feed is None and
        the ScheduledRequest.feed stays None, so an EXPIRED or EXHAUSTED report of it is FEED_UNSPECIFIED.

        Args:
            request (SubmitRequest): A corporate-actions request.

        Returns:
            Intake: The data scope, the CorporateActionsWork, no feed.

        Raises:
            InvalidRequestError: INVALID_REQUEST if the request carries no corporate-actions body.
        """
        actions = request.corporate_actions
        if actions is None:
            raise no_body(request.kind)
        return Intake(
            scope=ALPACA_DATA_SCOPE, payload=CorporateActionsWork(request=actions, priority=RequestPriority.BACKFILL)
        )
