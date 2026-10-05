"""THE CONTROL THAT MAKES PROTO-AS-SOURCE-OF-TRUTH SAFE: a DESCRIPTOR-DRIVEN round trip (tj-3mk3u5.29).

ADR tj-8konfu D1 keeps the .proto and the Pydantic twins as two source-of-truth artefacts with a
HAND-WRITTEN mapping between them (``common/rpc/mapping``). That buys a reviewable seam and costs exactly
one thing: a field added to a .proto and forgotten in the mapper compiles, generates, validates and
crosses the wire carrying nothing. This file is the thing that makes that red.

WHY NOT FIXTURES. A fixture-seeded round trip -- build a message by hand, convert it, compare -- stays
green forever when a new field is never mapped, because the fixture never sets the new field either. So
every message here is built BY REFLECTION over its own descriptor, with EVERY declared field set to a
non-default value, one case per oneof arm, recursing into nested messages, repeated fields and maps. The
builder's own completeness is asserted (``ListFields()`` must cover exactly the fields the case declares)
before any equality is, so "the round trip passed" can never mean "the builder set nothing".

HOW THE GENERATED CODE IS REACHED. Not by importing it: TID251 bans ``trader_joe.proto`` outside
``common/rpc`` and no per-file-ignore or noqa is granted to a test (architect, 05:11 UTC 2026-10-02).
tj-3mk3u5.28 therefore exposes ``CONTRACT_FILES``, ``CONTRACT_MESSAGES``, ``MESSAGE_MAPPERS``,
``ENVELOPE_MESSAGES`` and ``ENUM_MAPPINGS`` as public API, and this file walks those.

ITEM 4 OF THE BEAD -- a check that nothing outside ``common/rpc`` imports the generated package -- IS
ALREADY DISCHARGED and no import-walker is written here. It is a LINT failure, not a test concern:
``pyproject.toml`` bans the package through ruff's TID251 banned-api table with ``common/rpc/**`` as the
one per-file-ignore, and ``common/tests/test_ci_invariants.py`` pins that by running ruff over every
spelling of the import -- absolute, relative and namespace-relative -- and asserting TID251 fires, plus a
guard that it fires because of the BAN and not because ruff flags every import.

WHAT IS NOT HERE. The feed-equality invariant is a CROSS-MESSAGE STREAM rule that no per-message round
trip can express, and the stream's grammar is the client seam's: both live beside this file, in
``test_rpc_fetch_feed_equality.py`` and ``test_rpc_fetch_seam.py``.
"""

import itertools
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta, timezone

import pytest
from google.protobuf.descriptor import Descriptor, FieldDescriptor, OneofDescriptor
from google.protobuf.message import Message
from pydantic import BaseModel

from common.enums.data_stock import Feed
from common.rpc.mapping import (
    CONTRACT_FILES,
    CONTRACT_MESSAGES,
    ENUM_MAPPINGS,
    ENVELOPE_MESSAGES,
    MAX_PAGE_BARS,
    MESSAGE_MAPPERS,
    EnumMapping,
    MessageMapper,
    ProtoMappingError,
    bar_to_domain,
    request_to_domain,
    request_to_proto,
    to_timestamp,
)
from common.tests.domain_fields import aware_datetime_fields
from common.tests.proto_descriptors import PROTO_ROOT
from schemas.data_ingest import fetch_dataset as domain


pytestmark = pytest.mark.common

CONTRACT = 'trader_joe/proto/internal/ingest/v1/ingest.proto'

# The instant every generated Timestamp is offset from. Aware UTC, and far from the epoch so a mapper
# that silently produced a default would not land on it.
EPOCH_BASE = datetime(2026, 1, 1, tzinfo=UTC)

# How many elements a repeated field gets. TWO, never one: a mapper that reads only the first element of
# a list round-trips a one-element list perfectly.
REPEATED = 2

GOOGLE = 'google.protobuf'


