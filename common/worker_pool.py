from concurrent.futures import ThreadPoolExecutor

from common.environment import get_env_var

# Configure Worker Threads
EXECUTOR_WORKERS = get_env_var('EXECUTOR_WORKERS', cast_type=int)


class SharedWorkerPool:
    """Shared Worker Pool for the application."""
    __executor = None

    @classmethod
    def worker_startup(cls):
        """Initialize the worker pool."""
        if cls.__executor is None:
            cls.__executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)

    @classmethod
    def worker_shutdown(cls):
        """Shutdown the worker pool."""
        cls.__executor.shutdown()

    @classmethod
    def get_instance(cls) -> ThreadPoolExecutor:
        """Get the worker pool instance."""
        return cls.__executor
