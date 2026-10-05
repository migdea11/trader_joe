"""THE INTERNAL FetchDataset CONTRACT, pinned against its design (tj-3mk3u5.27).

What this file is for: the contract is a SHAPE, and every ruling that shaped it is invisible in the
generated code and easy to undo by accident. The .proto is read through protoc's own descriptors
(``common/tests/proto_descriptors``), never by importing ``trader_joe.proto`` -- TID251 keeps the
generated package private to ``common/rpc``, and a test is not that seam.

THE RULINGS PINNED HERE, each with the record that made it:

* THE ACK CARRIES THE RESOLVED FEED, and is always the stream's first event. The store writes its
  dataset entry, feed included, only once it has the ack (tj-ugl90j, tj-rh4b7f).
* A REFUSAL TRAVELS INSIDE THE ACK, NEVER AS A gRPC STATUS (user ruling, tj-3mk3u5.22 Q5). This is
  the one most at risk: "a refusal is an error, errors are statuses" is the obvious shape, it is what
  the bead originally said, and moving ``FetchRefused`` out to a status would still compile, still
  generate and still pass every other test in this repository.
* THE STREAM IS ack -> page* -> done, expressed as the one oneof on ``FetchDatasetResponse``, with
  ``FetchDone`` carrying ``served_range`` and ``as_of`` (ADR tj-fa1rpu D2, the SERVED provenance).
* NO dataset_id ANYWHERE: correlation is the transport's and the store stamps its own rows
  (ADR tj-8konfu D7.2, tj-rh4b7f).
* EVERY TIME IS google.protobuf.Timestamp (tj-3mk3u5.22 Q4), and aware on the Python side.
* THE PYDANTIC TWINS MATCH THE PROTO FIELD-FOR-FIELD. tj-3mk3u5.29's descriptor-driven round trip
  will check this again under the mapper's name table, but .29 needs the mapper (tj-3mk3u5.28) to
  exist. Until then a field added to one side and not the other is a defect nothing would see, and it
  would surface two beads downstream as a confusing round-trip failure rather than here as "these two
  declarations disagree".

WHAT THIS FILE DELIBERATELY DOES NOT DO, so it does not become tj-3mk3u5.29 written early: it
converts nothing. No proto message is built, no domain model is encoded, no value crosses between
them. It compares DECLARATIONS. The round trip -- values, the name table, UNSPECIFIED refused on
decode, offsets -- is .29's, and duplicating it here would mean guessing at a mapper that does not
exist yet.

The marker is ``data_ingest``: this drives the ingest service's fetch interface. The shared
vocabulary it leans on is pinned separately, in ``common/tests/test_proto_market_vocabulary.py``.
"""

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from common.enums.data_stock import Feed, UpdateType
from common.tests.proto_descriptors import field_names, file_named, messages, real_oneofs
from schemas.data_ingest import fetch_dataset
from schemas.data_ingest.fetch_dataset import (
    Bar,
    BarPage,
    FetchAccepted,
    FetchAck,
    FetchDatasetRequest,
    FetchDone,
    FetchRefused,
    ServedRange,
)


pytestmark = pytest.mark.data_ingest

CONTRACT = 'trader_joe/proto/internal/ingest/v1/ingest.proto'
BAR = 'trader_joe/proto/market/v1/bar.proto'

TIMESTAMP = '.google.protobuf.Timestamp'
MARKET = '.trader_joe.proto.market.v1'
INGEST = '.trader_joe.proto.internal.ingest.v1'

WHEN = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# THE SERVICE AND THE STREAM'S GRAMMAR


def test_fetch_dataset_is_one_server_stream_at_the_wire_path_the_design_names():
    """The method, its direction and its wire path, which the servicer and the client seam both bind to.

    A unary FetchDataset would still generate and would still carry an ack; it could not carry pages.
    The wire path is asserted as the string it is because that, not the Python symbol, is what a peer
    dials -- and it is what the servicer registration (tj-3mk3u5.9) and the client seam
    (tj-3mk3u5.28) must agree on.
    """
    file = file_named(CONTRACT)
    assert file.package == 'trader_joe.proto.internal.ingest.v1'
    assert [service.name for service in file.service] == ['IngestService']

    methods = {method.name: method for method in file.service[0].method}
    assert list(methods) == ['FetchDataset']
    fetch = methods['FetchDataset']
    assert fetch.server_streaming is True, 'bar pages need a stream; a unary call cannot carry them'
    assert fetch.client_streaming is False
    assert fetch.input_type == f'{INGEST}.FetchDatasetRequest'
    assert fetch.output_type == f'{INGEST}.FetchDatasetResponse'

    wire_path = f'/{file.package}.{file.service[0].name}/{fetch.name}'
    assert wire_path == '/trader_joe.proto.internal.ingest.v1.IngestService/FetchDataset'


