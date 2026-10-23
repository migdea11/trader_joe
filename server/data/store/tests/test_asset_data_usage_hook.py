"""GET /internal/asset-data records one read per dataset it returned bars of, labelled by X-Client-Id (tj-grna9p.46).

The design (tj-grna9p.46, the architect's 2026-10-13 notes, OPTION R of tj-grna9p.131):

* COUNTING: the route has no cursor, so ONE CALL IS ONE READ of every dataset it returned bars of, with that
  dataset's bar count; a query BY DATASET ID that returns no bars is still one read, of 0 bars;
* the label is the X-Client-Id header, classified by classify_client_label; the `owner`-style query filters are
  never the label (this route's query has none, and nothing else is read for it);
* the read path issues NO usage statement: the recorder buffers, and nothing it does may fail or change the read;
* listing and metadata reads record nothing: GET /store/{id} and GET /store/{asset_type}/{data_type}/{symbol}
  return dataset ENTRIES, not bars (the builder's finding, checked against the manifest: there is no bar GET
  under /store at 67fa993), and the /ui/v1 bar-pages and coverage reads are not bar reads either.

TIER. The real app and the real handler and crud, in-process through TestClient, with the database session
replaced by a fake that answers with ORM rows and the module's recorder replaced by a fresh one per test.
"""

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.client_label import CLIENT_ID_HEADER, OPERATOR_PRINCIPAL, UNATTRIBUTED
from data.store.app.database.database import async_db
from data.store.app.database.models.dataset_read import ReadClientKind
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.main import app
from data.store.app.usage_recorder import UsageRecorder
from routers.data_store import asset_dataset_store, internal_asset_data
from routers.data_store.app_endpoints import AssetDataInterface


pytestmark = pytest.mark.data_store

DATASET_A = uuid.UUID('00000000-0000-0000-0000-00000000a001')
DATASET_B = uuid.UUID('00000000-0000-0000-0000-00000000b002')
START = datetime(2026, 1, 2, tzinfo=UTC)
ROUTE = AssetDataInterface.GET_ASSET_DATA.value
SENTINEL = 'hook-client-sentinel-91c4'


def _bar(dataset_id: uuid.UUID, minute: int) -> StockMarketActivity:
    timestamp = START + timedelta(minutes=minute)
    return StockMarketActivity(
        id=minute + 1,
        dataset_id=dataset_id,
        source=DataSource.ALPACA_API,
        asset_symbol='AAPL',
        feed=Feed.IEX,
        granularity=Granularity.ONE_MINUTE,
        timestamp=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100,
        trade_count=10,
    )


class _Rows:
    def __init__(self, rows: list[StockMarketActivity]) -> None:
        self._rows = rows

    def scalars(self) -> '_Rows':
        return self

    def all(self) -> list[StockMarketActivity]:
        return self._rows


class BarSession:
    """Answers the bar read with fixed ORM rows, and records every statement and commit."""

    def __init__(self, rows: list[StockMarketActivity]) -> None:
        self.rows = rows
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.rows)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> UsageRecorder:
    fresh = UsageRecorder(flush_batch_size=1000)
    monkeypatch.setattr(internal_asset_data, 'usage_recorder', fresh)
    return fresh


@pytest.fixture
def get(recorder: UsageRecorder):
    """(rows, params, headers) -> (response, session): a GET of bars on the real app, no lifespan."""

    def _get(rows: list[StockMarketActivity], params: dict[str, str], headers: dict[str, str] | None = None):
        session = BarSession(rows)
        app.dependency_overrides[async_db] = lambda: session
        try:
            client = TestClient(app, raise_server_exceptions=False)
            url = app.url_path_for(
                internal_asset_data.read_stock_market_activity_data.__name__,
                asset_type='stock',
                data_type='market-activity',
            )
            return client.get(url, params=params, headers=headers or {}), session
        finally:
            app.dependency_overrides.clear()

    return _get


def _buffered(recorder: UsageRecorder) -> list[tuple[uuid.UUID, str, ReadClientKind, str, int]]:
    return [(r.dataset_id, r.client_id, r.client_kind, r.route, r.bars_returned) for r in recorder._buffer]


