from typing import Any

from fastapi import FastAPI

from data.store.app.app_depends import lifespan
from data.store.app.openapi_pruning import prune_unreferenced_schemas
from routers.common import ping
from routers.common.errors import PROBLEM_RESPONSES, install_error_handlers
from routers.data_store import asset_dataset_store, internal_asset_data, ui_config, ui_datasets


# EVERY NON-2xx THIS SERVICE ANSWERS IS problem+json (ADR tj-fa1rpu D1(c), TE-6). responses=... is
# what declares that in OpenAPI -- each status a reason renders as, 500, and a default -- and
# install_error_handlers is what makes the running service agree with the document: a TraderJoeError
# renders from its reason's row, FastAPI's request validation becomes a 422 INVALID_REQUEST, an
# HTTPException keeps its own status, and anything else is a 500 carrying only an error_id. One set
# per app, installed here at the composition root and nowhere else.
app = FastAPI(lifespan=lifespan, responses=PROBLEM_RESPONSES)
install_error_handlers(app)
app.include_router(ping.router)
app.include_router(internal_asset_data.router)
app.include_router(asset_dataset_store.router)
app.include_router(ui_config.router)
app.include_router(ui_datasets.router)

_generate_openapi = app.openapi


def _openapi_with_pruned_schemas() -> dict[str, Any]:
    """`app.openapi()`, with any component no `$ref` in the document reaches removed (tj-1b3aer).

    Wraps FastAPI's own `openapi()` instead of reimplementing it, so title, version, routes, and
    FastAPI's own `openapi_schema` cache all stay exactly as FastAPI would produce them -- pruning
    runs on every call, including every request that serves the OpenAPI route. It is idempotent
    on FastAPI's cached document, and each call costs one walk of the document, which is
    negligible.

    Returns:
        dict[str, Any]: The OpenAPI document, without any component nothing references.
    """
    schema = _generate_openapi()
    prune_unreferenced_schemas(schema)
    return schema


app.openapi = _openapi_with_pruned_schemas
