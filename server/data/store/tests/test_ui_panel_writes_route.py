"""The /ui/v1 panel writes W1 to W3 and the route table (tj-7zjov1.36, P24), validated by P24v tj-7zjov1.241, over SQLite.

THE DESIGN is the ROUTE CONTRACT on tj-7zjov1.36 (03:05 UTC 2026-11-01), the P23b gate notes on it (12:30 and 01:21 UTC),
ADR tj-26jt9w addendum 20 and panels.proto. Pinned:
  * POST only, never PUT or PATCH (each appends a row; a repeat appends another): PUT and PATCH are 405.
  * The instance secret first: a missing or wrong one is 401 before the body, the path or the panel is read, and nothing
    is written.
  * W1 POST /ui/v1/panels, CreatePanelRequest -> 201 PanelDetail: version 1, effective_pass unset, zero counts; cadence
    UNSPECIFIED -> the store's MONTHLY; source settings GIVEN iff any of fields 13-16 is present; no pass is started.
    REVERSED by ADR tj-2ayia2 (owner ruling 2026-11-07, D3/D4): an ABSENT rate is NO LIMIT (stored NULL, absent from
    the canonical JSON read back, never 0); a present 0, a ramp under 1, a cap under 2 is the store's 422 and a value
    above int32 max fails the parse (422), nothing written; on a source with NO version every create writes its
    version 1 (window-only, ramp-only, cap-only and nothing given are all 201; an absent window day count is 90).
  * W2 POST /ui/v1/panels/{id}/settings-versions -> 201 PanelSettingsVersion, effective_pass unset. Order: secret, 422
    body, 404 unknown panel, 422 store refusal, write.
  * W3 POST /ui/v1/panel-sources/{source}/settings-versions -> 201 PanelSourceSettingsVersion. Order: secret, 422 source
    or body, 422 store refusal, write. The window day counts are required; the rates take W1's rules (tj-2ayia2).
  * Every write: actor OPERATOR_PRINCIPAL (never the reserved dataset owner 'panel', and no body names an owner: an
    'owner' field is an unknown field, 422); the reason OPTIONAL (ADR tj-46jkey, owner ruling 2026-11-06): stored
    trimmed, absent or blank stored NULL and served as '' (absent from the canonical JSON); NO dataset_override_log row
    (the version row is the audit); nothing published on the events hub; every store refusal INVALID_REQUEST 422.
  * No size (ADR tj-46jkey): field 3 is reserved in all three messages, so a body still carrying 'size' names a field
    the message does not have and is refused 422 like any unknown field, with nothing written.
  * No raw SQL in the routers: the route module builds no statement and imports nothing from sqlalchemy but the session
    type; the mapping imports no sqlalchemy at all.

SOURCE SETTINGS FOR A SOURCE THAT ALREADY HAS A VERSION: create_panel refuses them (422, nothing written), as the ruling
on tj-7zjov1.209 (P20b-fix2 tj-7zjov1.212) requires, and panels.proto's CreatePanelRequest 13-16 comment says the same
since tj-7zjov1.245 (pinned in server/common/tests/test_proto_ui_panels.py): a source's settings change only through W3.
W1 passes the body through, so the route answers the store's 422. This file pins that pass-through and the store's answer.
"""

import ast
import inspect
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from data.store.app.client_label import OPERATOR_PRINCIPAL, PANEL_OWNER
from data.store.app.database.database import async_db
from data.store.app.events import EventHub
from data.store.app.main import app
from data.store.tests.panel_ui_harness import (
    NO_CALENDAR_SOURCE,
    NOW,
    OVERRIDES,
    PANELS,
    PASSES,
    SETTINGS,
    SOURCE,
    SOURCE_SETTINGS,
    World,
    make_world,
)
from data.store.tests.problem_body import problem
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, INSTANCE_SECRET_HEADER
from routers.data_store import ui_mapping, ui_panel
from routers.data_store.ui_datasets import utc_now


pytestmark = [pytest.mark.data_store, pytest.mark.asyncio]