def test_one_call_is_one_read_per_dataset_returned_with_its_bar_count(get, recorder: UsageRecorder):
    rows = [_bar(DATASET_A, 0), _bar(DATASET_B, 1), _bar(DATASET_A, 2), _bar(DATASET_A, 3)]
    response, session = get(rows, {'asset_symbol': 'AAPL'}, {CLIENT_ID_HEADER: SENTINEL})

    assert response.status_code == 200, response.text
    assert len(response.json()) == 4, 'the answer is unchanged by recording'
    assert sorted(_buffered(recorder)) == sorted(
        [(DATASET_A, SENTINEL, ReadClientKind.SDK, ROUTE, 3), (DATASET_B, SENTINEL, ReadClientKind.SDK, ROUTE, 1)]
    )
    # The read path wrote nothing: one statement (the bar SELECT), no commit.
    assert len(session.statements) == 1 and session.commits == 0


def test_a_query_by_dataset_id_that_returns_no_bars_is_one_read_of_zero(get, recorder: UsageRecorder):
    response, _ = get([], {'dataset_id': str(DATASET_A)}, {CLIENT_ID_HEADER: SENTINEL})
    assert response.status_code == 200 and response.json() == []
    assert _buffered(recorder) == [(DATASET_A, SENTINEL, ReadClientKind.SDK, ROUTE, 0)]


def test_a_query_by_symbol_that_returns_nothing_records_nothing(get, recorder: UsageRecorder):
    response, _ = get([], {'asset_symbol': 'AAPL'}, {CLIENT_ID_HEADER: SENTINEL})
    assert response.status_code == 200
    assert recorder.buffered == 0, 'no dataset was read'


def test_a_query_by_dataset_id_with_bars_counts_them_once(get, recorder: UsageRecorder):
    response, _ = get([_bar(DATASET_A, 0), _bar(DATASET_A, 1)], {'dataset_id': str(DATASET_A)})
    assert response.status_code == 200
    assert Counter(r.dataset_id for r in recorder._buffer) == {DATASET_A: 1}
    assert recorder._buffer[0].bars_returned == 2


@pytest.mark.parametrize(
    ('headers', 'label', 'kind'),
    [
        ({CLIENT_ID_HEADER: OPERATOR_PRINCIPAL}, OPERATOR_PRINCIPAL, ReadClientKind.UI),
        ({CLIENT_ID_HEADER: SENTINEL}, SENTINEL, ReadClientKind.SDK),
        ({'x-client-id': SENTINEL}, SENTINEL, ReadClientKind.SDK),  # header names are case-insensitive
        ({}, UNATTRIBUTED, ReadClientKind.SDK),
        ({CLIENT_ID_HEADER: ''}, UNATTRIBUTED, ReadClientKind.SDK),
        ({CLIENT_ID_HEADER: '_overflow'}, UNATTRIBUTED, ReadClientKind.SDK),
        ({CLIENT_ID_HEADER: 'x' * 129}, UNATTRIBUTED, ReadClientKind.SDK),
    ],
)
def test_the_label_comes_from_the_header(get, recorder: UsageRecorder, headers, label, kind):
    response, _ = get([_bar(DATASET_A, 0)], {'asset_symbol': 'AAPL'}, headers)
    assert response.status_code == 200
    ((_, client_id, client_kind, _, _),) = _buffered(recorder)
    assert (client_id, client_kind) == (label, kind)


def test_a_broken_recorder_never_fails_the_read(get, recorder: UsageRecorder, monkeypatch: pytest.MonkeyPatch):
    def broken(read):
        raise RuntimeError('buffer is gone')

    monkeypatch.setattr(recorder, '_append', broken)
    response, _ = get([_bar(DATASET_A, 0)], {'asset_symbol': 'AAPL'}, {CLIENT_ID_HEADER: SENTINEL})
    assert response.status_code == 200 and len(response.json()) == 1


def test_a_refused_query_records_nothing(get, recorder: UsageRecorder):
    response, _ = get([_bar(DATASET_A, 0)], {}, {CLIENT_ID_HEADER: SENTINEL})
    assert response.status_code == 422
    assert recorder.buffered == 0


def test_no_store_get_returns_bars():
    """The builder's finding, pinned: the /store GETs answer dataset entries, so none of them is a bar read to hook."""
    store_gets = {
        route.path: route.response_model
        for route in asset_dataset_store.router.routes
        if 'GET' in getattr(route, 'methods', set())
    }
    assert store_gets, 'the /store GETs were not found'
    for path, model in store_gets.items():
        assert 'StockDataMarketActivity' not in repr(model), f'{path} serves bars and records no read'
