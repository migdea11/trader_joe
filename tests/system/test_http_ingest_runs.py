"""The collection record over the real stack: POST /store against FakeRead, read back from Postgres (tj-7zjov1.235).

Design: the push path (P35, tj-7zjov1.28; server/data/store/CLAUDE.md, line 3 and the sections "Collection record and
live events" and "Delivery intake", and the name_change clamp, P51). POST /store writes an ingest_request row BEFORE it
sends it and no longer opens a RUNNING ingest_run row (the legacy run recorder, tj-grna9p.18/.79, is gone from the
POST); every REPORTED ATTEMPT is one ingest_run row written by the delivery intake, naming its request by request_id.
EVERY POST first sends its name_change clamp query, a CORPORATE_ACTIONS request whose attempt row carries the symbol
alone (no granularity, no dataset, EMPTY against FakeRead without a world). So this module reads a POST's BARS attempt
rows through the symbol's kind-BARS request (_bars_runs) and never by symbol alone; the clamp's row is never one of a
POST's bars runs. The acceptance --

  * a served fetch is ONE bars attempt row, SUCCEEDED (or EMPTY when the vendor held no bars), attempt 1, trigger
    REQUEST, naming the dataset, with no error at all; its bars request is finished in the same state with that
    dataset id;
  * a peer failure is SEVERAL FAILED attempt rows (attempt 1..N, trigger REQUEST then RETRY), no dataset, the
    vendor's sentence each; the request is FAILED ATTEMPTS_EXHAUSTED and the POST answers 503 VENDOR_UNAVAILABLE.
    The attempt row's error_domain and error_reason are NOT asserted either way (open question tj-7zjov1.236);
  * a POST naming IEX is SERVED on IEX (owner ruling 2026-11-06, tj-7zjov1.343): one SUCCEEDED attempt row whose
    feed is IEX, beside the default POST's SIP one, each naming its own dataset;
  * a refused feed (FEED_NOT_AVAILABLE; NOT_APPLICABLE, since SIP and IEX are both served) is refused at Submit: NO
    bars attempt row at all; the request is FAILED PERMANENT with no dataset, and the POST answers 422;
  * the bars request is COMMITTED and OPEN, readable from a second connection, while the fetch is in flight, before
    any attempt row; afterwards the same request is finished with its one attempt row. A first POST is NOT asserted
    as catalog Running: Running reads open requests WITH a dataset id, and a first POST has none until its first
    delivery;
  * runs keep their label after the dataset is deleted;
  * a STREAM create is a 422 INVALID_REQUEST refused at the edge (no request of any kind, no run), while a STORED
    STREAM entry still reads (tj-grna9p.71).

ADDED BY tj-grna9p.81 (phase 2 system check, RESCOPED note of 2026-10-12, item 1), at the end of this module:
last_run on the list and the detail after a real POST, and the running filter, DatasetFacets.running and
last_run's newest-wins rule over LEGACY RUNNING run rows seeded by SQL. That test pins the LEGACY read path
(running_dataset_ids still counts legacy RUNNING rows beside open requests), which stays until the follow-up that
removes recorded_run; the property under test is the read over Postgres (the partial index on RUNNING rows, the
window function behind last_run), which a seeded row exercises exactly.

WHAT ONLY THIS TIER SHOWS. The request row is committed on its own session before the request is sent, while the
POST waits; here that is read on a separate psycopg2 connection against a real Postgres, mid-POST.

CLEANUP. ingest_run and ingest_request have no foreign key, so the entry registry's cascade does not reach them: this
module deletes the run rows, then the request rows, carrying this run's symbols at the end of the session.
"""

import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from common.database.sql_alchemy_nullable_datetime import NullableDateTime
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.errors.vocabulary import Reason
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import (
    IngestRequest,
    IngestRequestFailureCause,
    IngestRequestKind,
    IngestRequestState,
)
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from routers.data_store.app_endpoints import AssetDatasetStoreInterface, UiDatasetsInterface
from tests.fakes.market_data import EMPTY_PREFIX, FAIL_PREFIX, SLOW_PREFIX
from tests.fakes.market_data import FEED as FAKE_DEFAULT_FEED
from tests.system.problem_json import assert_problem


