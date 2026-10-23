"""ui/v1: the collect intent (tj-grna9p.174, decision tj-hhy3dk section 3).

Expiry is the deletion date only; whether a DAILY or STREAM subscription is being collected is a separate
operator intent. On the wire that is ADDITIVE and renames nothing:

* every enum NAME and NUMBER is kept, RETIRED included (renaming it belongs to tj-grna9p.168), pinned here
  with numbers so a renumbering is red in the unit suite and not only in the buf step;
* RESUMED is documented as RETIRED's counterpart: collecting again (tj-grna9p.182, finding F3);
* RETIRED is re-documented as "a subscription whose collect intent is false", in data.proto (DatasetState,
  FreshnessStatus), events.proto (DatasetChange) and ops.proto (OpsHealth.retired);
* DatasetSummary gains ``optional bool collecting = 23`` (derived: a subscription's intent; STATIC, a fetch in
  progress), appended after the phase 3 fields;
* ``SetCollectingRequest { optional bool collect = 1; OperatorOverride override = 2; }``, the same
  OperatorOverride the expiry write uses. ``collect`` has presence (tj-grna9p.182, finding F1 on tj-grna9p.174):
  an absent value is refused by the route (422), never read as stop, so an empty or forgotten body cannot stop
  collection;
* SetExpiryRequest's comment says clearing the expiry clears the deletion date only and resumes nothing.

Descriptors come from protoc (common/tests/proto_descriptors.py); comments from protoc's source info; the
canonical JSON from gen/ alone in a subprocess, for the reason test_proto_ui_phase_two.py gives (TID251).
"""

import json
import os
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from common.tests.proto_descriptors import PROTO_ROOT, PROTOC_TIMEOUT_S, file_named, messages
from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.common

GENERATED_ROOT = REPO_ROOT / 'gen' / 'proto' / 'python'
UI_DIR = 'trader_joe/proto/ui/v1'
DATA = f'{UI_DIR}/data.proto'
OPS = f'{UI_DIR}/ops.proto'
EVENTS = f'{UI_DIR}/events.proto'
FIELD = descriptor_pb2.FieldDescriptorProto
MESSAGE_TYPE_PATH, FIELD_PATH, ENUM_TYPE_PATH, ENUM_VALUE_PATH = 4, 2, 5, 2
UI = '.trader_joe.proto.ui.v1.'
# The key sentence every RETIRED comment carries.
NOT_COLLECTING = 'collect intent is false'