SECRET = 'panel-writes-instance-secret'
HEADERS = {INSTANCE_SECRET_HEADER: SECRET}
CREATE = '/ui/v1/panels'
SOURCE_PATH = '/ui/v1/panel-sources/DATA_SOURCE_ALPACA_API/settings-versions'
RATES = {'rampDatasetsPerHour': 2, 'requestCapPerHour': 10}
PANEL_BODY = {'name': 'etfs', 'source': 'DATA_SOURCE_ALPACA_API', 'enabled': True, 'reason': '  first  '}
SETTINGS_BODY = {'enabled': False, 'cadence': 'PANEL_CADENCE_WEEKLY', 'reason': ' paused for review '}
SOURCE_BODY = RATES | {'corporateActionWindowPreDays': 30, 'corporateActionWindowPostDays': 7, 'reason': ' slower '}


class Published:
    def __init__(self) -> None:
        self.events: list[Any] = []


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    state = await make_world(monkeypatch)
    state.published = Published()

    async def pinned_now():
        return NOW

    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, SECRET)
    monkeypatch.setattr(EventHub, 'publish', lambda _hub, event: state.published.events.append(event))
    app.dependency_overrides[async_db] = state.request_session
    app.dependency_overrides[utc_now] = pinned_now
    try:
        yield state
    finally:
        app.dependency_overrides.clear()
        await state.engine.dispose()


@pytest_asyncio.fixture
async def client(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://store') as http:
        yield http


async def post(client: httpx.AsyncClient, route: str, body: Any, headers: dict | None = HEADERS) -> httpx.Response:
    content = body if isinstance(body, str | bytes) else json.dumps(body)
    return await client.post(route, content=content, headers=(headers or {}) | {'content-type': 'application/json'})


async def created(response: httpx.Response) -> dict:
    assert response.status_code == 201, response.text
    assert response.headers['content-type'].startswith('application/json')
    return response.json()


async def nothing_written(world: World, before: dict[str, int]) -> None:
    assert await world.counts() == before
    assert world.published.events == []


def settings_path(panel_id: Any) -> str:
    return f'/ui/v1/panels/{panel_id}/settings-versions'


# --------------------------------------------------------------------------------------------- the route table


async def test_the_eleven_routes_exist_at_exactly_the_ruled_methods_and_paths():
    served = {(method, route.path) for route in ui_panel.router.routes for method in route.methods}
    assert served == {
        ('GET', '/ui/v1/panels'),
        ('GET', '/ui/v1/panels/{panel_id}'),
        ('GET', '/ui/v1/panels/{panel_id}/settings-versions'),
        ('GET', '/ui/v1/panel-sources/{source}/settings-versions'),
        ('GET', '/ui/v1/panels/{panel_id}/boundaries'),
        ('GET', '/ui/v1/panels/{panel_id}/members'),
        ('GET', '/ui/v1/panels/{panel_id}/asset-list-history'),
        ('GET', '/ui/v1/datasets/{dataset_id}/corporate-actions'),
        ('POST', '/ui/v1/panels'),
        ('POST', '/ui/v1/panels/{panel_id}/settings-versions'),
        ('POST', '/ui/v1/panel-sources/{source}/settings-versions'),
    }


async def test_every_write_is_behind_the_instance_secret_and_no_read_is():
    from routers.common.instance_secret import require_instance_secret

    routes = ui_panel.router.routes
    assert len(routes) == 11
    for route in routes:
        guarded = any(dep.call is require_instance_secret for dep in route.dependant.dependencies)
        assert guarded == ('POST' in route.methods), route.path


@pytest.mark.parametrize('method', ['PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize(
    'route', [CREATE, settings_path(uuid.uuid4()), SOURCE_PATH, '/ui/v1/panels/sources/DATA_SOURCE_ALPACA_API/settings']
)
async def test_no_put_patch_or_delete_is_served(world, client, method, route):
    response = await client.request(method, route, content='{}', headers=HEADERS)
    assert response.status_code in (404, 405), response.text
    assert await world.counts() == dict.fromkeys(await world.counts(), 0)


async def test_the_routers_hold_no_sql():
    """ui_panel.py imports only the session type from sqlalchemy and calls no statement builder; the mapping none."""
    for module, allowed in ((ui_panel, {'sqlalchemy.ext.asyncio'}), (ui_mapping, set())):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('sqlalchemy')
        }
        imported |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith('sqlalchemy')
        }
        assert imported <= allowed, (module.__name__, imported)
        calls = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
        }
        assert not calls & {'select', 'insert', 'update', 'delete', 'text', 'execute', 'commit'}, module.__name__