def test_the_stream_is_ack_then_pages_then_done_and_the_ack_is_arm_one():
    """ADR tj-8konfu D7.1: the oneof exists from day one, and its arms ARE the stream's grammar.

    Field numbers are asserted, not just names. The ack being arm 1 is how "an ack is always the first
    event" is written down in a schema that cannot otherwise express ordering; a fourth arm appended
    later is additive and fine, but renumbering or reordering these three is not.
    """
    response = messages(file_named(CONTRACT))['FetchDatasetResponse']

    assert real_oneofs(response) == {'event': ['ack', 'page', 'done']}
    assert [(field.name, field.number, field.type_name) for field in response.field] == [
        ('ack', 1, f'{INGEST}.FetchAck'),
        ('page', 2, f'{INGEST}.BarPage'),
        ('done', 3, f'{INGEST}.FetchDone'),
    ]


# ---------------------------------------------------------------------------------------------
# THE ACK: THE RESOLVED FEED, AND A REFUSAL THAT NEVER BECOMES A STATUS


def test_the_ack_is_a_oneof_of_accepted_and_refused():
    """User ruling tj-3mk3u5.22 Q5 as amended 2026-10-02: the ack answers the feed, both ways.

    The ack being a oneof rather than an accepted-with-an-optional-error is what makes "exactly one of
    these happened" unrepresentable-otherwise rather than merely documented.
    """
    ack = messages(file_named(CONTRACT))['FetchAck']

    assert real_oneofs(ack) == {'outcome': ['accepted', 'refused']}
    assert [(field.name, field.number, field.type_name) for field in ack.field] == [
        ('accepted', 1, f'{INGEST}.FetchAccepted'),
        ('refused', 2, f'{INGEST}.FetchRefused'),
    ]


def test_the_accepted_arm_carries_the_resolved_feed_and_nothing_else():
    """The one value only ingest may decide, and the reason this whole shape exists (tj-ugl90j, tj-rh4b7f).

    "And nothing else" is load-bearing: the ack is not a general-purpose preamble. A second field here
    would be a thing the store must decide what to do with before it writes its dataset entry.
    """
    accepted = messages(file_named(CONTRACT))['FetchAccepted']

    assert [(field.name, field.type_name) for field in accepted.field] == [('feed', f'{MARKET}.Feed')]


def test_a_refusal_is_reachable_only_inside_the_ack_and_never_as_a_status_shape():
    """THE Q5 RULING, pinned structurally rather than by reading a comment.

    An unservable feed is refused IN the ack; it is never a gRPC status, and the stream still ends OK.
    What makes that true of the schema is that ``FetchRefused`` has exactly one referent in the whole
    contract -- ``FetchAck.refused`` -- so there is no second door it could arrive through. The
    failure this catches is a future edit that adds, say, ``FetchDatasetResponse.error`` or a status
    detail message: both would compile, both would generate, and both would quietly reintroduce the
    out-of-band refusal the user ruled against.

    Every OTHER failure on this hop is a google.rpc.Status + ErrorInfo built by common/rpc
    (tj-3mk3u5.37.7). Nothing in this proto models one, which is why the contract imports no
    google/rpc file.
    """
    file = file_named(CONTRACT)
    declared = messages(file)

    referents = [
        (name, field.name)
        for name, message in declared.items()
        for field in message.field
        if field.type_name == f'{INGEST}.FetchRefused'
    ]
    assert referents == [('FetchAck', 'refused')], f'FetchRefused reached from somewhere new: {referents}'

    assert not [dependency for dependency in file.dependency if dependency.startswith('google/rpc/')], (
        'the refused arm MIRRORS ErrorInfo as plain fields on purpose; importing google/rpc would need '
        'its own include root and the googleapis-common-protos package'
    )


