"""The request record's tables on a SQLite test engine (validator, tj-7zjov1.28).

WHY EVERY SQLITE TIER THAT READS RUNNING STATE NEEDS THEM. Since the push path (P35) a request is recorded as an
ingest_request row and Running is read from OPEN REQUESTS as well as legacy RUNNING runs (running_dataset_ids and
has_running_ingest in app/database/crud/ingest_run.py union the two tables). A fixture that created ingest_run alone
now fails every catalog, reaper and recorder read with "no such table: ingest_request".

THE ONE DIFFERENCE FROM THE MODEL. ingest_request.data_types is a Postgres ARRAY, which SQLite cannot create. The
tables are copied into a private MetaData and only the COPY's data_types column is JSON; the model's own table is
never mutated, so a test that compiles the model's DDL for Postgres is unaffected by import order. Statements the
store builds on the model's table compile against the copy by name, which is all SQLite needs.

ingest_instance is created beside it because the request path and the watcher read the marker; a tier that only
reads Running never touches it, and an empty table costs nothing.
"""

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest


def create_request_tables(sync_connection: Connection) -> None:
    """Create ingest_request (data_types as JSON) and ingest_instance on a SQLite connection.

    Args:
        sync_connection: The synchronous connection run_sync hands over.
    """
    metadata = sa.MetaData()
    requests = IngestRequest.__table__.to_metadata(metadata)
    requests.c.data_types.type = sa.JSON()
    IngestInstance.__table__.to_metadata(metadata)
    metadata.create_all(sync_connection)
