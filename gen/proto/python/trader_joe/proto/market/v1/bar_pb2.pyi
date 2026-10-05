import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from trader_joe.proto.market.v1 import enums_pb2 as _enums_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Bar(_message.Message):
    __slots__ = ("bar_start", "open", "high", "low", "close", "volume", "trade_count", "vwap", "feed")
    BAR_START_FIELD_NUMBER: _ClassVar[int]
    OPEN_FIELD_NUMBER: _ClassVar[int]
    HIGH_FIELD_NUMBER: _ClassVar[int]
    LOW_FIELD_NUMBER: _ClassVar[int]
    CLOSE_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    TRADE_COUNT_FIELD_NUMBER: _ClassVar[int]
    VWAP_FIELD_NUMBER: _ClassVar[int]
    FEED_FIELD_NUMBER: _ClassVar[int]
    bar_start: _timestamp_pb2.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int
    vwap: float
    feed: _enums_pb2.Feed
    def __init__(self, bar_start: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., open: _Optional[float] = ..., high: _Optional[float] = ..., low: _Optional[float] = ..., close: _Optional[float] = ..., volume: _Optional[float] = ..., trade_count: _Optional[int] = ..., vwap: _Optional[float] = ..., feed: _Optional[_Union[_enums_pb2.Feed, str]] = ...) -> None: ...
