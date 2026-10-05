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
    """Clear the SharedWorkerPool singleton around each test, without using the code under test.

    The pool caches its executor on the class, so a pool left running by an earlier test would make
    worker_startup() a no-op here.

    RULING on tj-1bv25s AC3, which asked whether this private reach is still needed now that
    worker_shutdown() clears the reference itself: it stays, deliberately. A fixture that isolates
    tests must not be written in terms of the function those tests exercise. Rewriting it as
    worker_shutdown() was tried and measured: with the production fix reverted, both tests in this
    module errored at SETUP with an AttributeError, so the restart defect was hidden behind a broken
    fixture instead of being reported by the test that exists to catch it. What the fix does buy is
    that nothing else in the suite needs the private name -- see data/ingest/tests/test_rpc_smoke.py,
    whose worker_pool fixture now shuts the pool down through the public API in teardown.

    The executor is stopped rather than merely dropped, so no threads leak either way.
    """
    from common.worker_pool import SharedWorkerPool

    def clear():
        executor = SharedWorkerPool.get_instance()
        SharedWorkerPool._SharedWorkerPool__executor = None
        if executor is not None:
            executor.shutdown()

    clear()
    yield
    clear()


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


@patch('common.worker_pool.EXECUTOR_THREADS', 2)
def test_restart_yields_a_working_pool():
    """Pin tj-1bv25s: start / shutdown / start hands back a pool that actually runs work.

    A real ThreadPoolExecutor, not a mock, because the regression is invisible to a mock: when
    worker_shutdown() left the dead executor on the class, worker_startup() became a no-op and
    get_instance() handed every caller a pool that accepts no work. ``is not None`` does not catch
    that -- the object is there, it just runs nothing -- so the assertion is that submitted work
    comes back.
    """
    from common.worker_pool import SharedWorkerPool

    SharedWorkerPool.worker_startup()
    SharedWorkerPool.worker_shutdown()
    SharedWorkerPool.worker_startup()

    pool = SharedWorkerPool.get_instance()
    assert pool is not None, 'worker_startup() after a shutdown left no executor'
    assert pool.submit(str.upper, 'ran').result(timeout=5) == 'RAN'


def test_shutdown_on_a_never_started_pool_is_a_no_op():
    """The other half of tj-1bv25s: shutting down a pool nobody started must not raise.

    The app lifespan and every test fixture reach for worker_shutdown() without knowing whether the
    pool came up, so an AttributeError here would turn a failed startup into a second, louder
    failure on the way down.
    """
    from common.worker_pool import SharedWorkerPool

    assert SharedWorkerPool.get_instance() is None, 'the reset fixture left a pool behind'
    SharedWorkerPool.worker_shutdown()