pytestmark = pytest.mark.data_store

RUNS = IngestRun.__table__
REQUESTS = IngestRequest.__table__
INSTANCES = IngestInstance.__table__
GRANULARITY = Granularity.ONE_MINUTE
START = datetime.fromisoformat('2001-02-05T14:30:00+00:00')
END = START + timedelta(minutes=30)
# A POST naming no feed is served on SIP, the production default the fake follows (owner ruling 2026-11-06, option A).
DEFAULT_FEED = Feed.SIP
NAMED_FEED = Feed.IEX
# SIP and IEX are both served; NOT_APPLICABLE is the feed Alpaca has no tape for. The POST body admits it (Feed | None).
UNSERVABLE_FEED = Feed.NOT_APPLICABLE
# How long the in-flight test polls for the bars request: the clamp query's round trip comes first, then FakeRead's
# SLOW_ delay (DEFAULT_SLOW_DELAY_SECONDS, 8 s) holds the bars request open. Generous on purpose.
IN_FLIGHT_POLL_SECONDS = 30.0
IN_FLIGHT_OPEN_STATES = {IngestRequestState.PENDING, IngestRequestState.QUEUED}


@pytest.fixture(scope='module', autouse=True)
def delete_this_runs_ingest_runs(pg_engine: Engine, run_identity) -> Iterator[None]:
    yield
    with pg_engine.begin() as conn:
        conn.execute(sa.delete(RUNS).where(RUNS.c.asset_symbol.contains(run_identity.symbol)))
        conn.execute(sa.delete(REQUESTS).where(REQUESTS.c.asset_symbol.contains(run_identity.symbol)))


def _store_path(symbol: str) -> str:
    return AssetDatasetStoreInterface.POST_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=symbol
    )


def _body(owner: str, **fields: Any) -> dict[str, Any]:
    return {
        'owner': owner,
        'source': DataSource.ALPACA_API.value,
        'granularity': GRANULARITY.value,
        'start': START.isoformat(),
        'end': END.isoformat(),
        **fields,
    }


def _is_bars_request_of(symbol: str) -> sa.ColumnElement[bool]:
    return sa.and_(REQUESTS.c.kind == IngestRequestKind.BARS, REQUESTS.c.asset_symbol == symbol)


def _bars_requests(pg_engine: Engine, symbol: str) -> list[sa.Row]:
    """The symbol's kind-BARS ingest_request rows, oldest first. The clamp's CORPORATE_ACTIONS request is not one."""
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(REQUESTS).where(_is_bars_request_of(symbol)).order_by(REQUESTS.c.created_at, REQUESTS.c.id)
            )
        )


def _bars_runs(pg_engine: Engine, symbol: str) -> list[sa.Row]:
    """The attempt rows of the symbol's BARS requests, in attempt order, joined on request_id.

    Never by asset_symbol alone: the clamp's corporate-actions attempt row carries the symbol too.
    """
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(RUNS)
                .select_from(RUNS.join(REQUESTS, RUNS.c.request_id == REQUESTS.c.id))
                .where(_is_bars_request_of(symbol))
                .order_by(REQUESTS.c.created_at, RUNS.c.attempt_no)
            )
        )


def _rows_naming(pg_engine: Engine, symbol: str) -> tuple[list[sa.Row], list[sa.Row]]:
    """Every ingest_run and every ingest_request row of ANY kind carrying the symbol."""
    with pg_engine.connect() as conn:
        runs = list(conn.execute(sa.select(RUNS.c.id).where(RUNS.c.asset_symbol == symbol)))
        requests = list(
            conn.execute(sa.select(REQUESTS.c.id, REQUESTS.c.kind).where(REQUESTS.c.asset_symbol == symbol))
        )
    return runs, requests


def _post(data_store, adopt: Callable[[str], list[UUID]], symbol: str, body: dict[str, Any]):
    try:
        response = data_store.post(_store_path(symbol), json=body, auth='right')
    finally:
        ids = adopt(symbol)
    return response, ids


