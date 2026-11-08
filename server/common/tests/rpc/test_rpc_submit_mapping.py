"""THE SUBMIT AND INSTANCE MAPPINGS, by the rules the reflective round trip cannot state (tj-7zjov1.15).

test_rpc_fetch_mapping.py already drives every field of every mapped message through both directions by
reflection, the seven new ones included. What it cannot express is a rule that depends on something the
message does not carry, or on a value the reflective builder never picks. Those are here:

* THE FEED RULE (architect ruling at the P07 gate, tj-7zjov1.13, binding on tj-7zjov1.15): the resolved feed
  is a fact about BARS only, so the SubmitResponse mapper is told the request's kind. Bars refuse the zero;
  asset_list and corporate_actions require it, decode it to None, refuse any other value, and refuse a
  refused ack. Both directions enforce it, so a server cannot encode what a client would refuse.
* THE KIND DEFAULTS TO BARS, the strict case, so a caller that forgets it is refused rather than loosened.
  A FLAGGED CHOICE for the architect gate; pinned here so changing it is a visible decision.
* attempt_count IS A uint32 on the wire: refused outside 0 .. 2**32 - 1 by the model AND by the encoder,
  because model_construct and model_copy skip the model's validators.
* ready_by IS PRESENCE: None stays unset on the wire and decodes back to None, never the epoch (owner ruling
  O3, tj-26jt9w addendum 3). A naive value is refused.
* THE STOPGAP'S REPLACEMENT: tj-7zjov1.13 named the seven messages in ENVELOPE_MESSAGES so the import-time
  totality check held; this bead moves all seven to MESSAGE_MAPPERS, and the guard below is what proves
  none was left behind as envelope-only.
* AssetListRequest.status IS REQUIRED (tj-7zjov1.105, tj-26jt9w addendum 7 item 9): ACTIVE and INACTIVE
  each cross as their own value; the zero, and an old sender's missing status, are refused, never defaulted.

Messages are reached through ``fetch_contract.message_class``, never by importing trader_joe.proto (TID251).
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from common.enums.data_stock import Feed
from common.rpc.mapping import (
    ASSET_LIST_STATUS,
    ENUM_MAPPINGS,
    ENVELOPE_MESSAGES,
    MESSAGE_MAPPERS,
    ProtoMappingError,
    asset_list_request_to_domain,
    asset_list_request_to_proto,
    bars_request_to_domain,
    bars_request_to_proto,
    corporate_actions_request_to_domain,
    corporate_actions_request_to_proto,
    instance_request_to_domain,
    instance_request_to_proto,
    instance_response_to_domain,
    instance_response_to_proto,
    submit_request_to_domain,
    submit_request_to_proto,
    submit_response_to_domain,
    submit_response_to_proto,
)
from common.tests.rpc.fetch_contract import message_class
from common.tests.rpc.submit_contract import (
    KINDS,
    NON_BARS,
    WHEN,
    SubmitResponseMessage,
    a_bars,
    a_corporate_actions,
    a_submit,
    accepted,
    an_asset_list,
    feed_number,
    raw_accepted,
    raw_refused,
    refused,
)
from schemas.data_ingest import submit as domain
from schemas.data_ingest.submit import MAX_ATTEMPT_COUNT, RequestKind


pytestmark = pytest.mark.common

INGEST = 'trader_joe.proto.internal.ingest.v1'

SubmitRequestMessage = message_class('SubmitRequest')
InstanceResponseMessage = message_class('InstanceResponse')


# ---------------------------------------------------------------------------------------------
# THE STOPGAP'S REPLACEMENT


SEVEN = {
    f'{INGEST}.SubmitRequest': (domain.SubmitRequest, submit_request_to_domain, submit_request_to_proto),
    f'{INGEST}.BarsRequest': (domain.BarsRequest, bars_request_to_domain, bars_request_to_proto),
    f'{INGEST}.AssetListRequest': (domain.AssetListRequest, asset_list_request_to_domain, asset_list_request_to_proto),
    f'{INGEST}.CorporateActionsRequest': (
        domain.CorporateActionsRequest,
        corporate_actions_request_to_domain,
        corporate_actions_request_to_proto,
    ),
    f'{INGEST}.SubmitResponse': (domain.SubmitResponse, submit_response_to_domain, submit_response_to_proto),
    f'{INGEST}.InstanceRequest': (domain.InstanceRequest, instance_request_to_domain, instance_request_to_proto),
    f'{INGEST}.InstanceResponse': (domain.InstanceResponse, instance_response_to_domain, instance_response_to_proto),
}


@pytest.mark.parametrize('name', sorted(SEVEN), ids=lambda name: name.rsplit('.', 1)[-1])
def test_each_of_the_seven_has_its_own_mapper_and_is_not_envelope_only(name: str):
    """The real replacement of the stopgap guard P07 deleted: every one mapped, none excused as transport.

    A message in BOTH tables would satisfy the import-time totality check and the descriptor walk alike, so
    the absence from ENVELOPE_MESSAGES is asserted on its own. The mapper is checked by identity: a
    MessageMapper that pointed the right proto type at the wrong function would round-trip nothing it should.

    Args:
        name: The message's full proto name.
    """
    domain_type, to_domain, to_proto = SEVEN[name]

    assert name not in ENVELOPE_MESSAGES, f'{name} is still excused as envelope-only; the P07 stopgap outlived P08'
    mapper = MESSAGE_MAPPERS[name]
    assert mapper.proto_type.DESCRIPTOR.full_name == name
    assert (mapper.domain_type, mapper.to_domain, mapper.to_proto) == (domain_type, to_domain, to_proto)


# ---------------------------------------------------------------------------------------------
# ROUND TRIPS, ONE PER KIND AND PER ANSWER, BY VALUE


@pytest.mark.parametrize('kind', list(RequestKind), ids=lambda kind: kind.value)
def test_a_submit_of_each_kind_survives_both_directions_with_its_arm(kind: RequestKind):
    """Domain -> wire -> domain is the identity, and the wire carries exactly the arm the domain set.

    Args:
        kind: Which arm the request carries.
    """
    request = a_submit(**KINDS[kind])
    assert request.kind is kind

    message = submit_request_to_proto(request)

    assert message.WhichOneof('kind') == kind.value
    assert submit_request_to_domain(message) == request
    assert submit_request_to_proto(submit_request_to_domain(message)) == message


def test_the_envelope_extremes_survive_the_trip():
    """attempt_count at its uint32 ceiling, every flag off, the client absent, an open-ended bars window."""
    request = a_submit(
        client=None,
        ready_by=None,
        best_effort=False,
        recovered=False,
        attempt_count=MAX_ATTEMPT_COUNT,
        bars=a_bars(end=None, feed=None),
    )

    message = submit_request_to_proto(request)

    assert message.attempt_count == MAX_ATTEMPT_COUNT
    assert not message.bars.HasField('end')
    assert message.bars.feed == 0
    assert submit_request_to_domain(message) == request


@pytest.mark.parametrize(
    ('kind', 'response'),
    [
        (RequestKind.BARS, accepted(Feed.SIP)),
        (RequestKind.BARS, refused()),
        (RequestKind.ASSET_LIST, accepted(None)),
        (RequestKind.CORPORATE_ACTIONS, accepted(None)),
    ],
    ids=['bars-accepted', 'bars-refused', 'asset_list-accepted', 'corporate_actions-accepted'],
)
def test_every_legal_answer_survives_both_directions(kind: RequestKind, response: domain.SubmitResponse):
    """The quota key and the instance id ride on accepted and refused alike.

    Args:
        kind: The kind of the request answered.
        response: A legal answer for that kind.
    """
    message = submit_response_to_proto(response, kind=kind)

    assert message.instance_id == 'ingest-1'
    assert submit_response_to_domain(message, kind=kind) == response


def test_the_instance_probe_and_its_answer_survive_both_directions():
    """The probe is empty; the answer's started_at comes back the same instant, aware UTC."""
    started = WHEN.astimezone(timezone(timedelta(hours=-5)))
    answer = domain.InstanceResponse(instance_id='ingest-9', started_at=started)

    decoded = instance_response_to_domain(instance_response_to_proto(answer))

    assert decoded == answer
    assert decoded.started_at.utcoffset() == timedelta(0)
    assert instance_request_to_domain(instance_request_to_proto(domain.InstanceRequest())) == domain.InstanceRequest()
    assert instance_request_to_proto(domain.InstanceRequest()).ListFields() == []


