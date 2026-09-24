from concurrent.futures import ThreadPoolExecutor

from common.environment import get_env_var


# Configure Worker Threads
EXECUTOR_THREADS = get_env_var('EXECUTOR_THREADS', cast_type=int)


class SharedWorkerPool:
    """Shared Worker Pool for the application."""

    __executor = None

    @classmethod
    def worker_startup(cls):
        """Initialize the worker pool."""
        if cls.__executor is None:
            cls.__executor = ThreadPoolExecutor(max_workers=EXECUTOR_THREADS)

    @classmethod
    def worker_shutdown(cls):
        """Shutdown the worker pool and clear the reference so it can be rebuilt.

        Clearing is the point: leaving the class attribute pointing at a shut-down executor
        makes get_instance() hand back a pool that accepts no work, and worker_startup()
        a no-op, for any process that stops and restarts the pool in one lifetime.
        """
        if cls.__executor is None:
            return
        executor, cls.__executor = cls.__executor, None
        executor.shutdown()

    @classmethod
    def get_instance(cls) -> ThreadPoolExecutor:
        """Get the worker pool instance."""
        return cls.__executor