def _ran(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


def _shape(field: descriptor_pb2.FieldDescriptorProto) -> tuple:
    """(name, number, type, repeated, `optional`), as test_proto_ui_phase_three.py reads a field."""
    kind = field.type_name if field.type in (FIELD.TYPE_MESSAGE, FIELD.TYPE_ENUM) else FIELD.Type.Name(field.type)
    return (field.name, field.number, kind, field.label == FIELD.LABEL_REPEATED, field.proto3_optional)


# ---------------------------------------------------------------------------------------------------
# THE ENUMS: EVERY NAME AND NUMBER KEPT


def _enum(file: str, enum: str) -> list[tuple[str, int]]:
    return [(value.name, value.number) for value in {e.name: e for e in file_named(file).enum_type}[enum].value]


KEPT_ENUMS = {
    (DATA, 'DatasetState'): [
        ('DATASET_STATE_UNSPECIFIED', 0),
        ('DATASET_STATE_ACTIVE', 1),
        ('DATASET_STATE_RETIRED', 2),
    ],
    (DATA, 'FreshnessStatus'): [
        ('FRESHNESS_STATUS_UNSPECIFIED', 0),
        ('FRESHNESS_STATUS_FRESH', 1),
        ('FRESHNESS_STATUS_LATE', 2),
        ('FRESHNESS_STATUS_OVERDUE', 3),
        ('FRESHNESS_STATUS_COMPLETE', 4),
        ('FRESHNESS_STATUS_GAPS', 5),
        ('FRESHNESS_STATUS_RETIRED', 6),
    ],
    (EVENTS, 'DatasetChange'): [
        ('DATASET_CHANGE_UNSPECIFIED', 0),
        ('DATASET_CHANGE_CREATED', 1),
        ('DATASET_CHANGE_UPDATED', 2),
        ('DATASET_CHANGE_RETIRED', 3),
        ('DATASET_CHANGE_RESUMED', 4),
        ('DATASET_CHANGE_DELETED', 5),
    ],
}


@pytest.mark.parametrize(('file', 'enum'), sorted(KEPT_ENUMS), ids=[e for _, e in sorted(KEPT_ENUMS)])
def test_a_retired_bearing_enum_keeps_every_name_and_number(file: str, enum: str):
    """Decision tj-hhy3dk (3): KEEP every enum name and number; RETIRED stays the word on the wire.

    Args:
        file: The .proto declaring it.
        enum: The enum.
    """
    assert _enum(file, enum) == KEPT_ENUMS[(file, enum)]


def test_ops_health_keeps_retired_as_field_four():
    """OpsHealth.retired is re-documented, not renamed or renumbered: the full numbering, unchanged."""
    assert [_shape(field) for field in messages(file_named(OPS))['OpsHealth'].field] == [
        ('healthy', 1, 'TYPE_INT64', False, False),
        ('late', 2, 'TYPE_INT64', False, False),
        ('failed', 3, 'TYPE_INT64', False, False),
        ('retired', 4, 'TYPE_INT64', False, False),
        ('running', 5, 'TYPE_INT64', False, False),
        ('queue_depth', 6, 'TYPE_INT64', False, False),
        ('failed_runs_24h', 7, 'TYPE_INT64', False, False),
    ]


# ---------------------------------------------------------------------------------------------------
# THE NEW FIELD AND THE NEW MESSAGE


def test_dataset_summary_appends_collecting_as_optional_bool_23_after_reaper_pending():
    """Appended after phase 3's reaper_pending = 22, never inserted; `optional`, since presence is meaningful.

    The phase 3 file pins 1..22 as a prefix; this pins what follows it, as the full tail.
    """
    fields = [_shape(field) for field in messages(file_named(DATA))['DatasetSummary'].field]
    names = [field[0] for field in fields]
    assert names.index('reaper_pending') == len(fields) - 2, names
    assert fields[-2:] == [
        ('reaper_pending', 22, 'TYPE_BOOL', False, False),
        ('collecting', 23, 'TYPE_BOOL', False, True),
    ]
    assert len({field[1] for field in fields}) == len(fields), 'a field number is reused'


def test_set_collecting_request_is_exactly_collect_and_the_override():
    """Name, number, type, cardinality and presence, as the full list: an extra is a design change."""
    assert [_shape(field) for field in messages(file_named(DATA))['SetCollectingRequest'].field] == [
        ('collect', 1, 'TYPE_BOOL', False, True),
        ('override', 2, f'{UI}OperatorOverride', False, False),
    ]


def test_set_collecting_reuses_the_expiry_writes_operator_override():
    """One OperatorOverride for both writes (the override rule and log "exactly as the expiry write")."""
    data = messages(file_named(DATA))
    by_name = {
        message: {field.name: field for field in data[message].field}
        for message in ('SetCollectingRequest', 'SetExpiryRequest', 'DeleteDatasetRequest')
    }
    types = {message: fields['override'].type_name for message, fields in by_name.items()}
    assert set(types.values()) == {f'{UI}OperatorOverride'}, types
    assert 'SetCollectingRequest' in data and 'OperatorOverride' in data
    assert 'SetCollectingRequest' not in {m.name for m in file_named(OPS).message_type}


def test_set_expiry_request_is_unchanged():
    """Only its comment moved: the field list is the phase 3 one, expiry still optional."""
    assert [_shape(field) for field in messages(file_named(DATA))['SetExpiryRequest'].field] == [
        ('expiry', 1, '.google.protobuf.Timestamp', False, True),
        ('override', 2, f'{UI}OperatorOverride', False, False),
    ]


# ---------------------------------------------------------------------------------------------------
# THE COMMENTS: RETIRED MEANS NOT COLLECTING; CLEARING AN EXPIRY RESUMES NOTHING


@cache
def _with_source_info(file: str) -> descriptor_pb2.FileDescriptorProto:
    """FILE compiled with its comments: the compiler's own reading of what documents what."""
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
        assert result.returncode == 0, _ran(result)
        parsed = descriptor_pb2.FileDescriptorSet()
        parsed.ParseFromString(out.read_bytes())
    (compiled,) = parsed.file
    return compiled


def _comments(file: str, path: list[int]) -> str:
    """Every comment protoc attaches at PATH (detached, leading, trailing), joined and with whitespace collapsed."""
    found = [location for location in _with_source_info(file).source_code_info.location if list(location.path) == path]
    assert found, f'no source location at {path} in {file}'
    location = found[0]
    text = ' '.join([*location.leading_detached_comments, location.leading_comments, location.trailing_comments])
    return ' '.join(text.split())


def _enum_path(file: str, enum: str, value: str | None = None) -> list[int]:
    enums = list(_with_source_info(file).enum_type)
    index = [e.name for e in enums].index(enum)
    if value is None:
        return [ENUM_TYPE_PATH, index]
    return [ENUM_TYPE_PATH, index, ENUM_VALUE_PATH, [v.name for v in enums[index].value].index(value)]


def _field_path(file: str, message: str, field: str | None = None) -> list[int]:
    found = list(_with_source_info(file).message_type)
    index = [m.name for m in found].index(message)
    if field is None:
        return [MESSAGE_TYPE_PATH, index]
    return [MESSAGE_TYPE_PATH, index, FIELD_PATH, [f.name for f in found[index].field].index(field)]


def test_dataset_state_documents_retired_as_a_subscription_not_collecting():
    """The enum's comment: collect intent false, not scheduled, deletion its expiry separately; no 'expiry retires'."""
    text = _comments(DATA, _enum_path(DATA, 'DatasetState'))
    assert 'RETIRED is a DAILY or STREAM subscription whose collect intent is false' in text, text
    assert 'not being scheduled' in text and 'deletion is its expiry, separately' in text, text
    assert 'expiry has been set' not in text and 'no longer collects' not in text, text


def test_freshness_retired_documents_a_subscription_not_collecting():
    text = _comments(DATA, _enum_path(DATA, 'FreshnessStatus', 'FRESHNESS_STATUS_RETIRED'))
    assert f'A DAILY or STREAM subscription whose {NOT_COLLECTING}' in text, text
    assert 'owes no freshness' in text and 'deletion is its expiry, separately' in text, text
    assert 'The dataset is retired' not in text, text


def test_dataset_change_retired_documents_stopped_collecting():
    text = _comments(EVENTS, _enum_path(EVENTS, 'DatasetChange', 'DATASET_CHANGE_RETIRED'))
    assert 'The dataset stopped collecting (its collect intent became false)' in text, text


def test_dataset_change_resumed_documents_collecting_again():
    """RETIRED's counterpart (tj-grna9p.182, F3): the intent became true, worded the same way."""
    text = _comments(EVENTS, _enum_path(EVENTS, 'DatasetChange', 'DATASET_CHANGE_RESUMED'))
    assert 'The dataset is collecting again (its collect intent became true)' in text, text


def test_ops_health_retired_documents_not_collecting():
    text = _comments(OPS, _field_path(OPS, 'OpsHealth', 'retired'))
    assert f'Datasets that are not collecting (a subscription whose {NOT_COLLECTING})' in text, text


def test_set_expiry_documents_the_deletion_date_only_and_resumes_nothing():
    """The old comment said clearing resumes a retired subscription; that meaning is gone (tj-hhy3dk (3))."""
    text = _comments(DATA, _field_path(DATA, 'SetExpiryRequest', 'expiry'))
    assert 'the deletion date and nothing else' in text, text
    assert 'Unset clears the deletion date only and resumes nothing' in text, text
    assert 'SetCollectingRequest' in text, text
    assert 'resumes if cleared' not in text and 'retired' not in text.lower(), text


def test_collecting_documents_the_derived_status_for_every_update_type():
    text = _comments(DATA, _field_path(DATA, 'DatasetSummary', 'collecting'))
    assert 'Derived, for every update type' in text, text
    assert "the operator's collect intent (false = RETIRED)" in text, text
    assert 'for STATIC it means a fetch is in progress' in text, text


def test_set_collecting_documents_subscriptions_only_and_no_deletion():
    message = _comments(DATA, _field_path(DATA, 'SetCollectingRequest'))
    assert 'DAILY and STREAM subscriptions only' in message and 'the route refuses it' in message, message
    collect = _comments(DATA, _field_path(DATA, 'SetCollectingRequest', 'collect'))
    assert 'false stops it (RETIRED)' in collect, collect
    assert 'does not delete anything or touch the expiry' in collect, collect
    override = _comments(DATA, _field_path(DATA, 'SetCollectingRequest', 'override'))
    assert "Required when the dataset's owner is not 'operator'" in override, override


def test_set_collecting_documents_that_an_absent_collect_is_refused_never_stop():
    """tj-grna9p.182 (F1): presence is the point, so the comment must say what absence means."""
    collect = _comments(DATA, _field_path(DATA, 'SetCollectingRequest', 'collect'))
    assert "an absent value means 'refuse' (the route returns 422), never 'stop'" in collect, collect
    assert 'An empty or forgotten body must not read as collect=false' in collect, collect


# ---------------------------------------------------------------------------------------------------
# THE GENERATED MODULES, AND THE JSON THE UI RECEIVES

_PROBE = """
import json, sys

from google.protobuf import json_format
from trader_joe.proto.ui.v1 import data_pb2

unknown = data_pb2.DatasetSummary(id='d-1')
stopped = data_pb2.DatasetSummary(id='d-2', collecting=False, state=data_pb2.DATASET_STATE_RETIRED)
running = data_pb2.DatasetSummary(id='d-3', collecting=True)
override = data_pb2.OperatorOverride(reason='owner asked', confirmed=True)
stop = data_pb2.SetCollectingRequest(collect=False, override=override)
start = data_pb2.SetCollectingRequest(collect=True)
unset = data_pb2.SetCollectingRequest(override=override)
parsed_stop = json_format.Parse('{"collect": false}', data_pb2.SetCollectingRequest())
parsed_empty = json_format.Parse('{}', data_pb2.SetCollectingRequest())


def round_trip(message):
    return json_format.Parse(json_format.MessageToJson(message), type(message)()) == message


every = (unknown, stopped, running, stop, start, unset)
json.dump(
    {
        'files_from': [data_pb2.__file__],
        'unknown': json_format.MessageToDict(unknown),
        'unknown_has': unknown.HasField('collecting'),
        'stopped': json_format.MessageToDict(stopped),
        'stopped_has': stopped.HasField('collecting'),
        'running': json_format.MessageToDict(running),
        'stop': json_format.MessageToDict(stop),
        'start': json_format.MessageToDict(start),
        'unset': json_format.MessageToDict(unset),
        'collect_has': {
            'stop': stop.HasField('collect'),
            'start': start.HasField('collect'),
            'unset': unset.HasField('collect'),
            'parsed_stop': parsed_stop.HasField('collect'),
            'parsed_empty': parsed_empty.HasField('collect'),
        },
        'collect_presence': data_pb2.SetCollectingRequest.DESCRIPTOR.fields_by_name['collect'].has_presence,
        'parsed_stop': parsed_stop.collect,
        'numbers': {
            'collecting': data_pb2.DatasetSummary.DESCRIPTOR.fields_by_name['collecting'].number,
            'collect': data_pb2.SetCollectingRequest.DESCRIPTOR.fields_by_name['collect'].number,
            'override': data_pb2.SetCollectingRequest.DESCRIPTOR.fields_by_name['override'].number,
        },
        'round_trips': [round_trip(m) for m in every],
    },
    sys.stdout,
)
"""


@cache
def _probe() -> dict:
    """Import the collect messages from gen/ alone and render them as canonical JSON."""
    assert (GENERATED_ROOT / 'trader_joe' / 'proto' / 'ui' / 'v1').is_dir(), (
        f'{GENERATED_ROOT} holds no ui/v1 package: make proto did not generate it'
    )
    env = {**os.environ, 'PYTHONPATH': str(GENERATED_ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(
            [sys.executable, '-P', '-c', _PROBE],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert result.returncode == 0, _ran(result)
    return json.loads(result.stdout)


def test_the_collect_messages_generate_and_import_from_gen_alone():
    seen = _probe()
    assert all(Path(path).is_relative_to(GENERATED_ROOT) for path in seen['files_from']), seen['files_from']
    assert all(seen['round_trips']), 'a collect message does not parse back from its canonical JSON'
    assert seen['numbers'] == {'collecting': 23, 'collect': 1, 'override': 2}


def test_collecting_has_presence_so_false_is_written_and_unset_is_absent():
    """`optional`: a false collecting reaches the UI as false, and an unknown one is absent, not false."""
    seen = _probe()
    assert seen['unknown'] == {'id': 'd-1'}
    assert seen['unknown_has'] is False
    assert seen['stopped'] == {'id': 'd-2', 'state': 'DATASET_STATE_RETIRED', 'collecting': False}
    assert seen['stopped_has'] is True
    assert seen['running'] == {'id': 'd-3', 'collecting': True}


def test_set_collecting_renders_as_the_canonical_json_the_route_parses():
    """Collect has presence (tj-grna9p.182): an explicit false is WRITTEN, and only an unset collect is absent.

    Before F1 an explicit false was the proto3 default and so dropped, which made a stop and an empty body the
    same JSON; the route could not tell them apart. Pinned both ways, for false and for true.
    """
    seen = _probe()
    override = {'reason': 'owner asked', 'confirmed': True}
    assert seen['stop'] == {'collect': False, 'override': override}
    assert seen['start'] == {'collect': True}
    assert seen['unset'] == {'override': override}
    assert 'collect' not in seen['unset']
    assert seen['parsed_stop'] is False


def test_set_collecting_collect_has_presence_in_the_generated_python():
    """HasField semantics: set false and set true are present; unset and a parsed '{}' are not."""
    seen = _probe()
    assert seen['collect_presence'] is True
    assert seen['collect_has'] == {
        'stop': True,
        'start': True,
        'unset': False,
        'parsed_stop': True,
        'parsed_empty': False,
    }