# ---------------------------------------------------------------------------------------------
# BUILDING A FULLY POPULATED MESSAGE, BY REFLECTION


class _Values:
    """A counter that makes every value in one message DISTINCT.

    Distinctness is the point, not variety: ``Bar`` has five doubles in a row, and a mapper that swapped
    ``high`` and ``low`` would round-trip perfectly if both carried the same number.
    """

    def __init__(self) -> None:
        self._issued = 0

    def next(self) -> int:
        """Returns: int: The next value, always >= 1 so it is never a proto3 default."""
        self._issued += 1
        return self._issued


def _is_map(field: FieldDescriptor) -> bool:
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def _is_well_known(descriptor: Descriptor) -> bool:
    return descriptor.file.package == GOOGLE


def _scalar(field: FieldDescriptor, values: _Values) -> object:
    issued = values.next()
    if field.type == FieldDescriptor.TYPE_STRING:
        return f'{field.name}-{issued}'
    if field.type == FieldDescriptor.TYPE_BYTES:
        return f'{field.name}-{issued}'.encode()
    if field.type == FieldDescriptor.TYPE_BOOL:
        return True
    if field.type in {FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT}:
        return issued + 0.5
    if field.type == FieldDescriptor.TYPE_ENUM:
        # Never the zero: it is <ENUM>_UNSPECIFIED, which has no Python member by design (tj-vhboky.1).
        # Rotating through the members means two enum fields of the same type rarely agree, so a mapper
        # that crossed them is caught.
        members = [value.number for value in field.enum_type.values if value.number != 0]
        return members[issued % len(members)]
    return issued


def _timestamp(message: Message, values: _Values) -> None:
    # MICROSECOND-ALIGNED and non-zero in both components: a datetime holds microseconds, the mapper
    # refuses anything finer, and a zero would be indistinguishable from an unset field.
    issued = values.next()
    message.seconds = int(EPOCH_BASE.timestamp()) + issued
    message.nanos = issued * 1000


def _real_oneofs(descriptor: Descriptor) -> list[OneofDescriptor]:
    # proto3's `optional` keyword is implemented as a ONE-ARM SYNTHETIC oneof named _<field>, so
    # Bar.trade_count reports a containing oneof named _trade_count. Treating that as a choice would make
    # half of Bar's fields mutually exclusive and the round trip would never see them together.
    # ``Descriptor.real_oneofs`` exists in the pure-Python runtime but NOT in the upb one this project
    # runs on, so the synthetic ones are recognised by the shape protoc gives them, as
    # ``common/tests/proto_descriptors.py`` already does from the FileDescriptorProto side.
    return [
        oneof
        for oneof in descriptor.oneofs
        if not (len(oneof.fields) == 1 and oneof.name == f'_{oneof.fields[0].name}')
    ]


def _real_oneof(field: FieldDescriptor, descriptor: Descriptor) -> OneofDescriptor | None:
    oneof = field.containing_oneof
    return oneof if oneof is not None and oneof in _real_oneofs(descriptor) else None


def _oneof_axes(descriptor: Descriptor, prefix: str = '') -> list[tuple[str, tuple[str, ...]]]:
    # Every real oneof reachable from this message, as (dotted path, arm names). One case per arm is the
    # bead's requirement, and a oneof inside a nested message is still a oneof.
    axes = [
        (f'{prefix}{oneof.name}', tuple(field.name for field in oneof.fields)) for oneof in _real_oneofs(descriptor)
    ]
    for field in descriptor.fields:
        if field.message_type is not None and not _is_map(field) and not _is_well_known(field.message_type):
            axes.extend(_oneof_axes(field.message_type, f'{prefix}{field.name}.'))
    return axes


def _cases(descriptor: Descriptor) -> list[Mapping[str, str]]:
    axes = _oneof_axes(descriptor)
    if not axes:
        return [{}]
    paths = [path for path, _ in axes]
    return [dict(zip(paths, arms, strict=True)) for arms in itertools.product(*(arms for _, arms in axes))]