@pytest.mark.parametrize(
    ('prefix', 'state', 'request_state'),
    [
        ('', IngestRunState.SUCCEEDED, IngestRequestState.SUCCEEDED),
        (EMPTY_PREFIX, IngestRunState.EMPTY, IngestRequestState.EMPTY),
    ],
)
def test_a_served_fetch_leaves_one_attempt_row_naming_its_dataset(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries, scenario_entries, prefix, state, request_state
):
    symbol = f'{prefix}{own_symbol}'
    response, ids = _post(data_store, scenario_entries if prefix else adopt_entries, symbol, _body(run_identity.owner))

    assert response.status_code == 200, data_store.describe(response)
    (request,) = _bars_requests(pg_engine, symbol)
    (run,) = _bars_runs(pg_engine, symbol)
    assert (run.state, run.bars_written, run.dataset_id) == (state, response.json()['data_points'], ids[0])
    assert (run.request_id, run.attempt_no, run.trigger) == (request.id, 1, IngestRunTrigger.REQUEST)
    assert FAKE_DEFAULT_FEED is DEFAULT_FEED, f'the fake serves an unnamed request on {FAKE_DEFAULT_FEED}, not SIP'
    assert (run.feed, run.granularity) == (DEFAULT_FEED, GRANULARITY)
    assert (run.requested_start, run.requested_end) == (START, END)
    assert run.finished_at is not None
    # A success carries no error at all: designed, not as-built.
    assert (run.error_domain, run.error_reason, run.error_message) == (None, None, None)
    assert (state is IngestRunState.EMPTY) == (run.bars_written == 0)
    assert (request.state, request.dataset_id) == (request_state, ids[0])
    _assert_delivered_by_the_running_ingest(pg_engine, run_identity, request, run, response.json())


def _assert_delivered_by_the_running_ingest(pg_engine: Engine, run_identity, request: sa.Row, run: sa.Row, answer):
    """The running-bind delivery (P26 S1, 16:14 UTC note on tj-7zjov1.38): the attempt came in over data-store-grpc.

    Read from test_client through the store's database only, never by joining ingest_store. The attempt row carries the
    window FakeRead served (its served_range is the query's [start, end), END being long past the fake's clock) and the
    id of the ingest process that made it; that id is the one the request was QUEUED under, and it is a LIVE instance
    the store sighted from the running ingest, not one this suite minted (a minted id carries the run id,
    tests/system/live_instances.py). The POST's own answer is read from the same row, so it names the same window.
    """
    assert (run.served_start, run.served_end) == (START, END), (
        f'the attempt row carries served range [{run.served_start}, {run.served_end}), not [{START}, {END})'
    )
    served = answer['served_range']
    assert (datetime.fromisoformat(served['start']), datetime.fromisoformat(served['end'])) == (START, END), answer
    assert run.instance_id, 'the attempt row names no ingest instance'
    assert run.instance_id == request.instance_id, (
        f'the attempt was made by {run.instance_id}, but the request was QUEUED under {request.instance_id}'
    )
    assert run_identity.run_id not in run.instance_id, 'the attempt names an instance this suite minted'
    with pg_engine.connect() as conn:
        sighted = conn.execute(
            sa.select(INSTANCES.c.superseded_at).where(INSTANCES.c.instance_id == run.instance_id)
        ).one_or_none()
    assert sighted is not None, 'the store never sighted the instance that made the attempt'
    assert sighted.superseded_at is None, 'the instance that made the attempt is superseded, not the live one'


