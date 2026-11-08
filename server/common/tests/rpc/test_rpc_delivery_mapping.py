"""THE DELIVERY MAPPINGS, by the rules the reflective round trip cannot state (tj-7zjov1.16).

test_rpc_fetch_mapping.py drives every field of every mapped message both ways by reflection, the thirteen
store messages included, and its offsets sweep covers their aware fields. What it cannot express is here:

* EVERY STORE MESSAGE IS MAPPED, by the right functions, and the one envelope is excused with a reason.
* last_attempt crosses BOTH ways as True and as False; the reflective builder only ever sets True.
* PRESENCE. AssetRecord active and tradable, VendorReported limit and remaining, and the trailer's
  vendor_stats, served_range and as_of are absent-not-zero; False and 0 are values and survive as values.
  The header's feed and the trailer's error_class map None to the zero and the zero back to None. The zero
  outcome and the zero verdict are refused: neither says anything. The two no-call outcomes, EXPIRED and
  EXHAUSTED (tj-wisp-g9rmw0), carry the outcome and nothing else, both ways.
* THE SPLIT BOUND. Asset and corporate-action lists split at MAX_RECORDS_PER_MESSAGE records or
  MAX_PART_BYTES of serialized records, bars at MAX_PAGE_BARS; order kept, nothing lost; a record over the
  4 MiB guard refused; an empty part sends nothing.
* THE STREAM GRAMMAR, as AttemptAssembler enforces it: one header first, payloads of one kind, one trailer
  last and nothing after it.
* REDELIVERY re-encodes identically, which is what makes the store's (request_id, attempt_no) key safe.
* THE ADDITIVE FIELDS of tj-7zjov1.105: AssetRecord.name and CorporateActionRecord's process_date and
  effective_date, each in its own field, each date absent independently, and an old sender's record read
  with no dates and an empty name.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from common.enums.data_stock import Feed
from common.rpc.config import MAX_MESSAGE_BYTES
from common.rpc.mapping import (
    DELIVERY_OUTCOME,
    ENVELOPE_MESSAGES,
    MAX_PAGE_BARS,
    MAX_PART_BYTES,
    MAX_RECORDS_PER_MESSAGE,
    MESSAGE_MAPPERS,
    AttemptAssembler,
    ProtoMappingError,
    asset_record_to_domain,
    asset_record_to_proto,
    asset_records_to_domain,
    asset_records_to_proto,
    attempt_to_messages,
    bar_page_to_domain,
    bar_page_to_proto,
    corporate_action_record_to_domain,
    corporate_action_record_to_proto,
    corporate_action_records_to_domain,
    corporate_action_records_to_proto,
    delivery_served_range_to_domain,
    delivery_served_range_to_proto,
    header_to_domain,
    header_to_proto,
    register_instance_request_to_domain,
    register_instance_request_to_proto,
    register_instance_response_to_domain,
    register_instance_response_to_proto,
    response_to_domain,
    response_to_proto,
    trailer_to_domain,
    trailer_to_proto,
    vendor_reported_to_domain,
    vendor_reported_to_proto,
    vendor_stats_to_domain,
    vendor_stats_to_proto,
)
from common.tests.rpc.delivery_contract import (
    NO_CALL_OUTCOMES,
    Envelope,
    RegisterRequestMessage,
    ResponseMessage,
    a_header,
    a_no_call_header,
    a_no_call_trailer,
    a_registration,
    a_response,
    a_trailer,
    an_action,
    an_asset,
    an_attempt,
    bars,
    records_of,
)
from common.tests.rpc.fetch_contract import message_class
from schemas.data_ingest import delivery as domain
from schemas.data_ingest.fetch_dataset import BarPage, ServedRange


pytestmark = pytest.mark.common

STORE = 'trader_joe.proto.internal.store.v1'

AssetRecordMessage = message_class('AssetRecord')
TrailerMessage = message_class('DeliveryTrailer')
HeaderMessage = message_class('DeliveryHeader')

# A small framing allowance for the envelope around one payload message: its oneof tag and length varint.
ENVELOPE_OVERHEAD = 16


# ---------------------------------------------------------------------------------------------
# EVERY STORE MESSAGE IS MAPPED


THIRTEEN = {
    'DeliveryHeader': (domain.DeliveryHeader, header_to_domain, header_to_proto),
    'DeliveryTrailer': (domain.DeliveryTrailer, trailer_to_domain, trailer_to_proto),
    'DeliverAttemptResponse': (domain.DeliverAttemptResponse, response_to_domain, response_to_proto),
    'BarPage': (BarPage, bar_page_to_domain, bar_page_to_proto),
    'AssetRecords': (domain.AssetRecords, asset_records_to_domain, asset_records_to_proto),
    'AssetRecord': (domain.AssetRecord, asset_record_to_domain, asset_record_to_proto),
    'CorporateActionRecords': (
        domain.CorporateActionRecords,
        corporate_action_records_to_domain,
        corporate_action_records_to_proto,
    ),
    'CorporateActionRecord': (
        domain.CorporateActionRecord,
        corporate_action_record_to_domain,
        corporate_action_record_to_proto,
    ),
    'VendorCallStats': (domain.VendorCallStats, vendor_stats_to_domain, vendor_stats_to_proto),
    'VendorReported': (domain.VendorReported, vendor_reported_to_domain, vendor_reported_to_proto),
    'ServedRange': (ServedRange, delivery_served_range_to_domain, delivery_served_range_to_proto),
    'RegisterInstanceRequest': (
        domain.RegisterInstanceRequest,
        register_instance_request_to_domain,
        register_instance_request_to_proto,
    ),
    'RegisterInstanceResponse': (
        domain.RegisterInstanceResponse,
        register_instance_response_to_domain,
        register_instance_response_to_proto,
    ),
}


@pytest.mark.parametrize('name', sorted(THIRTEEN))
def test_each_store_message_has_its_own_mapper_by_identity(name: str):
    """The import-time totality check accepts ANY mapper; this one pins the right functions for each message.

    Args:
        name: The simple proto name.
    """
    full_name = f'{STORE}.{name}'
    mapper = MESSAGE_MAPPERS[full_name]

    assert full_name not in ENVELOPE_MESSAGES
    assert mapper.proto_type.DESCRIPTOR.full_name == full_name
    assert (mapper.domain_type, mapper.to_domain, mapper.to_proto) == THIRTEEN[name]


def test_the_store_contract_maps_thirteen_messages_and_excuses_only_the_envelope():
    """Every store message is either one of the thirteen or the stream envelope, which carries its reason."""
    store_mapped = {name.rsplit('.', 1)[-1] for name in MESSAGE_MAPPERS if name.startswith(f'{STORE}.')}
    store_excused = {name for name in ENVELOPE_MESSAGES if name.startswith(f'{STORE}.')}

    assert store_mapped == set(THIRTEEN)
    assert store_excused == {f'{STORE}.DeliverAttemptRequest'}
    assert 'AttemptAssembler' in ENVELOPE_MESSAGES[f'{STORE}.DeliverAttemptRequest']


# ---------------------------------------------------------------------------------------------
# last_attempt, BOTH WAYS


@pytest.mark.parametrize('last_attempt', [True, False])
def test_last_attempt_survives_domain_to_wire_to_domain(last_attempt: bool):
    """The P31 rework's flag: the store closes a request that will not be retried only if this crosses.

    Args:
        last_attempt: The flag.
    """
    trailer = a_trailer(outcome=domain.DeliveryOutcome.FAILED, error_class=domain.DeliveryErrorClass.TRANSIENT)
    trailer = trailer.model_copy(update={'last_attempt': last_attempt})

    message = trailer_to_proto(trailer)

    assert message.last_attempt is last_attempt
    assert trailer_to_domain(message).last_attempt is last_attempt
    assert trailer_to_domain(message) == trailer


@pytest.mark.parametrize('last_attempt', [True, False])
def test_last_attempt_survives_wire_to_domain_to_wire(last_attempt: bool):
    """The other direction, from a wire trailer the production encoder did not build.

    Args:
        last_attempt: The flag.
    """
    message = TrailerMessage(outcome=2, error_class=1, last_attempt=last_attempt)

    decoded = trailer_to_domain(message)

    assert decoded.last_attempt is last_attempt
    assert trailer_to_proto(decoded) == message


def test_last_attempt_defaults_to_false_so_a_trailer_that_forgot_it_is_never_final_by_accident():
    """An omitted flag is the not-last case: the store's own count still bounds the request."""
    assert a_trailer().last_attempt is False
    assert trailer_to_domain(TrailerMessage(outcome=1)).last_attempt is False


