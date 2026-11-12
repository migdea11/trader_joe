"""The internal contracts: store.proto's result path, Submit and Instance on ingest, and quota_key everywhere.

From tj-grna9p.44's architect re-baseline of 2026-10-11, still standing:

* THE STORE CONTRACT RUNS ingest -> store, the opposite direction, so it is NOT on IngestService: its
  own file trader_joe/proto/internal/store/v1/store.proto declares StoreService.
* A CONTRACT IMPORTS market/v1 AND NEVER ANOTHER CONTRACT, so store.proto restates the shapes it shares
  with ingest.proto instead of importing them.
* quota_key IS A NON-SECRET CONFIGURED LABEL, NEVER KEY MATERIAL AND NEVER A HASH OF IT, and every
  comment on a quota_key field says so; no field on either internal contract names credential material.

tj-7zjov1.14 (PR 5 P31) replaces that file's Bootstrap with the result path, against the owner rulings
on tj-26jt9w of 2026-10-24 (addenda 5 and 6) and the architect's feed ruling from the P07 gate:

* R1 PUSH: StoreService has exactly DeliverAttempt (client-streaming: header, payload parts, trailer;
  answered CONTINUE or FINAL with the durable attempt count) and RegisterInstance (unary, the restart
  marker). Bootstrap and its messages are gone.
* THE ACK GATE: every attempt, success or failure, is reported with its outcome and spend and
  acknowledged before a retry; a redelivery is idempotent on (request_id, attempt_no).
* R4: an attempt expired at dequeue is reported as its own outcome, EXPIRED, with no vendor call and no
  stats.
* THE FEED FOLLOWS THE SUBMIT RULE on the header, and the vendor asset id is opaque data.

The second half pins tj-7zjov1.13 (PR 5 P07), the Submit and Instance contract on IngestService, against
the owner rulings recorded on tj-26jt9w on 2026-10-24:

* SUBMIT IS UNARY AND TAKES ONE REQUEST OF ANY KIND: an envelope (request_id, owner, client, ready_by,
  best_effort, recovered, attempt_count) and a ``kind`` oneof of exactly bars, asset_list and
  corporate_actions. It is answered at once by the old FetchAck plus the instance id; the result is
  pushed to the store (R1 ruled push), so no data and no vendor asset id ride the response.
* READY_BY MAY BE NULL AND THEN NEVER EXPIRES (O3); when set, a request dequeued after it is failed with
  cause expired (R4). It must stay a Timestamp MESSAGE field so unset and the epoch are distinguishable.
* THE ACK GATE AND THE RESTART RE-REQUEST: attempt_count is the store's durable count (ingest numbers
  the next attempt attempt_count + 1) and recovered marks a re-submission after a restart.
* CORPORATE ACTIONS CARRY AN EXPLICIT WINDOW: the store computes the default, ingest never does.
* INSTANCE is the unary restart probe: instance_id and started_at.

Read through protoc's own descriptors (``common/tests/proto_descriptors``) and, for comments, protoc's
source info -- never by importing the generated package, which TID251 keeps inside common/rpc. The
mapping-surface stopgap for the same messages is ``common/tests/rpc/test_rpc_submit_envelope_stopgap.py``,
kept apart so that a broken mapping table, which fails at import, cannot take these descriptor checks
down with it.
"""

import importlib
import re
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from common.tests.proto_descriptors import PROTO_ROOT, PROTOC_TIMEOUT_S, file_named, messages, real_oneofs


pytestmark = pytest.mark.common

STORE = 'trader_joe/proto/internal/store/v1/store.proto'
INGEST = 'trader_joe/proto/internal/ingest/v1/ingest.proto'
INTERNAL = (INGEST, STORE)
FIELD = descriptor_pb2.FieldDescriptorProto
MESSAGE_TYPE_PATH, FIELD_PATH = 4, 2
CREDENTIAL = re.compile(r'secret|token|password|passwd|credential|api_?key|private_?key|bearer', re.IGNORECASE)


def _shape(field: descriptor_pb2.FieldDescriptorProto) -> tuple:
    kind = field.type_name if field.type in (FIELD.TYPE_MESSAGE, FIELD.TYPE_ENUM) else FIELD.Type.Name(field.type)
    return (field.name, field.number, kind, field.label == FIELD.LABEL_REPEATED)


STORE_PACKAGE = 'trader_joe.proto.internal.store.v1'
_TS = '.google.protobuf.Timestamp'
_STORE = f'.{STORE_PACKAGE}'
_MARKET = '.trader_joe.proto.market.v1'
ENUM_TYPE_PATH, ENUM_VALUE_PATH, SERVICE_PATH, METHOD_PATH = 5, 2, 6, 2


def test_store_service_has_exactly_deliver_attempt_streaming_and_register_instance_unary():
    """R1 push: data_store serves, data_ingest calls. Bootstrap is removed; nothing else is added."""
    store = file_named(STORE)

    assert store.package == STORE_PACKAGE
    (service,) = store.service
    assert service.name == 'StoreService'
    shapes = {
        method.name: (method.input_type, method.output_type, method.client_streaming, method.server_streaming)
        for method in service.method
    }
    assert shapes == {
        'DeliverAttempt': (f'{_STORE}.DeliverAttemptRequest', f'{_STORE}.DeliverAttemptResponse', True, False),
        'RegisterInstance': (f'{_STORE}.RegisterInstanceRequest', f'{_STORE}.RegisterInstanceResponse', False, False),
    }

    ingest_methods = {method.name for method in file_named(INGEST).service[0].method}
    assert not ingest_methods & {'Bootstrap', 'DeliverAttempt', 'RegisterInstance'}, ingest_methods


