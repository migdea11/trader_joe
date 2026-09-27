from sqlalchemy import UUID, Column, DateTime, Enum, String, UniqueConstraint, func

from common.database.sql_alchemy_nullable_datetime import NullableDateTime as SqlNullableDateTime
from common.database.sql_alchemy_ordered_enum import OrderedEnum as SqlIntEnum
from common.database.sql_alchemy_table import AppBase, CustomTypeTable
from common.database.sql_alchemy_types import CustomColumn
from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, ExpiryType, Granularity, UpdateType


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
    # feed IS DELIBERATELY NOT HERE, though an earlier version of this record's section 7 listed
    # it as NEW. tj-rh4b7f (2026-09-25) deferred it: the entry is upserted before the ingest
    # adapter has resolved a feed (data/store/app/ingest/data_action_request.py upserts the entry
    # from the request body, then calls ingest), so a feed column on the entry would have to be
    # written before anything can supply a trustworthy value. The user ruled to defer both feed
    # on the entry and feed as an accepted create-request field to the gRPC transport work, where
    # an acknowledgement can carry the resolved value early enough to write it. No caller can
    # select a feed today -- there is exactly one feed per deployment -- so deferring costs
    # nothing yet. See tj-rh4b7f for the full reasoning.
    NATURAL_KEY = (
        'asset_symbol',
        'source',
        'granularity',
        'asset_type',
        'data_type',
        'owner',
        'expiry_type',
        'update_type',
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
    owner = Column(String, nullable=False, server_default='unassigned')

    # Data request details
    source = Column(Enum(DataSource), nullable=False)
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
        return (
            f"<StoreDatasetEntry(id='{self.id}', owner='{self.owner}', symbol='{self.asset_symbol}', "
            f"source='{self.source}', data_types='{self.data_type}', granularity='{self.granularity}', "
            f"start='{self.start}', end='{self.end}', expiry='{self.expiry}', expiry_type='{self.expiry_type}', "
            f"update_type='{self.update_type}', created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )
