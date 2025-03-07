from unittest.mock import MagicMock, patch

import pytest

from common.tests.fixtures.environment import mock_get_env_var


@pytest.fixture(autouse=True)
def set_env_vars():
    env_values = {'EXECUTOR_THREADS': (5, None, int)}
    mock, patcher = mock_get_env_var(env_values)
    yield
    patcher.stop()  # Stop the patcher after the test


@patch('common.worker_pool.ThreadPoolExecutor', new_callable=MagicMock)
def test_worker_startup(mock_executor):
    mock_executor_instance = MagicMock()
    mock_executor.return_value = mock_executor_instance

    from common.worker_pool import SharedWorkerPool
    SharedWorkerPool.worker_startup()
    mock_executor.assert_called_once_with(max_workers=5)

    SharedWorkerPool.worker_shutdown()
    mock_executor_instance.shutdown.assert_called_once()