def test_a_peer_failure_leaves_failed_attempts_and_an_exhausted_request(
    own_symbol, run_identity, data_store, pg_engine, scenario_entries
):
    symbol = f'{FAIL_PREFIX}{own_symbol}'
    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    assert response.status_code == 503, data_store.describe(response)
    assert response.json()['reason'] == Reason.VENDOR_UNAVAILABLE.value, data_store.describe(response)
    assert ids == [], 'no attempt delivered, so no entry was written'
    runs = _bars_runs(pg_engine, symbol)
    assert len(runs) > 1, f'retried before giving up: {runs}'
    assert [run.attempt_no for run in runs] == list(range(1, len(runs) + 1)), runs
    assert [run.trigger for run in runs] == [IngestRunTrigger.REQUEST] + [IngestRunTrigger.RETRY] * (len(runs) - 1)
    for run in runs:
        assert (run.state, run.dataset_id, run.bars_written) == (IngestRunState.FAILED, None, 0), run
        # The vendor's sentence; error_domain / error_reason are not asserted either way (tj-7zjov1.236).
        assert run.error_message and run.finished_at is not None, run
    (request,) = _bars_requests(pg_engine, symbol)
    assert (request.state, request.failure_cause, request.dataset_id) == (
        IngestRequestState.FAILED,
        IngestRequestFailureCause.ATTEMPTS_EXHAUSTED,
        None,
    )


def test_a_post_naming_iex_is_served_on_iex_with_its_own_attempt_row_beside_the_default_sip_one(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries
):
    """IEX named is served, not refused (tj-7zjov1.342): each POST's one attempt row carries the feed it resolved to.

    The same window on purpose: a PARTIAL cross-tape overlap is refused by the feed-blind own-overlap check (ruled
    acceptable, tj-7zjov1.351); the identical window is two datasets, because feed is part of the entry's identity.
    """
    default, after_default = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner))
    named, after_named = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner, feed=NAMED_FEED.value))

    assert default.status_code == 200, data_store.describe(default)
    assert named.status_code == 200, data_store.describe(named)
    assert len(after_named) == 2 and after_named[0] == after_default[0], f'IEX named: {after_named}'
    sip_entry, iex_entry = after_named
    requests = _bars_requests(pg_engine, own_symbol)
    runs = _bars_runs(pg_engine, own_symbol)
    assert len(requests) == 2 and len(runs) == 2, (requests, runs)
    by_request = {run.request_id: run for run in runs}
    assert set(by_request) == {request.id for request in requests}, 'a request without its own attempt row'
    feeds = {request.dataset_id: by_request[request.id].feed for request in requests}
    assert feeds == {sip_entry: DEFAULT_FEED, iex_entry: NAMED_FEED}, feeds
    for request in requests:
        run = by_request[request.id]
        assert (request.state, run.state) == (IngestRequestState.SUCCEEDED, IngestRunState.SUCCEEDED), (request, run)
        assert (run.attempt_no, run.trigger, run.dataset_id) == (1, IngestRunTrigger.REQUEST, request.dataset_id)
        assert run.bars_written == named.json()['data_points']


def test_a_refused_feed_leaves_a_failed_request_and_no_bars_attempt(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries
):
    """Refused at Submit: no vendor call, so no bars attempt row. FAILED is the REQUEST's state.

    The clamp's EMPTY corporate-actions attempt row also carries own_symbol; it is not this POST's bars run.
    """
    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner, feed=UNSERVABLE_FEED.value))

    assert_problem(
        response,
        status=422,
        reason=Reason.FEED_NOT_AVAILABLE.value,
        title='Unprocessable Entity',
        describe=data_store.describe,
    )
    assert ids == []
    assert _bars_runs(pg_engine, own_symbol) == [], 'a refused Submit writes no attempt row'
    (request,) = _bars_requests(pg_engine, own_symbol)
    assert (request.state, request.failure_cause, request.dataset_id) == (
        IngestRequestState.FAILED,
        IngestRequestFailureCause.PERMANENT,
        None,
    )


def _in_flight_read(pg_engine: Engine, symbol: str) -> list[sa.Row]:
    """The symbol's bars requests with their attempt-row count, in ONE statement, so one snapshot holds both."""
    attempts = sa.select(sa.func.count()).select_from(RUNS).where(RUNS.c.request_id == REQUESTS.c.id).scalar_subquery()
    with pg_engine.connect() as conn:
        return list(
            conn.execute(
                sa.select(REQUESTS.c.id, REQUESTS.c.state, REQUESTS.c.dataset_id, attempts.label('attempts')).where(
                    _is_bars_request_of(symbol)
                )
            )
        )


