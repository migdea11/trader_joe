import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from trader_joe.proto.market.v1 import bar_pb2 as _bar_pb2
from trader_joe.proto.market.v1 import enums_pb2 as _enums_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FetchDatasetRequest(_message.Message):
    __slots__ = ("owner", "source", "asset_symbol", "asset_type", "data_types", "granularity", "start", "end", "update_type", "feed")
    OWNER_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    ASSET_SYMBOL_FIELD_NUMBER: _ClassVar[int]
    ASSET_TYPE_FIELD_NUMBER: _ClassVar[int]
    DATA_TYPES_FIELD_NUMBER: _ClassVar[int]
    GRANULARITY_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    UPDATE_TYPE_FIELD_NUMBER: _ClassVar[int]
    FEED_FIELD_NUMBER: _ClassVar[int]
    owner: str
    source: _enums_pb2.DataSource
    asset_symbol: str
    asset_type: _enums_pb2.AssetType
    data_types: _containers.RepeatedScalarFieldContainer[_enums_pb2.DataType]
    granularity: _enums_pb2.Granularity
    start: _timestamp_pb2.Timestamp
    end: _timestamp_pb2.Timestamp
    update_type: _enums_pb2.UpdateType
    feed: _enums_pb2.Feed
    def __init__(self, owner: _Optional[str] = ..., source: _Optional[_Union[_enums_pb2.DataSource, str]] = ..., asset_symbol: _Optional[str] = ..., asset_type: _Optional[_Union[_enums_pb2.AssetType, str]] = ..., data_types: _Optional[_Iterable[_Union[_enums_pb2.DataType, str]]] = ..., granularity: _Optional[_Union[_enums_pb2.Granularity, str]] = ..., start: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., end: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., update_type: _Optional[_Union[_enums_pb2.UpdateType, str]] = ..., feed: _Optional[_Union[_enums_pb2.Feed, str]] = ...) -> None: ...

class FetchDatasetResponse(_message.Message):
    __slots__ = ("ack", "page", "done")
    ACK_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    ack: FetchAck
    page: BarPage
    done: FetchDone
    def __init__(self, ack: _Optional[_Union[FetchAck, _Mapping]] = ..., page: _Optional[_Union[BarPage, _Mapping]] = ..., done: _Optional[_Union[FetchDone, _Mapping]] = ...) -> None: ...

class FetchAck(_message.Message):
    __slots__ = ("accepted", "refused")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REFUSED_FIELD_NUMBER: _ClassVar[int]
    accepted: FetchAccepted
    refused: FetchRefused
    def __init__(self, accepted: _Optional[_Union[FetchAccepted, _Mapping]] = ..., refused: _Optional[_Union[FetchRefused, _Mapping]] = ...) -> None: ...

class FetchAccepted(_message.Message):
    __slots__ = ("feed",)
    FEED_FIELD_NUMBER: _ClassVar[int]
    feed: _enums_pb2.Feed
    def __init__(self, feed: _Optional[_Union[_enums_pb2.Feed, str]] = ...) -> None: ...

class FetchRefused(_message.Message):
    __slots__ = ("reason", "domain", "detail", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    REASON_FIELD_NUMBER: _ClassVar[int]
    DOMAIN_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    reason: str
    domain: str
    detail: str
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, reason: _Optional[str] = ..., domain: _Optional[str] = ..., detail: _Optional[str] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class BarPage(_message.Message):
    __slots__ = ("bars",)
    BARS_FIELD_NUMBER: _ClassVar[int]
    bars: _containers.RepeatedCompositeFieldContainer[_bar_pb2.Bar]
    def __init__(self, bars: _Optional[_Iterable[_Union[_bar_pb2.Bar, _Mapping]]] = ...) -> None: ...

class FetchDone(_message.Message):
    __slots__ = ("bar_count", "served_range", "as_of")
    BAR_COUNT_FIELD_NUMBER: _ClassVar[int]
    SERVED_RANGE_FIELD_NUMBER: _ClassVar[int]
    AS_OF_FIELD_NUMBER: _ClassVar[int]
    bar_count: int
    served_range: ServedRange
    as_of: _timestamp_pb2.Timestamp
    def __init__(self, bar_count: _Optional[int] = ..., served_range: _Optional[_Union[ServedRange, _Mapping]] = ..., as_of: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ServedRange(_message.Message):
    __slots__ = ("start", "end")
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    start: _timestamp_pb2.Timestamp
    end: _timestamp_pb2.Timestamp
    def __init__(self, start: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., end: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
