from enum import StrEnum

from common.environment import get_env_var


APP_NAME = get_env_var('DATA_STORE_NAME')
APP_PORT = get_env_var('DATA_STORE_PORT', cast_type=int)
APP_PORT_INTERNAL = get_env_var('APP_INTERNAL_PORT', cast_type=int)

# The five field-description constants that used to sit here now live in
# schemas/data_store/field_descriptions.py (tj-iontkq.3). They are OpenAPI contract text, and their
# only consumers are the schemas models, which had to import this module -- a router -- to reach
# them. Declare new description text there, not here.


class AssetDataInterface(StrEnum):
    POST_ASSET_DATA = '/internal/asset-data/{asset_type}/{data_type}'
    GET_ASSET_DATA = '/internal/asset-data/{asset_type}/{data_type}'


class AssetDatasetStoreInterface(StrEnum):
    POST_STORE_ASSET_DATASET = '/store/{asset_type}/{data_type}/{asset_symbol}'
    GET_STORE_ASSET_DATASET = '/store/{asset_type}/{data_type}/{asset_symbol}'

    GET_STORE_ASSET_DATASET_BY_ID = '/store/{id}'
    DELETE_STORE_ASSET_DATASET_BY_ID = '/store/{id}'


class UiDatasetsInterface(StrEnum):
    """The /ui/v1 read routes of the Data section (tj-grna9p.20). Served as protobuf canonical JSON."""

    GET_UI_DATASETS = '/ui/v1/datasets'
    # Declared and registered BEFORE the by-id path: a router matches in order, and 'facets' would
    # otherwise be taken as a dataset id.
    GET_UI_DATASET_FACETS = '/ui/v1/datasets/facets'
    GET_UI_DATASET = '/ui/v1/datasets/{dataset_id}'
    GET_UI_DATASET_BARS = '/ui/v1/datasets/{dataset_id}/bars'
    GET_UI_DATASET_BAR_PAGES = '/ui/v1/datasets/{dataset_id}/bar-pages'
    GET_UI_DATASET_RUNS = '/ui/v1/datasets/{dataset_id}/runs'
    GET_UI_DATASET_COVERAGE = '/ui/v1/datasets/{dataset_id}/coverage'


class UiStatsInterface(StrEnum):
    """The /ui/v1 usage and storage statistics (tj-grna9p.64). Open reads; none records usage."""

    GET_UI_USAGE_SUMMARY = '/ui/v1/stats/usage/summary'
    GET_UI_USAGE_LAST_READ_BUCKETS = '/ui/v1/stats/usage/last-read-buckets'
    GET_UI_USAGE_READS_PER_DAY = '/ui/v1/stats/usage/reads-per-day'
    GET_UI_USAGE_STALE = '/ui/v1/stats/usage/stale'
    GET_UI_STORAGE_CURRENT = '/ui/v1/stats/storage/current'


class UiDatasetWritesInterface(StrEnum):
    """The /ui/v1 expiry and delete writes (tj-grna9p.86). They take the instance secret."""

    PATCH_UI_DATASET_EXPIRY = '/ui/v1/datasets/{dataset_id}/expiry'
    DELETE_UI_DATASET = '/ui/v1/datasets/{dataset_id}'


class UiReaperInterface(StrEnum):
    """The expiry reaper's pending-deletions read (tj-grna9p.23). Open, like the other /ui/v1 GETs."""

    GET_UI_REAPER_PENDING = '/ui/v1/reaper/pending'


class UiConfigInterface(StrEnum):
    """The shell's configuration read (tj-grna9p.45). Served as protobuf canonical JSON."""

    GET_UI_CONFIG = '/ui/v1/config'


class UiEventsInterface(StrEnum):
    """The UI's live-event socket (tj-grna9p.73). A WebSocket, so it appears under no HTTP method."""

    UI_EVENTS = '/ui/v1/events'