# ---------------------------------------------------------------------------------------------
# THE FEED RULE: the four cases the architect named, and the refused ack on a non-bars kind


def test_bars_accepted_with_the_zero_feed_is_refused():
    """Case 1. A feed nobody resolved is an error, not a value (tj-vhboky.1), on Submit as on FetchDataset."""
    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        submit_response_to_domain(raw_accepted(0), kind=RequestKind.BARS)


def test_bars_accepted_with_a_feed_keeps_it():
    """Case 2. The resolved tape is the one fact the ack carries for bars."""
    decoded = submit_response_to_domain(raw_accepted(feed_number(Feed.SIP)), kind=RequestKind.BARS)

    assert decoded.ack.accepted is not None
    assert (decoded.ack.accepted.feed, decoded.ack.accepted.quota_key) == (Feed.SIP, 'alpaca-a')


@pytest.mark.parametrize('kind', NON_BARS, ids=lambda kind: kind.value)
def test_a_non_bars_kind_accepted_with_the_zero_feed_decodes_to_no_feed(kind: RequestKind):
    """Case 3. The zero is REQUIRED here and means no tape, so it is None and never a plausible default.

    Args:
        kind: asset_list or corporate_actions.
    """
    decoded = submit_response_to_domain(raw_accepted(0), kind=kind)

    assert decoded.ack.accepted is not None
    assert decoded.ack.accepted.feed is None
    assert decoded.ack.accepted.quota_key == 'alpaca-a'