def _chosen(descriptor: Descriptor, choices: Mapping[str, str], prefix: str) -> list[FieldDescriptor]:
    # The fields this case declares: everything, minus the arms of a real oneof that this case did not
    # pick. It is what the builder sets AND what ListFields() must then report, which is how the builder
    # is kept honest.
    chosen = []
    for field in descriptor.fields:
        oneof = _real_oneof(field, descriptor)
        if oneof is not None and choices.get(f'{prefix}{oneof.name}') != field.name:
            continue
        chosen.append(field)
    return chosen


def _populate(message: Message, choices: Mapping[str, str], values: _Values, prefix: str = '') -> None:
    for field in _chosen(message.DESCRIPTOR, choices, prefix):
        _set_field(message, field, choices, values, prefix)


def _set_field(message: Message, field: FieldDescriptor, choices: Mapping[str, str], values: _Values, prefix: str):
    target = getattr(message, field.name)
    if _is_map(field):
        entry = field.message_type
        for _ in range(REPEATED):
            target[_scalar(entry.fields_by_name['key'], values)] = _scalar(entry.fields_by_name['value'], values)
    elif field.is_repeated and field.message_type is not None:
        for _ in range(REPEATED):
            _build_sub(target.add(), field, choices, values, prefix)
    elif field.is_repeated:
        target.extend(_scalar(field, values) for _ in range(REPEATED))
    elif field.message_type is not None:
        _build_sub(target, field, choices, values, prefix)
    else:
        setattr(message, field.name, _scalar(field, values))


def _build_sub(sub: Message, field: FieldDescriptor, choices: Mapping[str, str], values: _Values, prefix: str):
    if _is_well_known(field.message_type):
        _timestamp(sub, values)
    else:
        _populate(sub, choices, values, f'{prefix}{field.name}.')


def _built(mapper: MessageMapper, choices: Mapping[str, str]) -> Message:
    message = mapper.proto_type()
    _populate(message, choices, _Values())
    return message


def _all_cases() -> Iterator[tuple[str, MessageMapper, Mapping[str, str]]]:
    for name, mapper in sorted(MESSAGE_MAPPERS.items()):
        for choices in _cases(mapper.proto_type.DESCRIPTOR):
            yield name, mapper, choices


CASES = list(_all_cases())
CASE_IDS = [
    f'{name.rsplit(".", 1)[-1]}[{",".join(sorted(choices.values())) or "no-oneof"}]' for name, _, choices in CASES
]


# ---------------------------------------------------------------------------------------------
# ITEM 1 -- THE ROUND TRIP


@pytest.mark.parametrize(('name', 'mapper', 'choices'), CASES, ids=CASE_IDS)
def test_the_builder_sets_every_field_this_case_declares(name: str, mapper: MessageMapper, choices):
    """GUARD THE GUARD, and run it first: an empty message round-trips through anything.

    The round trip below is only as strong as the message handed to it, and the message is built by
    reflection -- so a bug in the builder (a field type it silently skips, a oneof arm it misreads) would
    weaken every assertion in this file without failing one of them. ``ListFields()`` reports exactly the
    fields that are SET, so comparing it with the fields the case declares is a direct check that the
    builder populated all of them and nothing else.

    Args:
        name: The message's full proto name.
        mapper: Its entry in MESSAGE_MAPPERS.
        choices: Which arm of each reachable oneof this case takes.
    """
    message = _built(mapper, choices)

    declared = {field.name for field in _chosen(mapper.proto_type.DESCRIPTOR, choices, '')}

    assert {field.name for field, _ in message.ListFields()} == declared, (
        f'the reflective builder did not populate {name} completely, so every other assertion about it '
        'is weaker than it looks'
    )


