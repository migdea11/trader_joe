"""The reserved panel owner (tj-7zjov1.176, P20b): PANEL_OWNER, and POST /store refusing it at the route edge only.

THE DESIGN: ADR tj-26jt9w addendum 20 (3) (the name is PANEL; the string world is never written) and the P20b
architect note of 03:25 UTC 2026-10-28, ruling (2) (owner list Q4): the store refuses a POST /store whose body owner
equals PANEL_OWNER, AT THE ROUTE, BEFORE THE REQUEST ROW IS WRITTEN, with an existing reason (INVALID_REQUEST, 422)
and a message naming the reserved owner; the internal path the panel manager calls (app/ingest/data_action_request.py)
is NOT guarded. A panel-owned dataset is reserved like any non-operator owner: an operator write needs the override
reason (overriding_reason). Pins (7) and (10) of tj-7zjov1.178.

TIER. The real route over two worlds: a fake request session that records every statement (nothing may be sent
before the refusal), and post_path.py's SQLite push path (the request tables are real: they stay empty).
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Granularity
from data.store.app import client_label
from data.store.app.app_depends import get_ingest_submit_client
from data.store.app.client_label import OPERATOR_PRINCIPAL, PANEL_OWNER
from data.store.app.database.crud.dataset_override import OperatorOverrideInput, OverrideRequired, overriding_reason
from data.store.app.database.database import async_db
from data.store.app.database.models.ingest_request import IngestRequestKind, IngestRequestState
from data.store.app.ingest.data_action_request import store_market_activity_worker
from data.store.app.main import app
from data.store.tests.post_path import PostStore, SubmitDouble, post
from data.store.tests.post_path import post_store as _post_store  # noqa: F401 (a fixture)
from data.store.tests.problem_body import problem
from data.store.tests.test_store_dataset_entry_route import REQUEST_BODY, FakeSession, RefusalOnlyClient
from data.store.tests.test_ui_datasets_route import aware_sqlite_datetimes  # noqa: F401 (a fixture)
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from schemas.data_store.asset_dataset_store import StoreAssetDatasetBody, StoreAssetDatasetPath


pytestmark = pytest.mark.data_store

SERVER_ROOT = Path(__file__).resolve().parents[3]
REFUSAL = "The owner 'panel' is reserved for the panel manager: use another owner."
SECRET = 'panel-owner-instance-secret'
PANEL_BODY = REQUEST_BODY | {'owner': PANEL_OWNER}
# A caller label distinct from everything else: the refusal must never echo it.
CALLER_LABEL = 'caller-label-3c9e1a'


@pytest.fixture
def store(_post_store: PostStore) -> PostStore:  # noqa: F811 (the imported fixture, under its own name here)
    return _post_store


def _production_sources() -> list[Path]:
    return [
        path
        for path in SERVER_ROOT.rglob('*.py')
        if not {'tests', 'migrations', 'seeds', '.venv'} & set(path.relative_to(SERVER_ROOT).parts)
    ]


# --------------------------------------------------------------------------------------------- the constant


def test_panel_owner_is_panel_and_is_reserved_like_any_non_operator_owner():
    assert PANEL_OWNER == 'panel'
    assert PANEL_OWNER != OPERATOR_PRINCIPAL
    assert not PANEL_OWNER.startswith(client_label.RESERVED_PREFIX), 'a label, not one of the reserved _ values'
    with pytest.raises(OverrideRequired):
        overriding_reason(PANEL_OWNER, OPERATOR_PRINCIPAL, None)
    with pytest.raises(OverrideRequired):
        overriding_reason(PANEL_OWNER, OPERATOR_PRINCIPAL, OperatorOverrideInput(reason='   ', confirmed=True))
    reason = overriding_reason(PANEL_OWNER, OPERATOR_PRINCIPAL, OperatorOverrideInput(reason=' fix ', confirmed=True))
    assert reason == 'fix'


def test_panel_owner_is_the_one_definition_of_the_string_and_world_is_never_written():
    """No production module but client_label.py spells the owner; models/panel.py's 'panel' is its table name."""
    spelled = {
        path.relative_to(SERVER_ROOT).as_posix()
        for path in _production_sources()
        if re.search(r"""(['"])panel\1""", path.read_text(encoding='utf-8'))
    }
    assert spelled == {'data/store/app/client_label.py', 'data/store/app/database/models/panel.py'}
    table_line = re.compile(r"""TABLE_NAME = 'panel'$""", re.M)
    assert table_line.search((SERVER_ROOT / 'data/store/app/database/models/panel.py').read_text(encoding='utf-8'))
    worlds = [
        path.relative_to(SERVER_ROOT).as_posix()
        for path in _production_sources()
        if re.search(r"""(['"])world\1""", path.read_text(encoding='utf-8'))
    ]
    assert worlds == [], 'the retired owner name world is written'


