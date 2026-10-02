from collections.abc import Mapping

from fastapi import FastAPI

from common.enums.data_stock import DataSource
from data.ingest.app.app_depends import make_lifespan
from data.ingest.app.brokers.alpaca.read import AlpacaRead
from data.ingest.app.brokers.interface import BrokerRead
from routers.common import ping
from routers.data_ingest import get_dataset_request


def create_app(readers: Mapping[DataSource, BrokerRead]) -> FastAPI:
    """Compose the ingest app around the broker handles it should serve requests through.

    The composition root: the lifespan installs these handles before the RPC servers start and
    clears them after shutdown.

    Args:
        readers (Mapping[DataSource, BrokerRead]): Handle serving each data source.

    Returns:
        FastAPI: The ingest app.
    """
    app = FastAPI(lifespan=make_lifespan(readers))
    app.include_router(ping.router)
    app.include_router(get_dataset_request.router)
    return app


app = create_app({DataSource.ALPACA_API: AlpacaRead()})
