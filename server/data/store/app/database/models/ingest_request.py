from enum import StrEnum

from sqlalchemy import (
    ARRAY,
    UUID,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    func,
    text,
)

from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed, Granularity
from common.sensitive import REDACTED
from data.store.app.database.models.ingest_run import IngestRunTrigger
from schemas.data_ingest.submit import AssetListStatus


class IngestRequestKind(StrEnum):
    """Which arm of the internal Submit a request is. Store-local: the wire's RequestKind is lower case."""

    BARS = 'BARS'
    ASSET_LIST = 'ASSET_LIST'
    CORPORATE_ACTIONS = 'CORPORATE_ACTIONS'


class IngestRequestState(StrEnum):
    """Where a request stands (tj-26jt9w addenda 4 and 5; PR5-PLAN REV1 N4 item 1).

    PENDING is written and not yet answered by ingest. QUEUED is answered: ingest holds it and its instance id
    is recorded. NOT_DELIVERED is a request whose ingest instance was replaced before its result arrived; it is
    re-submitted and is never a failure and never an attempt. SUCCEEDED, EMPTY and FAILED are terminal, and the
    verdict is the store's.
    """

    PENDING = 'PENDING'
    QUEUED = 'QUEUED'
    NOT_DELIVERED = 'NOT_DELIVERED'
    SUCCEEDED = 'SUCCEEDED'
    EMPTY = 'EMPTY'
    FAILED = 'FAILED'

    @property
    def is_open(self) -> bool:
        """bool: True while the request may still produce a result."""
        return self in OPEN_STATES


OPEN_STATES = frozenset({IngestRequestState.PENDING, IngestRequestState.QUEUED, IngestRequestState.NOT_DELIVERED})


class IngestRequestFailureCause(StrEnum):
    """Why a request is FAILED. The column is a plain string, so a later cause is an additive code change.

    EXPIRED is a request taken off ingest's queue after its ready-by (owner ruling R4, 2026-10-24): it made no
    vendor call, wrote no attempt row, and is answered FINAL.
    """

    EXPIRED = 'EXPIRED'
    # The vendor refused the request for good (DELIVERY_ERROR_CLASS_PERMANENT).
    PERMANENT = 'PERMANENT'
    # Ingest reported its attempt budget spent with a failed attempt (DeliveryTrailer.last_attempt).
    ATTEMPTS_EXHAUSTED = 'ATTEMPTS_EXHAUSTED'
    # Not a failure: the request finished EMPTY because the dataset it wrote to was deleted or stopped before its
    # delivery landed (the bars arm's drop rule, P35). Nothing was stored. Written on an EMPTY request, as the cause.
    DISCARDED = 'DISCARDED'
    # The store's stop rule (P50): a bars delivery named another vendor asset id than the dataset's last accepted
    # delivery, so no bar was written and the dataset was stopped. Written on a FAILED request.
    VENDOR_ASSET_CHANGED = 'VENDOR_ASSET_CHANGED'


def _values(enum_cls: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_cls]