@pytest.mark.parametrize('feed', list(Feed), ids=lambda feed: feed.name)
@pytest.mark.parametrize('kind', NON_BARS, ids=lambda kind: kind.value)
def test_a_non_bars_kind_accepted_with_any_feed_is_refused(kind: RequestKind, feed: Feed):
    """Case 4. A nominal feed on a kind with no tape would record a false fact, so it is a protocol error.

    Args:
        kind: asset_list or corporate_actions.
        feed: Every real tape.
    """
    with pytest.raises(ProtoMappingError, match='feed'):
        submit_response_to_domain(raw_accepted(feed_number(feed)), kind=kind)


@pytest.mark.parametrize('kind', NON_BARS, ids=lambda kind: kind.value)
def test_a_non_bars_kind_answered_with_a_refused_ack_is_refused(kind: RequestKind):
    """Refused (an unservable feed) arises only for bars; the other kinds are refused only by a status.

    Args:
        kind: asset_list or corporate_actions.
    """
    with pytest.raises(ProtoMappingError, match='refused'):
        submit_response_to_domain(raw_refused(), kind=kind)


def test_a_refused_ack_on_bars_decodes_to_the_refusal():
    """The bars refusal is an ordinary answer, not an error: turning it into one is the caller's choice."""
    assert submit_response_to_domain(raw_refused(), kind=RequestKind.BARS) == refused()


def test_the_encoder_will_not_write_a_bars_acceptance_without_a_feed():
    """The two directions agree: what the decoder refuses, the encoder cannot produce."""
    with pytest.raises(ProtoMappingError, match='feed'):
        submit_response_to_proto(accepted(None), kind=RequestKind.BARS)


