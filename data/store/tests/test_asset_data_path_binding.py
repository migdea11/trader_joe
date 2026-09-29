"""The internal asset-data routes answer an unsupported asset_type/data_type pair with 422 (tj-vhboky.72).

WHY THIS FILE EXISTS (validator, gating e418c99). tj-vhboky.68 put the pair rule on AssetDataPath as a
``mode='after'`` model_validator, and schemas/tests/test_asset_data_path_pairs.py pins it at the SCHEMA
level. That half alone changed nothing over HTTP: under ``Annotated[AssetDataPath, Depends()]`` FastAPI
validates asset_type and data_type as two separate path parameters and then CALLS the class, so the
validator's pydantic ValidationError is raised inside a dependency call, is not a
RequestValidationError, and escapes as a 500. e418c99 binds the model with ``Path()`` on both routes,
so FastAPI validates it whole and reports the refusal as its own 422 at loc ['path']. The schema tests
are green under either binding; only a request through the real app can tell the two apart, so that is
what this file sends.

What is pinned here, on BOTH the GET and the POST:
  * every unsupported pair answers 422 with exactly one error, at loc ['path'], of type value_error,
    whose message names the pair and the supported set. The pairs are GENERATED from the AssetType x
    DataType product minus SUPPORTED_ASSET_DATA_PAIRS, never listed by hand, so a member added to either
    enum is covered the day it lands. Subtracting the constant is safe HERE because
    test_asset_data_path_pairs.py pins the constant equal to what the routes' match statements serve;
    this file does not re-derive that;
  * the refused request touched nothing: no statement, no staged row, no commit. For the POST that is
    the "writes nothing" requirement; for the GET it shows the refusal came before the read;
  * the served pair, stock/market-activity, still answers 200 on both;
  * a single bad enum value is still FastAPI's own per-field 422 at ['path', '<field>'], type enum, and
    the pair rule does not add a second error on top of it.

WHAT TIER THIS IS. The real app and the real handlers, in-process through TestClient, with the database
session replaced by a recording fake. It is in the PR gate. ``raise_server_exceptions=False`` so that
the regression this file exists for reads as "returned 500" rather than as a traceback.
"""

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import InvalidRequestError

from common.enums.data_select import AssetType, DataType
from data.store.app.database.database import async_db
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.main import app
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from schemas.data_store.asset_data_interface import SUPPORTED_ASSET_DATA_PAIRS


pytestmark = pytest.mark.data_store

CONFIGURED_SECRET = 'asset-data-path-binding-secret-4c1d'

# The endpoints' __name__s, which are also the symbols data_store.manifest records for these addresses.
READ_ROUTE = 'read_stock_market_activity_data'
WRITE_ROUTE = 'create_stock_market_activity_data'

# A query the GET accepts on its own, so the only thing a GET below can be refused for is its path.
# Without asset_symbol the unbounded-read guard (tj-vhboky.26) would add a ['query'] error of its own.
READ_QUERY = {'asset_symbol': 'AAPL'}