@pytest.mark.parametrize(('name', 'mapper', 'choices'), CASES, ids=CASE_IDS)
def test_a_fully_populated_message_survives_proto_to_domain_to_proto(name: str, mapper: MessageMapper, choices):
    """THE DEFECT THIS FILE EXISTS FOR: a field on the .proto that the hand-written mapper never reads.

    Such a field is set on the way in, dropped by ``to_domain``, never written by ``to_proto``, and the
    re-encoded message differs from the original -- here, and nowhere else in the suite. Fixture-seeded
    round trips cannot see it, because a fixture written today does not set a field added tomorrow.

    Args:
        name: The message's full proto name.
        mapper: Its entry in MESSAGE_MAPPERS.
        choices: Which arm of each reachable oneof this case takes.
    """
    message = _built(mapper, choices)

    assert mapper.to_proto(mapper.to_domain(message)) == message, (
        f'{name} does not survive the mapping; a field declared on the .proto is not mapped in '
        'common/rpc/mapping, so it crosses the wire carrying nothing'
    )


@pytest.mark.parametrize(('name', 'mapper', 'choices'), CASES, ids=CASE_IDS)
def test_the_domain_side_receives_a_value_for_every_field_the_case_sets(name: str, mapper: MessageMapper, choices):
    """THE REVERSE SET CHECK, on the domain side: every twin field actually got the wire's value.

    The equality above would also pass if ``to_domain`` and ``to_proto`` dropped the SAME field in step,
    as a mapper written from one side's field list would. This asserts the domain object itself is
    complete, so the two halves have to agree with the descriptor and not merely with each other.

    Args:
        name: The message's full proto name.
        mapper: Its entry in MESSAGE_MAPPERS.
        choices: Which arm of each reachable oneof this case takes.
    """
    model = mapper.to_domain(_built(mapper, choices))

    declared = {field.name for field in _chosen(mapper.proto_type.DESCRIPTOR, choices, '')}

    assert {name for name in type(model).model_fields if getattr(model, name) is not None} == declared


@pytest.mark.parametrize(('name', 'mapper', 'choices'), CASES, ids=CASE_IDS)
def test_a_fully_populated_domain_model_survives_domain_to_proto_to_domain(name: str, mapper: MessageMapper, choices):
    """The other direction, which the one above does not imply.

    A mapper can be faithful read-to-write and still lose a value write-to-read -- an encoder that writes
    a field the decoder ignores is exactly the shape of a half-finished edit.

    Args:
        name: The message's full proto name.
        mapper: Its entry in MESSAGE_MAPPERS.
        choices: Which arm of each reachable oneof this case takes.
    """
    model = mapper.to_domain(_built(mapper, choices))

    assert mapper.to_domain(mapper.to_proto(model)) == model


def test_every_message_the_contract_declares_is_mapped_or_named_as_transport():
    """A message added to a .proto with no mapper, which the round trip above cannot see: it enumerates MAPPERS.

    ``common/rpc/mapping`` runs the same check at IMPORT, over top-level messages. This one also walks
    NESTED messages -- skipping the entries protoc synthesises for a ``map<k, v>`` field, which are not
    contract messages -- so a message nested inside another, which the import check does not reach, is
    covered here.
    """
    declared = {
        descriptor.full_name
        for file in CONTRACT_FILES
        for descriptor in _declared_messages(file.message_types_by_name.values())
    }

    assert declared == MESSAGE_MAPPERS.keys() | ENVELOPE_MESSAGES.keys()
    assert CONTRACT_MESSAGES.keys() == MESSAGE_MAPPERS.keys()


def _declared_messages(descriptors) -> Iterator[Descriptor]:
    for descriptor in descriptors:
        if descriptor.GetOptions().map_entry:
            continue
        yield descriptor
        yield from _declared_messages(descriptor.nested_types)


# ---------------------------------------------------------------------------------------------
# ITEM 2 -- ENUM BIJECTION