@pytest.mark.parametrize('kind', NON_BARS, ids=lambda kind: kind.value)
def test_the_encoder_will_not_write_a_feed_or_a_refusal_for_a_non_bars_kind(kind: RequestKind):
    """And writes the zero, not a field left to chance, when it accepts one.

    Args:
        kind: asset_list or corporate_actions.
    """
    with pytest.raises(ProtoMappingError, match='feed'):
        submit_response_to_proto(accepted(Feed.IEX), kind=kind)
    with pytest.raises(ProtoMappingError, match='refused'):
        submit_response_to_proto(refused(), kind=kind)

    assert submit_response_to_proto(accepted(None), kind=kind).ack.accepted.feed == 0


def test_an_answer_with_neither_arm_is_refused():
    """The oneof holds one arm; an empty ack is a message this release cannot read."""
    with pytest.raises(ProtoMappingError, match='neither'):
        submit_response_to_domain(SubmitResponseMessage(instance_id='ingest-1'), kind=RequestKind.BARS)


# ---------------------------------------------------------------------------------------------
# THE KIND DEFAULT: a FLAGGED CHOICE, pinned so that changing it is a decision and not a drift


@pytest.mark.parametrize('mapper', [submit_response_to_domain, submit_response_to_proto], ids=lambda f: f.__name__)
def test_the_kind_defaults_to_bars_the_strict_case(mapper):
    """MESSAGE_MAPPERS and the reflective control call these with one argument, so the kind has a default.

    It is BARS on both sides, which is the STRICT case: the zero feed refused. A loose default would let a
    caller that forgot the kind read a bars acceptance with no tape as a valid answer.

    Args:
        mapper: Either direction.
    """
    kind = inspect.signature(mapper).parameters['kind']

    assert kind.default is RequestKind.BARS
    # KEYWORD-ONLY (architect ruling 1 at the P08 gate, carried to tj-7zjov1.48): a positional kind is a
    # second argument a caller can pass by accident, and the module docstring already says keyword.
    assert kind.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_kind_cannot_be_passed_positionally():
    """By behaviour as well as by signature: a positional kind is a TypeError in both directions."""
    with pytest.raises(TypeError):
        submit_response_to_domain(raw_accepted(0), RequestKind.ASSET_LIST)  # type: ignore[misc]
    with pytest.raises(TypeError):
        submit_response_to_proto(accepted(None), RequestKind.ASSET_LIST)  # type: ignore[misc]


def test_a_caller_that_omits_the_kind_gets_the_strict_bars_rule():
    """By behaviour, not by signature: an asset-list acceptance read without its kind is refused, not loosened."""
    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        submit_response_to_domain(raw_accepted(0))
    with pytest.raises(ProtoMappingError, match='feed'):
        submit_response_to_proto(accepted(None))
    assert submit_response_to_domain(raw_refused()) == refused()


# ---------------------------------------------------------------------------------------------
# attempt_count IS A uint32


@pytest.mark.parametrize('count', [-1, MAX_ATTEMPT_COUNT + 1], ids=['negative', 'past-uint32'])
def test_an_attempt_count_outside_uint32_is_refused_by_the_model(count: int):
    """The domain refuses it at construction, before anything reaches the encoder.

    Args:
        count: The rejected value.
    """
    with pytest.raises(ValidationError, match='attempt_count'):
        a_submit(attempt_count=count)


def test_the_uint32_bound_is_the_wires_own():
    """MAX_ATTEMPT_COUNT is the ceiling the descriptor declares, not a number chosen beside it."""
    field = SubmitRequestMessage.DESCRIPTOR.fields_by_name['attempt_count']

    assert field.type == field.TYPE_UINT32
    assert MAX_ATTEMPT_COUNT == 2**32 - 1