# ---------------------------------------------------------------------------------------------
# PRESENCE: AssetRecord active and tradable


@pytest.mark.parametrize('field', ['active', 'tradable'])
def test_an_unset_asset_flag_decodes_to_none_never_false(field: str):
    """The store detects delistings from flag flips; an unset flag read as False would be a false delisting.

    Args:
        field: active or tradable.
    """
    message = asset_record_to_proto(an_asset(1))
    message.ClearField(field)
    assert not message.HasField(field)

    assert getattr(asset_record_to_domain(message), field) is None


@pytest.mark.parametrize('field', ['active', 'tradable'])
def test_a_none_asset_flag_encodes_unset(field: str):
    """None is written as nothing, so the receiver's HasField sees the vendor said nothing.

    Args:
        field: active or tradable.
    """
    message = asset_record_to_proto(an_asset(1, **{field: None}))

    assert not message.HasField(field)
    assert getattr(asset_record_to_domain(message), field) is None


@pytest.mark.parametrize('value', [True, False])
@pytest.mark.parametrize('field', ['active', 'tradable'])
def test_a_set_asset_flag_survives_as_that_value_false_included(field: str, value: bool):
    """False is a value: it is SET on the wire and comes back False, not None.

    Args:
        field: active or tradable.
        value: The flag.
    """
    message = asset_record_to_proto(an_asset(1, **{field: value}))

    assert message.HasField(field)
    assert getattr(asset_record_to_domain(message), field) is value


