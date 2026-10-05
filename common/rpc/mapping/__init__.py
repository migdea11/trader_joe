"""THE HAND-WRITTEN PROTO <-> DOMAIN SEAM, and the public surface a control can walk.

ADR tj-8konfu D1 keeps the .proto and the Pydantic models as two source-of-truth artefacts with a
hand-written mapping between them; D3 says no caller ever touches a generated stub. This package is that
mapping for the internal FetchDataset contract, and this module is its front door.

    values.py          enums by member name, Timestamps as aware UTC, and ProtoMappingError
    fetch_dataset.py   one to_domain/to_proto pair per message
    fetch_stream.py    the SERVER-side encoder, which stamps a stream's bars with its resolved feed

WHY THE DESCRIPTORS ARE PART OF THE PUBLIC API. TID251 bans trader_joe.proto everywhere but common/rpc,
and a test is not the seam (architect, 05:11 UTC 2026-10-02). The descriptor-driven round-trip control
tj-3mk3u5.29 must therefore reach the generated messages THROUGH this surface: CONTRACT_FILES lets it walk
DESCRIPTOR.file, CONTRACT_MESSAGES hands it the message classes to build instances with, and
MESSAGE_MAPPERS pairs each one with the functions that are supposed to map it. A field added to a .proto
and not mapped then has somewhere to show up red.

THE TABLE IS TOTAL, AND THAT IS CHECKED AT IMPORT. Every top-level message of every contract file is
either in MESSAGE_MAPPERS or named in ENVELOPE_MESSAGES with the reason it has no domain twin, and every
enum is in ENUM_MAPPINGS. A new message that is neither fails the build rather than waiting for a test.
What import cannot check is whether a mapper maps all of ITS message's fields, which is exactly the hole
tj-3mk3u5.29 fills.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from google.protobuf.descriptor import FileDescriptor
from google.protobuf.message import Message
from pydantic import BaseModel

from common.rpc.mapping.fetch_dataset import (
    accepted_to_domain,
    accepted_to_proto,
    ack_to_domain,
    ack_to_proto,
    bar_to_domain,
    bar_to_proto,
    done_to_domain,
    done_to_proto,
    page_to_domain,
    page_to_proto,
    refused_to_domain,
    refused_to_proto,
    request_to_domain,
    request_to_proto,
    served_range_to_domain,
    served_range_to_proto,
)
from common.rpc.mapping.fetch_stream import MAX_PAGE_BARS, FetchStreamEncoder, refused_response
from common.rpc.mapping.values import (
    ASSET_TYPE,
    DATA_SOURCE,
    DATA_TYPE,
    ENUM_MAPPINGS,
    FEED,
    GRANULARITY,
    UPDATE_TYPE,
    EnumMapping,
    ProtoMappingError,
    timestamp_field,
    to_datetime,
    to_timestamp,
)
from schemas.data_ingest import fetch_dataset as domain
from trader_joe.proto.internal.ingest.v1 import ingest_pb2
from trader_joe.proto.market.v1 import bar_pb2, enums_pb2


@dataclass(frozen=True)
class MessageMapper[P: Message, D: BaseModel]:
    """One contract message and the two functions that convert it, so a control can pair them up.

    Attributes:
        proto_type: The generated message class.
        domain_type: Its Pydantic twin in schemas/data_ingest.
        to_domain: proto_type -> domain_type. Raises ProtoMappingError on anything it cannot read.
        to_proto: domain_type -> proto_type. Raises ProtoMappingError on anything it cannot write.
    """

    proto_type: type[P]
    domain_type: type[D]
    to_domain: Callable[[P], D]
    to_proto: Callable[[D], P]


# Every file of the contract, as the FileDescriptors a caller can walk without importing trader_joe.proto.
# market/v1 is shared vocabulary that the internal contract imports; both are here because a round trip
# over FetchDataset reaches Bar and the enums too.
CONTRACT_FILES: Final[tuple[FileDescriptor, ...]] = (ingest_pb2.DESCRIPTOR, bar_pb2.DESCRIPTOR, enums_pb2.DESCRIPTOR)

_MAPPERS: Final[tuple[MessageMapper, ...]] = (
    MessageMapper(ingest_pb2.FetchDatasetRequest, domain.FetchDatasetRequest, request_to_domain, request_to_proto),
    MessageMapper(ingest_pb2.FetchAck, domain.FetchAck, ack_to_domain, ack_to_proto),
    MessageMapper(ingest_pb2.FetchAccepted, domain.FetchAccepted, accepted_to_domain, accepted_to_proto),
    MessageMapper(ingest_pb2.FetchRefused, domain.FetchRefused, refused_to_domain, refused_to_proto),
    MessageMapper(ingest_pb2.BarPage, domain.BarPage, page_to_domain, page_to_proto),
    MessageMapper(ingest_pb2.FetchDone, domain.FetchDone, done_to_domain, done_to_proto),
    MessageMapper(ingest_pb2.ServedRange, domain.ServedRange, served_range_to_domain, served_range_to_proto),
    MessageMapper(bar_pb2.Bar, domain.Bar, bar_to_domain, bar_to_proto),
)

# The mapper for each message, by the message's full proto name.
MESSAGE_MAPPERS: Final[Mapping[str, MessageMapper]] = MappingProxyType(
    {mapper.proto_type.DESCRIPTOR.full_name: mapper for mapper in _MAPPERS}
)

# The message classes themselves, for a caller that builds instances -- by name, so nothing has to import
# the generated package to reach them.
CONTRACT_MESSAGES: Final[Mapping[str, type[Message]]] = MappingProxyType(
    {name: mapper.proto_type for name, mapper in MESSAGE_MAPPERS.items()}
)

# The messages that have NO domain twin, each with the reason, so the totality check above can be a check
# rather than a hope. A message joins this list only by review: it is the one place where "not mapped" is
# an answer.
ENVELOPE_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        'trader_joe.proto.internal.ingest.v1.FetchDatasetResponse': (
            "the stream's envelope is pure transport. Its oneof arm is dispatched by the client seam and "
            'set by the server-side encoder, and no domain model restates it (schemas/data_ingest/'
            'fetch_dataset.py, WHAT IS DELIBERATELY ABSENT)'
        )
    }
)


def _check_surface() -> None:
    """Refuse at import a contract this package does not cover, so no build can ship one.

    Raises:
        ValueError: If a contract file declares a top-level message that is neither mapped nor named in
            ENVELOPE_MESSAGES, an enum that is not in ENUM_MAPPINGS, or if ENVELOPE_MESSAGES names
            something that is not in the contract at all.
    """
    declared_messages = {
        descriptor.full_name for file in CONTRACT_FILES for descriptor in file.message_types_by_name.values()
    }
    covered = MESSAGE_MAPPERS.keys() | ENVELOPE_MESSAGES.keys()
    unmapped = sorted(declared_messages - covered)
    if unmapped:
        raise ValueError(
            f'{unmapped} have no mapper and are not named in ENVELOPE_MESSAGES; ADR tj-8konfu D1 maps every '
            'message of a contract by hand, and a new one is not covered until somebody writes it'
        )
    stale = sorted(covered - declared_messages)
    if stale:
        raise ValueError(f'{stale} are mapped here but are no longer declared by the contract')
    declared_enums = {
        descriptor.full_name for file in CONTRACT_FILES for descriptor in file.enum_types_by_name.values()
    }
    mapped_enums = {mapping.descriptor.full_name for mapping in ENUM_MAPPINGS}
    if declared_enums != mapped_enums:
        raise ValueError(
            f'the contract declares the enums {sorted(declared_enums)} and ENUM_MAPPINGS covers {sorted(mapped_enums)}'
        )


_check_surface()


__all__ = [
    'ASSET_TYPE',
    'CONTRACT_FILES',
    'CONTRACT_MESSAGES',
    'DATA_SOURCE',
    'DATA_TYPE',
    'ENUM_MAPPINGS',
    'ENVELOPE_MESSAGES',
    'FEED',
    'GRANULARITY',
    'MAX_PAGE_BARS',
    'MESSAGE_MAPPERS',
    'UPDATE_TYPE',
    'EnumMapping',
    'FetchStreamEncoder',
    'MessageMapper',
    'ProtoMappingError',
    'accepted_to_domain',
    'accepted_to_proto',
    'ack_to_domain',
    'ack_to_proto',
    'bar_to_domain',
    'bar_to_proto',
    'done_to_domain',
    'done_to_proto',
    'page_to_domain',
    'page_to_proto',
    'refused_response',
    'refused_to_domain',
    'refused_to_proto',
    'request_to_domain',
    'request_to_proto',
    'served_range_to_domain',
    'served_range_to_proto',
    'timestamp_field',
    'to_datetime',
    'to_timestamp',
]