async def test_the_panel_routes_record_no_usage_and_publish_nothing():
    """Reads record no usage (only GET .../bars does) and no write publishes: the module reaches neither seam."""
    tree = ast.parse(inspect.getsource(ui_panel))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not {module for module in imported if module.endswith(('usage_recorder', '.events'))}, imported
    assert not names & {'publish', 'CLIENT_ID_HEADER', 'UsageRecorder', 'record_read'}, names


# --------------------------------------------------------------------------------------------- W1 create


async def test_w1_creates_the_panel_its_version_1_and_its_sources_first_settings(world, client):
    body = await created(await post(client, CREATE, PANEL_BODY | RATES))

    assert body['name'] == 'etfs' and body['source'] == 'DATA_SOURCE_ALPACA_API' and body['createdAt']
    version = body['currentVersion']
    assert version['versionNo'] == 1 and 'effectivePass' not in version
    assert version['actor'] == OPERATOR_PRINCIPAL != PANEL_OWNER and version['reason'] == 'first'
    assert version['settings'] == {'enabled': True, 'cadence': 'PANEL_CADENCE_MONTHLY'}
    source = body['currentSourceVersion']
    assert source['versionNo'] == 1 and source['actor'] == OPERATOR_PRINCIPAL and 'effectivePass' not in source
    assert source['settings'] == {
        'rampDatasetsPerHour': 2,
        'requestCapPerHour': 10,
        'corporateActionWindowPreDays': 90,
        'corporateActionWindowPostDays': 90,
    }
    assert body.get('counts', {}) == {}
    (panel,) = await world.rows(PANELS)
    assert body['id'] == str(panel.id)
    counts = await world.counts()
    assert (counts['panel'], counts['panel_settings_version'], counts['panel_source_settings_version']) == (1, 1, 1)
    assert counts['panel_pass'] == 0 and counts['dataset_override_log'] == 0
    assert world.published.events == []


async def test_w1_keeps_a_named_cadence_and_a_given_window_day_count_and_an_absent_ramp_is_no_limit(world, client):
    """tj-2ayia2: 'ramp 0 pauses creation' is retired; an absent ramp is NULL and is ABSENT from the JSON read back."""
    body = await created(
        await post(
            client,
            CREATE,
            PANEL_BODY
            | {'cadence': 'PANEL_CADENCE_WEEKLY', 'requestCapPerHour': 4}
            | {'corporateActionWindowPreDays': 7, 'allowSymbols': ['SPY'], 'denySymbols': ['QQQ']}
            | {'assetClass': 'ASSET_TYPE_STOCK', 'exchange': 'ARCA', 'active': True, 'tradable': False},
        )
    )
    assert body['currentVersion']['settings'] == {
        'enabled': True,
        'cadence': 'PANEL_CADENCE_WEEKLY',
        'allowSymbols': ['SPY'],
        'denySymbols': ['QQQ'],
        'assetClass': 'ASSET_TYPE_STOCK',
        'exchange': 'ARCA',
        'active': True,
        'tradable': False,
    }
    assert body['currentSourceVersion']['settings'] == {
        'requestCapPerHour': 4,
        'corporateActionWindowPreDays': 7,
        'corporateActionWindowPostDays': 90,
    }
    (version,) = await world.rows(SOURCE_SETTINGS)
    assert version.ramp_datasets_per_hour is None and version.request_cap_per_hour == 4


