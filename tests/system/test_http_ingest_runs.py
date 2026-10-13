"""The ingest_run record over the real stack: POST /store against FakeRead, read back from Postgres (tj-grna9p.79).

Design: tj-grna9p.18's architect re-baseline of 2026-10-11 and its ACCEPTANCE for the validator --

  * POST /store that serves bars leaves exactly ONE SUCCEEDED row, bars_written equal to data_points and
    dataset_id set; a served-but-empty fetch leaves one EMPTY row;
  * a refused ack (FEED_NOT_AVAILABLE) and a peer failure each leave ONE FAILED row with (domain, reason) and a
    NULL dataset_id, while the fetch transaction itself still writes nothing;
  * the RUNNING row is visible from a second session before FetchDone;
  * runs keep their label after the dataset is deleted;
  * POST /store's response and status codes are unchanged (test_ingest_e2e.py pins those and still passes).

-- and tj-grna9p.71's: a STREAM create is a 422 INVALID_REQUEST while a STORED STREAM entry still reads.

WHAT ONLY THIS TIER SHOWS. The recorder writes on its OWN session while the fetch holds its transaction open:
SQLite has one writer, so the unit tier (data/store/tests/test_run_recording.py) cannot hold the two side by
side. Here the fetch transaction is a real Postgres one that commits at FetchDone or rolls back, and the run row
is read on a separate psycopg2 connection.

NOT PROVED HERE: the startup sweep after a real kill. The test client cannot restart data_store, and a sweep
driven from here would fail other tests' live runs. The sweep's SQL is pinned at the unit tier; a kill and
restart is reported as not run on tj-grna9p.79.

CLEANUP. ingest_run has no foreign key, so the entry registry's cascade does not reach it: this module deletes
the rows carrying this run's symbols at the end of the session.
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
from common.errors.vocabulary import ERROR_DOMAIN, Reason
from data.store.app.database.models.ingest_run import IngestRun, IngestRunState, IngestRunTrigger
from routers.data_store.app_endpoints import AssetDatasetStoreInterface, UiDatasetsInterface
from tests.fakes.market_data import EMPTY_PREFIX, FAIL_PREFIX, SLOW_PREFIX
from tests.fakes.market_data import FEED as FAKE_DEPLOYMENT_FEED
from tests.system.problem_json import assert_problem


pytestmark = pytest.mark.data_store

RUNS = IngestRun.__table__
GRANULARITY = Granularity.ONE_MINUTE
START = datetime.fromisoformat('2001-02-05T14:30:00+00:00')
END = START + timedelta(minutes=30)
UNSERVABLE_FEED = Feed.SIP


@pytest.fixture(scope='module', autouse=True)
def delete_this_runs_ingest_runs(pg_engine: Engine, run_identity) -> Iterator[None]:
    yield
    with pg_engine.begin() as conn:
        conn.execute(sa.delete(RUNS).where(RUNS.c.asset_symbol.contains(run_identity.symbol)))


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


def _runs(pg_engine: Engine, symbol: str) -> list[sa.Row]:
    with pg_engine.connect() as conn:
        return list(conn.execute(sa.select(RUNS).where(RUNS.c.asset_symbol == symbol).order_by(RUNS.c.started_at)))


def _post(data_store, adopt: Callable[[str], list[UUID]], symbol: str, body: dict[str, Any]):
    try:
        response = data_store.post(_store_path(symbol), json=body, auth='right')
    finally:
        ids = adopt(symbol)
    return response, ids


@pytest.mark.parametrize(('prefix', 'state'), [('', IngestRunState.SUCCEEDED), (EMPTY_PREFIX, IngestRunState.EMPTY)])
def test_a_served_fetch_leaves_one_finished_run_naming_its_dataset(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries, scenario_entries, prefix, state
):
    symbol = f'{prefix}{own_symbol}'
    response, ids = _post(data_store, scenario_entries if prefix else adopt_entries, symbol, _body(run_identity.owner))

    assert response.status_code == 200, data_store.describe(response)
    (run,) = _runs(pg_engine, symbol)
    assert (run.state, run.bars_written, run.dataset_id) == (state, response.json()['data_points'], ids[0])
    assert (run.trigger, run.feed, run.granularity) == (IngestRunTrigger.REQUEST, FAKE_DEPLOYMENT_FEED, GRANULARITY)
    assert (run.requested_start, run.requested_end) == (START, END)
    assert run.finished_at is not None and run.finished_at >= run.started_at
    assert (run.error_domain, run.error_reason, run.error_message) == (None, None, None)
    assert (state is IngestRunState.EMPTY) == (run.bars_written == 0)


def test_a_peer_failure_leaves_one_failed_run_with_no_dataset_and_no_entry(
    own_symbol, run_identity, data_store, pg_engine, scenario_entries
):
    symbol = f'{FAIL_PREFIX}{own_symbol}'
    response, ids = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    assert response.status_code == 503, data_store.describe(response)
    assert ids == [], 'the fetch transaction must still write nothing'
    (run,) = _runs(pg_engine, symbol)
    assert (run.state, run.dataset_id, run.bars_written) == (IngestRunState.FAILED, None, 0)
    assert (run.error_domain, run.error_reason) == (ERROR_DOMAIN, response.json()['reason'])
    assert run.error_message and run.finished_at is not None


def test_a_refused_ack_leaves_one_failed_run_naming_feed_not_available(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries
):
    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner, feed=UNSERVABLE_FEED.value))

    assert_problem(
        response,
        status=422,
        reason=Reason.FEED_NOT_AVAILABLE.value,
        title='Unprocessable Entity',
        describe=data_store.describe,
    )
    assert ids == []
    (run,) = _runs(pg_engine, own_symbol)
    assert (run.state, run.dataset_id, run.feed) == (IngestRunState.FAILED, None, None)
    assert (run.error_domain, run.error_reason) == (ERROR_DOMAIN, Reason.FEED_NOT_AVAILABLE.value)


def test_the_running_row_is_visible_from_another_session_while_the_fetch_is_in_flight(
    own_symbol, run_identity, data_store, pg_engine, scenario_entries
):
    """SLOW_ sleeps before answering, so the fetch is open for seconds: the RUNNING row is already committed."""
    symbol = f'{SLOW_PREFIX}{own_symbol}'
    result: dict[str, Any] = {}

    def post() -> None:
        result['response'], result['ids'] = _post(data_store, scenario_entries, symbol, _body(run_identity.owner))

    thread = threading.Thread(target=post)
    thread.start()
    try:
        seen: list[sa.Row] = []
        deadline = time.monotonic() + 6
        while not seen and time.monotonic() < deadline:
            seen = _runs(pg_engine, symbol)
            time.sleep(0.2)
    finally:
        thread.join(timeout=90)

    assert seen, 'no run row was visible while the fetch was in flight'
    (in_flight,) = seen
    assert (in_flight.state, in_flight.finished_at) == (IngestRunState.RUNNING, None)
    assert result['response'].status_code == 200, data_store.describe(result['response'])
    (finished,) = _runs(pg_engine, symbol)
    assert (finished.id, finished.state, finished.dataset_id) == (
        in_flight.id,
        IngestRunState.SUCCEEDED,
        result['ids'][0],
    )


def test_runs_survive_the_deletion_of_their_dataset_with_their_label(
    own_symbol, run_identity, data_store, pg_engine, adopt_entries
):
    response, ids = _post(data_store, adopt_entries, own_symbol, _body(run_identity.owner))
    assert response.status_code == 200, data_store.describe(response)
    runs_path = UiDatasetsInterface.GET_UI_DATASET_RUNS.format(dataset_id=ids[0])
    listed = data_store.get(runs_path)
    assert [item['id'] for item in listed.json()['items']] == [str(_runs(pg_engine, own_symbol)[0].id)]

    deleted = data_store.delete(f'/store/{ids[0]}', params={'owner': run_identity.owner}, auth='right')
    assert deleted.status_code == 200, data_store.describe(deleted)

    (run,) = _runs(pg_engine, own_symbol)
    assert (run.state, run.dataset_id, run.asset_symbol) == (IngestRunState.SUCCEEDED, ids[0], own_symbol)
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
    assert ids == [] and _runs(pg_engine, own_symbol) == [], 'refused at the edge, before any run or fetch'

    # As a STREAM entry created before the refusal existed would be stored: open-ended, a non-BULK expiry type.
    stored = insert_entry(
        asset_symbol=run_identity.alt_symbol('STRM'),
        update_type=UpdateType.STREAM.value,
        expiry_type=ExpiryType.ROLLING.value,
        # The open end as the write path stores it: the EPOCH sentinel (OverlapKey.column_values).
        end=NullableDateTime.EPOCH,
    )
    read = data_store.get(f'/store/{stored.id}')
    assert read.status_code == 200, data_store.describe(read)
    assert read.json()['update_type'] == 'STREAM'