# ---------------------------------------------------------------------------------------------
# PRESENCE: VendorReported limit and remaining


@pytest.mark.parametrize('field', ['limit', 'remaining'])
def test_an_absent_vendor_count_is_unset_and_decodes_to_none(field: str):
    """Absent is not zero: zero remaining would read as an exhausted quota.

    Args:
        field: limit or remaining.
    """
    message = vendor_reported_to_proto(domain.VendorReported(**{field: None}))

    assert not message.HasField(field)
    assert getattr(vendor_reported_to_domain(message), field) is None


@pytest.mark.parametrize('field', ['limit', 'remaining'])
def test_a_zero_vendor_count_is_set_and_decodes_to_zero(field: str):
    """Zero is a value the vendor reported, and survives as zero.

    Args:
        field: limit or remaining.
    """
    message = vendor_reported_to_proto(domain.VendorReported(**{field: 0}))

    assert message.HasField(field)
    assert getattr(vendor_reported_to_domain(message), field) == 0


def test_an_empty_vendor_report_round_trips_as_empty():
    """Nothing reported is three Nones, and nothing on the wire."""
    message = vendor_reported_to_proto(domain.VendorReported())

    assert message.ListFields() == []
    assert vendor_reported_to_domain(message) == domain.VendorReported()


# ---------------------------------------------------------------------------------------------
# PRESENCE: the trailer's optional members, the header's feed, the error class


@pytest.mark.parametrize('field', ['vendor_stats', 'served_range', 'as_of'])
def test_an_absent_trailer_member_is_unset_and_decodes_to_none(field: str):
    """An EXPIRED report carries none of these; a default stats block or the epoch would be a false fact.

    Args:
        field: The optional member.
    """
    message = trailer_to_proto(a_trailer(**{field: None}))

    assert not message.HasField(field)
    assert getattr(trailer_to_domain(message), field) is None


def test_an_expired_trailer_with_nothing_set_round_trips():
    """The bare EXPIRED report: an outcome and nothing else."""
    trailer = domain.DeliveryTrailer(outcome=domain.DeliveryOutcome.EXPIRED)
    message = trailer_to_proto(trailer)

    assert [field.name for field, _ in message.ListFields()] == ['outcome']
    assert trailer_to_domain(message) == trailer


def test_the_outcome_table_is_exactly_the_four_ruled_outcomes_by_wire_number():
    """SUCCEEDED 1, FAILED 2, EXPIRED 3, EXHAUSTED 4 (tj-wisp-g9rmw0 ruling 1); the zero has no domain member."""
    assert {outcome: DELIVERY_OUTCOME.to_proto(outcome) for outcome in domain.DeliveryOutcome} == {
        domain.DeliveryOutcome.SUCCEEDED: 1,
        domain.DeliveryOutcome.FAILED: 2,
        domain.DeliveryOutcome.EXPIRED: 3,
        domain.DeliveryOutcome.EXHAUSTED: 4,
    }
    assert DELIVERY_OUTCOME.to_domain(4) is domain.DeliveryOutcome.EXHAUSTED