@pytest.mark.parametrize('mapping', ENUM_MAPPINGS, ids=[mapping.python_type.__name__ for mapping in ENUM_MAPPINGS])
def test_a_proto_enums_value_names_are_a_bijection_with_its_python_members(mapping: EnumMapping):
    """Enums cross by member NAME and never by number (tj-vhboky.30): Granularity.ONE_MINUTE is '1min' in Python.

    So the two vocabularies must name the same members, with the buf prefix stripped, and every member
    must survive a round trip. ``common/rpc/mapping/values.py`` proves this at import too; pinned here as
    well because an import-time check that is quietly loosened leaves nothing behind.

    Args:
        mapping: One proto enum and the Python enum it mirrors.
    """
    on_wire = {value.name.removeprefix(mapping.prefix) for value in mapping.descriptor.values if value.number != 0}

    assert on_wire == set(mapping.python_type.__members__)
    for member in mapping.python_type:
        assert mapping.to_domain(mapping.to_proto(member)) is member


@pytest.mark.parametrize('mapping', ENUM_MAPPINGS, ids=[mapping.python_type.__name__ for mapping in ENUM_MAPPINGS])
def test_the_unspecified_zero_is_refused_rather_than_decoded(mapping: EnumMapping):
    """proto3 forces a zero and buf names it <ENUM>_UNSPECIFIED; common/enums deliberately has no member for it.

    A value that should never be written is better expressed as an ERROR than as an enum member (user
    ruling, tj-vhboky.1), so decoding the zero is refused rather than defaulted to a plausible tape.

    Args:
        mapping: One proto enum and the Python enum it mirrors.
    """
    assert mapping.descriptor.values_by_number[0].name == f'{mapping.prefix}UNSPECIFIED'
    assert 'UNSPECIFIED' not in mapping.python_type.__members__

    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        mapping.to_domain(0)


@pytest.mark.parametrize(
    ('message_name', 'field'),
    [
        ('trader_joe.proto.market.v1.Bar', 'feed'),
        ('trader_joe.proto.internal.ingest.v1.FetchAccepted', 'feed'),
        ('trader_joe.proto.internal.ingest.v1.FetchDatasetRequest', 'source'),
        ('trader_joe.proto.internal.ingest.v1.FetchDatasetRequest', 'asset_type'),
        ('trader_joe.proto.internal.ingest.v1.FetchDatasetRequest', 'granularity'),
        ('trader_joe.proto.internal.ingest.v1.FetchDatasetRequest', 'update_type'),
    ],
)
def test_a_required_enum_left_unset_makes_the_whole_message_unmappable(message_name: str, field: str):
    """The refusal reaching a real message, not only the leaf conversion.

    A message that leaves a required enum at the proto3 zero is one this release cannot read, and the
    seam turns that into PEER_PROTOCOL_ERROR rather than a bar whose tape nobody resolved.

    Args:
        message_name: The contract message's full proto name.
        field: The required enum field to leave unset.
    """
    mapper = MESSAGE_MAPPERS[message_name]
    message = _built(mapper, {})
    message.ClearField(field)

    with pytest.raises(ProtoMappingError):
        mapper.to_domain(message)


def test_the_requests_feed_is_the_one_zero_that_means_something():
    """FetchDatasetRequest.feed is the exception, and it is the only one: unset means "the deployment decides".

    The .proto deliberately gives it no ``optional`` keyword, so unset and FEED_UNSPECIFIED are one state
    (tj-3mk3u5.22 Q5). It must therefore decode to None rather than being refused like every other enum,
    and must not be defaulted to a tape.
    """
    mapper = MESSAGE_MAPPERS['trader_joe.proto.internal.ingest.v1.FetchDatasetRequest']
    message = _built(mapper, {})
    message.ClearField('feed')

    assert mapper.to_domain(message).feed is None
    assert mapper.to_proto(mapper.to_domain(message)) == message