def test_the_bars_request_is_committed_and_open_while_the_fetch_is_in_flight(
    own_symbol, run_identity, data_store, pg_engine, scenario_entries
):
    """The request record is committed BEFORE the request is sent: a second connection reads it mid-POST.

    SLOW_ sleeps before serving, so the bars request stays open for seconds after the clamp query is answered. It is
    seen PENDING or QUEUED, with no dataset and no attempt row; after the 200 the SAME request is SUCCEEDED with its
    one attempt row. Catalog Running is not asserted: a first POST has no dataset id until its first delivery.
    """
    symbol = f'{SLOW_PREFIX}{own_symbol}'
    result: dict[str, Any] = {}

    def post() -> None:
        result['response'], result['ids'] = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    thread = threading.Thread(target=post)
    thread.start()
    seen: list[sa.Row] = []
    still_in_flight = False
    try:
        deadline = time.monotonic() + IN_FLIGHT_POLL_SECONDS
        while thread.is_alive() and time.monotonic() < deadline:
            rows = _in_flight_read(pg_engine, symbol)
            if rows:
                # Alive after the read returned means alive while it ran: the POST had not answered yet.
                seen, still_in_flight = rows, thread.is_alive()
                break
            time.sleep(0.2)
    finally:
        thread.join(timeout=90)

    assert seen and still_in_flight, f'no bars request was visible while the POST was in flight: {seen}'
    (in_flight,) = seen
    assert in_flight.state in IN_FLIGHT_OPEN_STATES, in_flight
    assert (in_flight.dataset_id, in_flight.attempts) == (None, 0), in_flight

    assert not thread.is_alive(), 'the POST did not answer within the join bound'
    assert result['response'].status_code == 200, data_store.describe(result['response'])
    dataset_id = result['ids'][0]
    (finished,) = _bars_requests(pg_engine, symbol)
    assert (finished.id, finished.state, finished.dataset_id) == (
        in_flight.id,
        IngestRequestState.SUCCEEDED,
        dataset_id,
    )
    (attempt,) = _bars_runs(pg_engine, symbol)
    assert (attempt.request_id, attempt.state, attempt.trigger, attempt.dataset_id) == (
        in_flight.id,
        IngestRunState.SUCCEEDED,
        IngestRunTrigger.REQUEST,
        dataset_id,
    )


def test_runs_survive_the_deletion_of_their_dataset_with_their_label(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries
):
    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner))
    assert response.status_code == 200, data_store.describe(response)
    (attempt,) = _bars_runs(pg_engine, own_symbol)
    runs_path = UiDatasetsInterface.GET_UI_DATASET_RUNS.format(dataset_id=ids[0])
    listed = data_store.get(runs_path)
    assert listed.status_code == 200, data_store.describe(listed)
    # The clamp's attempt row names no dataset, so the dataset's runs are its one bars attempt.
    assert [item['id'] for item in listed.json()['items']] == [str(attempt.id)]

    deleted = data_store.delete(f'/store/{ids[0]}', params={'owner': run_identity.owner}, auth='right')
    assert deleted.status_code == 200, data_store.describe(deleted)

    (run,) = _bars_runs(pg_engine, own_symbol)
    assert (run.id, run.state, run.dataset_id, run.asset_symbol) == (
        attempt.id,
        IngestRunState.SUCCEEDED,
        ids[0],
        own_symbol,
    )
    assert_problem(
        data_store.get(runs_path),
        status=404,
        reason=Reason.NOT_FOUND.value,
        title='Not Found',
        describe=data_store.describe,
    )