def test_store_proto_imports_no_other_contract():
    """'A contract may import market/v1 and never another contract': store.proto restates, never imports."""
    dependencies = list(file_named(STORE).dependency)

    assert all(not dependency.startswith('trader_joe/proto/internal/') for dependency in dependencies), dependencies
    # Non-vacuity, and the whole allowed set: market/v1 and the well-known Timestamp, nothing else.
    assert set(dependencies) == {
        'google/protobuf/timestamp.proto',
        'trader_joe/proto/market/v1/bar.proto',
        'trader_joe/proto/market/v1/enums.proto',
    }, dependencies


STORE_MESSAGES = {
    'DeliverAttemptRequest': [
        ('header', 1, f'{_STORE}.DeliveryHeader', False),
        ('bar_page', 2, f'{_STORE}.BarPage', False),
        ('asset_records', 3, f'{_STORE}.AssetRecords', False),
        ('corporate_action_records', 4, f'{_STORE}.CorporateActionRecords', False),
        ('trailer', 5, f'{_STORE}.DeliveryTrailer', False),
    ],
    'DeliveryHeader': [
        ('request_id', 1, 'TYPE_STRING', False),
        ('instance_id', 2, 'TYPE_STRING', False),
        ('attempt_no', 3, 'TYPE_UINT32', False),
        ('quota_key', 4, 'TYPE_STRING', False),
        ('feed', 5, f'{_MARKET}.Feed', False),
        ('vendor_asset_id', 6, 'TYPE_STRING', False),
    ],
    'DeliveryTrailer': [
        ('outcome', 1, f'{_STORE}.DeliveryOutcome', False),
        ('error_class', 2, f'{_STORE}.DeliveryErrorClass', False),
        ('detail', 3, 'TYPE_STRING', False),
        ('vendor_stats', 4, f'{_STORE}.VendorCallStats', False),
        ('served_range', 5, f'{_STORE}.ServedRange', False),
        ('as_of', 6, _TS, False),
        ('last_attempt', 7, 'TYPE_BOOL', False),
        # tj-7zjov1.345: the vocabulary Reason of a FAILED attempt, a plain string (the domain is always
        # ERROR_DOMAIN, so it is not carried).
        ('reason', 8, 'TYPE_STRING', False),
    ],
    'DeliverAttemptResponse': [
        ('verdict', 1, f'{_STORE}.DeliveryVerdict', False),
        ('attempt_count', 2, 'TYPE_UINT32', False),
    ],
    'BarPage': [('bars', 1, f'{_MARKET}.Bar', True)],
    'AssetRecords': [('records', 1, f'{_STORE}.AssetRecord', True)],
    'AssetRecord': [
        ('vendor_asset_id', 1, 'TYPE_STRING', False),
        ('symbol', 2, 'TYPE_STRING', False),
        ('raw_payload', 3, 'TYPE_BYTES', False),
        ('active', 4, 'TYPE_BOOL', False),
        ('tradable', 5, 'TYPE_BOOL', False),
        ('exchange', 6, 'TYPE_STRING', False),
        # tj-7zjov1.105 (tj-26jt9w addendum 7 item 9): additive, a new number.
        ('name', 7, 'TYPE_STRING', False),
    ],
    'CorporateActionRecords': [('records', 1, f'{_STORE}.CorporateActionRecord', True)],
    'CorporateActionRecord': [
        ('vendor_action_id', 1, 'TYPE_STRING', False),
        ('action_type', 2, 'TYPE_STRING', False),
        ('ex_date', 3, _TS, False),
        ('record_date', 4, _TS, False),
        ('payable_date', 5, _TS, False),
        ('identifiers', 6, f'{_STORE}.CorporateActionRecord.IdentifiersEntry', True),
        ('raw_payload', 7, 'TYPE_BYTES', False),
        # tj-7zjov1.105 (tj-26jt9w addendum 7 item 9): additive, new numbers after every existing one.
        ('process_date', 8, _TS, False),
        ('effective_date', 9, _TS, False),
    ],
    'VendorCallStats': [
        ('calls', 1, 'TYPE_UINT64', False),
        ('pages', 2, 'TYPE_UINT64', False),
        ('throttled_429', 3, 'TYPE_UINT64', False),
        ('retries', 4, 'TYPE_UINT64', False),
        ('first_call_at', 5, _TS, False),
        ('last_call_at', 6, _TS, False),
        ('vendor_reported', 7, f'{_STORE}.VendorReported', False),
    ],
    'VendorReported': [
        ('limit', 1, 'TYPE_UINT64', False),
        ('remaining', 2, 'TYPE_UINT64', False),
        ('reset_at', 3, _TS, False),
    ],
    'ServedRange': [('start', 1, _TS, False), ('end', 2, _TS, False)],
    'RegisterInstanceRequest': [('instance_id', 1, 'TYPE_STRING', False), ('started_at', 2, _TS, False)],
    'RegisterInstanceResponse': [],
}

STORE_ENUMS = {
    'DeliveryOutcome': [
        ('DELIVERY_OUTCOME_UNSPECIFIED', 0),
        ('DELIVERY_OUTCOME_SUCCEEDED', 1),
        ('DELIVERY_OUTCOME_FAILED', 2),
        ('DELIVERY_OUTCOME_EXPIRED', 3),
        ('DELIVERY_OUTCOME_EXHAUSTED', 4),
    ],
    'DeliveryErrorClass': [
        ('DELIVERY_ERROR_CLASS_UNSPECIFIED', 0),
        ('DELIVERY_ERROR_CLASS_TRANSIENT', 1),
        ('DELIVERY_ERROR_CLASS_RATE_LIMITED', 2),
        ('DELIVERY_ERROR_CLASS_PERMANENT', 3),
    ],
    'DeliveryVerdict': [
        ('DELIVERY_VERDICT_UNSPECIFIED', 0),
        ('DELIVERY_VERDICT_CONTINUE', 1),
        ('DELIVERY_VERDICT_FINAL', 2),
    ],
}