@pytest.mark.parametrize('count', [-1, MAX_ATTEMPT_COUNT + 1], ids=['negative', 'past-uint32'])
@pytest.mark.parametrize('bypass', ['model_construct', 'model_copy', 'assignment'])
def test_an_attempt_count_that_bypassed_the_model_is_still_refused_by_the_encoder(count: int, bypass: str):
    """model_construct, model_copy(update=...) and plain assignment all skip the validator; the encoder does not.

    The refusal is ProtoMappingError naming the field, not protobuf's own ValueError from the setter.

    Args:
        count: The rejected value.
        bypass: How validation was skipped.
    """
    good = a_submit()
    if bypass == 'model_construct':
        bad = domain.SubmitRequest.model_construct(
            request_id=good.request_id, owner=good.owner, attempt_count=count, bars=good.bars
        )
    elif bypass == 'model_copy':
        bad = good.model_copy(update={'attempt_count': count})
    else:
        bad = good.model_copy()
        bad.attempt_count = count
    assert bad.attempt_count == count, 'the bypass did not actually reach the encoder with the bad count'

    with pytest.raises(ProtoMappingError, match='attempt_count'):
        submit_request_to_proto(bad)


# ---------------------------------------------------------------------------------------------
# ready_by IS PRESENCE


def test_a_null_ready_by_is_unset_on_the_wire_and_decodes_back_to_none_not_the_epoch():
    """O3: null means no deadline. The epoch is a real instant, already passed, which would expire everything."""
    message = submit_request_to_proto(a_submit(ready_by=None))

    assert not message.HasField('ready_by')
    assert submit_request_to_domain(message).ready_by is None


def test_a_set_ready_by_crosses_as_the_same_instant_in_utc():
    """The offset is not carried (a Timestamp has none); the instant is, and comes back aware UTC."""
    ready_by = datetime(2026, 3, 2, 9, 30, tzinfo=timezone(timedelta(hours=-5)))

    decoded = submit_request_to_domain(submit_request_to_proto(a_submit(ready_by=ready_by)))

    assert decoded.ready_by == ready_by
    assert decoded.ready_by is not None and decoded.ready_by.utcoffset() == timedelta(0)


def test_a_naive_ready_by_is_refused_by_the_model_and_by_the_encoder():
    """A naive deadline would be read as whatever the host thinks; neither side assumes UTC."""
    naive = datetime(2026, 3, 2, 9, 30)

    with pytest.raises(ValidationError, match='ready_by'):
        a_submit(ready_by=naive)
    with pytest.raises(ProtoMappingError, match='naive'):
        submit_request_to_proto(a_submit().model_copy(update={'ready_by': naive}))


# ---------------------------------------------------------------------------------------------
# THE IDS AND THE CLIENT LABEL


def test_an_empty_request_id_is_refused_by_the_model_and_on_decode():
    """request_id is the idempotency key; an empty one would collide every request that lacked one."""
    with pytest.raises(ValidationError, match='request_id'):
        a_submit(request_id='')

    message = submit_request_to_proto(a_submit())
    message.request_id = ''
    with pytest.raises(ProtoMappingError, match='request_id'):
        submit_request_to_domain(message)


def test_an_empty_instance_id_is_refused_on_either_answer():
    """The store learns of a restart from an id it has not seen; an empty id is no id."""
    with pytest.raises(ValidationError, match='instance_id'):
        domain.SubmitResponse(ack=accepted(Feed.IEX).ack, instance_id='')
    with pytest.raises(ValidationError, match='instance_id'):
        domain.InstanceResponse(instance_id='', started_at=WHEN)

    submit_answer = raw_accepted(feed_number(Feed.IEX))
    submit_answer.instance_id = ''
    with pytest.raises(ProtoMappingError, match='instance_id'):
        submit_response_to_domain(submit_answer, kind=RequestKind.BARS)

    instance_answer = instance_response_to_proto(domain.InstanceResponse(instance_id='ingest-1', started_at=WHEN))
    instance_answer.instance_id = ''
    with pytest.raises(ProtoMappingError, match='instance_id'):
        instance_response_to_domain(instance_answer)


