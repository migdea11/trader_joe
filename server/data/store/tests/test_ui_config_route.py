"""GET /ui/v1/config: what the UI shell is told about this deployment (validator, gating tj-grna9p.45).

THE CONTRACT, from the bead's phase 1 SPLIT note and ADR tj-grna9p.10: allowed_groups is SIMULATION and only
SIMULATION until PR 5; deployment_label comes from DEPLOYMENT_LABEL, for display only; server_version comes from
SERVER_VERSION, else the installed distribution's version, else 'unknown'. The browser reads it, so nothing that is a
credential, a key, a DSN or an address may appear in it, whatever the process environment holds. Reads are open, like
the other GETs (the edge auth, tj-grna9p.6, protects them in production).

WHAT TIER THIS IS. The real app and handler through TestClient, never entered as a context manager, so no lifespan
runs; the route touches no database. The environment is set per test with monkeypatch, which works because the
config reads it per request.
"""

from importlib import metadata

import pytest
from fastapi.testclient import TestClient
from google.protobuf import json_format
from starlette.routing import Match

import data.store.app.ui_config as ui_config_module
from data.store.app.database.database import async_db
from data.store.app.main import app
from routers.common.instance_secret import INSTANCE_SECRET_ENV_VAR, require_instance_secret
from routers.data_store.ui_mapping import UiConfigMessage


pytestmark = pytest.mark.data_store

URL = '/ui/v1/config'


def served_routes() -> list:
    """The app's routes in the order a request is matched against them, the included routers' flattened in place.

    main.py includes every router without a prefix, so an included router's own routes match as declared.
    """
    flat = []
    for route in app.router.routes:
        included = getattr(route, 'original_router', None)
        flat.extend(included.routes if included is not None else [route])
    return flat


CONFIG_ENV = ('DEPLOYMENT_LABEL', 'SERVER_VERSION')


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client over the store app with neither config variable set; each test sets what it needs."""
    for name in CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    return TestClient(app)


def body_of(client: TestClient) -> dict:
    response = client.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('application/json')
    # Canonical JSON of the declared message: it parses back as a UiConfig and nothing else is in it.
    json_format.Parse(response.text, UiConfigMessage())
    return response.json()


def test_the_default_call_serves_simulation_and_nothing_else(client):
    """SIMULATION is the only group until PR 5 (ADR tj-grna9p.10): not PAPER, not LIVE, not an empty list."""
    assert body_of(client)['allowedGroups'] == ['ACCOUNT_GROUP_SIMULATION']


@pytest.mark.parametrize(
    ('label', 'served'),
    [('staging', 'staging'), ('  home lab \n', 'home lab'), ('PROD', 'PROD')],
    ids=['plain', 'trimmed', 'any text, display only'],
)
def test_the_deployment_label_is_served_trimmed(client, monkeypatch, label, served):
    monkeypatch.setenv('DEPLOYMENT_LABEL', label)
    assert body_of(client)['deploymentLabel'] == served


@pytest.mark.parametrize('label', [None, '', '   '], ids=['unset', 'empty', 'whitespace'])
def test_no_label_is_absent_from_the_body(client, monkeypatch, label):
    """An empty string is proto3's default, so canonical JSON omits it rather than sending ''."""
    if label is not None:
        monkeypatch.setenv('DEPLOYMENT_LABEL', label)
    assert 'deploymentLabel' not in body_of(client)


def test_the_deployment_label_changes_nothing_else(client, monkeypatch):
    """Display only (tj-0rpt9t): a label that reads like a mode does not move the groups."""
    monkeypatch.setenv('DEPLOYMENT_LABEL', 'LIVE')
    assert body_of(client)['allowedGroups'] == ['ACCOUNT_GROUP_SIMULATION']


def test_server_version_from_the_environment_wins(client, monkeypatch):
    monkeypatch.setenv('SERVER_VERSION', '4.2.0+build.7')
    monkeypatch.setattr(ui_config_module.metadata, 'version', lambda name: '9.9.9')
    assert body_of(client)['serverVersion'] == '4.2.0+build.7'