def test_store_proto_declares_exactly_the_ruled_messages():
    """The builder's message set and no other: every Bootstrap message is gone and nothing extra crept in."""
    assert set(messages(file_named(STORE))) == set(STORE_MESSAGES)


@pytest.mark.parametrize('name', sorted(STORE_MESSAGES))
def test_store_messages_have_exactly_the_ruled_fields(name: str):
    """Names, numbers, types and cardinality, in declaration order, so a renumber or a retype goes red.

    Args:
        name: One message of the result-path contract.
    """
    declared = messages(file_named(STORE))
    assert name in declared, f'{name} is not declared in {STORE}'
    assert [_shape(field) for field in declared[name].field] == STORE_MESSAGES[name]


def test_store_proto_declares_exactly_the_ruled_enums_each_with_an_unspecified_zero():
    """Outcome, error class and verdict, every value pinned; the zero is UNSPECIFIED so unset is never a verdict."""
    declared = {enum.name: [(value.name, value.number) for value in enum.value] for enum in file_named(STORE).enum_type}

    assert declared == STORE_ENUMS


def test_deliver_attempt_request_is_one_oneof_part_of_header_payloads_and_trailer():
    """Header first, payload parts, trailer last: one oneof, so a later payload kind is additive."""
    assert real_oneofs(messages(file_named(STORE))['DeliverAttemptRequest']) == {
        'part': ['header', 'bar_page', 'asset_records', 'corporate_action_records', 'trailer']
    }


def test_corporate_action_identifiers_is_a_string_to_string_map():
    """``identifiers`` must be a real map<string, string>, not a repeated message that merely looks like one."""
    (record,) = [message for message in file_named(STORE).message_type if message.name == 'CorporateActionRecord']
    (entry,) = record.nested_type

    assert entry.name == 'IdentifiersEntry'
    assert entry.options.map_entry
    assert [(field.name, field.type) for field in entry.field] == [
        ('key', FIELD.TYPE_STRING),
        ('value', FIELD.TYPE_STRING),
    ]


# THE PR 4 FetchDataset STREAM IS GONE (P37, tj-7zjov1.280; owner ruling 2026-11-02 on tj-7zjov1.18). Its
# response envelope, its done event and the four shapes store.proto used to restate left ingest.proto with it.
REMOVED_INGEST_MESSAGES = (
    'FetchDatasetResponse',
    'FetchDone',
    'BarPage',
    'VendorCallStats',
    'VendorReported',
    'ServedRange',
)


@pytest.mark.parametrize('name', REMOVED_INGEST_MESSAGES)
def test_ingest_proto_no_longer_declares_the_fetch_stream_messages(name: str):
    """None of the stream's messages is declared on ingest.proto; the four the store restated live on there alone.

    store.proto's copies were restatements of ingest.proto's (a contract never imports another). They are now the
    only copies, and their shapes are pinned field for field by STORE_MESSAGES above, which is what used to be
    compared against ingest.proto here.

    Args:
        name: One message the removal took out of ingest.proto.
    """
    assert name not in messages(file_named(INGEST)), f'ingest.proto still declares {name}'
    if name in ('BarPage', 'VendorCallStats', 'VendorReported', 'ServedRange'):
        assert name in messages(file_named(STORE)), f'store.proto lost its own {name}'
        assert name in STORE_MESSAGES, name


def _store_message_comment(message: str, field: str | None = None) -> str:
    names = [declared.name for declared in _with_source_info(STORE).message_type]
    assert message in names, f'{message} is not declared in {STORE}'
    index = names.index(message)
    if field is None:
        return _normalised(_comment_at(STORE, [MESSAGE_TYPE_PATH, index]))
    fields = [declared.name for declared in _with_source_info(STORE).message_type[index].field]
    assert field in fields, f'{message} has no field {field}'
    return _normalised(_comment_at(STORE, [MESSAGE_TYPE_PATH, index, FIELD_PATH, fields.index(field)]))


def _store_enum_value_comment(enum: str, value: str) -> str:
    enums = [declared.name for declared in _with_source_info(STORE).enum_type]
    assert enum in enums, f'{enum} is not declared in {STORE}'
    index = enums.index(enum)
    values = [declared.name for declared in _with_source_info(STORE).enum_type[index].value]
    assert value in values, f'{enum} has no value {value}'
    return _normalised(_comment_at(STORE, [ENUM_TYPE_PATH, index, ENUM_VALUE_PATH, values.index(value)]))


def test_header_feed_comment_pins_the_submit_rule_bars_only_resolved_and_unspecified_otherwise():
    """The architect's feed ruling from the P07 gate, documented on the header field as the ruling requires."""
    comment = _store_message_comment('DeliveryHeader', 'feed')

    assert 'THE FEED FOLLOWS THE SUBMIT RULE' in comment, comment
    assert 'For a BARS attempt it is the resolved tape, and never FEED_UNSPECIFIED.' in comment, comment
    assert 'For an ASSET_LIST or CORPORATE_ACTIONS attempt it is always FEED_UNSPECIFIED' in comment, comment
    assert (
        "An EXPIRED or EXHAUSTED report made no vendor call and carries the request's kind rule unchanged"
    ) in comment, comment