@pytest.mark.parametrize(
    ('change', 'stored', 'json_settings'),
    [
        ({}, (None, None, 90, 90), {'corporateActionWindowPreDays': 90, 'corporateActionWindowPostDays': 90}),
        (
            {'corporateActionWindowPreDays': 5},
            (None, None, 5, 90),
            {'corporateActionWindowPreDays': 5, 'corporateActionWindowPostDays': 90},
        ),
        (
            {'corporateActionWindowPostDays': 5, 'rampDatasetsPerHour': 1},
            (1, None, 90, 5),
            {'rampDatasetsPerHour': 1, 'corporateActionWindowPreDays': 90, 'corporateActionWindowPostDays': 5},
        ),
        (
            {'rampDatasetsPerHour': 1},
            (1, None, 90, 90),
            {'rampDatasetsPerHour': 1, 'corporateActionWindowPreDays': 90, 'corporateActionWindowPostDays': 90},
        ),
        (
            {'requestCapPerHour': 2},
            (None, 2, 90, 90),
            {'requestCapPerHour': 2, 'corporateActionWindowPreDays': 90, 'corporateActionWindowPostDays': 90},
        ),
        (
            {'rampDatasetsPerHour': 2147483647, 'requestCapPerHour': 2147483647},
            (2147483647, 2147483647, 90, 90),
            {
                'rampDatasetsPerHour': 2147483647,
                'requestCapPerHour': 2147483647,
                'corporateActionWindowPreDays': 90,
                'corporateActionWindowPostDays': 90,
            },
        ),
    ],
    ids=['nothing-given', 'window-only', 'window-and-ramp', 'ramp-only', 'cap-only-minimum', 'int32-max'],
)
async def test_w1_on_a_source_with_no_version_writes_its_version_1_whatever_13_16_carry(
    world, client, change, stored, json_settings
):
    """tj-2ayia2 D4: these were 422s under the reversed ruling. An absent rate is NULL and ABSENT from the JSON."""
    body = await created(await post(client, CREATE, PANEL_BODY | change))

    source = body['currentSourceVersion']
    assert source['versionNo'] == 1 and source['actor'] == OPERATOR_PRINCIPAL and source['reason'] == 'first'
    assert source['settings'] == json_settings
    (row,) = await world.rows(SOURCE_SETTINGS)
    assert (
        row.ramp_datasets_per_hour,
        row.request_cap_per_hour,
        row.corporate_action_window_pre_days,
        row.corporate_action_window_post_days,
    ) == stored
    counts = await world.counts()
    assert (counts['panel'], counts['panel_settings_version']) == (1, 1)


OUT_OF_RANGE_RATES = [
    pytest.param({'rampDatasetsPerHour': 0}, id='ramp-zero'),
    pytest.param({'requestCapPerHour': 0}, id='cap-zero'),
    pytest.param({'rampDatasetsPerHour': 0, 'requestCapPerHour': 0}, id='both-zero'),
    pytest.param({'requestCapPerHour': 1}, id='cap-one'),
    pytest.param({'rampDatasetsPerHour': -1}, id='ramp-negative'),
    pytest.param({'rampDatasetsPerHour': 2147483648}, id='ramp-above-int32'),
    pytest.param({'requestCapPerHour': 2147483648}, id='cap-above-int32'),
]


@pytest.mark.parametrize('rates', OUT_OF_RANGE_RATES)
async def test_w1_a_present_zero_a_cap_of_one_or_a_value_above_int32_is_a_422_writing_nothing(world, client, rates):
    before = await world.counts()
    problem(await post(client, CREATE, PANEL_BODY | rates), status=422, reason='INVALID_REQUEST')
    await nothing_written(world, before)


@pytest.mark.parametrize('rates', OUT_OF_RANGE_RATES)
async def test_w3_a_present_zero_a_cap_of_one_or_a_value_above_int32_is_a_422_writing_nothing(world, client, rates):
    await world.source_version(SOURCE)
    before = await world.counts()
    problem(await post(client, SOURCE_PATH, SOURCE_BODY | rates), status=422, reason='INVALID_REQUEST')
    await nothing_written(world, before)