def test_the_domain_exhausted_outcome_is_its_own_member_named_exhausted():
    """A distinct outcome, never an alias of FAILED or EXPIRED: the store keys its cause on it."""
    assert domain.DeliveryOutcome.EXHAUSTED == 'EXHAUSTED'
    assert domain.DeliveryOutcome.EXHAUSTED not in (domain.DeliveryOutcome.FAILED, domain.DeliveryOutcome.EXPIRED)


@pytest.mark.parametrize('outcome', NO_CALL_OUTCOMES, ids=lambda member: member.name)
def test_a_no_call_trailer_with_nothing_set_round_trips_both_ways(outcome: domain.DeliveryOutcome):
    """EXHAUSTED is shaped as EXPIRED: an outcome and nothing else, and nothing else appears on the way back.

    Args:
        outcome: EXPIRED or EXHAUSTED.
    """
    trailer = a_no_call_trailer(outcome)

    message = trailer_to_proto(trailer)
    decoded = trailer_to_domain(message)

    assert [field.name for field, _ in message.ListFields()] == ['outcome']
    for member in ('vendor_stats', 'served_range', 'as_of'):
        assert not message.HasField(member), member
    assert message.error_class == 0
    assert message.last_attempt is False
    assert decoded == trailer
    assert (decoded.vendor_stats, decoded.served_range, decoded.as_of, decoded.error_class) == (None, None, None, None)
    assert decoded.last_attempt is False
    assert trailer_to_proto(decoded) == message


def test_an_exhausted_trailer_from_the_wire_decodes_to_exhausted():
    """The other direction, from a wire trailer the production encoder did not build."""
    message = TrailerMessage(outcome=4)

    decoded = trailer_to_domain(message)

    assert decoded == domain.DeliveryTrailer(outcome=domain.DeliveryOutcome.EXHAUSTED)
    assert trailer_to_proto(decoded) == message


@pytest.mark.parametrize('outcome', NO_CALL_OUTCOMES, ids=lambda member: member.name)
def test_a_whole_no_call_attempt_survives_split_and_assembly(outcome: domain.DeliveryOutcome):
    """Header and trailer alone; the header charges no quota and names no instrument but keeps the tape.

    Args:
        outcome: EXPIRED or EXHAUSTED.
    """
    attempt = an_attempt(header=a_no_call_header(), trailer=a_no_call_trailer(outcome))

    messages = list(attempt_to_messages(attempt))
    decoded = _decoded(messages)

    assert _arms(messages) == ['header', 'trailer']
    assert decoded == attempt
    assert (decoded.header.quota_key, decoded.header.vendor_asset_id) == ('', '')
    assert decoded.header.feed is Feed.IEX
    assert decoded.trailer.outcome is outcome


def test_a_header_with_no_feed_writes_the_zero_and_the_zero_reads_back_as_none():
    """asset_list and corporate_actions have no tape; FEED_UNSPECIFIED is how the wire says so."""
    message = header_to_proto(a_header(feed=None))

    assert message.feed == 0
    assert header_to_domain(message).feed is None


@pytest.mark.parametrize('feed', list(Feed), ids=lambda feed: feed.name)
def test_a_header_feed_survives_as_the_same_tape(feed: Feed):
    """A bars header names the resolved tape.

    Args:
        feed: Every real tape.
    """
    message = header_to_proto(a_header(feed=feed))

    assert message.feed != 0
    assert header_to_domain(message).feed is feed


def test_an_absent_error_class_writes_the_zero_and_the_zero_reads_back_as_none():
    """error_class belongs to FAILED; on any other outcome it is UNSPECIFIED, which is None here."""
    message = trailer_to_proto(a_trailer(error_class=None))

    assert message.error_class == 0
    assert trailer_to_domain(message).error_class is None


@pytest.mark.parametrize('error_class', list(domain.DeliveryErrorClass), ids=lambda member: member.name)
def test_each_error_class_survives(error_class: domain.DeliveryErrorClass):
    """Args: error_class: Every member."""
    trailer = a_trailer(outcome=domain.DeliveryOutcome.FAILED, error_class=error_class)

    assert trailer_to_domain(trailer_to_proto(trailer)).error_class is error_class


def test_a_trailer_with_the_zero_outcome_is_refused():
    """An attempt that does not say how it ended is not a report."""
    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        trailer_to_domain(TrailerMessage(outcome=0))