def test_header_vendor_asset_id_comment_says_opaque_data_never_parsed_and_never_an_argument():
    """The vendor asset id is opaque data reported by the handle, keyed on and never interpreted."""
    comment = _store_message_comment('DeliveryHeader', 'vendor_asset_id')

    assert 'REPORTED BY THE BROKER HANDLE AS OPAQUE DATA' in comment, comment
    assert 'nothing here parses, compares or interprets' in comment, comment
    assert 'never an argument of any call made to the handle' in comment, comment


def test_header_documents_the_idempotency_key_request_id_and_attempt_no():
    """A redelivered report repeats its attempt number and is answered from the stored row."""
    request_id = _store_message_comment('DeliveryHeader', 'request_id')
    attempt_no = _store_message_comment('DeliveryHeader', 'attempt_no')
    header = _normalised(_comment_at(STORE, [12]))

    assert 'The first half of the idempotency key' in request_id, request_id
    assert 'The second half of the idempotency key' in attempt_no, attempt_no
    assert 'the same number is sent again when a report is redelivered' in attempt_no, attempt_no
    assert 'DeliverAttempt is IDEMPOTENT on (request_id, attempt_no) in its header' in header, header


def test_header_quota_key_comment_says_non_secret_label_never_key_material_never_a_hash():
    """The ruling restated on this field, not left to the file-wide rule alone."""
    comment = _store_message_comment('DeliveryHeader', 'quota_key').lower()

    assert 'non-secret configured label' in comment, comment
    assert 'never key material' in comment, comment
    assert 'never a hash' in comment, comment


def test_file_header_states_the_ack_gate_for_every_attempt_success_or_failure():
    """Every attempt, success or failure, is reported with its spend and acknowledged before a retry."""
    header = _normalised(_comment_at(STORE, [12]))

    assert (
        'Every attempt, success or failure, is reported and acknowledged before ingest runs a retry or makes any '
        'further vendor call for that request.'
    ) in header, header
    assert 'only then answers' in header, header
    assert 'NO CREDENTIAL MATERIAL crosses here' in header, header


def test_expired_outcome_comment_says_no_vendor_call_no_stats_and_final_with_no_attempt_row():
    """R4: expired at dequeue is its own outcome, never sent to the vendor and never counted as an attempt."""
    comment = _store_enum_value_comment('DeliveryOutcome', 'DELIVERY_OUTCOME_EXPIRED')

    assert 'NEVER SENT TO THE VENDOR' in comment, comment
    assert 'no vendor_stats' in comment, comment
    assert 'cause expired' in comment, comment
    assert 'answers FINAL and writes no attempt row' in comment, comment


def test_exhausted_outcome_comment_says_never_sent_no_spend_failed_attempts_exhausted_no_row_and_final():
    """Ruling 1 of tj-wisp-g9rmw0: a request whose budget was spent before it arrived is its own outcome.

    Never sent to the vendor, shaped as EXPIRED, and not an attempt: the store writes it FAILED with cause
    attempts exhausted, writes no attempt row, leaves the count, records no spend and answers FINAL.
    """
    comment = _store_enum_value_comment('DeliveryOutcome', 'DELIVERY_OUTCOME_EXHAUSTED')

    assert 'attempt budget already spent' in comment, comment
    assert 'NEVER SENT TO THE VENDOR' in comment, comment
    assert 'Shaped exactly as EXPIRED' in comment, comment
    assert 'no vendor_stats, no payload, no served_range, no as_of, empty quota_key, empty vendor_asset_id' in (
        comment
    ), comment
    assert 'error_class UNSPECIFIED and last_attempt false' in comment, comment
    assert "attempt_no is the request's attempt count plus one and is only the idempotency key" in comment, comment
    assert 'the store writes the request FAILED with cause attempts exhausted' in comment, comment
    assert 'writes no attempt row' in comment, comment
    assert 'leaves the durable attempt count as it was' in comment, comment
    assert 'records no spend' in comment, comment
    assert 'answers FINAL' in comment, comment


def test_header_vendor_asset_id_comment_says_empty_on_an_expired_or_exhausted_report():
    """No call was made, so no vendor ever named the instrument."""
    comment = _store_message_comment('DeliveryHeader', 'vendor_asset_id')

    assert 'and on an expired or exhausted report' in comment, comment


def test_trailer_as_of_comment_says_unset_on_expired_and_exhausted():
    """No vendor answered, so there is no answer time."""
    comment = _store_message_comment('DeliveryTrailer', 'as_of')

    assert 'Unset on EXPIRED and EXHAUSTED.' in comment, comment


def test_final_verdict_comment_lists_a_budget_already_spent():
    """The FINAL list names every finished case, the exhausted one included."""
    comment = _store_enum_value_comment('DeliveryVerdict', 'DELIVERY_VERDICT_FINAL')

    assert 'a budget already spent' in comment, comment
    assert 'ingest drops it and runs no retry' in comment, comment


def test_trailer_vendor_stats_comment_includes_failed_attempts_and_excludes_expired():
    """Spend is reported for failures too; an expiry made no call and so carries none."""
    comment = _store_message_comment('DeliveryTrailer', 'vendor_stats')

    assert 'FAILED attempts included' in comment, comment
    assert 'Unset on EXPIRED and EXHAUSTED, which made no call' in comment, comment


def test_register_instance_comment_names_it_the_restart_marker():
    """Addendum 5: ingest announces the id it minted at start so the store notices a restart."""
    methods = [method.name for method in _with_source_info(STORE).service[0].method]
    comment = _normalised(_comment_at(STORE, [SERVICE_PATH, 0, METHOD_PATH, methods.index('RegisterInstance')]))

    assert 'the instance id it minted at start' in comment, comment
    assert 'notices a restart' in comment, comment


def _store_field(message: str, field: str) -> descriptor_pb2.FieldDescriptorProto:
    declared = messages(file_named(STORE))
    assert message in declared, f'{message} is not declared in {STORE}'
    by_name = {candidate.name: candidate for candidate in declared[message].field}
    assert field in by_name, f'{message} has no field {field}'
    return by_name[field]


def test_trailer_last_attempt_is_field_7_a_plain_bool():
    """Architect gate A: ingest holds the attempt budget, so it says when this attempt was its last."""
    field = _store_field('DeliveryTrailer', 'last_attempt')

    assert (field.number, field.type, field.label) == (7, FIELD.TYPE_BOOL, FIELD.LABEL_OPTIONAL)
    assert not field.proto3_optional, 'last_attempt is a plain bool: false means more attempts may follow'


@pytest.mark.parametrize(('name', 'number'), [('active', 4), ('tradable', 5)])
def test_asset_record_status_flags_are_optional_bools_with_explicit_presence(name: str, number: int):
    """Architect gate B: unset (the vendor said nothing) must be distinguishable from false, so proto3 optional.

    Args:
        name: One of the two vendor status flags.
        number: Its ruled field number.
    """
    field = _store_field('AssetRecord', name)
    record = messages(file_named(STORE))['AssetRecord']

    assert (field.number, field.type) == (number, FIELD.TYPE_BOOL)
    assert field.proto3_optional, f'{name} must be declared optional so unset is never read as false'
    assert field.HasField('oneof_index'), f'{name} has no synthetic presence oneof'
    assert record.oneof_decl[field.oneof_index].name == f'_{name}'
    # The synthetic oneof is not a real one: AssetRecord gains no oneof a mapper would have to branch on.
    assert real_oneofs(record) == {}


def test_asset_record_exchange_is_field_6_a_plain_string_not_optional():
    """The vendor's venue wording, uninterpreted; empty when none, so no presence is needed."""
    field = _store_field('AssetRecord', 'exchange')

    assert (field.number, field.type, field.label) == (6, FIELD.TYPE_STRING, FIELD.LABEL_OPTIONAL)
    assert not field.proto3_optional, 'exchange is a plain string: empty means the vendor gave none'


def test_file_header_lists_vendor_reported_among_the_restated_messages():
    """Architect gate C: the header's restated list names all four shapes the equality test keeps in step."""
    header = _normalised(_comment_at(STORE, [12]))

    assert (
        'BarPage, VendorCallStats, VendorReported and ServedRange are restated below rather than imported from '
        'ingest.proto'
    ) in header, header


def test_header_quota_key_comment_says_empty_on_an_expired_report():
    """Architect gate D: an expired report made no vendor call and so charged no quota."""
    comment = _store_message_comment('DeliveryHeader', 'quota_key')

    assert 'EMPTY ON AN EXPIRED OR EXHAUSTED REPORT' in comment, comment
    assert 'nothing was charged to any quota' in comment, comment


def test_trailer_last_attempt_comment_says_failed_only_and_permanent_is_final_regardless():
    """The flag closes a budget-spent request; a PERMANENT class never needed it."""
    comment = _store_message_comment('DeliveryTrailer', 'last_attempt')

    assert 'its attempt budget is spent with this attempt' in comment, comment
    assert 'answer FINAL, write the request FAILED' in comment, comment
    assert 'Meaningful only on FAILED.' in comment, comment
    assert 'A PERMANENT error class is final regardless of this flag.' in comment, comment
    assert 'Failures are reported, never inferred.' in comment, comment


def test_asset_record_active_comment_says_unset_is_never_read_as_inactive():
    """The reason active and tradable are optional, stated where a mapper author will read it."""
    active = _store_message_comment('AssetRecord', 'active')
    tradable = _store_message_comment('AssetRecord', 'tradable')
    exchange = _store_message_comment('AssetRecord', 'exchange')

    assert 'OPTIONAL so that unset (the vendor said nothing) is never read as inactive' in active, active
    assert 'Same presence rule as active' in tradable, tradable
    assert "The vendor's venue wording, uninterpreted. Empty when the vendor gave none." in exchange, exchange


@pytest.mark.parametrize('file', INTERNAL)
def test_no_internal_field_or_message_names_credential_material(file: str):
    """Acceptance (.78): no field carries credentials. quota_key is a label, and the regex does not match it.

    Args:
        file: One internal contract.
    """
    named = [
        f'{message.name}.{field.name}'
        for message in file_named(file).message_type
        for field in message.field
        if CREDENTIAL.search(field.name) or CREDENTIAL.search(message.name)
    ]
    assert not named, named


@cache
def _with_source_info(file: str) -> descriptor_pb2.FileDescriptorProto:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'file.bin'
        result = subprocess.run(
            [
                sys.executable,
                '-m',
                'grpc_tools.protoc',
                f'-I{PROTO_ROOT}',
                f'--descriptor_set_out={out}',
                '--include_source_info',
                file,
            ],
            capture_output=True,
            text=True,
            timeout=PROTOC_TIMEOUT_S,
            check=False,
        )
        assert result.returncode == 0, f'exit {result.returncode}\n{result.stderr}'
        parsed = descriptor_pb2.FileDescriptorSet()
        parsed.ParseFromString(out.read_bytes())
    (compiled,) = parsed.file
    return compiled


def _comment_at(file: str, path: list[int]) -> str:
    for location in _with_source_info(file).source_code_info.location:
        if list(location.path) == path:
            return ' '.join(
                [*location.leading_detached_comments, location.leading_comments, location.trailing_comments]
            )
    return ''


