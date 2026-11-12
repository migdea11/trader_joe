from enum import StrEnum

from sqlalchemy import UUID, Boolean, Column, DateTime, Enum, Index, Integer, LargeBinary, String, func

from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource
from schemas.data_ingest.submit import AssetListStatus


class AssetListEventKind(StrEnum):
    """What a snapshot diff found about one vendor asset id (tj-7zjov1.30, P17). The labels are the Postgres enum's."""

    # An id new to the list, seen in no earlier snapshot of the other list.
    APPEARED = 'appeared'
    # An id in the list's previous snapshot and not in this one.
    VANISHED = 'vanished'
    # An id new to the list that an earlier snapshot of the OTHER list held: active to inactive or back.
    STATUS_FLIP = 'status_flip'


def _values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


class AssetListSnapshot(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One DATED asset-list snapshot: the header of a delivery of the vendor's active or inactive list (P17).

    APPEND-ONLY, NEVER UPDATED OR DELETED. The vendor purges some inactive assets, so our dated snapshots are the only
    record (survivorship). One row per delivered attempt, empty ones included: an empty list is recorded as a fact with
    zero rows and derives no event. The list comes from the REQUEST (ingest_request.asset_list_status), never from the
    records' flags (ADR tj-26jt9w addendum 17). The same (request, attempt) is one snapshot: the unique index backs the
    delivery's idempotency. No foreign key: snapshots outlive their request, as runs do.
    """

    TABLE_NAME = 'asset_list_snapshot'
    __tablename__ = TABLE_NAME

    ATTEMPT_UNIQUE_INDEX = 'uq_asset_list_snapshot_request_id_attempt_no'
    LIST_INDEX = 'ix_asset_list_snapshot_source_asset_list_as_of'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    source = Column(Enum(DataSource), nullable=False)
    asset_type = Column(Enum(AssetType), nullable=False)
    asset_list = Column(Enum(AssetListStatus, name='asset_list_status', values_callable=_values), nullable=False)
    request_id = Column(UUID(as_uuid=True), nullable=False)
    attempt_no = Column(Integer, nullable=False)
    # The snapshot instant: when the vendor answered (the delivery's as_of), else the transaction start.
    as_of = Column(DateTime(timezone=True), nullable=False)
    # How many rows the snapshot holds; zero is an empty snapshot, never a diff base.
    row_count = Column(Integer, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index(ATTEMPT_UNIQUE_INDEX, 'request_id', 'attempt_no', unique=True),
        Index(LIST_INDEX, 'source', 'asset_list', 'as_of'),
    )

    def __repr__(self):
        return f"<AssetListSnapshot(id='{self.id}', asset_list='{self.asset_list}', row_count='{self.row_count}')>"


class AssetListRow(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One vendor asset exactly as a snapshot served it, with the vendor's own payload byte for byte (P17).

    APPEND-ONLY. Nothing is interpreted: active and tradable are the vendor's presence-not-value flags (NULL when it
    said nothing), exchange and name are its strings as given (empty when it gave none). A symbol is whatever the
    vendor listed the id under at that instant; a same-id symbol change is visible in these rows only. No foreign key.
    """

    TABLE_NAME = 'asset_list_row'
    __tablename__ = TABLE_NAME

    SNAPSHOT_INDEX = 'ix_asset_list_row_snapshot_id'
    VENDOR_ASSET_INDEX = 'ix_asset_list_row_vendor_asset_id'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    snapshot_id = Column(UUID(as_uuid=True), nullable=False)
    vendor_asset_id = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    name = Column(String, nullable=False)
    exchange = Column(String, nullable=False)
    active = Column(Boolean, nullable=True)
    tradable = Column(Boolean, nullable=True)
    raw_payload = Column(LargeBinary, nullable=False)

    __table_args__ = (Index(SNAPSHOT_INDEX, 'snapshot_id'), Index(VENDOR_ASSET_INDEX, 'vendor_asset_id'))

    def __repr__(self):
        return f"<AssetListRow(id='{self.id}', snapshot_id='{self.snapshot_id}', vendor_asset_id='{self.vendor_asset_id}')>"


class AssetListEvent(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One derived event: an id appeared in a list, vanished from it, or flipped from the other list (P17).

    Derived by vendor asset id in the delivery transaction that wrote the snapshot, against the previous NON-EMPTY
    snapshot of the same list; an empty snapshot derives none. APPEND-ONLY. Each event names the id, the symbols as
    observed (symbol in this snapshot, or in the previous one for a vanish; previous_symbol on a flip is the symbol the
    other list held) and both snapshot instants (as_of this snapshot, previous_as_of the snapshot compared against: for
    a vanish the last one holding the id, for an appearance the base, for a flip the other-list snapshot that last held
    it). No rename or reuse events and no listing tables (ADR tj-26jt9w addenda 12 to 17). No foreign key.
    """

    TABLE_NAME = 'asset_list_event'
    __tablename__ = TABLE_NAME

    UNIQUE_INDEX = 'uq_asset_list_event_snapshot_id_vendor_asset_id_kind'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)
    source = Column(Enum(DataSource), nullable=False)
    asset_list = Column(Enum(AssetListStatus, name='asset_list_status', values_callable=_values), nullable=False)
    kind = Column(Enum(AssetListEventKind, name='asset_list_event_kind', values_callable=_values), nullable=False)
    vendor_asset_id = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    previous_symbol = Column(String, nullable=True)
    snapshot_id = Column(UUID(as_uuid=True), nullable=False)
    previous_snapshot_id = Column(UUID(as_uuid=True), nullable=False)
    as_of = Column(DateTime(timezone=True), nullable=False)
    previous_as_of = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index(UNIQUE_INDEX, 'snapshot_id', 'vendor_asset_id', 'kind', unique=True),)

    def __repr__(self):
        return (
            f"<AssetListEvent(id='{self.id}', kind='{self.kind}', vendor_asset_id='{self.vendor_asset_id}', "
            f"asset_list='{self.asset_list}')>"
        )