def test_the_refused_arm_mirrors_error_info_field_for_field():
    """ADR tj-fa1rpu's REFUSED identity, as four plain fields (the 2026-10-02 addendum to tj-3mk3u5.27).

    ``(domain, reason)`` is the error's identity and ``metadata`` is allowlisted context, so the client
    seam can build the SAME domain error a REFUSED status would have produced. ``reason`` is a string
    and not an enum deliberately: a client must survive a reason it has never heard of.

    No ``reset_at`` and no retry hint: REFUSED is permanent for the request as asked, and a retry field
    here would invite a caller to wait for something no wait cures.
    """
    refused = messages(file_named(CONTRACT))['FetchRefused']

    assert field_names(refused) == ['reason', 'domain', 'detail', 'metadata']
    by_name = {field.name: field for field in refused.field}
    for scalar in ('reason', 'domain', 'detail'):
        assert by_name[scalar].type == by_name[scalar].TYPE_STRING, f'{scalar} must be a string'
    # protoc compiles map<string, string> to a repeated nested MetadataEntry message.
    assert by_name['metadata'].label == by_name['metadata'].LABEL_REPEATED
    entry = {nested.name: nested for nested in refused.nested_type}['MetadataEntry']
    assert entry.options.map_entry is True
    assert [(field.name, field.type) for field in entry.field] == [
        ('key', entry.field[0].TYPE_STRING),
        ('value', entry.field[1].TYPE_STRING),
    ]


# ---------------------------------------------------------------------------------------------
# THE PAGE AND THE DONE


def test_a_page_wraps_the_shared_bar_and_restates_none_of_it():
    """Decision tj-3mk3u5.42 F1 rule 8: Bar is IMPORTED, never copied.

    A BarPage that redeclared the bar's fields -- or carried a second, page-local bar message -- is the
    duplicated structure the hierarchy exists to prevent; the two copies drift and nothing says so.
    """
    page = messages(file_named(CONTRACT))['BarPage']

    assert [(field.name, field.type_name, field.label) for field in page.field] == [
        ('bars', f'{MARKET}.Bar', page.field[0].LABEL_REPEATED)
    ]
    assert not page.nested_type, f'a page declares no bar shape of its own: {[n.name for n in page.nested_type]}'


def test_done_carries_the_count_and_the_served_provenance():
    """ADR tj-fa1rpu D2 plus the 2026-10-02 addendum: what was actually served, not what was asked for.

    FetchDone existing at all is what makes a stream that merely stops distinguishable from one that
    finished, and ``bar_count`` 0 with a served range is how "no data" stays a success carrying
    provenance rather than an error.
    """
    declared = messages(file_named(CONTRACT))
    done = declared['FetchDone']

    assert [(field.name, field.type_name) for field in done.field] == [
        ('bar_count', ''),
        ('served_range', f'{INGEST}.ServedRange'),
        ('as_of', TIMESTAMP),
    ]
    assert done.field[0].type == done.field[0].TYPE_UINT64

    assert [(field.name, field.type_name) for field in declared['ServedRange'].field] == [
        ('start', TIMESTAMP),
        ('end', TIMESTAMP),
    ]


# ---------------------------------------------------------------------------------------------
# THE REQUEST, AND WHAT IS DELIBERATELY NOT ON THE WIRE


def test_the_request_carries_exactly_what_a_reader_consumes():
    """Established by reading data/ingest, not by copying GetDatasetRequest.

    The exact list is asserted both ways. An omission breaks the fetch; an ADDITION is the thing worth
    catching, because a field that no reader touches is one a future author will assume is honoured.
    """
    request = messages(file_named(CONTRACT))['FetchDatasetRequest']

    assert [(field.name, field.number, field.type_name) for field in request.field] == [
        ('owner', 1, ''),
        ('source', 2, f'{MARKET}.DataSource'),
        ('asset_symbol', 3, ''),
        ('asset_type', 4, f'{MARKET}.AssetType'),
        ('data_types', 5, f'{MARKET}.DataType'),
        ('granularity', 6, f'{MARKET}.Granularity'),
        ('start', 7, TIMESTAMP),
        ('end', 8, TIMESTAMP),
        ('update_type', 9, f'{MARKET}.UpdateType'),
        ('feed', 10, f'{MARKET}.Feed'),
    ]
    by_name = {field.name: field for field in request.field}
    assert by_name['data_types'].label == by_name['data_types'].LABEL_REPEATED, 'at least one, so repeated'


def test_an_unset_end_is_distinguishable_from_the_epoch_but_an_unset_feed_is_not():
    """The two "absent" fields, which need OPPOSITE treatments, and each would be wrong as the other.

    ``end`` is a Timestamp MESSAGE, and a message field has explicit presence in proto3, so "no end"
    and "the epoch" are two different requests without an extra flag. An open end means "up to
    whatever is current" and is ordinary, not an omission.

    ``feed`` must NOT be ``optional``. It is an enum, so with the keyword "unset" and "set to
    FEED_UNSPECIFIED" would be two spellings of the same request, and the deployment-decides case
    would have two encodings for the client seam to keep in step. Zero already means it
    (tj-3mk3u5.22 Q5).
    """
    by_name = {field.name: field for field in messages(file_named(CONTRACT))['FetchDatasetRequest'].field}

    assert by_name['end'].type == by_name['end'].TYPE_MESSAGE
    assert by_name['feed'].proto3_optional is False
    assert by_name['start'].proto3_optional is False


def test_no_message_on_this_contract_carries_a_dataset_id():
    """ADR tj-8konfu D7.2 and tj-rh4b7f: a data_store row id does not cross this wire.

    Swept over every message rather than asserted on the request alone, because the page is where it
    would most plausibly be re-added -- "so the store knows which dataset these bars are for" -- and
    the answer is that correlation belongs to the transport and the store stamps its own rows.
    """
    offenders = {
        name: field.name
        for name, message in messages(file_named(CONTRACT)).items()
        for field in message.field
        if 'dataset_id' in field.name
    }
    assert not offenders, f'dataset_id does not cross this wire: {offenders}'


def test_every_time_on_this_contract_is_a_well_known_timestamp():
    """tj-3mk3u5.22 Q4 = google.protobuf.Timestamp, swept rather than spot-checked.

    An int64 of epoch seconds or a string would both work and both lose the one property Timestamp
    buys: every consumer of proto/ decodes it the same way without agreeing on a convention first.
    """
    files = [file_named(CONTRACT), file_named(BAR)]
    times = [
        (file.name, message.name, field.name, field.type_name)
        for file in files
        for message in file.message_type
        for field in message.field
        if field.name in {'start', 'end', 'as_of', 'bar_start'}
    ]
    assert times, 'the sweep found no time fields at all, so it is asserting nothing'
    for file_name, message_name, field_name, type_name in times:
        assert type_name == TIMESTAMP, f'{file_name} {message_name}.{field_name} is {type_name!r}'


# ---------------------------------------------------------------------------------------------
# THE PYDANTIC TWINS, FIELD FOR FIELD


# Each proto message of the contract, with the domain model that mirrors it. tj-3mk3u5.29's round trip
# will re-derive this under the mapper's name table; until the mapper exists this is what keeps the two
# declarations in step.
TWINS: list[tuple[str, str, type[BaseModel]]] = [
    (CONTRACT, 'FetchDatasetRequest', FetchDatasetRequest),
    (CONTRACT, 'FetchAck', FetchAck),
    (CONTRACT, 'FetchAccepted', FetchAccepted),
    (CONTRACT, 'FetchRefused', FetchRefused),
    (CONTRACT, 'BarPage', BarPage),
    (CONTRACT, 'FetchDone', FetchDone),
    (CONTRACT, 'ServedRange', ServedRange),
    (BAR, 'Bar', Bar),
]

# The one contract message with no domain twin, and why. The stream's envelope is pure transport: the
# client seam dispatches the oneof and yields the ack, the pages and the done, so no domain model
# restates it. Listed rather than merely absent, so "we forgot one" cannot pass as "deliberate".
NOT_MODELLED: frozenset[str] = frozenset({'FetchDatasetResponse'})


@pytest.mark.parametrize(('file_name', 'message_name', 'model'), TWINS, ids=[name for _, name, _ in TWINS])
def test_a_domain_twin_declares_exactly_its_protos_fields(file_name: str, message_name: str, model: type[BaseModel]):
    """The defect this exists for: a field added to one declaration and not the other.

    Asserted as an ordered list, so the declaration ORDER matches too. That is not cosmetic here --
    both files are read side by side by whoever writes the mapper, and a reordering is the cheapest
    possible way to make a reviewer's eye skip a missing field.

    Args:
        file_name: The canonical .proto file holding the message.
        message_name: The proto message.
        model: The Pydantic model that mirrors it.
    """
    proto_fields = field_names(messages(file_named(file_name))[message_name])

    assert list(model.model_fields) == proto_fields, (
        f'{model.__name__} and {message_name} have drifted apart; every field name matches its proto '
        f'field name on purpose, so that tj-3mk3u5.29 needs no entry in the mapper name table'
    )


def test_every_contract_message_is_either_twinned_or_listed_as_transport():
    """Fail on a message added to the contract that no twin covers and no list excuses.

    Without this the parametrization above silently stops being a check on the contract and becomes a
    check on whatever someone last remembered to add to TWINS.
    """
    declared = set(messages(file_named(CONTRACT)))
    covered = {name for file_name, name, _ in TWINS if file_name == CONTRACT} | NOT_MODELLED

    assert declared == covered


