from fastapi import FastAPI
from typing import TYPE_CHECKING

from common.enums.config_enum import RunMode
from common.environment import get_env_var, get_run_mode
from common.logging import get_logger

if TYPE_CHECKING:
    from starlette.routing import Route

log = get_logger(__name__)


def startup_logs(app: FastAPI):
    """Startup logs for the app

    Args:
        app (FastAPI): The FastAPI app
    """
    log.info("Starting up app...")
    log.info(f"App run mode: {get_run_mode()}")
    log.info("Routes:")
    route: 'Route'
    for route in app.routes:
        log.info(f"  Path: {route.path}, Method(s): {route.methods}, Name: {route.name}")


def init_debugger():
    """Initialize debug mode"""
    if get_run_mode() is RunMode.DEV:
        debug_internal_host = get_env_var("APP_INTERNAL_DEBUG_HOST", cast_type=str)
        debug_internal_port = get_env_var("APP_INTERNAL_DEBUG_PORT", cast_type=int)
        log.info(f"Initializing debugger to port {debug_internal_port}...")
        import debugpy
        debugpy.listen((debug_internal_host, debug_internal_port))


def teardown_logs(app: FastAPI):
    """Teardown logs for the app

    Args:
        app (FastAPI): The FastAPI app
    """
    log.info("Shutting down app...")
