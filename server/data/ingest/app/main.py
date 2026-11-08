from collections.abc import Mapping

from fastapi import FastAPI

from common.enums.data_stock import DataSource
from data.ingest.app.app_depends import make_lifespan
from data.ingest.app.brokers.alpaca.handle import AlpacaHandle
from data.ingest.app.brokers.handle import BrokerHandle
from routers.common import ping
from routers.common.errors import PROBLEM_RESPONSES, install_error_handlers
from routers.data_ingest import get_dataset_request


def create_app(handles: Mapping[DataSource, BrokerHandle] | None = None) -> FastAPI:
    """Compose the ingest app around the broker handles it should serve requests through.

    The composition root: it decides which handle serves each data source, and the lifespan hands
    them to the gRPC servicers it registers. It also installs the problem+json handlers and declares
    their responses, one set per app (ADR tj-fa1rpu D1(c)). Its HTTP surface is /ping and the latency
    harness, and no status there changes.

    Args:
        handles (Mapping[DataSource, BrokerHandle] | None): The broker handles that queue, attempt and report
            Submit requests. Without them Submit is not served.

    Returns:
        FastAPI: The ingest app.
    """
    app = FastAPI(lifespan=make_lifespan(handles), responses=PROBLEM_RESPONSES)
    install_error_handlers(app)
    app.include_router(ping.router)
    app.include_router(get_dataset_request.router)
    return app


alpaca = AlpacaHandle()
app = create_app({DataSource.ALPACA_API: alpaca})