class IngestRequest(AppBase.DATA_STORE_BASE, CustomTypeTable):
    """One request the store asked ingest for: the DURABLE REQUEST RECORD (tj-26jt9w addendum 4).

    WRITTEN BEFORE THE REQUEST IS SENT, and committed, so a store restart re-sends what it finds PENDING (Submit
    is idempotent on request_id). The row's id IS the request id on the wire, and ingest_run.request_id names it.
    The store holds the durable attempt count, the summed spend and the verdict; ingest holds only the budget.

    NO FOREIGN KEY, ON PURPOSE. dataset_id is nullable and references nothing: a request outlives
    its dataset and an asset-list request names none. ingest_run rows
    point back here by request_id with no constraint either, because runs outlive requests.

    client_label is a SensitiveString like StoreDatasetEntry.owner: the caller's label, kept so a recovered
    request can be re-submitted as it was, never logged and never in a repr.
    """

    TABLE_NAME = 'ingest_request'
    __tablename__ = TABLE_NAME

    OPEN_INDEX = 'ix_ingest_request_open_state_created_at'
    QUEUED_INSTANCE_INDEX = 'ix_ingest_request_queued_instance_id'
    DATASET_INDEX = 'ix_ingest_request_dataset_id'
    SOURCE_CREATED_INDEX = 'ix_ingest_request_source_created_at'
    ASSET_LIST_STATUS_CONSTRAINT = 'ck_ingest_request_asset_list_status'

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), nullable=False)

    kind = Column(Enum(IngestRequestKind, name='ingest_request_kind', values_callable=_values), nullable=False)
    # Every kind names a source, and the request is routed by it.
    source = Column(Enum(DataSource), nullable=False)
    # NULL for a request that is not about one dataset (an asset list), and until ingest acknowledges a new one.
    dataset_id = Column(UUID(as_uuid=True), nullable=True)

    # The request parameters. Each is NULL where the kind has no such parameter: an asset list has no symbol,
    # corporate actions have no granularity, update type, feed or data types.
    asset_symbol = Column(String, nullable=True)
    asset_type = Column(Enum(AssetType), nullable=True)
    # DataType values (for example market-activity), in the order asked.
    data_types = Column(ARRAY(String), nullable=True)
    granularity = Column(Enum(Granularity), nullable=True)
    # UpdateType's integer value, as ingest_run.update_type.
    update_type = Column(Integer, nullable=True)
    # The tape asked for, NULL where the deployment decides. Not the resolved feed.
    feed = Column(Enum(Feed, name='feed', values_callable=lambda enum_cls: [m.value for m in enum_cls]), nullable=True)
    # The window asked for, half-open [start, end). NULL end is an open end.
    requested_start = Column(DateTime(timezone=True), nullable=True)
    requested_end = Column(DateTime(timezone=True), nullable=True)

    # NULL means no deadline: the request never expires and sorts after every request that has one.
    ready_by = Column(DateTime(timezone=True), nullable=True)
    best_effort = Column(Boolean, nullable=False, server_default=text('false'))
    client_label = Column(SensitiveString, nullable=False)
    # What started the request: REQUEST (a POST) or SCHEDULE (the DAILY loop). NULL reads as REQUEST. The attempt row of
    # the first attempt takes it; every later attempt row is RETRY.
    trigger = Column(Enum(IngestRunTrigger, name='ingest_run_trigger', values_callable=_values), nullable=True)
    # The POST body's lifecycle fields, kept so the dataset entry can be written inside the delivery transaction. Both
    # NULL where the request carries none (a scheduled refresh, a non-bars kind).
    expiry = Column(DateTime(timezone=True), nullable=True)
    collect = Column(Boolean, nullable=True)
    # The POST body's expiry type (ExpiryType's integer value, as the entry's column stores it): identity of the entry
    # written on the first delivered attempt. NULL where the request carries none (the DAILY loop, which sets
    # dataset_id at request time; an asset list; corporate actions). No default: a guessed identity is a wrong one.
    expiry_type = Column(Integer, nullable=True)
    # Which of the vendor's two asset lists an ASSET_LIST request asks for (ADR tj-26jt9w addendum 17, P17). NULL for every
    # other kind, enforced both ways by ASSET_LIST_STATUS_CONSTRAINT. A Postgres enum, not a free string: the column
    # picks the snapshot's diff base, and a misspelt label must not start a third list. The type is shared with
    # asset_list_snapshot.asset_list.
    asset_list_status = Column(Enum(AssetListStatus, name='asset_list_status', values_callable=_values), nullable=True)

    state = Column(
        Enum(IngestRequestState, name='ingest_request_state', values_callable=_values),
        nullable=False,
        server_default=IngestRequestState.PENDING.value,
    )
    # The ingest process holding the request, as an opaque string. NULL until it answers.
    instance_id = Column(String, nullable=True)
    # The durable attempt count: attempts reported and counted, never the ones that did not reach the store.
    attempts = Column(Integer, nullable=False, server_default='0')
    # Spend summed over the request's reported attempts, failed ones included.
    spent_calls = Column(BigInteger, nullable=False, server_default='0')
    spent_pages = Column(BigInteger, nullable=False, server_default='0')
    spent_throttled_429 = Column(BigInteger, nullable=False, server_default='0')
    spent_retries = Column(BigInteger, nullable=False, server_default='0')
    # Counted attempts that reported no vendor stats (a FAILED report from the runner's crash path), whose cost is
    # unknown and is NOT in the sums above. Non-zero means the sums are a lower bound.
    spend_unknown_attempts = Column(Integer, nullable=False, server_default='0')

    # Set only when state is FAILED. failure_detail is the trailer's detail, never str() of an exception.
    failure_cause = Column(String, nullable=True)
    failure_detail = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # NULL while the request is open.
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The open requests, oldest first: the restart re-send of PENDING, the NOT_DELIVERED re-request and the
        # Running facet read only these few rows.
        Index(
            OPEN_INDEX,
            'state',
            'created_at',
            postgresql_where=state.in_(
                [IngestRequestState.PENDING, IngestRequestState.QUEUED, IngestRequestState.NOT_DELIVERED]
            ),
        ),
        # A new instance marks every QUEUED request of an older instance NOT_DELIVERED in one statement.
        Index(QUEUED_INSTANCE_INDEX, 'instance_id', postgresql_where=(state == IngestRequestState.QUEUED)),
        # A dataset's requests, for the Running facet and the dataset detail.
        Index(DATASET_INDEX, 'dataset_id', postgresql_where=dataset_id.is_not(None)),
        # The panel manager's request-rate window: a source's requests in the trailing 60 minutes.
        Index(SOURCE_CREATED_INDEX, 'source', 'created_at'),
        # An asset-list request always names its list, and no other kind names one.
        CheckConstraint("(kind = 'ASSET_LIST') = (asset_list_status IS NOT NULL)", name=ASSET_LIST_STATUS_CONSTRAINT),
    )

    def __repr__(self):
        # client_label is sensitive: the marker, never the value.
        return (
            f"<IngestRequest(id='{self.id}', kind='{self.kind}', client_label='{REDACTED}', "
            f"state='{self.state}', attempts='{self.attempts}')>"
        )