@pytest.mark.parametrize(
    'absent', [('rampDatasetsPerHour',), ('requestCapPerHour',), ('rampDatasetsPerHour', 'requestCapPerHour')]
)
async def test_w3_an_absent_rate_is_no_limit_stored_null_and_absent_from_the_json(world, client, absent):
    await world.source_version(SOURCE)
    sent = {key: value for key, value in SOURCE_BODY.items() if key not in absent}

    body = await created(await post(client, SOURCE_PATH, sent))

    assert body['versionNo'] == 2
    assert not set(absent) & set(body['settings']), body['settings']
    assert body['settings'] == {key: sent[key] for key in body['settings']}
    (row,) = [row for row in await world.rows(SOURCE_SETTINGS) if row.version_no == 2]
    for key, column in (
        ('rampDatasetsPerHour', 'ramp_datasets_per_hour'),
        ('requestCapPerHour', 'request_cap_per_hour'),
    ):
        assert getattr(row, column) == (None if key in absent else SOURCE_BODY[key]), column
    listed = (await client.get(SOURCE_PATH)).json()['items'][0]
    assert listed['versionNo'] == 2 and not set(absent) & set(listed['settings']), listed


async def test_w1_on_a_source_that_has_a_version_needs_no_source_settings(world, client):
    await world.source_version(SOURCE, reason='set earlier')
    body = await created(await post(client, CREATE, PANEL_BODY))
    assert body['currentSourceVersion']['reason'] == 'set earlier'
    assert len(await world.rows(SOURCE_SETTINGS)) == 1


@pytest.mark.parametrize(
    ('change', 'why'),
    [
        # tj-2ayia2: a window without both rates, the ramp alone and nothing given are 201s now (pinned above).
        ({'requestCapPerHour': 1}, 'the cap alone, under the minimum 2'),
        (RATES | {'name': '   '}, 'a blank name'),
        (RATES | {'size': 25}, 'a size field (removed: field 3 is reserved)'),
        (RATES | {'size': 0}, 'a zero size field (presence, not value)'),
        (RATES | {'rampDatasetsPerHour': -1}, 'a negative rate'),
        (RATES | {'source': 'DATA_SOURCE_UNSPECIFIED'}, 'no source'),
        ({key: value for key, value in (PANEL_BODY | RATES).items() if key != 'enabled'}, 'no enabled'),
        (RATES | {'owner': 'panel'}, 'an owner field (unknown)'),
        (RATES | {'cadence': 'PANEL_CADENCE_HOURLY'}, 'an unknown cadence name'),
    ],
)
async def test_w1_refusals_are_422_and_write_nothing(world, client, change, why):
    body = PANEL_BODY | change
    if 'enabled' not in change and why == 'no enabled':
        body.pop('enabled')
    before = await world.counts()
    problem(await post(client, CREATE, body), status=422, reason='INVALID_REQUEST')
    await nothing_written(world, before)


async def test_w1_a_used_name_is_a_422_with_the_stores_message(world, client):
    await created(await post(client, CREATE, PANEL_BODY | RATES))
    before = await world.counts()
    answer = problem(await post(client, CREATE, PANEL_BODY | {'name': ' etfs '}), status=422, reason='INVALID_REQUEST')
    assert 'already exists' in answer['detail']
    await nothing_written(world, before)


async def test_w1_source_settings_for_a_source_that_has_a_version_are_the_stores_422(world, client):
    """The store refuses, nothing written (ruling on tj-7zjov1.209), as panels.proto's 13-16 comment says (tj-7zjov1.245)."""
    await world.source_version(SOURCE)
    before = await world.counts()
    answer = problem(await post(client, CREATE, PANEL_BODY | RATES), status=422, reason='INVALID_REQUEST')
    assert 'already has settings' in answer['detail']
    await nothing_written(world, before)


@pytest.mark.parametrize('body', ['', 'not json', '[]', '{"name": 3}'])
async def test_w1_a_malformed_body_is_a_422(world, client, body):
    problem(await post(client, CREATE, body), status=422, reason='INVALID_REQUEST')
    assert await world.counts() == dict.fromkeys(await world.counts(), 0)


