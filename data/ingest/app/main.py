from collections.abc import Mapping

from fastapi import FastAPI

from common.enums.data_stock import DataSource
from data.ingest.app.app_depends import make_lifespan
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.interface import BrokerRead
from routers.common import ping
from routers.common.errors import PROBLEM_RESPONSES, install_error_handlers
from routers.data_ingest import get_dataset_request


def create_app(readers: Mapping[DataSource, BrokerRead]) -> FastAPI:
    """Compose the ingest app around the broker handles it should serve requests through.

    The composition root: the lifespan installs these handles before the RPC servers start and
    clears them after shutdown. It also installs the problem+json handlers and declares their
    responses, one set per app (ADR tj-fa1rpu D1(c)). Its HTTP surface is /ping and the latency
    harness, and no status there changes.

    Args:
        readers (Mapping[DataSource, BrokerRead]): Handle serving each data source.

    Returns:
        FastAPI: The ingest app.
    """
    app = FastAPI(lifespan=make_lifespan(readers), responses=PROBLEM_RESPONSES)
    install_error_handlers(app)
    app.include_router(ping.router)
    app.include_router(get_dataset_request.router)
    return app


app = create_app({DataSource.ALPACA_API: AlpacaRead()})
