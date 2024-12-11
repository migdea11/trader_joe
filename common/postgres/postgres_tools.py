import time
from typing import Dict, Tuple
import psycopg2

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker, Session

from common.logging import get_logger

log = get_logger(__name__)

__POSTGRES_INSTANCE: Dict[str, Tuple[Engine, sessionmaker]] = {}


def wait_for_db(url: str, timeout: int) -> bool:
    start_time = time.time()
    while True:
        try:
            conn = psycopg2.connect(url)
            conn.close()
            log.info("Postgres is ready!")
            break
        except psycopg2.OperationalError:
            elapsed_time = time.time() - start_time
            if elapsed_time >= timeout:
                log.error("Failed to connect to Postgres after {} seconds.".format(timeout))
                return False
            log.debug("Waiting for Postgres to be ready...")
            time.sleep(5)
    return True


def get_instance(host: str, port: int, timeout: int) -> Session:
    url = f"{host}:{port}"
    if url not in __POSTGRES_INSTANCE and wait_for_db(url, port, timeout):
        engine = create_engine(url)
        session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        __POSTGRES_INSTANCE[url] = (engine, session_maker)
    engine, session_maker = __POSTGRES_INSTANCE[url]
    return session_maker(engine)