def _quota_key_fields(file: str) -> list[tuple[str, str]]:
    # (message, comment) for every quota_key field: the field's own comment plus its message's.
    found = []
    for index, message in enumerate(_with_source_info(file).message_type):
        for position, field in enumerate(message.field):
            if field.name == 'quota_key':
                own = _comment_at(file, [MESSAGE_TYPE_PATH, index, FIELD_PATH, position])
                found.append((message.name, own + ' ' + _comment_at(file, [MESSAGE_TYPE_PATH, index])))
    return found


def _says_label(text: str) -> bool:
    lowered = ' '.join(text.lower().split())
    return 'label' in lowered and ('non-secret' in lowered or 'never key material' in lowered)


def test_the_ack_quota_key_says_it_is_a_configured_label_never_key_material_and_never_a_hash():
    """The acceptance names this comment exactly: FetchAccepted.quota_key, the one ingest charges first."""
    (comment,) = [text for message, text in _quota_key_fields(INGEST) if message == 'FetchAccepted']
    lowered = ' '.join(comment.lower().split())

    assert 'configured label' in lowered, comment
    assert 'never key material' in lowered, comment
    assert 'never a hash' in lowered, comment


@pytest.mark.parametrize('file', INTERNAL)
def test_every_quota_key_is_documented_as_a_label_or_covered_by_the_files_rule(file: str):
    """Each quota_key field states it is a non-secret label, or the file header states it for all of them.

    Args:
        file: One internal contract.
    """
    header = ' '.join(_comment_at(file, [12]).lower().split())
    file_rule = 'quota_key is a non-secret configured label' in header
    fields = _quota_key_fields(file)

    assert fields, f'{file} declares no quota_key, so this sweep is asserting nothing'
    undocumented = [message for message, text in fields if not _says_label(text)]
    assert file_rule or not undocumented, undocumented


# ---------------------------------------------------------------------------------------------------------
# tj-7zjov1.13 (PR 5 P07): Submit and Instance on IngestService.
# ---------------------------------------------------------------------------------------------------------

INGEST_PACKAGE = 'trader_joe.proto.internal.ingest.v1'
TIMESTAMP = '.google.protobuf.Timestamp'
MARKET = '.trader_joe.proto.market.v1'
SUBMIT_MESSAGES = (
    'SubmitRequest',
    'BarsRequest',
    'AssetListRequest',
    'CorporateActionsRequest',
    'SubmitResponse',
    'InstanceRequest',
    'InstanceResponse',
)

SUBMIT_SHAPES = {
    'SubmitRequest': [
        ('request_id', 1, 'TYPE_STRING', False),
        ('owner', 2, 'TYPE_STRING', False),
        ('client', 3, 'TYPE_STRING', False),
        ('ready_by', 4, TIMESTAMP, False),
        ('best_effort', 5, 'TYPE_BOOL', False),
        ('recovered', 6, 'TYPE_BOOL', False),
        ('attempt_count', 7, 'TYPE_UINT32', False),
        ('bars', 10, f'.{INGEST_PACKAGE}.BarsRequest', False),
        ('asset_list', 11, f'.{INGEST_PACKAGE}.AssetListRequest', False),
        ('corporate_actions', 12, f'.{INGEST_PACKAGE}.CorporateActionsRequest', False),
    ],
    'BarsRequest': [
        ('source', 1, f'{MARKET}.DataSource', False),
        ('asset_symbol', 2, 'TYPE_STRING', False),
        ('asset_type', 3, f'{MARKET}.AssetType', False),
        ('data_types', 4, f'{MARKET}.DataType', True),
        ('granularity', 5, f'{MARKET}.Granularity', False),
        ('start', 6, TIMESTAMP, False),
        ('end', 7, TIMESTAMP, False),
        ('update_type', 8, f'{MARKET}.UpdateType', False),
        ('feed', 9, f'{MARKET}.Feed', False),
    ],
    'AssetListRequest': [
        ('source', 1, f'{MARKET}.DataSource', False),
        ('asset_type', 2, f'{MARKET}.AssetType', False),
        # tj-7zjov1.105 (tj-26jt9w addendum 7 item 9): additive, required by the mapper (the zero is refused).
        ('status', 3, f'.{INGEST_PACKAGE}.AssetListStatus', False),
    ],
    # Both ends and nothing that could default them: no window length, no "days", no default flag.
    'CorporateActionsRequest': [
        ('source', 1, f'{MARKET}.DataSource', False),
        ('asset_symbol', 2, 'TYPE_STRING', False),
        ('start', 3, TIMESTAMP, False),
        ('end', 4, TIMESTAMP, False),
    ],
    # The old ack, verbatim, plus the instance id. No data and no vendor asset id: the result is pushed.
    'SubmitResponse': [('ack', 1, f'.{INGEST_PACKAGE}.FetchAck', False), ('instance_id', 2, 'TYPE_STRING', False)],
    'InstanceRequest': [],
    'InstanceResponse': [('instance_id', 1, 'TYPE_STRING', False), ('started_at', 2, TIMESTAMP, False)],
    # Unchanged by P07: Submit reuses it, so its meaning must not drift now the old stream is gone (P37).
    'FetchAccepted': [('feed', 1, f'{MARKET}.Feed', False), ('quota_key', 2, 'TYPE_STRING', False)],
}


def test_ingest_service_has_exactly_unary_submit_instance_and_quote():
    """THE DESCRIPTOR PIN (P37): IngestService is exactly Submit, Instance and Quote, all three unary.

    FetchDataset, the server-streaming pull stream of PR 4, is gone (owner ruling 2026-11-02 on tj-7zjov1.18).
    Exact equality, not a subset: an rpc re-added under any name, streaming or not, reds this.
    """
    (service,) = file_named(INGEST).service
    assert service.name == 'IngestService'
    shapes = {
        method.name: (method.input_type, method.output_type, method.client_streaming, method.server_streaming)
        for method in service.method
    }

    assert shapes == {
        'Submit': (f'.{INGEST_PACKAGE}.SubmitRequest', f'.{INGEST_PACKAGE}.SubmitResponse', False, False),
        'Instance': (f'.{INGEST_PACKAGE}.InstanceRequest', f'.{INGEST_PACKAGE}.InstanceResponse', False, False),
        'Quote': (f'.{INGEST_PACKAGE}.QuoteRequest', f'.{INGEST_PACKAGE}.QuoteResponse', False, False),
    }


def test_fetch_dataset_request_survives_as_quote_requests_fetch_and_the_ack_as_submits():
    """The KEEP half of the removal: the two shapes still in use stay under their names (no rename churn).

    FetchDatasetRequest is QuoteRequest.fetch's type, and FetchAck (with FetchAccepted and FetchRefused) is
    SubmitResponse.ack's.
    """
    declared = messages(file_named(INGEST))
    (fetch,) = [field for field in declared['QuoteRequest'].field if field.name == 'fetch']
    (ack,) = [field for field in declared['SubmitResponse'].field if field.name == 'ack']

    assert fetch.type_name == f'.{INGEST_PACKAGE}.FetchDatasetRequest'
    assert ack.type_name == f'.{INGEST_PACKAGE}.FetchAck'
    assert {'FetchDatasetRequest', 'FetchAck', 'FetchAccepted', 'FetchRefused'} <= set(declared)


@pytest.mark.parametrize(
    'module',
    ['common.rpc.clients.ingest_fetch', 'common.rpc.mapping.fetch_stream'],
    ids=['the-fetch-client', 'the-stream-encoder'],
)
def test_the_stream_client_and_encoder_modules_are_gone(module: str):
    """The client seam and the server-side encoder of the stream were deleted with it, not left orphaned.

    Args:
        module: The deleted module.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


@pytest.mark.parametrize('name', sorted(SUBMIT_SHAPES))
def test_submit_and_instance_messages_have_exactly_the_ruled_fields(name: str):
    """Names, numbers, types and cardinality, in declaration order, so a renumber or a retype goes red.

    Args:
        name: One message of the Submit and Instance contract, or FetchAccepted.
    """
    declared = messages(file_named(INGEST))
    assert name in declared, f'{name} is not declared in {INGEST}'
    assert [_shape(field) for field in declared[name].field] == SUBMIT_SHAPES[name]


# ---------------------------------------------------------------------------------------------------------
# tj-7zjov1.105 (PR 5 P41, tj-26jt9w addendum 7 item 9): the additive wire change.
# ---------------------------------------------------------------------------------------------------------


def test_asset_list_status_is_exactly_unspecified_active_inactive_by_number():
    """Which list a request wants: the zero is UNSPECIFIED (refused by the mapper), then the vendor's two lists."""
    declared = {
        enum.name: [(value.name, value.number) for value in enum.value] for enum in file_named(INGEST).enum_type
    }

    assert declared.get('AssetListStatus') == [
        ('ASSET_LIST_STATUS_UNSPECIFIED', 0),
        ('ASSET_LIST_STATUS_ACTIVE', 1),
        ('ASSET_LIST_STATUS_INACTIVE', 2),
    ], declared


def test_asset_record_name_is_a_plain_string_so_empty_means_the_vendor_gave_none():
    """535 rows of the vendor's list carry no name (addendum 7 fact e); empty is that state, not an optional."""
    field = _store_field('AssetRecord', 'name')

    assert (field.number, field.type, field.label) == (7, FIELD.TYPE_STRING, FIELD.LABEL_OPTIONAL)
    assert not field.proto3_optional, 'name is a plain string: empty means the vendor gave none'


@pytest.mark.parametrize(('name', 'number'), [('process_date', 8), ('effective_date', 9)])
def test_the_new_corporate_action_dates_are_timestamp_messages_so_unset_is_not_the_epoch(name: str, number: int):
    """A name change carries process_date only; a message field has presence, so the missing date stays missing.

    Args:
        name: One of the two new dates.
        number: Its field number.
    """
    field = _store_field('CorporateActionRecord', name)

    assert (field.number, field.type, field.type_name) == (number, FIELD.TYPE_MESSAGE, _TS)
    assert field.label != FIELD.LABEL_REPEATED


def test_ready_by_is_a_timestamp_message_so_unset_is_distinguishable_from_the_epoch():
    """O3: unset means never expires. Only a MESSAGE field has presence without ``optional`` in proto3.

    A scalar (an int64 of seconds, say) would make "no deadline" and "the epoch" the same zero, and a
    request with no ready_by would read as one that expired in 1970 -- failed at dequeue, never sent.
    """
    (ready_by,) = [field for field in messages(file_named(INGEST))['SubmitRequest'].field if field.name == 'ready_by']

    assert ready_by.type == FIELD.TYPE_MESSAGE
    assert ready_by.type_name == TIMESTAMP
    assert ready_by.label != FIELD.LABEL_REPEATED


def test_submit_kind_is_one_oneof_of_exactly_bars_asset_list_and_corporate_actions():
    """Every kind rides one queue and one gate; a fourth arm, or an arm moved out, is a design change."""
    assert real_oneofs(messages(file_named(INGEST))['SubmitRequest']) == {
        'kind': ['bars', 'asset_list', 'corporate_actions']
    }


def test_submit_response_carries_no_vendor_asset_id_and_reuses_the_ack_outcomes():
    """The vendor asset id moved to the delivery header (P31); the ack keeps its two outcomes."""
    declared = messages(file_named(INGEST))
    vendorish = [field.name for field in declared['SubmitResponse'].field if re.search('vendor|asset', field.name)]

    assert not vendorish, vendorish
    assert real_oneofs(declared['FetchAck']) == {'outcome': ['accepted', 'refused']}


def _message_index(name: str) -> int:
    names = [message.name for message in _with_source_info(INGEST).message_type]
    assert name in names, f'{name} is not declared in {INGEST}'
    return names.index(name)


def _field_comment(message: str, field: str) -> str:
    index = _message_index(message)
    fields = [declared.name for declared in _with_source_info(INGEST).message_type[index].field]
    assert field in fields, f'{message} has no field {field}'
    text = _comment_at(INGEST, [MESSAGE_TYPE_PATH, index, FIELD_PATH, fields.index(field)])
    return ' '.join(text.lower().split())


def test_ready_by_comment_says_unset_never_expires_and_set_expires_at_dequeue():
    """The acceptance names this comment: null never expires (O3); expired at dequeue, never sent (R4)."""
    comment = _field_comment('SubmitRequest', 'ready_by')

    assert 'never expires' in comment, comment
    assert 'cause expired' in comment, comment
    assert 'dequeue' in comment, comment
    assert 'never sent to the vendor' in comment, comment


def test_client_comment_says_carried_and_unused():
    """The reserved client label: nothing may branch on it yet."""
    comment = _field_comment('SubmitRequest', 'client')

    assert 'reserved' in comment, comment
    assert 'carried and unused' in comment, comment


def test_attempt_count_comment_says_the_next_attempt_is_attempt_count_plus_one():
    """The ack gate: the store's durable count, so a restart never resets the attempt budget."""
    comment = _field_comment('SubmitRequest', 'attempt_count')

    assert 'attempt_count + 1' in comment, comment


def test_recovered_comment_says_the_lost_attempt_is_not_counted():
    """The restart re-request: a recovered request was not delivered, and that is not a broker failure."""
    comment = _field_comment('SubmitRequest', 'recovered')

    assert 'not delivered' in comment, comment
    assert 'not counted' in comment, comment


def test_corporate_actions_comment_says_the_store_computes_the_default_window():
    """Ingest never defaults the window; both ends are required on the wire."""
    index = _message_index('CorporateActionsRequest')
    comment = ' '.join(_comment_at(INGEST, [MESSAGE_TYPE_PATH, index]).lower().split())

    assert "store's to compute" in comment, comment
    assert 'ingest never defaults' in comment, comment


def _normalised(text: str) -> str:
    # Whitespace only, case kept: the pinned phrases wrap across comment lines and name FEED_UNSPECIFIED.
    return ' '.join(text.split())


def test_fetch_accepted_feed_comment_scopes_the_never_unspecified_rule_to_bars():
    """The feed ruling on this bead: resolved for bars only; asset_list and corporate_actions carry the zero.

    The mapper half lands with P08 (tj-7zjov1.15) and reads this comment, so it must say which kinds the
    zero is refused for and which it is accepted for.
    """
    index = _message_index('FetchAccepted')
    fields = [declared.name for declared in _with_source_info(INGEST).message_type[index].field]
    comment = _normalised(_comment_at(INGEST, [MESSAGE_TYPE_PATH, index, FIELD_PATH, fields.index('feed')]))

    assert 'Never FEED_UNSPECIFIED for bars' in comment, comment
    assert 'so the mapper refuses the zero for bars only' in comment, comment
    # Reworded by P37 (a3727e1): FetchDataset no longer exists, so the rule covers a bars Submit alone.
    assert 'That covers a Submit of kind bars.' in comment, comment
    assert 'FetchDataset' not in comment, comment
    assert (
        'A Submit of kind asset_list or corporate_actions has no tape, so its accepted ack always carries '
        'FEED_UNSPECIFIED, and the mapper accepts the zero there.'
    ) in comment, comment


def test_submit_response_comment_says_the_feed_and_the_in_band_refusal_are_for_bars_only():
    """Accepted carries a resolved feed for bars only; the other two kinds are never refused in the ack."""
    comment = _normalised(_comment_at(INGEST, [MESSAGE_TYPE_PATH, _message_index('SubmitResponse')]))

    assert (
        'accepted carries the resolved feed for bars and FEED_UNSPECIFIED for asset_list and corporate_actions'
    ) in comment, comment
    assert 'Refused (an unservable feed) arises only for bars' in comment, comment
    assert 'the other two kinds are refused only by a gRPC status, if at all' in comment, comment


def test_file_header_submit_line_qualifies_the_feed_outcomes_by_kind():
    """The header's summary of Submit must not contradict the FetchAccepted and SubmitResponse comments."""
    header = _normalised(_comment_at(INGEST, [12]))

    assert (
        'for bars, accepted with the resolved feed or refused for its feed; for asset list and corporate '
        'actions, accepted with FEED_UNSPECIFIED (no tape), never refused in the ack'
    ) in header, header


def test_the_credential_sweep_reaches_the_new_messages():
    """Non-vacuity for test_no_internal_field_or_message_names_credential_material over ingest.proto."""
    swept = {message.name for message in file_named(INGEST).message_type}

    assert set(SUBMIT_MESSAGES) <= swept, set(SUBMIT_MESSAGES) - swept
