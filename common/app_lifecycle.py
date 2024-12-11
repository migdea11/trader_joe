from fastapi import FastAPI

from common.logging import get_logger

log = get_logger(__name__)


def startup_logs(app: FastAPI):
    log.info("Starting up app...")
    log.info("Routes:")
    for route in app.routes:
        log.info(f"  Path: {route.path}, Method(s): {route.methods}, Name: {route.name}")


def teardown_logs(app: FastAPI):
    log.info("Shutting down app...")