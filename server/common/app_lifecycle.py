from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from common.enums.config_enum import RunMode
from common.environment import get_env_var, get_run_mode
from common.logging import get_logger


log = get_logger(__name__)

# debugpy.listen() opens a socket that runs arbitrary code inside this process, so the
# fallback binds loopback. Reaching the debugger from outside the container takes two
# deliberate steps -- setting APP_INTERNAL_DEBUG_HOST to a wider address *and* publishing
# the port in compose -- and neither is a default.
DEFAULT_DEBUG_HOST = '127.0.0.1'
DEFAULT_DEBUG_PORT = 5678


def startup_logs(app: FastAPI):
    """Startup logs for the app.

    Args:
        app (FastAPI): The FastAPI app
    """
    log.info('Starting up app...')
    log.info(f'App run mode: {get_run_mode()}')
    log.info('Routes:')
    # fastapi 0.141 no longer puts the included routes in app.routes: each
    # include_router() call leaves a single _IncludedRouter placeholder, which is a
    # BaseRoute with no .path/.methods/.name. iter_route_contexts flattens those back
    # into the real endpoints with their prefixes applied -- it is what fastapi's own
    # OpenAPI generation uses (fastapi/openapi/utils.py), so it stays in step with how
    # the app actually routes. Skipping the placeholders instead would silently reduce
    # this log to /docs, /redoc and /openapi.json.
    for route in iter_route_contexts(app.routes):
        # Version-fragile, pinned to fastapi 0.141.1: a websocket route reached through
        # include_router() comes back with path/name blank (an HTTP one resolves fine).
        # original_route only carries the innermost router's prefix, so the fallback can
        # be a partial path -- flagged rather than printed as if it were the real one.
        # Recheck on the next fastapi bump; there are no websocket routes today.
        path = route.path or f'{getattr(route.original_route, "path", "?")} (unresolved)'
        methods = ', '.join(sorted(route.methods)) if route.methods else '-'
        name = route.name or getattr(route.original_route, 'name', '') or '-'
        log.info(f'  Path: {path}, Method(s): {methods}, Name: {name}')


def init_debugger():
    """Initialize debug mode."""
    if get_run_mode() is RunMode.DEV:
        debug_internal_host = get_env_var('APP_INTERNAL_DEBUG_HOST', default=DEFAULT_DEBUG_HOST, cast_type=str)
        debug_internal_port = get_env_var('APP_INTERNAL_DEBUG_PORT', default=DEFAULT_DEBUG_PORT, cast_type=int)
        log.info(f'Initializing debugger on {debug_internal_host}:{debug_internal_port}...')
        import debugpy

        debugpy.listen((debug_internal_host, debug_internal_port))


def teardown_logs(app: FastAPI):
    """Teardown logs for the app.

    Args:
        app (FastAPI): The FastAPI app
    """
    log.info('Shutting down app...')