def test_every_model_in_the_module_is_a_twin_of_something():
    """And the other direction: a domain model with no proto message behind it.

    That one matters more than it looks. These models are the internal representation of a WIRE
    contract; a model here that nothing on the wire corresponds to is either dead or is a field the
    mapper will have to invent a value for.
    """
    declared = {
        name
        for name, obj in vars(fetch_dataset).items()
        if not name.startswith('_') and inspect.isclass(obj) and obj.__module__ == fetch_dataset.__name__
    }

    assert declared == {model.__name__ for _, _, model in TWINS}


# ---------------------------------------------------------------------------------------------
# WHAT THE DOMAIN SIDE ADDS ON TOP OF THE WIRE


def test_an_ack_sets_exactly_one_arm():
    """A proto oneof holds one arm; two nullable fields do not, so the model enforces it.

    Both failing directions are asserted. Neither-set is the one that would otherwise slip through: an
    ack decoded from a message whose oneof was never set is an empty ack, and without this it would
    construct happily and be read as "accepted" or "refused" by whichever branch was tested first.
    """
    accepted = FetchAccepted(feed=Feed.IEX)
    refused = FetchRefused(reason='FEED_NOT_AVAILABLE', domain='trader-joe', detail='no SIP entitlement')

    assert FetchAck(accepted=accepted).accepted is accepted
    assert FetchAck(refused=refused).refused is refused

    for payload in ({}, {'accepted': accepted, 'refused': refused}):
        with pytest.raises(ValidationError) as excinfo:
            FetchAck(**payload)
        assert [error['type'] for error in excinfo.value.errors()] == ['value_error'], payload


# Every AwareDatetime field of the twins, with a payload that is valid apart from the naive time at
# that field.
_NAIVE_CASES: list[tuple[type[BaseModel], str, dict[str, Any]]] = [
    (
        FetchDatasetRequest,
        'start',
        {
            'owner': 'rebalancer',
            'source': 'ALPACA',
            'asset_symbol': 'VFV',
            'asset_type': 'stock',
            'data_types': ['market-activity'],
            'granularity': '1day',
            'start': WHEN,
            # UpdateType is a NamedIntEnum: its VALUES are 1, 2, 3 and only its encoder puts the
            # member name on the wire, so the member itself is what constructs the model.
            'update_type': UpdateType.STATIC,
        },
    ),
    (
        Bar,
        'bar_start',
        {'bar_start': WHEN, 'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 10.0, 'feed': Feed.IEX},
    ),
    (ServedRange, 'start', {'start': WHEN, 'end': WHEN}),
    (ServedRange, 'end', {'start': WHEN, 'end': WHEN}),
    (FetchDone, 'as_of', {'bar_count': 0, 'served_range': ServedRange(start=WHEN, end=WHEN), 'as_of': WHEN}),
]


@pytest.mark.parametrize(
    ('model', 'field', 'payload'), _NAIVE_CASES, ids=[f'{m.__name__}.{f}' for m, f, _ in _NAIVE_CASES]
)
def test_a_naive_time_is_refused_at_its_own_field(model: type[BaseModel], field: str, payload: dict[str, Any]):
    """REFUSE, never convert -- the tj-1bl90i rule as ruled on tj-vhboky.20 (D2 = A), on this contract.

    It matters more here than on a JSON contract: google.protobuf.Timestamp carries no offset at all,
    so a Timestamp decoded without attaching UTC is naive and names no instant. A mapper that forgot
    the tzinfo would hand this layer a naive datetime, and the error must land at the field rather
    than somewhere downstream in a comparison that silently assumes local time.

    The error list is asserted exactly -- one error, this field, type ``timezone_aware`` -- so a
    refusal for an unrelated reason cannot satisfy it, and a CONVERT implementation reds it.

    Args:
        model: The twin under test.
        field: Its aware-datetime field.
        payload: A payload valid apart from the naive value this puts at ``field``.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(**payload | {field: datetime(2026, 1, 1)})

    assert [(error['loc'], error['type']) for error in excinfo.value.errors()] == [((field,), 'timezone_aware')]


def test_no_twin_can_express_the_wires_unspecified_enum_zero():
    """tj-vhboky.1's "no sentinel for we do not know", holding on the wire.

    Each ``<ENUM>_UNSPECIFIED`` exists only because proto3 and buf require a zero value. It has no
    Python counterpart, so a message that leaves a required enum at zero cannot produce a valid model
    -- the refusal is structural, not a check the mapper has to remember to write.
    """
    assert 'UNSPECIFIED' not in Feed.__members__

    with pytest.raises(ValidationError):
        FetchAccepted(feed='FEED_UNSPECIFIED')
    with pytest.raises(ValidationError):
        FetchAccepted(feed='UNSPECIFIED')
