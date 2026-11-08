from sqlalchemy import Column, DateTime, String, func

from common.database.sql_alchemy_table import AppBase, CustomTypeTable


class IngestInstance(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One ingest process the store has seen: the INSTANCE MARKER (tj-26jt9w addendum 5).

    An id the store has not seen before is a new process, and its arrival supersedes every older instance.
    The id is opaque: ingest mints it at start and the store compares it and nothing else. It is seen on a
    RegisterInstance, on a Submit answer, on a delivery header and on the store's own probe, and
    last_seen_at moves on each. superseded_at is set once, when a newer instance arrives, and is never cleared.

    No foreign key points here: ingest_request.instance_id and ingest_run.instance_id name an instance by value.
    """

    TABLE_NAME = 'ingest_instance'
    __tablename__ = TABLE_NAME

    instance_id = Column(String, primary_key=True, nullable=False)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # NULL while this is the newest instance the store has seen.
    superseded_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<IngestInstance(instance_id='{self.instance_id}', superseded_at='{self.superseded_at}')>"
