"""A throwaway app for the problem+json edge (TE-3 tj-3mk3u5.37.4): the handler set installed, and nothing else.

routers.common.errors installs nothing, and no service app installs it yet (TE-4 and TE-6 do), so the tests drive
it through an app built here the way an app adopts it: FastAPI(responses=PROBLEM_RESPONSES), then
install_error_handlers(app). Not a test module: test_problem_json_*.py import it.
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.errors.vocabulary import METADATA_KEYS
from routers.common.errors import PROBLEM_RESPONSES, install_error_handlers


PROBLEM_MEDIA_TYPE = 'application/problem+json'
ABOUT_BLANK = 'about:blank'
ERRORS_LOGGER = 'routers.common.errors'

RAISE_PATH = '/raise'

# Allowlisted, but TE-1 carries them on the reset_at attribute and refuses them through metadata (TE-1 ruling (c)).
DERIVED_KEYS = frozenset({'reset_at', 'retry_after'})
# The allowlisted keys an error carries through its metadata.
CARRIED_KEYS = sorted(METADATA_KEYS - DERIVED_KEYS)

# The two shapes TE-1 lets every carried key hold (tj-3mk3u5.37.3): a str, kept as it is, and a sequence of str,
# stored as a tuple. Each is paired with what the wire carries for it: the same string, or the same items as a
# JSON array. Neither shape is ever turned into the other.
METADATA_SHAPES: dict[str, tuple[str | tuple[str, ...], str | list[str]]] = {
    'str': ('a value', 'a value'),
    'sequence': (('one', 'two'), ['one', 'two']),
}
METADATA_SHAPE_CASES = [
    pytest.param(key, shape, id=f'{key}-{shape}') for key in CARRIED_KEYS for shape in METADATA_SHAPES
]


def problem_app() -> FastAPI:
    """Build an app with the problem+json handlers and responses, and no routes yet.

    Returns:
        FastAPI: The app.
    """
    app = FastAPI(responses=PROBLEM_RESPONSES)
    install_error_handlers(app)
    return app


def client_for(app: FastAPI) -> TestClient:
    """Build a client that returns what a caller receives, even for a bug.

    Starlette's ServerErrorMiddleware re-raises a bug after the 500 handler has answered. A caller sees only the
    answer, so the client is told not to re-raise it into the test.

    Args:
        app: The app to call.

    Returns:
        TestClient: The client.
    """
    return TestClient(app, raise_server_exceptions=False)


def answer_to(error: BaseException) -> httpx.Response:
    """Return what a caller receives from a route that raises error.

    Args:
        error: The exception the route raises.

    Returns:
        httpx.Response: The response.
    """
    app = problem_app()

    @app.get(RAISE_PATH)
    async def raise_it() -> None:
        raise error

    return client_for(app).get(RAISE_PATH)
