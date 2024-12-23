import time
from typing import Dict
import psycopg2

from sqlalchemy import URL,  create_engine
from sqlalchemy.orm import sessionmaker, Session

from common.logging import get_logger

log = get_logger(__name__)


class SharedPostgresSession:
    __POSTGRES_INSTANCE: Dict[str, Session] = {}

    @classmethod
    def wait_for_db(cls, uri: URL, timeout: int, retry: int = 1) -> bool:
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
                time.sleep(retry)
        return True

    @classmethod
    def get_instance(cls, uri: URL, timeout: int) -> Session:
        uri_str = uri.render_as_string(hide_password=False)
        if uri_str not in cls.__POSTGRES_INSTANCE and cls.wait_for_db(uri, timeout):
            engine = create_engine(uri)
            session_maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            cls.__POSTGRES_INSTANCE[uri_str] = session_maker()

        if uri_str not in cls.__POSTGRES_INSTANCE:
            raise ConnectionError("Database startup timed out.")
        return cls.__POSTGRES_INSTANCE[uri_str]

    @classmethod
    def shutdown(cls):
        for session in cls.__POSTGRES_INSTANCE.values():
            session.close()
        cls.__POSTGRES_INSTANCE.clear()
        log.info("Postgres session shutdown.")
