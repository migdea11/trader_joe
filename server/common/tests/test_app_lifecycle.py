import logging

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.concurrency import asynccontextmanager
from fastapi.testclient import TestClient

from common.app_lifecycle import startup_logs, teardown_logs


LOGGER_NAME = 'common.app_lifecycle'


def _build_app() -> FastAPI:
    """Build an app whose endpoints all arrive through include_router().

    This is the shape that regressed: on fastapi 0.141 every include_router() call
    leaves a single _IncludedRouter placeholder in app.routes rather than the routes
    themselves, so an app built this way has no directly attached endpoints at all.

    Returns:
        FastAPI: App with a prefixed router, a nested router and a websocket.
    """
    nested = APIRouter(prefix='/internal')

    @nested.get('/asset-data/{symbol}')
    def get_asset_data(symbol: str):
        return {'symbol': symbol}

    store = APIRouter(prefix='/store')

    @store.post('/datasets')
    def create_dataset():
        return {}

    @store.websocket('/stream')
    async def stream(websocket):  # pragma: no cover - never connected to
        await websocket.accept()

    store.include_router(nested)

    # Mirrors the services: the lifespan logs the routes, so a failure here is a
    # startup failure, not a logging failure.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_logs(app)
        yield
        teardown_logs(app)

    app = FastAPI(lifespan=lifespan)
    app.include_router(store)
    return app


@pytest.fixture
def lifespan_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Run the app's lifespan start-to-finish and return what startup_logs emitted.

    Entering the TestClient context manager is what makes this a startup test: the
    bug it guards against was invisible to anything that only constructed the app.

    Args:
        caplog (pytest.LogCaptureFixture): Pytest log capture fixture.

    Returns:
        list[str]: The messages logged by common.app_lifecycle during the lifespan.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    app = _build_app()
    with TestClient(app):
        pass
    return [record.getMessage() for record in caplog.records if record.name == LOGGER_NAME]


def test_lifespan_completes_with_an_included_router(lifespan_logs: list[str]):
    # The regression raised AttributeError inside the lifespan, so reaching this at
    # all is half the assertion; the rest confirms both ends actually ran.
    assert any('Starting up app...' in message for message in lifespan_logs)
    assert any('Shutting down app...' in message for message in lifespan_logs)


def test_startup_logs_reports_included_routes_with_their_prefixes(lifespan_logs: list[str]):
    logged_paths = {message.split('Path: ', 1)[1].split(',', 1)[0] for message in lifespan_logs if 'Path: ' in message}

    # Prefixes must be resolved, and a router included into a router must be walked
    # through both levels.
    assert '/store/datasets' in logged_paths
    assert '/store/internal/asset-data/{symbol}' in logged_paths


def test_startup_logs_flags_a_route_fastapi_cannot_resolve(lifespan_logs: list[str]):
    # fastapi 0.141.1 hands back a blank path for a websocket reached through
    # include_router(). Pin the fallback: the route still appears and is marked, rather
    # than logging an empty path as though it were a real one. If a future fastapi
    # resolves this, this test fails and the fallback can go.
    entry = next(message for message in lifespan_logs if 'unresolved' in message)

    assert 'Path: /store/stream (unresolved)' in entry
    assert 'Name: stream' in entry


def test_startup_logs_is_not_reduced_to_the_docs_routes(lifespan_logs: list[str]):
    logged_paths = {message.split('Path: ', 1)[1].split(',', 1)[0] for message in lifespan_logs if 'Path: ' in message}

    # Skipping the entries without a .path would still let the lifespan complete, so
    # the above alone would not catch it -- only the docs routes would survive.
    assert logged_paths - {'/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json'}


def test_startup_logs_reports_methods_for_included_routes(lifespan_logs: list[str]):
    entry = next(message for message in lifespan_logs if 'Path: /store/datasets,' in message)

    assert 'Method(s): POST' in entry