# ---------------------------------------------------------------------------------------------
# ITEM 3 -- DATETIME OFFSETS, through the real mapping


OFFSETS = [timezone(timedelta(hours=-5)), timezone(timedelta(hours=9, minutes=30)), UTC]
OFFSET_IDS = ['minus-05-00', 'plus-09-30', 'Z']


def _twins_at(when: datetime) -> dict[type[BaseModel], BaseModel]:
    # One instance of every twin that HAS an aware field, with all of them at `when`. The tripwire below
    # asserts this covers exactly those twins and exactly those fields.
    served = domain.ServedRange(start=when, end=when + timedelta(hours=1))
    return {
        domain.FetchDatasetRequest: domain.FetchDatasetRequest(
            owner='rebalancer',
            source='ALPACA',
            asset_symbol='VFV',
            asset_type='stock',
            data_types=['market-activity'],
            granularity='1day',
            start=when,
            end=when + timedelta(hours=1),
            update_type=2,
            feed=Feed.IEX,
        ),
        domain.Bar: domain.Bar(bar_start=when, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0, feed=Feed.IEX),
        domain.ServedRange: served,
        domain.FetchDone: domain.FetchDone(bar_count=3, served_range=served, as_of=when),
    }


def test_the_offsets_sweep_drives_every_aware_field_of_every_twin():
    """The tripwire the hand-written sweep in schemas/tests did not have, applied to this one.

    A new time field on a twin, or a twin that gains its first one, has to arrive with a case here. The
    set is DERIVED from the annotations, so adding the field is what makes this red -- not remembering to.
    """
    driven = {
        (type(model), field) for model in _twins_at(EPOCH_BASE).values() for field in aware_datetime_fields(type(model))
    }
    declared = {
        (mapper.domain_type, field)
        for mapper in MESSAGE_MAPPERS.values()
        for field in aware_datetime_fields(mapper.domain_type)
    }

    assert driven == declared


@pytest.mark.parametrize('offset', OFFSETS, ids=OFFSET_IDS)
def test_an_offset_survives_the_real_encode_and_decode_as_the_same_instant_in_utc(offset: timezone):
    """The successor to the Kafka envelope's offset test (23f953f), through the MAPPING and not a model dump.

    Q4 was ruled O-a (user, 02:40 UTC 2026-09-28): the wire type is google.protobuf.Timestamp, which
    carries no offset at all. So what is asserted is the INSTANT and that the result is AWARE and UTC --
    never naive, because a naive datetime is exactly what the twins refuse, and the transport must never
    be the thing that trips that refusal. The sender's own utcoffset is NOT asserted: a Timestamp cannot
    carry it, and the user ruled that loss acceptable.

    Args:
        offset: The sender's timezone.
    """
    when = EPOCH_BASE.astimezone(offset)
    assert when.utcoffset() == offset.utcoffset(None), 'the case did not actually move the datetime off UTC'

    for model in _twins_at(when).values():
        mapper = next(m for m in MESSAGE_MAPPERS.values() if m.domain_type is type(model))
        decoded = mapper.to_domain(mapper.to_proto(model))
        for field in aware_datetime_fields(type(model)):
            original, result = getattr(model, field), getattr(decoded, field)
            assert result == original, f'{type(model).__name__}.{field} changed instant'
            assert result.utcoffset() == timedelta(0), f'{type(model).__name__}.{field} came back off UTC'
            assert result.tzinfo is not None


def test_a_naive_datetime_is_refused_on_the_way_out_rather_than_assumed_to_be_utc():
    """Encoding is where a naive value must die: a Timestamp would silently read it as whatever the host thinks.

    The twins refuse a naive value at construction, so this reaches the leaf conversion directly -- which
    is the layer a servicer building a message by hand would go through.
    """
    with pytest.raises(ProtoMappingError, match='naive'):
        to_timestamp(datetime(2026, 1, 1))