def test_an_absent_client_label_is_the_empty_string_on_the_wire_and_none_in_the_domain():
    """The client label is RESERVED: carried and unused. Absent decodes to None, never to ''; a label survives."""
    bare = submit_request_to_proto(a_submit(client=None))

    assert bare.client == ''
    assert submit_request_to_domain(bare).client is None
    assert submit_request_to_domain(submit_request_to_proto(a_submit(client='web'))).client == 'web'


def test_an_empty_client_label_is_refused_by_the_model_and_none_is_still_allowed():
    """tj-7zjov1.48 (3): '' and None would both be the empty string on the wire, so only None may mean absent.

    That makes the domain round trip symmetric: '' encoded would decode back as None, a different value.
    """
    with pytest.raises(ValidationError, match='client'):
        a_submit(client='')
    assert a_submit(client=None).client is None
    assert a_submit(client='w').client == 'w'


def test_a_wire_request_with_an_empty_client_still_decodes_to_none():
    """The decoder keeps mapping the wire's empty string to None; it does not now refuse what it used to read."""
    message = submit_request_to_proto(a_submit())
    message.client = ''

    assert submit_request_to_domain(message).client is None


def test_an_empty_quota_key_is_refused_by_the_model_and_on_decode():
    """tj-7zjov1.48 (4): the quota key is a configured label, and an empty scope would be recorded as a fact."""
    with pytest.raises(ValidationError, match='quota_key'):
        domain.SubmitAccepted(feed=Feed.IEX, quota_key='')
    assert domain.SubmitAccepted(feed=Feed.IEX, quota_key='q').quota_key == 'q'

    answer = raw_accepted(feed_number(Feed.IEX))
    answer.ack.accepted.quota_key = ''
    with pytest.raises(ProtoMappingError, match='quota_key'):
        submit_response_to_domain(answer, kind=RequestKind.BARS)


# ---------------------------------------------------------------------------------------------
# THE KIND ONEOF


def test_a_request_with_no_kind_or_two_kinds_is_refused_by_the_model():
    """The oneof holds one arm, so the domain refuses both shapes the wire cannot carry."""
    with pytest.raises(ValidationError, match='exactly one'):
        a_submit(bars=None)
    with pytest.raises(ValidationError, match='exactly one'):
        a_submit(asset_list=an_asset_list())


@pytest.mark.parametrize('arms', [{}, {'asset_list': 'set'}], ids=['no-kind', 'two-kinds'])
def test_a_request_with_no_kind_or_two_kinds_that_bypassed_the_model_is_refused_by_the_encoder(arms):
    """Without the encoder's check, two kinds would encode as the LAST arm written and silently drop one.

    Args:
        arms: What to set beside (or instead of) the bars arm.
    """
    good = a_submit()
    update: dict[str, object] = {'bars': None} if not arms else {'asset_list': an_asset_list()}
    bad = good.model_copy(update=update)

    with pytest.raises(ProtoMappingError, match='exactly one'):
        submit_request_to_proto(bad)


def test_a_request_that_arrives_with_no_kind_is_refused_on_decode():
    """An unset oneof is a message this release cannot read, not a request of some default kind."""
    message = submit_request_to_proto(a_submit())
    message.ClearField('bars')
    assert message.WhichOneof('kind') is None

    with pytest.raises(ProtoMappingError, match='no kind'):
        submit_request_to_domain(message)


# ---------------------------------------------------------------------------------------------
# REQUIRED TIMESTAMPS ON THE NEW MESSAGES


@pytest.mark.parametrize('field', ['start', 'end'])
def test_an_unset_corporate_actions_bound_is_refused_rather_than_defaulted(field: str):
    """Ingest never defaults the corporate-actions window; the store computes it, so both ends are required.

    Args:
        field: The bound left unset.
    """
    message = corporate_actions_request_to_proto(a_corporate_actions())
    message.ClearField(field)

    with pytest.raises(ProtoMappingError, match='unset'):
        corporate_actions_request_to_domain(message)