def test_an_empty_trailer_is_refused_and_never_read_as_a_no_call_outcome():
    """A bare EXPIRED or EXHAUSTED trailer sets only its outcome; with none set it is not either of them."""
    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        trailer_to_domain(TrailerMessage())


def test_an_answer_with_the_zero_verdict_is_refused():
    """An acknowledgement that says nothing is not an acknowledgement; the gate must not open on it."""
    with pytest.raises(ProtoMappingError, match='UNSPECIFIED'):
        response_to_domain(ResponseMessage(verdict=0, attempt_count=3))


@pytest.mark.parametrize('verdict', list(domain.DeliveryVerdict), ids=lambda member: member.name)
def test_each_verdict_survives_with_its_count(verdict: domain.DeliveryVerdict):
    """Args: verdict: CONTINUE or FINAL."""
    response = a_response(verdict, count=7)

    assert response_to_domain(response_to_proto(response)) == response


# ---------------------------------------------------------------------------------------------
# THE ids AND THE uint32 BOUNDS


def test_a_header_with_attempt_no_zero_or_an_empty_id_is_refused_on_decode():
    """attempt_no starts at 1 and the ids are the idempotency key: the zero values are not keys."""
    for field, value in (('attempt_no', 0), ('request_id', ''), ('instance_id', '')):
        message = header_to_proto(a_header())
        setattr(message, field, value)
        with pytest.raises(ProtoMappingError, match=field):
            header_to_domain(message)


@pytest.mark.parametrize('count', [-1, 2**32], ids=['negative', 'past-uint32'])
def test_an_attempt_no_that_bypassed_the_model_is_refused_by_the_encoder(count: int):
    """model_copy skips the validator; the encoder still names the field.

    Args:
        count: The rejected value.
    """
    with pytest.raises(ValidationError):
        a_header(attempt_no=count)
    with pytest.raises(ProtoMappingError, match='attempt_no'):
        header_to_proto(a_header().model_copy(update={'attempt_no': count}))


def test_an_unset_started_at_is_refused_rather_than_read_as_the_epoch():
    """The instance announcement needs its start instant."""
    with pytest.raises(ProtoMappingError, match='unset'):
        register_instance_request_to_domain(RegisterRequestMessage(instance_id='ingest-1'))
    assert register_instance_request_to_domain(register_instance_request_to_proto(a_registration())) == (
        a_registration()
    )


# ---------------------------------------------------------------------------------------------
# THE SPLIT BOUND


def _arms(messages) -> list[str]:
    return [message.WhichOneof('part') for message in messages]


def _decoded(messages) -> domain.AttemptDelivery:
    assembler = AttemptAssembler()
    for message in messages:
        assembler.feed(message)
    return assembler.finish()


def test_the_bounds_are_the_numbers_the_design_states():
    """1000 records, 1 MiB (D6.3's target, a quarter of the guard), 5000 bars (BarPage's own bound)."""
    assert MAX_RECORDS_PER_MESSAGE == 1000
    assert MAX_PART_BYTES == 1024 * 1024
    assert MAX_PART_BYTES * 4 == MAX_MESSAGE_BYTES
    assert MAX_PAGE_BARS == 5000


@pytest.mark.parametrize(
    ('make', 'arm'), [(an_asset, 'asset_records'), (an_action, 'corporate_action_records')], ids=['assets', 'actions']
)
def test_one_record_past_the_count_cap_becomes_two_messages_in_order(make, arm: str):
    """1001 small records: 1000 and 1, header first, trailer last, every record back in its place.

    Args:
        make: The record factory.
        arm: The envelope arm the kind travels in.
    """
    part_type = domain.AssetRecords if arm == 'asset_records' else domain.CorporateActionRecords
    records = [make(index) for index in range(MAX_RECORDS_PER_MESSAGE + 1)]
    attempt = an_attempt(part_type(records=records))

    messages = attempt_to_messages(attempt)

    assert _arms(messages) == ['header', arm, arm, 'trailer']
    assert [len(getattr(message, arm).records) for message in messages[1:-1]] == [MAX_RECORDS_PER_MESSAGE, 1]
    assert records_of(_decoded(messages)) == records


def test_exactly_the_count_cap_is_one_message():
    """The boundary itself: 1000 records do not split."""
    messages = attempt_to_messages(an_attempt(domain.AssetRecords(records=[an_asset(i) for i in range(1000)])))

    assert _arms(messages) == ['header', 'asset_records', 'trailer']


