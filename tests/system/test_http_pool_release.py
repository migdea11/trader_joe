"""data_store returns its pooled connection after every read, over real HTTP (tj-vhboky.76.5, test 5).

Bug tj-vhboky.76: async_db never closed the request session, so every read pinned a pooled
connection. After pool_size + max_overflow = 5 + 10 = 15 reads the pool was exhausted and every
later request waited pool_timeout (30 s) and failed. Fixed by fca381f (ADR tj-8z213c).

This makes 45 sequential GETs -- three times the prod default pool of 15 -- against GET /store, a
read route that runs search_entries and nothing else. The symbol is this test's own ZZSYS symbol,
so the result is empty and nothing is created. Every request must answer 200 within
PER_REQUEST_TIMEOUT_SECONDS, well under the 30 s pool timeout, so a leak shows up as a timeout at
request 16 rather than as a slow pass.

The component-tier pins (data/store/tests/test_session_release.py) prove the same property on a
real pool without Postgres; this is the live-stack confirmation the host sitting runs
(tj-ijpys9.7 item 4).
"""

import time

import httpx
import pytest

from common.enums.data_select import AssetType, DataType
from routers.data_store.app_endpoints import AssetDatasetStoreInterface


pytestmark = pytest.mark.data_store

PROD_DEFAULT_POOL = 5 + 10  # create_async_engine defaults: pool_size + max_overflow
REQUESTS = 3 * PROD_DEFAULT_POOL
PER_REQUEST_TIMEOUT_SECONDS = 5.0


def test_sequential_reads_beyond_the_pool_all_answer_promptly(data_store, data_store_url: str, own_symbol: str):
    """45 sequential reads, each 200 within 5 s. The `data_store` fixture proves the stack answers first."""
    path = AssetDatasetStoreInterface.GET_STORE_ASSET_DATASET.format(
        asset_type=AssetType.STOCK.value, data_type=DataType.MARKET_ACTIVITY.value, asset_symbol=own_symbol
    )
    with httpx.Client(base_url=data_store_url, timeout=PER_REQUEST_TIMEOUT_SECONDS) as client:
        for attempt in range(1, REQUESTS + 1):
            started = time.monotonic()
            try:
                response = client.get(path)
            except httpx.TimeoutException as exc:
                pytest.fail(
                    f'GET {path} request {attempt} of {REQUESTS} did not answer within '
                    f'{PER_REQUEST_TIMEOUT_SECONDS} s ({type(exc).__name__}): the pool is exhausted '
                    f'if this is request {PROD_DEFAULT_POOL + 1} or later (tj-vhboky.76)',
                    pytrace=False,
                )
            elapsed = time.monotonic() - started
            assert response.status_code == 200, (
                f'GET {path} request {attempt} of {REQUESTS}: {response.status_code} after {elapsed:.2f} s '
                f'{response.text[:500]}'
            )
            assert response.json() == [], (
                f'request {attempt}: {own_symbol} should match no entry: {response.text[:500]}'
            )
