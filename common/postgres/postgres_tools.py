import time
import os
import psycopg2

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

__POSTGRES_INSTANCE = {}

def wait_for_db(url: str, timeout: int):
    start_time = time.time()
    while True:
        try:
            conn = psycopg2.connect(url)
            conn.close()
            print("Database is ready!")
            break
        except psycopg2.OperationalError:
            elapsed_time = time.time() - start_time
            if elapsed_time >= timeout:
                print("Failed to connect to Kafka after {} seconds.".format(timeout))
                return False
            print("Waiting for Kafka to be ready...")
            time.sleep(5)
    return True

def get_instance(host: str, port: int, timeout: int):
    url = f"{host}:{port}"
    if url not in __POSTGRES_INSTANCE and wait_for_db(url, port, timeout):
        engine = create_engine(url)
        session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        __POSTGRES_INSTANCE[url] = (engine, session)
    return __POSTGRES_INSTANCE[url][1]