def test_large_records_split_by_the_byte_budget_and_every_message_stays_under_it():
    """Ten 300 KiB records: the count cap never bites, so only the byte budget can keep a message bounded."""
    records = [an_asset(index, payload_bytes=300 * 1024) for index in range(10)]

    messages = attempt_to_messages(an_attempt(domain.AssetRecords(records=records)))

    payloads = messages[1:-1]
    assert len(payloads) > 1, 'the byte budget did not split a 3 MiB list'
    for message in payloads:
        assert message.asset_records.ByteSize() <= MAX_PART_BYTES
        assert message.ByteSize() <= MAX_PART_BYTES + ENVELOPE_OVERHEAD
    assert records_of(_decoded(messages)) == records


def test_a_record_over_the_budget_but_under_the_guard_travels_alone():
    """A 2 MiB record cannot be split and is not refused: it gets a message to itself, under the guard."""
    records = [an_asset(0), an_asset(1, payload_bytes=2 * 1024 * 1024), an_asset(2)]

    messages = attempt_to_messages(an_attempt(domain.AssetRecords(records=records)))

    assert [len(message.asset_records.records) for message in messages[1:-1]] == [1, 1, 1]
    assert all(message.ByteSize() < MAX_MESSAGE_BYTES for message in messages)
    assert records_of(_decoded(messages)) == records


def test_a_record_over_the_guard_is_refused_rather_than_sent_or_dropped():
    """No message can carry it, so it is an ingest defect, raised before anything is sent."""
    oversized = an_asset(0, payload_bytes=MAX_MESSAGE_BYTES)

    with pytest.raises(ProtoMappingError, match='guard'):
        attempt_to_messages(an_attempt(domain.AssetRecords(records=[an_asset(1), oversized])))


def test_bars_split_at_the_page_bound():
    """5001 bars in one domain part: a page of 5000 and a page of 1, order kept."""
    page = bars(MAX_PAGE_BARS + 1)

    messages = attempt_to_messages(an_attempt(page))

    assert _arms(messages) == ['header', 'bar_page', 'bar_page', 'trailer']
    assert [len(message.bar_page.bars) for message in messages[1:-1]] == [MAX_PAGE_BARS, 1]
    assert records_of(_decoded(messages)) == page.bars


def test_an_oversized_page_handed_to_the_page_encoder_directly_is_refused():
    """The split is attempt_to_messages's job; a caller that skips it is refused, not silently truncated."""
    with pytest.raises(ProtoMappingError, match=str(MAX_PAGE_BARS)):
        bar_page_to_proto(bars(MAX_PAGE_BARS + 1))


@pytest.mark.parametrize(
    'part',
    [BarPage(bars=[]), domain.AssetRecords(records=[]), domain.CorporateActionRecords(records=[])],
    ids=['bars', 'assets', 'actions'],
)
def test_an_empty_part_yields_no_message(part):
    """An attempt with no records is the header and the trailer; the trailer says whether it succeeded.

    Args:
        part: An empty part of each kind.
    """
    messages = attempt_to_messages(an_attempt(part))

    assert _arms(messages) == ['header', 'trailer']


def test_several_domain_parts_keep_their_order_across_the_split():
    """Two parts of 1500 records each: four messages, every record in its original position."""
    first = [an_asset(index) for index in range(1500)]
    second = [an_asset(index) for index in range(1500, 3000)]

    messages = attempt_to_messages(an_attempt(domain.AssetRecords(records=first), domain.AssetRecords(records=second)))

    assert [len(message.asset_records.records) for message in messages[1:-1]] == [1000, 500, 1000, 500]
    assert records_of(_decoded(messages)) == first + second


def test_a_whole_attempt_survives_split_and_assembly():
    """Header and trailer by value, the payload record for record."""
    attempt = an_attempt(domain.CorporateActionRecords(records=[an_action(i) for i in range(3)]))

    decoded = _decoded(attempt_to_messages(attempt))

    assert decoded == attempt


# ---------------------------------------------------------------------------------------------
# REDELIVERY RE-ENCODES IDENTICALLY


