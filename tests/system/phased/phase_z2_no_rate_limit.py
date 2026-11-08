"""P26 Z2: no failed attempt or request of the session was a 429 or a rate limit (tj-7zjov1.321, for tj-7zjov1.38).

THE RULE (owner ruling (b), 05:20 UTC 2026-11-03 on tj-7zjov1.38): the ingest limiter should make a 429 impossible, so
in any run a 429 or a rate-limit error seen by ingest is a LIMITER FAULT, not an expected event. Z1 is the
orchestrator's read of data_ingest's log (not a test). Z2 is this file: the store's record of the same session, read
from test_client through the database, never by joining ingest's network.

WHAT IT READS. Every ingest_run row in state FAILED and every ingest_request row in state FAILED in the database.
'Since the session's test_reset' is what makes that the session's: test_reset empties the schema at the session's
base step, so every row present was written in this session. Run it on a stack that was not reset and it judges
older rows too.

A ROW IS A HIT (rate_limit_causes) when any of these holds:
  * its reason is a rate reason: an attempt row's error_reason, or a request's failure_cause, is VENDOR_RATE_LIMITED
    or RATE_BUDGET. The store keeps no error class, only the reason. DeliveryErrorClass.RATE_LIMITED
    (data/ingest/app/brokers/bars_attempt.py error_class) covers every reason whose REASONS row requires a reset_at,
    and since 387f75b that includes RANGE_TOO_RECENT, an entitlement edge of the real Alpaca reader that is not a
    rate limit and that FakeRead never answers; it is not a hit here;
  * its error text matches RATE_LIMIT_TEXT, case-insensitive, the Z1 pattern ruled on tj-7zjov1.38 (00:28 UTC
    2026-11-04): a 429 only as 'status 429', 'HTTP 429' or 'code 429' (a bare 429 is in request UUIDs and counts),
    'Too Many Requests', 'rate limit' in any spelling ('rate-limited', 'RATE_LIMITED', 'ratelimit'), 'Retry-After'.
    Read on an attempt row's error_domain, error_reason and error_message, and on a request's failure_cause and
    failure_detail;
  * a vendor status 429 was counted on it: an attempt row's vendor_throttled_429, or a request's spent_throttled_429,
    is above 0.
On a hit it fails quoting every hit row verbatim (all its columns), with why it is one.

WHERE IT RUNS IN THE SESSION (the GATES paragraph of tj-7zjov1.38, ruled 2026-11-04). After the last scenario, and
after the orchestrator's Z1 log read: (1) run THIS FILE, which must pass; (2) run phase_zc_rate_limit_control.py, the
positive control; (3) the orchestrator re-reads data_ingest's log (Z1), which must find the control's lines and only
those; (4) run THIS FILE again, which must FAIL naming only rows of the control's request. A hit at step (1), or a Z1
hit before the control, fails the session (the 05:20 FAIL RULE): the session note is FAILED, quoting the row, and a
limiter bug is filed for builder-ingest before anything closes.

NEVER IN A WHOLE-TIER RUN. A phase_*.py file (scenario.py and conftest.py beside it say how): it is collected only when
named, and in a run of its own: run_system_tests paths ['tests/system/phased/phase_z2_no_rate_limit.py'].

Reds on: no production mutant reaches it on the fake stack (W2 on tj-7zjov1.38: FakeRead answers a rate limit only for
a RATELIMIT_ symbol and counts no calls, so a broken limiter yields no 429). Its red is proven by the positive control
instead: step (4) above, after phase_zc_rate_limit_control.py.
"""

import re
from collections.abc import Iterable

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.errors.vocabulary import Reason
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestState
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState


pytestmark = pytest.mark.data_store

RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
RATE_REASONS = frozenset({Reason.VENDOR_RATE_LIMITED.value, Reason.RATE_BUDGET.value})
RATE_LIMIT_TEXT = re.compile(
    r'\b(status|http|code)\W{0,3}429\b|too many requests|rate[ _-]?limit|retry-after', re.IGNORECASE
)
ATTEMPT_TEXT_COLUMNS = ('error_domain', 'error_reason', 'error_message')
REQUEST_TEXT_COLUMNS = ('failure_cause', 'failure_detail')


def _text_causes(row, columns: Iterable[str]) -> list[str]:
    causes = []
    for column in columns:
        text = getattr(row, column)
        if text and RATE_LIMIT_TEXT.search(text):
            causes.append(f'{column} matches the rate-limit pattern')
    return causes


def attempt_causes(row) -> list[str]:
    """Why a FAILED ingest_run row is a rate-limit hit; empty when it is not one."""
    causes = []
    if row.error_reason in RATE_REASONS:
        causes.append(f'error_reason {row.error_reason}')
    causes += _text_causes(row, ATTEMPT_TEXT_COLUMNS)
    throttled = row.vendor_throttled_429 or 0
    if throttled:
        causes.append(f'vendor_throttled_429 {throttled}')
    return causes


def request_causes(row) -> list[str]:
    """Why a FAILED ingest_request row is a rate-limit hit; empty when it is not one."""
    causes = []
    if row.failure_cause in RATE_REASONS:
        causes.append(f'failure_cause {row.failure_cause}')
    causes += _text_causes(row, REQUEST_TEXT_COLUMNS)
    spent = row.spent_throttled_429 or 0
    if spent:
        causes.append(f'spent_throttled_429 {spent}')
    return causes


def rate_limit_hits(attempts: Iterable, requests: Iterable) -> list[str]:
    """Each hit row quoted verbatim, every column, with its causes."""
    hits = []
    for kind, rows, causes_of in (
        ('ingest_run', attempts, attempt_causes),
        ('ingest_request', requests, request_causes),
    ):
        for row in rows:
            causes = causes_of(row)
            if causes:
                hits.append(f'{kind} {dict(row._mapping)!r} -- {"; ".join(causes)}')
    return hits


def test_z2_no_failed_attempt_or_request_was_a_rate_limit(pg_engine: Engine, data_store) -> None:
    with pg_engine.connect() as conn:
        attempts = list(conn.execute(sa.select(RUNS).where(RUNS.c.state == IngestRunState.FAILED)))
        requests = list(conn.execute(sa.select(REQUESTS).where(REQUESTS.c.state == IngestRequestState.FAILED)))
    hits = rate_limit_hits(attempts, requests)
    assert not hits, (
        f'{len(hits)} failed row(s) of the session were a 429 or a rate limit, a limiter fault under the owner ruling '
        f'(05:20 UTC 2026-11-03 on tj-7zjov1.38), out of {len(attempts)} failed attempt row(s) and {len(requests)} '
        'failed request(s):\n' + '\n'.join(hits)
    )