@pytest.mark.parametrize('headers', [None, {INSTANCE_SECRET_HEADER: 'wrong'}, {INSTANCE_SECRET_HEADER: ''}])
async def test_w1_without_the_secret_is_a_401_before_the_body_is_read(world, client, headers):
    for body in (PANEL_BODY | RATES, 'not json'):
        problem(await post(client, CREATE, body, headers=headers), status=401)
    assert await world.counts() == dict.fromkeys(await world.counts(), 0)


async def test_w1_is_a_401_when_the_deployment_has_no_secret(world, client, monkeypatch):
    monkeypatch.delenv(INSTANCE_SECRET_ENV_VAR)
    problem(await post(client, CREATE, PANEL_BODY | RATES), status=401)
    assert await world.counts() == dict.fromkeys(await world.counts(), 0)


# --------------------------------------------------------------------------------------------- W2 settings version


async def test_w2_appends_the_next_version_as_sent_and_disabling_shows_in_the_list(world, client):
    seeded = await world.panel('etfs')

    body = await created(await post(client, settings_path(seeded.panel_id), SETTINGS_BODY | {'exchange': 'NYSE'}))

    assert body['versionNo'] == 2 and 'effectivePass' not in body and body['savedAt']
    assert body['actor'] == OPERATOR_PRINCIPAL and body['reason'] == 'paused for review'
    assert body['settings'] == {'cadence': 'PANEL_CADENCE_WEEKLY', 'exchange': 'NYSE'}
    (listed,) = (await client.get('/ui/v1/panels')).json()['items']
    assert not listed.get('enabled', False)
    assert len(await world.rows(SETTINGS)) == 2
    assert await world.rows(OVERRIDES) == [] and await world.rows(PASSES) == []
    assert world.published.events == []
    again = await created(await post(client, settings_path(seeded.panel_id), SETTINGS_BODY))
    assert again['versionNo'] == 3  # not idempotent: a repeat appends another


async def test_w2_order_secret_then_body_then_panel_then_store_refusal(world, client):
    seeded = await world.panel('etfs')
    unknown = settings_path(uuid.uuid4())
    known = settings_path(seeded.panel_id)
    before = await world.counts()

    problem(await post(client, unknown, 'not json', headers=None), status=401)
    problem(await post(client, settings_path('not-a-uuid'), 'not json', headers=None), status=401)
    problem(await post(client, unknown, 'not json'), status=422, reason='INVALID_REQUEST')
    problem(await post(client, unknown, {'reason': 'x'}), status=422, reason='INVALID_REQUEST')
    problem(await post(client, unknown, SETTINGS_BODY | {'size': 30}), status=422, reason='INVALID_REQUEST')
    problem(await post(client, unknown, SETTINGS_BODY | {'reason': ' '}), status=404, reason='NOT_FOUND')
    problem(await post(client, unknown, SETTINGS_BODY), status=404, reason='NOT_FOUND')
    problem(await post(client, settings_path('not-a-uuid'), SETTINGS_BODY), status=422, reason='INVALID_REQUEST')
    for refused in ({'size': 30}, {'cadence': 'PANEL_CADENCE_UNSPECIFIED'}):
        problem(await post(client, known, SETTINGS_BODY | refused), status=422, reason='INVALID_REQUEST')
    body = {key: value for key, value in SETTINGS_BODY.items() if key != 'enabled'}
    problem(await post(client, known, body), status=422, reason='INVALID_REQUEST')
    problem(await post(client, known, SETTINGS_BODY | {'owner': 'panel'}), status=422, reason='INVALID_REQUEST')
    await nothing_written(world, before)


# --------------------------------------------------------------------------------------------- W3 source settings


async def test_w3_appends_the_sources_next_version(world, client):
    await world.source_version(SOURCE)
    body = await created(await post(client, SOURCE_PATH, SOURCE_BODY))
    assert body['versionNo'] == 2 and body['source'] == 'DATA_SOURCE_ALPACA_API' and 'effectivePass' not in body
    assert body['actor'] == OPERATOR_PRINCIPAL and body['reason'] == 'slower' and body['savedAt']
    assert body['settings'] == {
        'rampDatasetsPerHour': 2,
        'requestCapPerHour': 10,
        'corporateActionWindowPreDays': 30,
        'corporateActionWindowPostDays': 7,
    }
    first = await created(await post(client, '/ui/v1/panel-sources/ib_api/settings-versions', SOURCE_BODY))
    assert first['versionNo'] == 1 and first['source'] == 'DATA_SOURCE_IB_API'
    assert await world.rows(OVERRIDES) == [] and world.published.events == []
    assert {row.source for row in await world.rows(SOURCE_SETTINGS)} == {SOURCE, NO_CALENDAR_SOURCE}


