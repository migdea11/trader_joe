import time
from typing import Dict, Tuple
import psycopg2

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.orm import sessionmaker, Session

from common.logging import get_logger

log = get_logger(__name__)

__POSTGRES_INSTANCE: Dict[str, Tuple[Engine, sessionmaker]] = {}


def wait_for_db(uri: URL, timeout: int) -> bool:
    uri_str = uri.render_as_string(hide_password=False)
    start_time = time.time()
    while True:
        try:
            conn = psycopg2.connect(uri_str)
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


def get_instance(uri: URL, timeout: int) -> Session:
    uri_str = uri.render_as_string(hide_password=False)
    if uri not in __POSTGRES_INSTANCE and wait_for_db(uri, timeout):
        engine = create_engine(uri)
        session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        __POSTGRES_INSTANCE[uri_str] = session_maker
    session_maker = __POSTGRES_INSTANCE[uri_str]
    return session_maker()
