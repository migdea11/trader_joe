import os
from concurrent.futures import ThreadPoolExecutor

# Configure Worker Threads
EXECUTOR_WORKERS = int(os.getenv('EXECUTOR_WORKERS'))
EXECUTOR = None


def worker_startup():
    global EXECUTOR
    EXECUTOR = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)


def worker_shutdown():
    global EXECUTOR
    EXECUTOR.shutdown()