# A complete, valid single bar, the same shape as test_bar_post_malformed_body.py's VALID_BAR, so the
# only thing a POST below can be refused for is its path.
VALID_BAR = {
    'dataset_id': '8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f',
    'asset_symbol': 'AAPL',
    'source': 'ALPACA',
    'feed': 'IEX',
    'granularity': '1day',
    'timestamp': '2026-01-02T00:00:00Z',
    'data': {'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 100, 'trade_count': 10},
}

UNSUPPORTED_PAIRS = [
    (asset, data)
    for asset, data in itertools.product(AssetType, DataType)
    if (asset, data) not in SUPPORTED_ASSET_DATA_PAIRS
]
SUPPORTED_TEXT = ', '.join(f'{asset}/{data}' for asset, data in sorted(SUPPORTED_ASSET_DATA_PAIRS))


class _NoRows:
    def scalars(self) -> '_NoRows':
        return self

    def all(self) -> list:
        return []


class RecordingSession:
    """An async session that reaches no database and records every call either route could make.

    execute answers with no rows, which is all the read needs. refresh refuses an instance it was never
    handed, as a real session does, and fills the server-generated columns the response model requires.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.statements: list[Any] = []
        self.commits = 0

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def execute(self, statement: Any) -> _NoRows:
        self.statements.append(statement)
        return _NoRows()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def refresh(self, instance: Any) -> None:
        if not any(instance is added for added in self.added):
            raise InvalidRequestError(f'Instance {type(instance).__name__} is not persisted in this Session')
        now = datetime.now(UTC)
        for column, value in (('id', 1), ('created_at', now), ('updated_at', now)):
            if getattr(instance, column, None) is None:
                setattr(instance, column, value)

    async def close(self) -> None:
        return None


@pytest.fixture
def send(monkeypatch: pytest.MonkeyPatch):
    """A callable (method, asset_type, data_type, session) -> response, on the real app.

    The POST carries the instance secret, since the guard answers before the path model is even
    considered; without it every POST here would be a 401 and nothing below would reach the binding.
    URLs come from ``app.url_path_for`` because only main.py knows the mount prefix; it takes any
    string, so a bad enum value can be addressed the same way.
    """
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, CONFIGURED_SECRET)

    def _send(method: str, asset_type: str, data_type: str, session: RecordingSession):
        app.dependency_overrides[async_db] = lambda: session
        try:
            client = TestClient(app, raise_server_exceptions=False)
            if method == 'GET':
                url = app.url_path_for(READ_ROUTE, asset_type=asset_type, data_type=data_type)
                return client.get(url, params=READ_QUERY)
            url = app.url_path_for(WRITE_ROUTE, asset_type=asset_type, data_type=data_type)
            return client.post(url, json=VALID_BAR, headers={INSTANCE_SECRET_HEADER: CONFIGURED_SECRET})
        finally:
            # `app` is a module-level singleton other test modules import.
            app.dependency_overrides.clear()

    return _send


def _assert_untouched(session: RecordingSession) -> None:
    assert session.statements == [], f'the refused request still sent statements: {session.statements}'
    assert session.added == [], f'the refused request still staged rows: {session.added}'
    assert session.commits == 0, 'the refused request was still committed'


def test_the_refusal_cases_are_not_vacuous():
    """The product is 3 x 3 today with one pair served, so eight are refused. Zero would prove nothing."""
    assert SUPPORTED_ASSET_DATA_PAIRS, 'no pair is served, so nothing below tests a served route'
    assert UNSUPPORTED_PAIRS, 'every pair is served, so nothing below tests a refusal'


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize(('asset_type', 'data_type'), [pytest.param(a, d, id=f'{a}/{d}') for a, d in UNSUPPORTED_PAIRS])
def test_an_unsupported_pair_answers_422_at_the_path_and_touches_nothing(
    send, method: str, asset_type: AssetType, data_type: DataType
):
    """THE REGRESSION tj-vhboky.72 FIXES: under Depends() this was a 500 on both routes.

    One error, compared whole: loc ['path'] is what Path() binding gives a model-level error, and it is
    what distinguishes FastAPI having validated the model from the handler's ``case _`` raising. The
    message must name the pair the caller sent and the set it could have sent instead.
    """
    session = RecordingSession()

    response = send(method, asset_type.value, data_type.value, session)

    assert response.status_code == 422, f'{method} returned {response.status_code}: {response.text}'
    errors = response.json()['detail']
    assert [(error['loc'], error['type']) for error in errors] == [(['path'], 'value_error')], errors
    message = errors[0]['msg']
    assert f'{asset_type.value}/{data_type.value}' in message, f'the refusal does not name the pair: {message!r}'
    assert f'supported: {SUPPORTED_TEXT}' in message, f'the refusal does not name the supported set: {message!r}'
    _assert_untouched(session)


@pytest.mark.parametrize(
    ('asset_type', 'data_type'), [pytest.param(a, d, id=f'{a}/{d}') for a, d in sorted(SUPPORTED_ASSET_DATA_PAIRS)]
)
def test_the_served_pair_still_reads(send, asset_type: AssetType, data_type: DataType):
    """Path() binding must not refuse what the routes serve: the read reaches the database layer."""
    session = RecordingSession()

    response = send('GET', asset_type.value, data_type.value, session)

    assert response.status_code == 200, f'returned {response.status_code}: {response.text}'
    assert response.json() == []
    assert len(session.statements) == 1, f'the read executed {len(session.statements)} statements, not one'


@pytest.mark.parametrize(
    ('asset_type', 'data_type'), [pytest.param(a, d, id=f'{a}/{d}') for a, d in sorted(SUPPORTED_ASSET_DATA_PAIRS)]
)
def test_the_served_pair_still_writes(send, asset_type: AssetType, data_type: DataType):
    """And the write still stages one bar row and commits it."""
    session = RecordingSession()

    response = send('POST', asset_type.value, data_type.value, session)

    assert response.status_code == 200, f'returned {response.status_code}: {response.text}'
    assert len(session.added) == 1 and isinstance(session.added[0], StockMarketActivity), session.added
    assert session.commits == 1, f'expected one commit, got {session.commits}'
    assert response.json()['asset_symbol'] == VALID_BAR['asset_symbol']


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize(
    ('asset_type', 'data_type', 'loc'),
    [
        pytest.param('bogus', 'quote', ['path', 'asset_type'], id='bad-asset-type'),
        pytest.param('stock', 'bogus', ['path', 'data_type'], id='bad-data-type'),
    ],
)
def test_a_bad_enum_value_is_still_fastapis_per_field_422(
    send, method: str, asset_type: str, data_type: str, loc: list[str]
):
    """A value outside an enum is reported against its own field, and the pair rule adds nothing.

    Binding the whole model could in principle change how a field error is located; this is the check
    that it did not. Exactly one error, so a pair refusal stacked on top of the field error (which
    ``mode='after'`` prevents) would fail here too.
    """
    session = RecordingSession()

    response = send(method, asset_type, data_type, session)

    assert response.status_code == 422, f'{method} returned {response.status_code}: {response.text}'
    errors = [(error['loc'], error['type']) for error in response.json()['detail']]
    assert errors == [(loc, 'enum')], errors
    _assert_untouched(session)
