"""P26 Zc: the positive control for Z1 and Z2, one deliberate rate limit (tj-7zjov1.321, for tj-7zjov1.38).

WHY A CONTROL (W2 on tj-7zjov1.38, ruled 00:28 UTC 2026-11-04). No production mutant can make the fake stack answer
a 429: FakeRead answers VENDOR_RATE_LIMITED only for a symbol with its RATELIMIT_ prefix (tests/fakes/market_data.py)
and counts no calls. So Z1 (the orchestrator's log read) and Z2 (phase_z2_no_rate_limit.py) reading clean would prove
nothing unless a known rate limit is shown to reach both. This file makes exactly one.

WHAT IT DOES. It POSTs ONE bars request for RATELIMIT_<this test's symbol> under the run's owner, with a ready_by
READY_BY_S ahead, waits until the request's attempt row exists, and requires it to carry the typed reason: FAILED,
verdict CONTINUE, error_domain ERROR_DOMAIN and error_reason VENDOR_RATE_LIMITED (tj-7zjov1.316; the P26 gate's F3 on
tj-7zjov1.38 found it written with both NULL), so that Z2's REASON branch (attempt_reason_cause), not only its text
pattern, names it.
The path, by reading: the POST's name_change clamp query is served (FakeRead's corporate actions carry no scenario);
attempt 1 of the bars request fails VENDOR_RATE_LIMITED, class RATE_LIMITED, with a reset_at 60 s ahead
(RATELIMIT_RESET_SECONDS), and the store writes its FAILED attempt row; ingest holds the retry until the reset, and
when it re-enters the queue the ready_by has passed, so it is EXPIRED at dequeue: one attempt row, the request FAILED
with cause EXPIRED, no dataset. The POST waits for that verdict, about a minute, longer than the suite's HTTP timeout
(conftest.HTTP_TIMEOUT_SECONDS, 30 s), so a READ timeout of the POST is expected and accepted: the request was sent
and the store took it (the attempt row this file then waits for, and Z1 and Z2 after it, show it got there). The suite
client (conftest.DataStoreHttp.request) turns every transport error into a failure of the test; this file alone passes
read_timeout_ok=True, which re-raises httpx.ReadTimeout for it to catch (tj-7zjov1.353: before that the catch was
unreachable and Zc failed at its own timeout, P26 F2 on tj-7zjov1.38). Any other transport error (a connect or pool
timeout, a refused connection) still fails the test naming the URL. An answer, if one comes, must not be a success.
Its rows are LEFT in the database on purpose: Z2's rerun must find them.

WHERE IT RUNS: LAST IN THE SESSION, and only after Z1 and Z2 read clean (the GATES paragraph of tj-7zjov1.38):
  (1) Z1: logs data_ingest, the whole session, finds nothing (pattern in phase_z2_no_rate_limit.py's docstring);
  (2) run_system_tests paths ['tests/system/phased/phase_z2_no_rate_limit.py']  -> 1 passed;
  (3) run_system_tests paths ['tests/system/phased/phase_zc_rate_limit_control.py']  -> 1 passed (this file);
  (4) Z1 again: logs data_ingest finds lines, and ONLY lines tied to this request (its id or its RATELIMIT_ symbol;
      Z2's failure message at step (5) quotes both);
  (5) run_system_tests paths ['tests/system/phased/phase_z2_no_rate_limit.py']  -> 1 FAILED, naming only rows of
      this request: its attempt row (and its request row only if the request's failure detail quotes the rate limit).
AFTER IT THE STACK IS NOT CLEAN: ingest's rate budget may hold the scope until the reset, and the database holds the
control's rows, which fail every later Z2. The stack must be test_up'd before any later use, and the next session's
test_reset removes the rows.

NEVER IN A WHOLE-TIER RUN, and never twice in one session: a phase_*.py file, collected only when named.

Reds on: none in the limiter; it is the control. It fails when the rate limit never reaches the store's record, which
is itself a finding (the fake, the classification or the delivery of a rate-limited attempt is broken), and when the
attempt row lacks its typed reason (ingest left DeliveryTrailer.reason unset, the mapping dropped it, or the intake did
not write error_domain and error_reason).
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from common.errors.vocabulary import ERROR_DOMAIN, Reason
from data.store.app.database.models.ingest_request import IngestRequest, IngestRequestKind
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState
from routers.data_store.app_endpoints import AssetDatasetStoreInterface
from tests.fakes.market_data import RATELIMIT_PREFIX
from tests.system.phased.phase_z2_no_rate_limit import attempt_causes, attempt_reason_cause


pytestmark = pytest.mark.data_store

RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
GRANULARITY = Granularity.ONE_MINUTE
START = datetime(2001, 2, 5, 14, 30, tzinfo=UTC)
END = START + timedelta(minutes=30)
# Ahead enough for the clamp query and attempt 1, short of the 60 s reset, so the retry expires at dequeue.
READY_BY_S = 30.0
CONTROL_WAIT_S = 90.0
POLL_S = 2.0


def _body(owner: str, ready_by: datetime) -> dict[str, Any]:
    return {
        'owner': owner,
        'source': DataSource.ALPACA_API.value,
        'granularity': GRANULARITY.value,
        'start': START.isoformat(),
        'end': END.isoformat(),
        'ready_by': ready_by.isoformat(),
    }


def _bars_attempts(pg_engine: Engine, symbol: str) -> tuple[list[sa.Row], list[sa.Row]]:
    """The symbol's BARS requests and their attempt rows (never the clamp's corporate-actions request)."""
    with pg_engine.connect() as conn:
        requests = list(
            conn.execute(
                sa.select(REQUESTS).where(REQUESTS.c.kind == IngestRequestKind.BARS, REQUESTS.c.asset_symbol == symbol)
            )
        )
        runs = list(
            conn.execute(
                sa.select(RUNS).where(RUNS.c.request_id.in_([row.id for row in requests])).order_by(RUNS.c.attempt_no)
            )
        )
    return requests, runs


def test_zc_one_rate_limited_request_reaches_the_stores_record(
    own_symbol, run_identity, data_store, pg_engine, scenario_entries
) -> None:
    symbol = f'{RATELIMIT_PREFIX}{own_symbol}'
    path = AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )
    ready_by = datetime.now(UTC) + timedelta(seconds=READY_BY_S)
    try:
        # read_timeout_ok: the suite client fails the test on every transport error unless told otherwise; here a
        # READ timeout is the expected outcome and comes back as httpx.ReadTimeout (tj-7zjov1.353).
        response = data_store.post(path, json=_body(run_identity.owner, ready_by), auth='right', read_timeout_ok=True)
    except httpx.ReadTimeout:
        response = None
    finally:
        # A rate-limited request makes no dataset; one that did is adopted, so the session's cleanup deletes it.
        made = scenario_entries(symbol)
    assert made == [], f'the rate-limited POST made dataset(s) {made}: FakeRead served {symbol}'
    if response is not None:
        assert not response.is_success, f'the rate-limited POST succeeded: {data_store.describe(response)}'

    # Wait for the control's FIRST attempt row, whatever it says, then judge it: a row written without its typed reason
    # (the P26 gate's F3) fails here naming its columns, never as a timeout.
    deadline = time.monotonic() + CONTROL_WAIT_S
    while True:
        requests, runs = _bars_attempts(pg_engine, symbol)
        if runs:
            break
        if time.monotonic() >= deadline:
            pytest.fail(
                f'no attempt row for {symbol} within {CONTROL_WAIT_S:.0f} s: requests '
                f'{[(row.id, row.state.value, row.failure_cause) for row in requests]}',
                pytrace=False,
            )
        time.sleep(POLL_S)

    assert len(requests) == 1, f'{len(requests)} bars requests for {symbol}, not the one POST'
    (request,) = requests
    assert request.client_label == run_identity.owner, 'the control request does not carry the run owner'

    # THE TYPED REASON (tj-7zjov1.316, pinned for the stack by tj-7zjov1.347): attempt 1 is FAILED, verdict CONTINUE
    # (the retry is held to the reset), and carries FakeRead's VENDOR_RATE_LIMITED in the vocabulary's domain.
    first = runs[0]
    row = dict(first._mapping)
    assert (first.attempt_no, first.state, first.verdict) == (1, IngestRunState.FAILED, 'CONTINUE'), row
    assert (first.error_domain, first.error_reason) == (ERROR_DOMAIN, Reason.VENDOR_RATE_LIMITED.value), (
        f'the control attempt row does not carry its typed reason: {row}'
    )
    # ... so Z2's REASON branch, not only its text pattern, names it at the gate's step (5).
    assert attempt_reason_cause(first) == f'error_reason {Reason.VENDOR_RATE_LIMITED.value}', row
    assert attempt_reason_cause(first) in attempt_causes(first), row
