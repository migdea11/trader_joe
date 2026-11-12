"""The scheduler module: quota limits, a FIFO queue with a ready-by check, a cost function, credentials, and the executor.

The executor is acknowledgement-gated: retry budget, bounded outgoing buffer, stall alarm.

Lives under data_ingest in PR 5; the move to server/common/scheduler/ is a later builder-shared bead.
"""

from data.ingest.app.scheduler.credentials import (
    Credential,
    CredentialProvider,
    StaticCredentialProvider,
    most_headroom,
)
from data.ingest.app.scheduler.executor import AckGatedExecutor, AttemptResult, AttemptRunner, ExecutorSettings
from data.ingest.app.scheduler.limits import Limit, SchedulerConfigError
from data.ingest.app.scheduler.outbox import OutgoingBuffer, PendingReport
from data.ingest.app.scheduler.queue import ExpiredRequest, FailureCause, FifoQueue, ScheduledRequest
from data.ingest.app.scheduler.retry import RetryPolicy
from data.ingest.app.scheduler.scheduler import CostFunction, ExpiredSink, Scheduler, SchedulerBuilder, ScopeWindow


__all__ = [
    'AckGatedExecutor',
    'AttemptResult',
    'AttemptRunner',
    'CostFunction',
    'Credential',
    'CredentialProvider',
    'ExecutorSettings',
    'ExpiredRequest',
    'ExpiredSink',
    'FailureCause',
    'FifoQueue',
    'Limit',
    'OutgoingBuffer',
    'PendingReport',
    'RetryPolicy',
    'ScheduledRequest',
    'Scheduler',
    'SchedulerBuilder',
    'SchedulerConfigError',
    'ScopeWindow',
    'StaticCredentialProvider',
    'most_headroom',
]
