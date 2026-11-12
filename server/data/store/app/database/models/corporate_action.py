from sqlalchemy import UUID, Column, DateTime, Enum, Index, Integer, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import JSONB

from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.enums.data_stock import DataSource


class CorporateAction(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One corporate-action OBSERVATION: a vendor record exactly as a delivery published it (tj-7zjov1.31, P18).

    APPEND-ONLY, NEVER UPDATED OR DELETED. Bias-free data must hold what was known and when (owner ruling 2026-10-26),
    and the vendor gives no guarantee on when a record is created, so a later re-fetch is not equivalent. A row says:
    this payload, with these identifiers and dates, was delivered by this attempt of this request at as_of.

    NOTHING HERE IS TIED TO A LISTING OR A DATASET (ADR tj-26jt9w addendum 12: symbol identity is not modelled). The
    symbol is whatever the request asked for, kept as the requesting symbol; the identifiers are the vendor's own.

    DEDUPE IS BY CONTENT. The same vendor action id with the same payload bytes is the same observation, so a second
    delivery of it (overlapping windows over-read on purpose) writes nothing; the same id with different bytes is a
    vendor revision and is a NEW row with its own as_of. The unique index is the arbiter of the insert.

    No foreign key: observations outlive their request, as runs do.
    """

    TABLE_NAME = 'corporate_action'
    __tablename__ = TABLE_NAME

    OBSERVATION_UNIQUE_INDEX = 'uq_corporate_action_source_vendor_action_id_payload_hash'
    REQUEST_INDEX = 'ix_corporate_action_request_id'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)

    source = Column(Enum(DataSource), nullable=False)
    # The vendor's id for the action, opaque, and its type wording, uninterpreted.
    vendor_action_id = Column(String, nullable=False)
    action_type = Column(String, nullable=False)

    # The vendor's dates, each NULL when the vendor gave none.
    ex_date = Column(DateTime(timezone=True), nullable=True)
    record_date = Column(DateTime(timezone=True), nullable=True)
    payable_date = Column(DateTime(timezone=True), nullable=True)
    process_date = Column(DateTime(timezone=True), nullable=True)
    effective_date = Column(DateTime(timezone=True), nullable=True)

    # Every identifier the vendor gave, keyed by the vendor's own name.
    identifiers = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # The vendor's record, byte for byte, and the SHA-256 (hex) of those bytes.
    raw_payload = Column(LargeBinary, nullable=False)
    payload_hash = Column(String, nullable=False)

    # What delivered it: the request, the attempt number within it, and the symbol the request asked for (NULL when it
    # named none). requested_start and requested_end are the window asked for, kept; they are not a filter.
    request_id = Column(UUID(as_uuid=True), nullable=False)
    attempt_no = Column(Integer, nullable=False)
    requesting_symbol = Column(String, nullable=True)
    requested_start = Column(DateTime(timezone=True), nullable=True)
    requested_end = Column(DateTime(timezone=True), nullable=True)

    # When the vendor answered (the delivery's as_of) and when the store wrote the row (the transaction start).
    as_of = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index(OBSERVATION_UNIQUE_INDEX, 'source', 'vendor_action_id', 'payload_hash', unique=True),
        Index(REQUEST_INDEX, 'request_id'),
    )

    def __repr__(self):
        return (
            f"<CorporateAction(id='{self.id}', source='{self.source}', vendor_action_id='{self.vendor_action_id}', "
            f"action_type='{self.action_type}')>"
        )