def test_the_same_attempt_encodes_to_the_same_messages_every_time():
    """A redelivery is the SAME AttemptDelivery; its stream must be the same, so the store's key is honest."""
    attempt = an_attempt(domain.CorporateActionRecords(records=[an_action(i) for i in range(1200)]))

    first, second = attempt_to_messages(attempt), attempt_to_messages(attempt)

    assert first == second
    assert [m.SerializeToString(deterministic=True) for m in first] == [
        m.SerializeToString(deterministic=True) for m in second
    ]
    assert (second[0].header.request_id, second[0].header.attempt_no) == ('req-7', 3)


# ---------------------------------------------------------------------------------------------
# THE STREAM GRAMMAR


def _header():
    return Envelope(header=header_to_proto(a_header()))


def _trailer():
    return Envelope(trailer=trailer_to_proto(a_trailer()))


def _assets(*indices: int):
    return Envelope(asset_records=asset_records_to_proto(domain.AssetRecords(records=[an_asset(i) for i in indices])))


def _bars():
    return Envelope(bar_page=bar_page_to_proto(bars(2)))


def _actions():
    return Envelope(
        corporate_action_records=corporate_action_records_to_proto(
            domain.CorporateActionRecords(records=[an_action(0)])
        )
    )


BROKEN_STREAMS = {
    'no header, trailer only': [_trailer()],
    'payload before the header': [_assets(0), _header(), _trailer()],
    'a second header': [_header(), _header(), _trailer()],
    'a second header after a payload': [_header(), _assets(0), _header(), _trailer()],
    'bars then assets': [_header(), _bars(), _assets(0), _trailer()],
    'assets then actions': [_header(), _assets(0), _actions(), _trailer()],
    'a payload after the trailer': [_header(), _trailer(), _assets(0)],
    'a header after the trailer': [_header(), _trailer(), _header()],
    'a second trailer': [_header(), _trailer(), _trailer()],
    'an empty oneof': [_header(), Envelope(), _trailer()],
}


@pytest.mark.parametrize('stream', list(BROKEN_STREAMS.values()), ids=list(BROKEN_STREAMS))
def test_a_stream_that_breaks_the_grammar_is_refused_as_it_arrives(stream):
    """Each departure is a ProtoMappingError from feed, at the offending message.

    Args:
        stream: The messages, in arrival order.
    """
    assembler = AttemptAssembler()
    with pytest.raises(ProtoMappingError):
        for message in stream:
            assembler.feed(message)


@pytest.mark.parametrize(
    ('stream', 'match'),
    [([], 'header'), ([_header()], 'trailer'), ([_header(), _assets(0)], 'trailer')],
    ids=['empty stream', 'header only', 'no trailer'],
)
def test_a_stream_that_ends_early_is_refused_at_finish(stream, match: str):
    """An incomplete attempt writes nothing; finish is where the end of the stream is judged.

    Args:
        stream: The messages that did arrive.
        match: What the refusal names.
    """
    assembler = AttemptAssembler()
    for message in stream:
        assembler.feed(message)

    with pytest.raises(ProtoMappingError, match=match):
        assembler.finish()


def test_parts_are_returned_in_arrival_order():
    """Two asset messages, as the splitter sends them: two parts, the first first."""
    decoded = _decoded([_header(), _assets(0, 1), _assets(2), _trailer()])

    assert [[record.vendor_asset_id for record in part.records] for part in decoded.parts] == [
        ['asset-0', 'asset-1'],
        ['asset-2'],
    ]


def test_a_header_and_trailer_alone_is_a_complete_attempt_with_no_parts():
    """A failed or expired report carries no payload and is still a whole attempt."""
    decoded = _decoded([_header(), _trailer()])

    assert decoded.parts == []
    assert decoded == an_attempt()


def test_a_mixed_kind_attempt_is_refused_by_the_model_too():
    """The domain says the same as the assembler, so a caller cannot build what the store would refuse."""
    with pytest.raises(ValidationError, match='one kind'):
        an_attempt(bars(1), domain.AssetRecords(records=[an_asset(0)]))


def test_an_unreadable_header_in_the_stream_is_refused():
    """The grammar is not all: a header that will not map is refused at feed, before any payload is read."""
    bad = Envelope(header=HeaderMessage(request_id='req-7', instance_id='ingest-1', attempt_no=0))

    with pytest.raises(ProtoMappingError, match='attempt_no'):
        AttemptAssembler().feed(bad)


def test_an_asset_record_message_with_nothing_set_decodes_flags_as_none():
    """The bare wire record: empty strings and bytes, and None for both flags."""
    decoded = asset_record_to_domain(AssetRecordMessage())

    assert (decoded.active, decoded.tradable) == (None, None)


