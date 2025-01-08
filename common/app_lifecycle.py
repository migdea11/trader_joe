from fastapi import FastAPI
from typing import TYPE_CHECKING

from common.logging import get_logger

if TYPE_CHECKING:
    from starlette.routing import Route

log = get_logger(__name__)


def startup_logs(app: FastAPI):
    log.info("Starting up app...")
    log.info("Routes:")
    route: 'Route'
    for route in app.routes:
        log.info(f"  Path: {route.path}, Method(s): {route.methods}, Name: {route.name}")


def teardown_logs(app: FastAPI):
    log.info("Shutting down app...")