def test_a_stream_create_is_refused_and_a_stored_stream_entry_still_reads(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries, insert_entry
):
    """tj-grna9p.71: the refusal runs on the REQUEST body only, never on a stored row read back."""
    body = _body(run_identity.owner, update_type='STREAM', expiry_type='ROLLING')
    body.pop('end')
    response, ids = _post(data_store, adopt_entries, own_symbol, body)

    assert_problem(
        response,
        status=422,
        reason=Reason.INVALID_REQUEST.value,
        title='Unprocessable Entity',
        describe=data_store.describe,
    )
    assert 'STREAM' in response.text
    assert ids == []
    # Refused at the edge, before the clamp query is written: no request of any kind, and no run.
    assert _rows_naming(pg_engine, own_symbol) == ([], []), 'refused at the edge, before any request or run'

    # As a STREAM entry created before the refusal existed would be stored: open-ended, a non-BULK expiry type.
    stored = insert_entry(
        # NOT 'STRM': test_http_ui_datasets.py scopes its STATIC listing to the prefix alt_symbol('ST'), and an
        # entry whose symbol starts with it is judged in that listing too (it went red in a full-suite run,
        # tj-grna9p.81). 'X' is not a hex digit, so no own_symbol digest can start with it either.
        asset_symbol=run_identity.alt_symbol('XSTRM'),
        update_type=UpdateType.STREAM.value,
        expiry_type=ExpiryType.ROLLING.value,
        # The open end as the write path stores it: the EPOCH sentinel (OverlapKey.column_values).
        end=NullableDateTime.EPOCH,
    )
    read = data_store.get(f'/store/{stored.id}')
    assert read.status_code == 200, data_store.describe(read)
    assert read.json()['update_type'] == 'STREAM'


# ---------------------------------------------------------------------------------------------
# last_run, the running filter and DatasetFacets.running (tj-grna9p.81, RESCOPED item 1).

LIST_PATH = UiDatasetsInterface.GET_UI_DATASETS.value
FACETS_PATH = UiDatasetsInterface.GET_UI_DATASET_FACETS.value
WIRE_RUNNING = 'INGEST_RUN_STATE_RUNNING'
WIRE_SUCCEEDED = 'INGEST_RUN_STATE_SUCCEEDED'


def _ok(data_store, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = data_store.get(path, params=params)
    assert response.status_code == 200, data_store.describe(response)
    return response.json()


def test_a_served_post_is_the_last_run_on_the_list_and_the_detail(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries
):
    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner))
    assert response.status_code == 200, data_store.describe(response)
    (run,) = _bars_runs(pg_engine, own_symbol)

    (item,) = _ok(data_store, LIST_PATH, {'asset_symbol': own_symbol})['items']
    detail = _ok(data_store, UiDatasetsInterface.GET_UI_DATASET.format(dataset_id=ids[0]))

    for where, last_run in (('list', item['lastRun']), ('detail', detail['lastRun'])):
        assert (last_run['id'], last_run['datasetId'], last_run['state']) == (
            str(run.id),
            str(ids[0]),
            WIRE_SUCCEEDED,
        ), f'{where}: {last_run}'
        assert int(last_run['barsWritten']) == response.json()['data_points'], f'{where}: {last_run}'
        assert last_run['label'] == f'{own_symbol} {GRANULARITY.value}' and 'finishedAt' in last_run, where


def _seed_run(pg_engine: Engine, entry: sa.Row, state: IngestRunState, started_at: datetime) -> UUID:
    """One LEGACY run row for `entry`, as the legacy recorder would leave it: acknowledged, in `state`, no request."""
    finished = state is not IngestRunState.RUNNING
    values = {
        'dataset_id': entry.id,
        'trigger': IngestRunTrigger.REQUEST,
        'actor': entry.owner,
        'asset_symbol': entry.asset_symbol,
        'asset_type': entry.asset_type,
        'data_type': entry.data_type,
        'source': entry.source,
        'granularity': entry.granularity,
        'update_type': entry.update_type,
        'feed': entry.feed,
        'requested_start': entry.start,
        'requested_end': entry.end,
        'state': state,
        'bars_written': 5 if finished else 0,
        'started_at': started_at,
        'finished_at': started_at + timedelta(seconds=1) if finished else None,
    }
    with pg_engine.begin() as conn:
        return conn.execute(sa.insert(RUNS).values(values).returning(RUNS.c.id)).scalar_one()