# ---------------------------------------------------------------------------------------------
# PRESENCE, AND THE TWO NUMBERS THE WIRE CANNOT CARRY


def test_an_absent_end_stays_absent_in_both_directions():
    """FetchDatasetRequest.end: the optional time field, and the one the hand-written sweep had missed.

    An open end is an ORDINARY request -- "up to whatever is current" -- so it must not become the epoch
    on decode nor a set field on encode. proto3 gives a message field explicit presence, which is what
    makes the two states distinguishable without a flag.
    """
    mapper = MESSAGE_MAPPERS['trader_joe.proto.internal.ingest.v1.FetchDatasetRequest']
    message = _built(mapper, {})
    message.ClearField('end')

    open_ended = mapper.to_domain(message)
    assert open_ended.end is None
    assert not request_to_proto(open_ended).HasField('end')
    assert request_to_proto(open_ended) == message

    bounded = open_ended.model_copy(update={'end': datetime(2026, 6, 1, 12, 30, tzinfo=UTC)})
    assert request_to_proto(bounded).HasField('end')
    assert request_to_domain(request_to_proto(bounded)).end == bounded.end


def test_an_absent_optional_on_a_bar_stays_absent_rather_than_becoming_zero():
    """Bar.trade_count and Bar.vwap: a vendor that reports neither is not reporting zero of each.

    proto3's ``optional`` keyword gives them real presence, so None has somewhere to go. Without the
    HasField check a bar with no vwap would arrive as a bar whose vwap is 0.0, which is a price.
    """
    mapper = MESSAGE_MAPPERS['trader_joe.proto.market.v1.Bar']
    message = _built(mapper, {})
    message.ClearField('trade_count')
    message.ClearField('vwap')

    bar = mapper.to_domain(message)
    assert (bar.trade_count, bar.vwap) == (None, None)
    assert mapper.to_proto(bar) == message


def test_a_sub_microsecond_timestamp_is_refused_rather_than_rounded():
    """A datetime holds microseconds; a Timestamp holds nanoseconds, so the wire can name an instant Python cannot.

    Rounding it would move the instant a bar is stamped with, silently -- and would break the identity
    the round trip above asserts, since the re-encoded Timestamp would no longer equal the original.
    """
    mapper = MESSAGE_MAPPERS['trader_joe.proto.market.v1.Bar']
    message = _built(mapper, {})
    message.bar_start.nanos += 1

    with pytest.raises(ProtoMappingError, match='sub-microsecond'):
        bar_to_domain(message)


def test_an_unset_required_timestamp_is_refused_rather_than_read_as_the_epoch():
    """1970-01-01 is a real instant, and an unset field is not it.

    Every Timestamp on this contract but ``FetchDatasetRequest.end`` is required, so the absence is a
    message this release cannot read rather than a default worth decoding.
    """
    mapper = MESSAGE_MAPPERS['trader_joe.proto.internal.ingest.v1.ServedRange']
    message = _built(mapper, {})
    message.ClearField('start')

    with pytest.raises(ProtoMappingError, match='unset'):
        mapper.to_domain(message)


def test_the_exported_page_size_is_the_number_the_contract_states():
    """MAX_PAGE_BARS exists because the page size is the SERVER's to honour and the wire cannot express it.

    The number lives in a .proto comment, where no Python caller can reach it, so the servicer that
    chunks a fetch (tj-3mk3u5.9) reads it from ``common.rpc.mapping`` instead. Two copies of a number
    that must agree is exactly the drift this reads the .proto to prevent.
    """
    stated = re.search(r'THE PAGE SIZE IS (\d+) BARS', (PROTO_ROOT / CONTRACT).read_text(encoding='utf-8'))

    assert stated is not None, 'ingest.proto no longer states its page size, which is the only place it is written'
    assert int(stated.group(1)) == MAX_PAGE_BARS