async def test_w3_order_secret_then_source_or_body_then_store_refusal(world, client):
    before = await world.counts()
    bad_source = '/ui/v1/panel-sources/DATA_SOURCE_UNSPECIFIED/settings-versions'
    problem(await post(client, bad_source, 'not json', headers=None), status=401)
    problem(await post(client, SOURCE_PATH, SOURCE_BODY, headers=None), status=401)
    problem(await post(client, bad_source, SOURCE_BODY), status=422, reason='INVALID_REQUEST')
    problem(await post(client, '/ui/v1/panel-sources/ALPACA/settings-versions', SOURCE_BODY), status=422)
    problem(await post(client, SOURCE_PATH, 'not json'), status=422, reason='INVALID_REQUEST')
    # The window day counts stay required; an absent RATE is no limit now (tj-2ayia2), pinned in its own test.
    for missing in ('corporateActionWindowPreDays', 'corporateActionWindowPostDays'):
        body = {key: value for key, value in SOURCE_BODY.items() if key != missing}
        problem(await post(client, SOURCE_PATH, body), status=422, reason='INVALID_REQUEST')
    for refused in ({'requestCapPerHour': -1}, {'requestCapPerHour': 1}, {'source': 'DATA_SOURCE_IB_API'}):
        problem(await post(client, SOURCE_PATH, SOURCE_BODY | refused), status=422, reason='INVALID_REQUEST')
    await nothing_written(world, before)


# --------------------------------------------------------------------------------------------- no reason (tj-46jkey)


def _without_reason(body: dict[str, Any], reason: str | None) -> dict[str, Any]:
    trimmed = {key: value for key, value in body.items() if key != 'reason'}
    return trimmed if reason is None else trimmed | {'reason': reason}


@pytest.mark.parametrize('reason', [None, '', '   '], ids=['absent', 'empty', 'blank'])
async def test_w1_with_no_reason_is_a_201_storing_null_for_both_versions(world, client, reason):
    body = await created(await post(client, CREATE, _without_reason(PANEL_BODY, reason) | RATES))

    assert 'reason' not in body['currentVersion'] and 'reason' not in body['currentSourceVersion']
    assert body['currentVersion']['actor'] == OPERATOR_PRINCIPAL
    ((version,), (source,)) = (await world.rows(SETTINGS), await world.rows(SOURCE_SETTINGS))
    assert version.reason is None and source.reason is None, 'a blank reason is stored NULL, never an empty string'


@pytest.mark.parametrize('reason', [None, '', '   '], ids=['absent', 'empty', 'blank'])
async def test_w2_with_no_reason_is_a_201_storing_null(world, client, reason):
    seeded = await world.panel('etfs')
    body = await created(await post(client, settings_path(seeded.panel_id), _without_reason(SETTINGS_BODY, reason)))
    assert body['versionNo'] == 2 and 'reason' not in body and body['actor'] == OPERATOR_PRINCIPAL
    (stored,) = [row for row in await world.rows(SETTINGS) if row.version_no == 2]
    assert stored.reason is None


@pytest.mark.parametrize('reason', [None, '', '   '], ids=['absent', 'empty', 'blank'])
async def test_w3_with_no_reason_is_a_201_storing_null(world, client, reason):
    await world.source_version(SOURCE)
    body = await created(await post(client, SOURCE_PATH, _without_reason(SOURCE_BODY, reason)))
    assert body['versionNo'] == 2 and 'reason' not in body and body['actor'] == OPERATOR_PRINCIPAL
    (stored,) = [row for row in await world.rows(SOURCE_SETTINGS) if row.version_no == 2]
    assert stored.reason is None
