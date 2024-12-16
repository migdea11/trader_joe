from concurrent.futures import ThreadPoolExecutor

from common.environment import get_env_var

# Configure Worker Threads
EXECUTOR_WORKERS = get_env_var('EXECUTOR_WORKERS', is_num=True)


class SharedWorkerPool:
    __executor = None

    @classmethod
    def worker_startup(cls):
        if cls.__executor is None:
            cls.__executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)

    @classmethod
    def worker_shutdown(cls):
        cls.__executor.shutdown()

    @classmethod
    def get_instance(cls) -> ThreadPoolExecutor:
        return cls.__executor
