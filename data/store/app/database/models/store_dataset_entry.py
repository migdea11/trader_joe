from sqlalchemy import UUID, Column, DateTime, Enum, String, UniqueConstraint, func

from common.database.sql_alchemy_nullable_datetime import NullableDateTime as SqlNullableDateTime
from common.database.sql_alchemy_ordered_enum import OrderedEnum as SqlIntEnum
from common.database.sql_alchemy_sensitive_string import SensitiveString
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.database.sql_alchemy_types import CustomColumn
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Feed, Granularity, UpdateType
from common.sensitive import REDACTED


class StoreDatasetEntry(AppBase.DATA_STORE_BASE, CustomTypeTable):
    TABLE_NAME = 'store_dataset_entry'
    __tablename__ = TABLE_NAME

    # Named so a future ON CONFLICT clause can target it by name rather than by column list,
    # matching the convention already established by
    # StockMarketActivity.NATURAL_KEY_CONSTRAINT. The prior constraint was UNNAMED, which is
    # exactly the trap this name exists to avoid -- see the migration for how an unnamed
    # constraint has to be dropped (looked up from pg_constraint, never guessed).
    NATURAL_KEY_CONSTRAINT = 'uq_store_dataset_entry_identity'

    # Every field here is identity (tj-vhboky.1 section 2): two requests name the same dataset
    # only if they agree on all of these, the range included. asset_symbol LEADS -- not owner --
    # because search_entries filters asset_symbol on EVERY call and never filters owner (reads
    # are open); an owner-leading index would give that listing no usable prefix at all. start
    # and end go LAST so the own-overlap check (same owner, same everything else, ranges that
    # overlap) gets a long equality prefix before the range comparison.
    #
    # feed IS HERE NOW, and the deferral this comment used to record is RETIRED (tj-3mk3u5.31,
    # closing tj-f2qz44). tj-rh4b7f (2026-09-25) deferred it because of WRITE ORDER, not taste:
    # the entry was upserted from the request body BEFORE ingest was called, so nothing had
    # resolved a tape at the moment the row was written, and feed is identity -- a placeholder
    # written then and corrected later would MUTATE identity and silently merge two datasets that
    # asked for different tapes. The FetchDataset cutover (tj-3mk3u5.10) reversed that order: the
    # acknowledgement arrives FIRST and carries the resolved feed, so
    # data/store/app/ingest/data_action_request.py now upserts the entry ON THE ACK with a tape
    # that is already decided. An IEX request and a SIP request over the same window are two
    # entries again, which is what tj-f2qz44 asked for.
    #
    # feed IS IDENTITY AND IS NOT AN OVERLAP TERM (tj-xn3qa6 D1), and the distinction lives one
    # layer up: the own-overlap REFUSAL in the crud keys on this tuple MINUS feed, because "is
    # this the same dataset" and "does this owner already hold data covering this window" are
    # different questions. Only the first one gets feed.
    #
    # SO feed GOES LAST AMONG THE EQUALITY COLUMNS, immediately before the range: that is the
    # ordering rule above applied to a column the own-overlap check does NOT filter on. The check
    # keys on the other eight, so any position earlier in this tuple would truncate its usable
    # index prefix at feed; here the eight it does filter on stay contiguous and leading, and feed
    # costs that check nothing. Identity itself does not care where feed sits -- a unique
    # constraint is order-blind -- so the index is the only thing deciding the position.
    NATURAL_KEY = (
        'asset_symbol',
        'source',
        'granularity',
        'asset_type',
        'data_type',
        'owner',
        'expiry_type',
        'update_type',
        'feed',
        'start',
        'end',
    )

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid(), unique=True, nullable=False
    )
    # The caller's declared principal (tj-vhboky.1 section 5). NOT NULL because it is identity
    # and joins the unique constraint below -- Postgres treats NULL as distinct from NULL there,
    # so a nullable owner would silently stop the exact-repeat-returns-the-id guarantee from
    # firing for any request that omitted it. The server default exists only so this column can
    # be added NOT NULL in one step; the API-side schema (StoreAssetDatasetBody.owner) has no
    # default of its own; a caller must always name itself.
    # SensitiveString (tj-vhboky.41 Addendum 1, D3, M1): every statement binding owner renders
    # it as the redaction marker in exception text and engine echo; the stored value and the DDL
    # (VARCHAR) are String's, so no migration.
    owner = Column(SensitiveString, nullable=False, server_default='unassigned')

    # Data request details
    source = Column(Enum(DataSource), nullable=False)

    # THE RESOLVED TAPE, never the caller's preference (tj-3mk3u5.31, closing tj-f2qz44). It is
    # written from FetchAccepted, the one place the tape is decided (tj-3mk3u5.22 Q5);
    # StoreAssetDatasetBody.feed is a different value with the same name and is optional, which is
    # why AssetDatasetStoreCreate.feed overrides it as required -- read that model's docstring
    # before touching either.
    #
    # NOT NULL AND NO SERVER DEFAULT, which is the "no sentinel" ruling (tj-vhboky.1 / tj-vhboky.1
    # as restated on tj-3mk3u5.22): Feed carries no UNKNOWN member for a default to point at,
    # because a value that should never be written is better expressed as an error than as a
    # vocabulary member. An unresolved feed is therefore a loud failure at the boundary, not a
    # placeholder sitting on an identity column that no later correction could rewrite. The
    # migration that adds it DELETES every existing entry and bar rather than backfilling (user
    # ruling, tj-3mk3u5.22 Q6).
    #
    # Same enum type and values_callable as the bar's column (base_market_activity.py), so both
    # columns share one Postgres type named 'feed' and both persist the member VALUE rather than
    # its name -- identical today, and the day a member's name stops equalling its value the two
    # tables do not drift apart.
    feed = Column(
        Enum(Feed, name='feed', values_callable=lambda enum_cls: [member.value for member in enum_cls]), nullable=False
    )

    asset_symbol = Column(String, nullable=False)
    asset_type = Column(Enum(AssetType), nullable=False)
    data_type = Column(Enum(DataType), nullable=False)

    # Data time and period
    granularity = Column(Enum(Granularity), nullable=False)
    start = Column(DateTime(timezone=True), nullable=False)
    end = CustomColumn(SqlNullableDateTime, nullable=False)

    # When this dataset's data dies. Per-DATASET now, not per-fetch (tj-vhboky.1 section 9): the
    # bar's old expiry column had the same last-write-wins defect as its old dataset_id, because
    # it was one value per bar rather than one value per fetch. Plain nullable DateTime(timezone=
    # True), NOT the NullableDateTime custom type `end` uses -- expiry is NOT part of the unique
    # key (expiry_TYPE is; the value itself is a policy the request asks for, not a thing that
    # makes one dataset different from another), so there is no need to map None to a sentinel
    # to keep it out of a unique index. timezone=True is load-bearing: the bar's old expiry
    # column was a naive DateTime while every other timestamp here is timestamptz, and a naive
    # value bound to a timestamptz column is interpreted in the session timezone -- a silent,
    # environment-dependent wrong answer about when data dies. Fixed here rather than carried
    # forward.
    expiry = Column(DateTime(timezone=True), nullable=True)

    # Manage data expiry and updates. NOT NULL, matching the schema defaults (ExpiryType.BULK,
    # UpdateType.STATIC): both join the unique constraint below, so a nullable column here would
    # have the same silent-duplicate hazard as a nullable owner -- upsert_entry currently passes
    # exclude_none=True, so a request that explicitly sent null would write NULL into a key
    # column and the ON CONFLICT would simply stop matching it.
    expiry_type = CustomColumn(SqlIntEnum(ExpiryType), nullable=False, server_default=str(ExpiryType.BULK.value))
    update_type = CustomColumn(SqlIntEnum(UpdateType), nullable=False, server_default=str(UpdateType.STATIC.value))

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint(*NATURAL_KEY, name=NATURAL_KEY_CONSTRAINT),)

    def __repr__(self):
        # owner is sensitive (tj-vhboky.41 Addendum 1, D3, M3): the marker, never the value.
        return (
            f"<StoreDatasetEntry(id='{self.id}', owner='{REDACTED}', symbol='{self.asset_symbol}', "
            f"source='{self.source}', feed='{self.feed}', data_types='{self.data_type}', "
            f"granularity='{self.granularity}', "
            f"start='{self.start}', end='{self.end}', expiry='{self.expiry}', expiry_type='{self.expiry_type}', "
            f"update_type='{self.update_type}', created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )
