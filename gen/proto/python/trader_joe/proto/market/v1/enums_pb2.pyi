from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class DataSource(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DATA_SOURCE_UNSPECIFIED: _ClassVar[DataSource]
    DATA_SOURCE_IB_API: _ClassVar[DataSource]
    DATA_SOURCE_ALPACA_API: _ClassVar[DataSource]
    DATA_SOURCE_MANUAL_ENTRY: _ClassVar[DataSource]

class AssetType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ASSET_TYPE_UNSPECIFIED: _ClassVar[AssetType]
    ASSET_TYPE_STOCK: _ClassVar[AssetType]
    ASSET_TYPE_CRYPTO: _ClassVar[AssetType]
    ASSET_TYPE_OPTION: _ClassVar[AssetType]

class DataType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DATA_TYPE_UNSPECIFIED: _ClassVar[DataType]
    DATA_TYPE_MARKET_ACTIVITY: _ClassVar[DataType]
    DATA_TYPE_QUOTE: _ClassVar[DataType]
    DATA_TYPE_TRADE: _ClassVar[DataType]

class Granularity(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GRANULARITY_UNSPECIFIED: _ClassVar[Granularity]
    GRANULARITY_ONE_MINUTE: _ClassVar[Granularity]
    GRANULARITY_FIVE_MINUTES: _ClassVar[Granularity]
    GRANULARITY_THIRTY_MINUTES: _ClassVar[Granularity]
    GRANULARITY_ONE_HOUR: _ClassVar[Granularity]
    GRANULARITY_ONE_DAY: _ClassVar[Granularity]
    GRANULARITY_ONE_WEEK: _ClassVar[Granularity]
    GRANULARITY_ONE_MONTH: _ClassVar[Granularity]

class Feed(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FEED_UNSPECIFIED: _ClassVar[Feed]
    FEED_IEX: _ClassVar[Feed]
    FEED_SIP: _ClassVar[Feed]
    FEED_NOT_APPLICABLE: _ClassVar[Feed]

class UpdateType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    UPDATE_TYPE_UNSPECIFIED: _ClassVar[UpdateType]
    UPDATE_TYPE_STATIC: _ClassVar[UpdateType]
    UPDATE_TYPE_DAILY: _ClassVar[UpdateType]
    UPDATE_TYPE_STREAM: _ClassVar[UpdateType]
DATA_SOURCE_UNSPECIFIED: DataSource
DATA_SOURCE_IB_API: DataSource
DATA_SOURCE_ALPACA_API: DataSource
DATA_SOURCE_MANUAL_ENTRY: DataSource
ASSET_TYPE_UNSPECIFIED: AssetType
ASSET_TYPE_STOCK: AssetType
ASSET_TYPE_CRYPTO: AssetType
ASSET_TYPE_OPTION: AssetType
DATA_TYPE_UNSPECIFIED: DataType
DATA_TYPE_MARKET_ACTIVITY: DataType
DATA_TYPE_QUOTE: DataType
DATA_TYPE_TRADE: DataType
GRANULARITY_UNSPECIFIED: Granularity
GRANULARITY_ONE_MINUTE: Granularity
GRANULARITY_FIVE_MINUTES: Granularity
GRANULARITY_THIRTY_MINUTES: Granularity
GRANULARITY_ONE_HOUR: Granularity
GRANULARITY_ONE_DAY: Granularity
GRANULARITY_ONE_WEEK: Granularity
GRANULARITY_ONE_MONTH: Granularity
FEED_UNSPECIFIED: Feed
FEED_IEX: Feed
FEED_SIP: Feed
FEED_NOT_APPLICABLE: Feed
UPDATE_TYPE_UNSPECIFIED: UpdateType
UPDATE_TYPE_STATIC: UpdateType
UPDATE_TYPE_DAILY: UpdateType
UPDATE_TYPE_STREAM: UpdateType