def test_an_unset_started_at_is_refused_rather_than_read_as_the_epoch():
    """A process that started in 1970 is no answer to which process is answering."""
    with pytest.raises(ProtoMappingError, match='unset'):
        instance_response_to_domain(InstanceResponseMessage(instance_id='ingest-1'))


# ---------------------------------------------------------------------------------------------
# tj-7zjov1.105 (PR 5 P41, tj-26jt9w addendum 7 item 9): AssetListRequest.status, required

AssetListRequestMessage = message_class('AssetListRequest')


@pytest.mark.parametrize('status', list(domain.AssetListStatus), ids=lambda status: status.name)
def test_each_asset_list_status_survives_both_directions_as_its_own_wire_value(status: domain.AssetListStatus):
    """ACTIVE and INACTIVE each cross as their own non-zero number and come back as themselves.

    Args:
        status: Which of the vendor's two lists.
    """
    request = an_asset_list(status)

    message = asset_list_request_to_proto(request)

    assert message.status != 0
    assert (
        AssetListRequestMessage.DESCRIPTOR.fields_by_name['status'].enum_type.values_by_number[message.status].name
        == f'ASSET_LIST_STATUS_{status.name}'
    )
    assert asset_list_request_to_domain(message) == request
    assert asset_list_request_to_proto(asset_list_request_to_domain(message)) == message


def test_the_two_statuses_are_distinct_on_the_wire():
    """A mapper that wrote one list's number for both would pass each round trip that only reads its own back."""
    numbers = {asset_list_request_to_proto(an_asset_list(status)).status for status in domain.AssetListStatus}

    assert len(numbers) == len(domain.AssetListStatus) == 2


def test_an_unspecified_asset_list_status_is_refused_by_the_mapper():
    """The explicit-value rule, as for the feed: a request always names its list, so the zero is refused."""
    message = asset_list_request_to_proto(an_asset_list())
    message.status = 0

    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        asset_list_request_to_domain(message)


def test_an_old_senders_asset_list_request_with_no_status_is_refused_not_defaulted():
    """A sender built before tj-7zjov1.105 writes no status at all; it reads as the zero and is refused.

    Built from the descriptor with only the two fields that existed before, then round-tripped through bytes.
    """
    old = AssetListRequestMessage(
        source=asset_list_request_to_proto(an_asset_list()).source,
        asset_type=asset_list_request_to_proto(an_asset_list()).asset_type,
    )

    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        asset_list_request_to_domain(AssetListRequestMessage.FromString(old.SerializeToString()))


def test_the_refusal_reaches_a_whole_submit_of_the_asset_list_kind():
    """Not only the leaf: a Submit whose asset_list arm names no list is unmappable as a whole."""
    message = submit_request_to_proto(a_submit(**KINDS[RequestKind.ASSET_LIST]))
    message.asset_list.status = 0

    with pytest.raises(ProtoMappingError):
        submit_request_to_domain(message)


def test_the_domain_asset_list_request_requires_a_status_and_has_no_unspecified_member():
    """The twin refuses a missing status too, so no caller can build a request the mapper would have to guess."""
    assert 'UNSPECIFIED' not in domain.AssetListStatus.__members__
    with pytest.raises(ValidationError, match='status'):
        domain.AssetListRequest(source='ALPACA', asset_type='stock')


def test_the_asset_list_status_mapping_is_registered_for_the_import_time_totality_check():
    """ENUM_MAPPINGS is what the package's import-time check and the bijection sweep both walk."""
    assert ASSET_LIST_STATUS in ENUM_MAPPINGS
    assert ASSET_LIST_STATUS.descriptor.full_name == f'{INGEST}.AssetListStatus'
    assert ASSET_LIST_STATUS.python_type is domain.AssetListStatus
