"""The leaf conversions every message mapper is built from: enums by member NAME, and Timestamps.

ADR tj-8konfu D1 keeps the .proto and the Pydantic models as two source-of-truth artefacts with a
hand-written mapping between them. This module is the bottom of that mapping: the values a message is made
of, with the rules that decide whether a message can be mapped at all.

ENUMS CROSS BY MEMBER NAME, NEVER BY NUMBER (tj-vhboky.30). The Python enums in common/enums are canonical
and their VALUES are not what the wire carries: Granularity.ONE_MINUTE is '1min' in Python and
GRANULARITY_ONE_MINUTE on the wire. Buf's STANDARD lint requires every proto value to carry its enum's name
as a prefix (decision tj-3mk3u5.42 F1 rule 9), because protoc scopes enum values as siblings of the enum, so
the prefix is DERIVED from the enum's own name here rather than written out beside it -- a hand-written
prefix is one more thing that can drift.

THE ZERO VALUE NEVER REACHES A DOMAIN MODEL. proto3 requires a zero and buf requires it to end
_UNSPECIFIED, but common/enums deliberately has no "we do not know" member: Feed.UNKNOWN was removed by the
user's ruling on tj-vhboky.1, because "a value that should never be written is better expressed as an ERROR
than as an enum member". So each _UNSPECIFIED has no Python counterpart and to_domain REFUSES it. Where a
field's ABSENCE is meaningful -- FetchDatasetRequest.feed, the one such field on this contract -- the
message mapper reads the zero as None before it ever gets here; it never asks this module to decode one.

THE TABLE IS CHECKED AT IMPORT, so an enum value added to a .proto and not to common/enums (or the reverse)
fails the build rather than one call. That check is the enum half of what tj-3mk3u5.29's descriptor-driven
control does for fields, and it holds even where no test runs.

DATETIMES ARE AWARE UTC IN BOTH DIRECTIONS. google.protobuf.Timestamp carries no offset, so a Timestamp
decoded without tzinfo is naive and names no instant -- and the domain models use AwareDatetime, which
REFUSES a naive value rather than converting it (the tj-1bl90i rule, ruled on tj-vhboky.20). The transport
must therefore never be what trips that refusal: every datetime produced here is aware UTC, and every naive
datetime handed to it is refused here, where the message being built can be named.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Final

from google.protobuf.descriptor import EnumDescriptor
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

from common.enums.data_select import AssetType, DataType
from common.enums.data_stock import DataSource, Feed, Granularity, UpdateType
from trader_joe.proto.market.v1 import enums_pb2


class ProtoMappingError(ValueError):
    """A message will not map between the wire and the domain, in either direction.

    It is a ValueError because that is what the thing it reports is: a value the other side of the mapping
    cannot express. Which failure it stands for depends on the direction, and the two are not the same kind
    of event:

        DECODING    the peer sent something this release cannot read -- an unspecified enum, a required
                    field left unset, a timestamp finer than a datetime can hold. The client seam turns it
                    into PEER_PROTOCOL_ERROR (ADR tj-fa1rpu C8); it is never let out raw.
        ENCODING    a domain object this service built cannot be put on the wire. That is OUR bug, and the
                    servicer boundary (common/rpc/errors.py) answers INTERNAL with an error_id, which is the
                    correct treatment: nothing a caller sent can cause it.
    """


# A buf STANDARD enum value is <ENUM_NAME_IN_UPPER_SNAKE>_<MEMBER>, so the prefix is the enum's own name with
# a separator inserted at every lower-to-upper boundary: DataSource -> DATA_SOURCE_, Feed -> FEED_.
_CAMEL_BOUNDARY: Final = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')

# What proto3's mandatory zero is called, after the prefix. It has no Python counterpart by design.
_UNSPECIFIED: Final = 'UNSPECIFIED'

# A datetime holds microseconds; a Timestamp holds nanoseconds.
_NANOS_PER_MICROSECOND: Final = 1_000


def _value_prefix(enum_name: str) -> str:
    return f'{_CAMEL_BOUNDARY.sub("_", enum_name).upper()}_'


@dataclass(frozen=True)
class EnumMapping[E: Enum]:
    """One proto enum and the Python enum it mirrors, mapped by member name in both directions.

    Built only by _mapping() below, which derives the prefix and proves the two vocabularies agree.

    Attributes:
        descriptor: The proto enum's descriptor, e.g. enums_pb2.Feed.DESCRIPTOR.
        python_type: The canonical Python enum it mirrors, from common/enums.
        prefix: The value prefix buf requires, derived from the proto enum's name.
    """

    descriptor: EnumDescriptor
    python_type: type[E]
    prefix: str

    def to_domain(self, number: int) -> E:
        """Decode one wire value to its Python member.

        Args:
            number: The enum value as the message carries it.

        Returns:
            E: The member of python_type whose NAME matches, prefix stripped.

        Raises:
            ProtoMappingError: If the value is the _UNSPECIFIED zero, which names no member (tj-vhboky.1),
                or is a number this release's copy of the contract does not define, which is deploy skew.
        """
        value = self.descriptor.values_by_number.get(number)
        if value is None:
            raise ProtoMappingError(
                f'{self.descriptor.full_name} has no value numbered {number} in this release; '
                f'it knows {sorted(self.descriptor.values_by_name)}'
            )
        if value.number == 0:
            raise ProtoMappingError(
                f'{value.name} is the zero proto3 requires, not a value: {self.python_type.__name__} has no '
                'member for "we do not know" (tj-vhboky.1), so a message that left this field unset is refused'
            )
        return self.python_type[value.name.removeprefix(self.prefix)]

    def to_proto(self, member: E) -> int:
        """Encode one Python member as its wire value.

        Args:
            member: A member of python_type.

        Returns:
            int: The proto enum value with the matching name.

        Raises:
            ProtoMappingError: If the member is not one of python_type's, which is a caller passing the
                wrong enum entirely.
        """
        value = self.descriptor.values_by_name.get(f'{self.prefix}{getattr(member, "name", member)}')
        if value is None:
            raise ProtoMappingError(
                f'{member!r} is not a {self.python_type.__name__}, so it has no {self.descriptor.full_name} value'
            )
        return value.number


def _mapping[E: Enum](descriptor: EnumDescriptor, python_type: type[E]) -> EnumMapping[E]:
    # Refuse at import an enum pair that could not round-trip, so no build can ship one: a value added to
    # the .proto and not to common/enums, or the reverse, is caught here rather than on the one call that
    # happens to carry it.
    prefix = _value_prefix(descriptor.name)
    zero = descriptor.values_by_number.get(0)
    if zero is None or zero.name != f'{prefix}{_UNSPECIFIED}':
        raise ValueError(
            f'{descriptor.full_name} must name its zero {prefix}{_UNSPECIFIED}, as proto3 and buf require, '
            f'not {zero.name if zero is not None else "nothing"}'
        )
    unprefixed = sorted(value.name for value in descriptor.values if not value.name.startswith(prefix))
    if unprefixed:
        raise ValueError(f'{descriptor.full_name} values {unprefixed} do not carry the prefix {prefix}')
    on_wire = {value.name.removeprefix(prefix) for value in descriptor.values if value.number != 0}
    in_python = set(python_type.__members__)
    if on_wire != in_python:
        raise ValueError(
            f'{descriptor.full_name} and {python_type.__name__} name different members: '
            f'only on the wire {sorted(on_wire - in_python)}, only in Python {sorted(in_python - on_wire)}'
        )
    return EnumMapping(descriptor=descriptor, python_type=python_type, prefix=prefix)


ASSET_TYPE: Final = _mapping(enums_pb2.AssetType.DESCRIPTOR, AssetType)
DATA_SOURCE: Final = _mapping(enums_pb2.DataSource.DESCRIPTOR, DataSource)
DATA_TYPE: Final = _mapping(enums_pb2.DataType.DESCRIPTOR, DataType)
FEED: Final = _mapping(enums_pb2.Feed.DESCRIPTOR, Feed)
GRANULARITY: Final = _mapping(enums_pb2.Granularity.DESCRIPTOR, Granularity)
UPDATE_TYPE: Final = _mapping(enums_pb2.UpdateType.DESCRIPTOR, UpdateType)

# Every enum of the shared market/v1 vocabulary, for a caller that wants to walk them -- the round-trip
# control (tj-3mk3u5.29) reaches the generated enums through this and not through trader_joe.proto.
ENUM_MAPPINGS: Final[tuple[EnumMapping, ...]] = (ASSET_TYPE, DATA_SOURCE, DATA_TYPE, FEED, GRANULARITY, UPDATE_TYPE)


def to_timestamp(value: datetime) -> Timestamp:
    """Encode an aware datetime as a Timestamp, in UTC.

    Args:
        value: The instant. Timezone aware; its offset is applied, not discarded.

    Returns:
        Timestamp: The same instant.

    Raises:
        ProtoMappingError: If the datetime is naive, which names no instant (tj-1bl90i).
    """
    if value.utcoffset() is None:
        raise ProtoMappingError(f'{value!r} is naive, and a naive datetime names no instant, so it has no Timestamp')
    timestamp = Timestamp()
    timestamp.FromDatetime(value)
    return timestamp


def to_datetime(value: Timestamp) -> datetime:
    """Decode a Timestamp as an AWARE UTC datetime.

    Never naive: a Timestamp carries no offset, so decoding one without tzinfo would produce exactly the
    value the domain models refuse, and the transport would be what tripped the refusal.

    Args:
        value: The Timestamp from the message.

    Returns:
        datetime: The instant, aware, in UTC.

    Raises:
        ProtoMappingError: If the Timestamp carries sub-microsecond precision, which a datetime cannot
            hold, or an instant outside what a datetime can represent.
    """
    if value.nanos % _NANOS_PER_MICROSECOND:
        raise ProtoMappingError(
            f'the Timestamp {value.seconds}.{value.nanos:09d} carries sub-microsecond precision, which a '
            'datetime cannot hold; decoding it would silently round the instant the bar names'
        )
    try:
        return value.ToDatetime(tzinfo=UTC)
    except (OSError, OverflowError, ValueError) as error:
        raise ProtoMappingError(
            f'the Timestamp {value.seconds}.{value.nanos:09d} is outside the range a datetime can represent'
        ) from error


def timestamp_field(message: Message, field: str) -> datetime:
    """Decode a REQUIRED Timestamp field, refusing an unset one rather than reading it as the epoch.

    proto3 gives a message field explicit presence, so "unset" and "1970-01-01" are distinguishable -- and on
    this contract every Timestamp but FetchDatasetRequest.end is required, so an unset one is a message that
    cannot be mapped, not a default.

    Args:
        message: The message holding the field.
        field: The field's name, e.g. 'bar_start'.

    Returns:
        datetime: The instant, aware, in UTC.

    Raises:
        ProtoMappingError: If the field is unset, or as to_datetime raises.
    """
    if not message.HasField(field):
        raise ProtoMappingError(
            f'{message.DESCRIPTOR.full_name}.{field} is unset, and it is required; an unset Timestamp is not the epoch'
        )
    return to_datetime(getattr(message, field))