# ---------------------------------------------------------------------------------------------
# tj-7zjov1.105 (PR 5 P41, tj-26jt9w addendum 7 item 9): AssetRecord.name, CorporateActionRecord's two new dates
#
# The reflective round trip in test_rpc_fetch_mapping.py reaches every field by descriptor; these state what
# it cannot: which date is which, that each is absent independently, and what an old sender's record reads as.

ActionMessage = message_class('CorporateActionRecord')

NEW_DATES = ('process_date', 'effective_date')


def test_the_asset_name_survives_both_directions():
    """The vendor's name crosses as itself, domain to wire to domain and wire to domain to wire."""
    record = an_asset(1, name='Vanguard S&P 500 Index ETF')

    message = asset_record_to_proto(record)

    assert message.name == 'Vanguard S&P 500 Index ETF'
    assert asset_record_to_domain(message) == record
    assert asset_record_to_proto(asset_record_to_domain(message)) == message


def test_an_empty_asset_name_stays_empty_rather_than_becoming_none_or_a_placeholder():
    """535 rows of the vendor's list carry no name (addendum 7 fact e): empty is that state, both ways."""
    message = asset_record_to_proto(an_asset(1, name=''))

    assert message.name == ''
    assert asset_record_to_domain(message).name == ''


def test_each_new_date_lands_in_its_own_field_and_comes_back_in_it():
    """A merger carries both dates; distinct instants make a swap or a copy between them visible."""
    record = an_action(1, action_type='stock_merger')
    assert record.process_date != record.effective_date

    message = corporate_action_record_to_proto(record)

    assert message.process_date.ToDatetime(tzinfo=UTC) == record.process_date
    assert message.effective_date.ToDatetime(tzinfo=UTC) == record.effective_date
    assert corporate_action_record_to_domain(message) == record
    assert corporate_action_record_to_proto(corporate_action_record_to_domain(message)) == message


@pytest.mark.parametrize('absent', NEW_DATES)
def test_each_new_date_is_absent_independently_of_the_other(absent: str):
    """A name change carries process_date only (addendum 7 fact d); either may be the missing one.

    Args:
        absent: The date the vendor gave none of.
    """
    (present,) = [date for date in NEW_DATES if date != absent]
    record = an_action(1, action_type='name_change', **{absent: None})

    message = corporate_action_record_to_proto(record)

    assert not message.HasField(absent), f'{absent} was None and is set on the wire'
    assert message.HasField(present)
    decoded = corporate_action_record_to_domain(message)
    assert getattr(decoded, absent) is None
    assert getattr(decoded, present) == getattr(record, present)
    assert decoded == record


def test_an_old_senders_records_read_with_no_new_dates_and_an_empty_name():
    """A sender built before tj-7zjov1.105 writes none of the new fields; its records decode, never guessed at.

    Built from the descriptor with only the fields that existed before, as such a sender's bytes would parse.
    """
    old_action = ActionMessage(vendor_action_id='ca-1', action_type='cash_dividend', raw_payload=b'{}')
    old_action.ex_date.FromDatetime(datetime(2026, 3, 2, tzinfo=UTC))
    old_asset = AssetRecordMessage(vendor_asset_id='a-1', symbol='VFV', raw_payload=b'{}', exchange='NYSE')

    action = corporate_action_record_to_domain(ActionMessage.FromString(old_action.SerializeToString()))
    asset = asset_record_to_domain(AssetRecordMessage.FromString(old_asset.SerializeToString()))

    assert (action.process_date, action.effective_date) == (None, None)
    assert action.ex_date == datetime(2026, 3, 2, tzinfo=UTC)
    assert asset.name == ''
    assert asset.symbol == 'VFV'


def test_the_new_fields_survive_a_whole_attempt_split_and_assembled():
    """Through the production splitter and assembler, not only the leaf mappers."""
    actions = [an_action(0), an_action(1, effective_date=None), an_action(2, process_date=None)]
    for attempt in (
        an_attempt(domain.CorporateActionRecords(records=actions)),
        an_attempt(domain.AssetRecords(records=[an_asset(0), an_asset(1, name='')])),
    ):
        assembler = AttemptAssembler()
        for message in attempt_to_messages(attempt):
            assembler.feed(message)

        assert records_of(assembler.finish()) == records_of(attempt)
