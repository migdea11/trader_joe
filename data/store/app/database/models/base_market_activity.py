from abc import abstractmethod

from sqlalchemy import UUID, Column, DateTime, Enum, ForeignKey, Integer, String, func

from common.database.sql_alchemy_table import AppBase
from common.enums.data_select import AssetType
from common.enums.data_stock import DataSource, Feed, Granularity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry


class BaseMarketActivity(AppBase.DATA_STORE_BASE):
    __abstract__ = True

    # The natural key of a bar. dataset_id LEADS it (tj-vhboky.1, the record; T3 tj-vhboky.4):
    # coverage is per-dataset now, not shared across entries, so two fetches of the same minute
    # through two different dataset entries are deliberately two different rows -- the opposite
    # of the pre-duplication design, where a mutable dataset_id made ownership last-write-wins
    # and produced tj-k207b7. feed sits inside the key too: it is denormalised provenance and a
    # filter for a future correction UPDATE, not a collision guard -- dataset_id already keeps an
    # IEX bar and a SIP bar apart, because they now belong to two different entries. Concrete
    # tables declare the constraint itself so each gets its own name.
    NATURAL_KEY = ('dataset_id', 'asset_symbol', 'source', 'feed', 'granularity', 'timestamp')

    # Surrogate key, kept deliberately: it is exposed as StockDataMarketActivity.id and is a
    # cheaper join and delete target than the six-column natural key above.
    id = Column(Integer, primary_key=True)
    # No index=True: dataset_id now LEADS the unique constraint over NATURAL_KEY, so a
    # dataset-scoped range read already gets a five-column equality prefix and an ordered
    # timestamp from that constraint. A standalone index here would be redundant (tj-vhboky.1).
    dataset_id = Column(UUID, ForeignKey(f'{StoreDatasetEntry.TABLE_NAME}.id', ondelete='CASCADE'), nullable=False)

    source = Column(Enum(DataSource), nullable=False)
    asset_symbol = Column(String, nullable=False)

    # Which tape this bar's numbers came from. NOT NULL, imported off the composed class rather
    # than a hand-written member list (common/enums/data_stock.py: Feed) -- a literal list gets
    # no error the day a market is added and fails at runtime on the first insert instead.
    # values_callable so the stored label matches the string Pydantic and the API use: sa.Enum
    # persists the member NAME by default, and while every current Feed member's name equals its
    # value, that stops being true the moment one doesn't.
    # NO server_default: the enum's "no sentinel for we do not know" ruling (tj-vhboky.1, ruling
    # of 2026-09-25) removed the UNKNOWN member a default would have pointed at, so every write
    # must supply an explicit, adapter-resolved value. The bar is written after the fetch, so the
    # adapter has already resolved feed by the time this row exists (tj-rh4b7f) -- unlike the
    # dataset entry, where feed is deferred rather than added (see store_dataset_entry.py).
    feed = Column(
        Enum(Feed, name='feed', values_callable=lambda enum_cls: [member.value for member in enum_cls]), nullable=False
    )

    timestamp = Column(DateTime(timezone=True), nullable=False)
    granularity = Column(Enum(Granularity), nullable=False)

    # Dates used to manage split and dividends adjustments
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    # expiry is NOT here. It used to be a per-fetch attribute stored on a per-bar row, which is
    # how it acquired the same last-write-wins defect as dataset_id used to have. It now lives on
    # the dataset entry, the row that IS the fetch (tj-vhboky.1 section 9).
    __table_args__ = ()

    def _repr(self, table_name: str, additional_fields: str) -> str:
        return (
            f"<{table_name}(id='{self.id}', dataset_id='{self.dataset_id}', source='{self.source}, "
            f"symbol='{self.asset_symbol}', timestamp='{self.timestamp}', granularity='{self.granularity}', "
            f'{additional_fields}'
            f"created_at='{self.created_at}', updated_at='{self.updated_at}')>"
        )

    @abstractmethod
    def get_asset_type(self) -> AssetType: ...

    @abstractmethod
    def __repr__(self): ...