def _finish(pg_engine: Engine, run_id: UUID, state: IngestRunState, finished_at: datetime) -> None:
    with pg_engine.begin() as conn:
        conn.execute(sa.update(RUNS).where(RUNS.c.id == run_id).values(state=state, finished_at=finished_at))


def test_the_running_filter_facet_and_last_run_follow_the_run_rows_on_postgres(
    own_symbol, insert_entry, data_store, pg_engine
):
    """Two datasets, each with an older and a newer run: last_run is the newest, running is any RUNNING row.

    last_run is the NEWEST run whatever its state; running means "has a run in state RUNNING right now",
    whichever run that is. A: an older success, a newer RUNNING run. B: an older RUNNING row (stale) under a newer success. Both read as
    running until B's stale row is finished; then running=true lists A alone and DatasetFacets.running counts it,
    equal to that list's total. Finishing A's run empties both.

    This pins the LEGACY read path: the rows are legacy-shaped RUNNING runs (no request, no attempt number), which
    running_dataset_ids still counts beside open requests. It stays until the follow-up that removes recorded_run.
    """
    entry_a = insert_entry(asset_symbol=f'{own_symbol}A')
    entry_b = insert_entry(asset_symbol=f'{own_symbol}B')
    t0 = datetime.fromisoformat('2026-10-01T12:00:00+00:00')
    _seed_run(pg_engine, entry_a, IngestRunState.SUCCEEDED, t0)
    running_a = _seed_run(pg_engine, entry_a, IngestRunState.RUNNING, t0 + timedelta(hours=1))
    stale_b = _seed_run(pg_engine, entry_b, IngestRunState.RUNNING, t0)
    newest_b = _seed_run(pg_engine, entry_b, IngestRunState.SUCCEEDED, t0 + timedelta(hours=1))
    scoped = {'asset_symbol': own_symbol}

    by_id = {UUID(item['id']): item['lastRun'] for item in _ok(data_store, LIST_PATH, scoped)['items']}
    assert set(by_id) == {entry_a.id, entry_b.id}, by_id
    assert (by_id[entry_a.id]['id'], by_id[entry_a.id]['state']) == (str(running_a), WIRE_RUNNING), by_id
    assert 'finishedAt' not in by_id[entry_a.id], by_id
    assert (by_id[entry_b.id]['id'], by_id[entry_b.id]['state']) == (str(newest_b), WIRE_SUCCEEDED), by_id
    detail_a = _ok(data_store, UiDatasetsInterface.GET_UI_DATASET.format(dataset_id=entry_a.id))
    assert detail_a['lastRun']['id'] == str(running_a), detail_a

    both = _ok(data_store, LIST_PATH, {**scoped, 'running': 'true'})
    assert {UUID(item['id']) for item in both['items']} == {entry_a.id, entry_b.id}, both
    assert int(_ok(data_store, FACETS_PATH, scoped).get('running', '0')) == 2

    _finish(pg_engine, stale_b, IngestRunState.FAILED, t0 + timedelta(seconds=1))

    only_a = _ok(data_store, LIST_PATH, {**scoped, 'running': 'true'})
    assert [UUID(item['id']) for item in only_a['items']] == [entry_a.id], only_a
    facets = _ok(data_store, FACETS_PATH, scoped)
    assert (int(facets.get('running', '0')), int(facets['all'])) == (1, 2), facets
    # Under running=true the facets describe the filtered list: all equals its total, and so does running.
    filtered = _ok(data_store, FACETS_PATH, {**scoped, 'running': 'true'})
    assert (int(filtered['all']), int(filtered.get('running', '0'))) == (1, 1), filtered

    _finish(pg_engine, running_a, IngestRunState.SUCCEEDED, t0 + timedelta(hours=2))

    assert _ok(data_store, LIST_PATH, {**scoped, 'running': 'true'}).get('items', []) == []
    assert 'running' not in _ok(data_store, FACETS_PATH, scoped), 'canonical JSON omits a zero count'
