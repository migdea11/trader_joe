from unittest.mock import MagicMock, patch

import pytest

from common.tests.fixtures.environment import mock_get_env_var


@pytest.fixture(autouse=True)
def set_env_vars():
    env_values = {'EXECUTOR_THREADS': (5, None, int)}
    _mock, patcher = mock_get_env_var(env_values)
    yield
    patcher.stop()  # Stop the patcher after the test


@pytest.fixture(autouse=True)
def reset_worker_pool():
    """Clear the SharedWorkerPool singleton around each test.

    The pool caches its executor on the class, so a pool left running by an
    earlier test would make worker_startup() a no-op here.
    """
    from common.worker_pool import SharedWorkerPool

    SharedWorkerPool._SharedWorkerPool__executor = None
    yield
    SharedWorkerPool._SharedWorkerPool__executor = None


# EXECUTOR_THREADS is read at import time (see common/CLAUDE.md, pitfall 1), so mocking
# get_env_var only takes effect if this module happens to be imported first. Patch the
# resolved constant instead, which holds however the suite is ordered.
@patch('common.worker_pool.EXECUTOR_THREADS', 5)
@patch('common.worker_pool.ThreadPoolExecutor', new_callable=MagicMock)
def test_worker_startup(mock_executor):
    mock_executor_instance = MagicMock()
    mock_executor.return_value = mock_executor_instance

    from common.worker_pool import SharedWorkerPool

    SharedWorkerPool.worker_startup()
    mock_executor.assert_called_once_with(max_workers=5)

    SharedWorkerPool.worker_shutdown()
    mock_executor_instance.shutdown.assert_called_once()