# --------------------------------------------------------------------------------------------- the route edge


def _post_on_fake(session: FakeSession, client: RefusalOnlyClient, body: dict, monkeypatch) -> object:
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, SECRET)
    app.dependency_overrides[async_db] = lambda: session
    app.dependency_overrides[get_ingest_submit_client] = lambda: client
    try:
        return TestClient(app).post(
            app.url_path_for('store_data', asset_type='stock', data_type='market-activity', asset_symbol='AAPL'),
            json=body,
            headers={INSTANCE_SECRET_HEADER: SECRET, client_label.CLIENT_ID_HEADER: CALLER_LABEL},
        )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    'extra',
    [{}, {'update_type': 'DAILY', 'expiry_type': 'ROLLING'}, {'end': '2026-02-01T00:00:00Z'}],
    ids=['static-open', 'daily', 'static-closed'],
)
def test_a_post_claiming_the_panel_owner_is_422_invalid_request_before_any_statement(monkeypatch, extra):
    session = FakeSession()
    client = RefusalOnlyClient()

    response = _post_on_fake(session, client, PANEL_BODY | extra, monkeypatch)

    body = problem(response, status=422, reason='INVALID_REQUEST')
    assert body['detail'] == REFUSAL
    assert CALLER_LABEL not in response.text
    assert session.statements == [], 'a statement was sent before the refusal'
    assert (session.commits, client.submitted) == (0, [])


@pytest.mark.asyncio
async def test_the_refusal_comes_before_the_request_row_and_the_clamp_query(store, monkeypatch):
    """On the real push path: no ingest_request row of any kind (not even the name_change query) and no Submit."""
    client = SubmitDouble(store)

    response = await post(store, client, PANEL_BODY | {'update_type': 'DAILY', 'expiry_type': 'ROLLING'}, monkeypatch)

    assert problem(response, status=422, reason='INVALID_REQUEST')['detail'] == REFUSAL
    assert await store.requests() == []
    assert (client.submitted, client.queries) == ([], [])
    assert not [step for step in store.journal if step.startswith(('INSERT', 'UPDATE'))], store.journal


@pytest.mark.asyncio
async def test_another_owner_still_posts_on_the_same_world(store, monkeypatch):
    """The guard refuses the reserved owner only: the same body under another owner is written and answered."""
    client = SubmitDouble(store)

    response = await post(store, client, REQUEST_BODY, monkeypatch)

    assert response.status_code == 200, response.text
    assert [row.client_label for row in await store.bars_requests()] == [REQUEST_BODY['owner']]


@pytest.mark.asyncio
async def test_the_internal_path_is_not_guarded_and_records_the_panel_owner(store):
    """The panel manager creates its own datasets through store_market_activity_worker: PANEL_OWNER is accepted."""
    client = SubmitDouble(store)
    path = StoreAssetDatasetPath(asset_type=AssetType.STOCK, data_type=DataType.MARKET_ACTIVITY, asset_symbol='AAPL')
    payload = StoreAssetDatasetBody(
        owner=PANEL_OWNER, source=DataSource.ALPACA_API, granularity=Granularity.ONE_DAY, start=REQUEST_BODY['start']
    )

    async with store.session() as db:
        stored = await store_market_activity_worker(path, payload, db, client)

    assert stored is not None
    (bars,) = await store.bars_requests()
    # The double's default delivery is a zero-bar success: the request finishes EMPTY, never refused.
    assert (bars.client_label, IngestRequestState(bars.state)) == (PANEL_OWNER, IngestRequestState.EMPTY)
    assert [IngestRequestKind(row.kind) for row in await store.requests()].count(IngestRequestKind.BARS) == 1
    assert len(client.submitted) == 1
    created, _ = store.created[0]
    assert created.owner == PANEL_OWNER