def test_server_version_falls_back_to_the_installed_distribution(client, monkeypatch):
    asked: list[str] = []

    def version(name: str) -> str:
        asked.append(name)
        return '0.1.0'

    monkeypatch.setattr(ui_config_module.metadata, 'version', version)
    assert body_of(client)['serverVersion'] == '0.1.0'
    assert asked == ['trader-joe']


@pytest.mark.parametrize('configured', [None, ''], ids=['unset', 'empty'])
def test_server_version_is_unknown_when_nothing_names_one(client, monkeypatch, configured):
    if configured is not None:
        monkeypatch.setenv('SERVER_VERSION', configured)

    def missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(ui_config_module.metadata, 'version', missing)
    assert body_of(client)['serverVersion'] == 'unknown'


# Credential-shaped values a deployment's environment really holds. Each is distinctive, so finding any of it in
# the body can only mean it was copied there.
CREDENTIALS = {
    INSTANCE_SECRET_ENV_VAR: 'instance-secret-5f1c2e',
    'DATABASE_URI': 'postgresql+asyncpg://store:pg-pass-8a7b@db.internal:5432/store',
    'ALPACA_API_KEY': 'PKALPACAKEY7F3Q',
    'ALPACA_API_SECRET': 'alpaca-secret-91d0',
    'DATA_INGEST_GRPC_TARGET': 'data-ingest.internal:50051',
    'API_TOKEN': 'tok-3e4f5a',
}


def test_no_credential_or_address_reaches_the_body(client, monkeypatch):
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('DEPLOYMENT_LABEL', 'lab')
    monkeypatch.setenv('SERVER_VERSION', '1.0.0')
    response = client.get(URL)
    assert set(response.json()) == {'allowedGroups', 'deploymentLabel', 'serverVersion'}
    for value in CREDENTIALS.values():
        assert value not in response.text
    for fragment in ('pg-pass', 'db.internal', '5432', '50051', 'secret', 'token', 'key'):
        assert fragment not in response.text.lower(), fragment


def test_the_route_is_open(client, monkeypatch):
    """A read, so no instance secret: answered with the secret configured and no header sent."""
    monkeypatch.setenv(INSTANCE_SECRET_ENV_VAR, 'instance-secret-5f1c2e')
    assert client.get(URL).status_code == 200
    (route,) = [route for route in served_routes() if route.path == URL]
    assert all(dependency.dependency is not require_instance_secret for dependency in route.dependant.dependencies)
    assert not route.dependencies


def test_openapi_records_the_message_and_no_field_schema():
    """ADR tj-grna9p.4 section 4: the route names its message; the .proto, not OpenAPI, is the schema."""
    operation = app.openapi()['paths'][URL]['get']
    assert operation['x-proto-message'] == 'trader_joe.proto.ui.v1.UiConfig'
    assert 'parameters' not in operation and 'requestBody' not in operation


def _route_name(path: str) -> str:
    scope = {'type': 'http', 'path': path, 'method': 'GET', 'root_path': ''}
    for route in served_routes():
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route.name
    raise AssertionError(f'nothing serves GET {path}')


@pytest.mark.parametrize(
    ('path', 'name'),
    [
        ('/ui/v1/config', 'get_ui_config'),
        ('/ui/v1/datasets', 'list_datasets'),
        ('/ui/v1/datasets/facets', 'get_dataset_facets'),
        ('/ui/v1/datasets/8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f', 'get_dataset'),
        ('/ui/v1/datasets/8f41c2d7-a3b9-4d1e-9c2f-0a1b2c3d4e5f/bars', 'get_dataset_bars'),
        # 'config' under datasets is a dataset id, not the config route.
        ('/ui/v1/datasets/config', 'get_dataset'),
    ],
)
def test_no_ui_route_shadows_another(path, name):
    assert _route_name(path) == name


def test_config_under_datasets_is_not_served_as_config(client):
    """The id route takes it and refuses it as a UUID: a 422, never a UiConfig. No session is ever used."""

    async def no_database():
        yield None

    app.dependency_overrides[async_db] = no_database
    try:
        assert client.get('/ui/v1/datasets/config').status_code == 422
    finally:
        app.dependency_overrides.clear()
