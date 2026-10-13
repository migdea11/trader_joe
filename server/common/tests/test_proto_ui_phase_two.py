"""ui/v1, phase 2: runs, the request body, the request log, events and stats (tj-grna9p.70, validated by .78).

The design is tj-grna9p.70's architect re-baseline of 2026-10-11, which wins over the bead body. Its
ACCEPTANCE for this bead, each item pinned below:

* every new message compiles, imports from gen/ alone and renders the canonical JSON of decision
  tj-grna9p.4 section 2 (as test_proto_ui_vocabulary.py does for phase 1);
* phase 1 numbers and names are unchanged -- the change is ADDITIVE ONLY, since ui/v1 is under buf
  breaking (FILE); pinned here by the full phase 1 numbering as it merged, so a renumbering is red in
  the unit suite and not only in the buf step;
* DatasetFacets.running (6) and DatasetSummary.last_run (20) are present;
* RequestState carries ADMITTED and REFUSED, documented as unused until the planner;
* there is no SettingsChanged, and no attempts, next_attempt_at, shortfall or fits anywhere;
* half-open is documented on every range;
* no credential-like names.

The generated package is reached in a subprocess with only gen/proto/python on PYTHONPATH, for the same
reason as the phase 1 file: TID251 keeps trader_joe.proto out of everything but common/rpc.
"""

import json
import os
import re
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
SHELL = f'{UI_DIR}/shell.proto'
OPS = f'{UI_DIR}/ops.proto'
EVENTS = f'{UI_DIR}/events.proto'
STATS = f'{UI_DIR}/stats.proto'
UI_FILES = (DATA, SHELL, OPS, EVENTS, STATS)
RANGED_FILES = (DATA, OPS, STATS)
FIELD = descriptor_pb2.FieldDescriptorProto
MESSAGE_TYPE_PATH, FIELD_PATH, ENUM_TYPE_PATH, ENUM_VALUE_PATH = 4, 2, 5, 2
# The same two as test_proto_ui_vocabulary.py, restated rather than imported from a test module.
HALF_OPEN = '[start, end)'
CREDENTIAL = re.compile(r'secret|token|password|passwd|credential|api_?key|private_?key|bearer', re.IGNORECASE)


def _ran(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


# ---------------------------------------------------------------------------------------------------
# THE GENERATED MODULES, AND THE JSON THE UI RECEIVES

_PROBE = """
import json, sys

from google.protobuf import json_format
from trader_joe.proto.market.v1 import enums_pb2
from trader_joe.proto.ui.v1 import data_pb2, events_pb2, ops_pb2, stats_pb2

run = data_pb2.IngestRun(
    id='9a6f8a52-0000-4000-8000-000000000001',
    label='VFV 1day',
    trigger=data_pb2.RUN_TRIGGER_REQUEST,
    state=data_pb2.INGEST_RUN_STATE_FAILED,
    bars_written=2**40,
    error=data_pb2.IngestRunError(domain='trader-joe', reason='PEER_UNAVAILABLE', message='ingest is down'),
    quota_key='alpaca-a',
)
run.start.FromJsonString('2026-01-02T00:00:00Z')
run.started_at.FromJsonString('2026-01-05T14:30:00Z')
summary = data_pb2.DatasetSummary(id='1b4e28ba-2fa1-11d2-883f-0016d3cca427', last_run=run)
event = events_pb2.UiEvent(ingest_run_changed=events_pb2.IngestRunChanged(run=run))
event.server_time.FromJsonString('2026-01-05T14:30:01Z')
changed = events_pb2.UiEvent(
    dataset_changed=events_pb2.DatasetChanged(id=summary.id, change=events_pb2.DATASET_CHANGE_DELETED)
)
reaper = events_pb2.UiEvent(reaper_pending_changed=events_pb2.ReaperPendingChanged())
entry = ops_pb2.RequestLogEntry(
    id='r-1',
    state=ops_pb2.REQUEST_STATE_QUEUED,
    actor_kind=ops_pb2.ACTOR_KIND_SCHEDULER,
    queue_position=3,
    quote=ops_pb2.QuoteSnapshot(
        estimated_vendor_calls=4, scopes=[ops_pb2.ScopeCapacity(quota_key='alpaca-a', cap=200, remaining=150)]
    ),
    runs=[run],
)
duration = stats_pb2.RunDurationEntry(day='2026-01-05', runs=7)
duration.p50.FromJsonString('1.500s')
duration.p95.FromJsonString('12s')
facets = data_pb2.DatasetFacets(all=3, running=2)
usage = ops_pb2.QuotaScopeUsage(quota_key='alpaca-a', calls_in_window=0)
usage_capped = ops_pb2.QuotaScopeUsage(quota_key='alpaca-a', cap=0)


def round_trip(message):
    return json_format.Parse(json_format.MessageToJson(message), type(message)()) == message


json.dump(
    {
        'files': sorted(module.DESCRIPTOR.name for module in (data_pb2, events_pb2, ops_pb2, stats_pb2)),
        'files_from': sorted(module.__file__ for module in (data_pb2, events_pb2, ops_pb2, stats_pb2)),
        'summary': json_format.MessageToDict(summary),
        'event': json_format.MessageToDict(event),
        'changed': json_format.MessageToDict(changed),
        'reaper': json_format.MessageToDict(reaper),
        'entry': json_format.MessageToDict(entry),
        'duration': json_format.MessageToDict(duration),
        'facets': json_format.MessageToDict(facets),
        'usage': json_format.MessageToDict(usage),
        'usage_capped': json_format.MessageToDict(usage_capped),
        'round_trips': [round_trip(m) for m in (summary, event, changed, reaper, entry, duration, facets)],
    },
    sys.stdout,
)
"""


@cache
def _probe() -> dict:
    """Import the phase 2 ui/v1 modules from gen/ alone and render them as canonical JSON."""
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


def test_the_phase_two_files_generate_and_import_from_gen_alone():
    """ops, events and stats come from gen/, under their canonical names, beside data.proto."""
    seen = _probe()
    assert seen['files'] == sorted([DATA, EVENTS, OPS, STATS])
    assert all(Path(path).is_relative_to(GENERATED_ROOT) for path in seen['files_from']), seen['files_from']
    assert all(seen['round_trips']), 'a phase 2 message does not parse back from its canonical JSON'


def test_an_ingest_run_event_renders_as_the_canonical_json_the_ui_reads():
    """The frame the events socket sends (tj-grna9p.73): int64 as a string, enum NAMES, RFC 3339 'Z'.

    bars_written is past 2**32, so an int32 would show here. The oneof arm renders under its own
    lowerCamelCase key, which is how the client dispatches.
    """
    run = {
        'id': '9a6f8a52-0000-4000-8000-000000000001',
        'label': 'VFV 1day',
        'trigger': 'RUN_TRIGGER_REQUEST',
        'start': '2026-01-02T00:00:00Z',
        'state': 'INGEST_RUN_STATE_FAILED',
        'barsWritten': '1099511627776',
        'error': {'domain': 'trader-joe', 'reason': 'PEER_UNAVAILABLE', 'message': 'ingest is down'},
        'startedAt': '2026-01-05T14:30:00Z',
        'quotaKey': 'alpaca-a',
    }
    seen = _probe()
    assert seen['event'] == {'serverTime': '2026-01-05T14:30:01Z', 'ingestRunChanged': {'run': run}}
    assert seen['summary'] == {'id': '1b4e28ba-2fa1-11d2-883f-0016d3cca427', 'lastRun': run}
    assert seen['changed'] == {
        'datasetChanged': {'id': '1b4e28ba-2fa1-11d2-883f-0016d3cca427', 'change': 'DATASET_CHANGE_DELETED'}
    }
    assert seen['reaper'] == {'reaperPendingChanged': {}}, 'an empty arm must still say which arm it is'


def test_the_request_log_stats_and_facets_render_as_canonical_json():
    """RequestLogEntry with its quote and inline runs, a Duration as seconds text, facets.running."""
    seen = _probe()
    assert seen['entry']['state'] == 'REQUEST_STATE_QUEUED'
    assert seen['entry']['actorKind'] == 'ACTOR_KIND_SCHEDULER'
    assert seen['entry']['queuePosition'] == 3
    assert seen['entry']['quote'] == {
        'estimatedVendorCalls': '4',
        'scopes': [{'quotaKey': 'alpaca-a', 'cap': '200', 'remaining': '150'}],
    }
    assert [run['id'] for run in seen['entry']['runs']] == ['9a6f8a52-0000-4000-8000-000000000001']
    assert seen['duration'] == {'day': '2026-01-05', 'p50': '1.500s', 'p95': '12s', 'runs': '7'}
    assert seen['facets'] == {'all': '3', 'running': '2'}


def test_a_quota_scope_cap_has_presence_so_zero_is_not_unknown():
    """QuotaScopeUsage.cap is `optional`: "no snapshot yet" (absent) must not read as a cap of zero."""
    seen = _probe()
    assert 'cap' not in seen['usage']
    assert seen['usage_capped']['cap'] == '0'


# ---------------------------------------------------------------------------------------------------
# ADDITIVE ONLY: PHASE 1 AS IT MERGED

# Every phase 1 message of data.proto with its full (name, number) list as merged on main (20756e3).
# A phase 2 field may only be appended; this pins that nothing before it moved or was renamed.
PHASE_ONE_NUMBERING = {
    'Freshness': [('status', 1), ('expected_last_bar', 2), ('gap_count', 3), ('as_of', 4)],
    'DatasetSibling': [('id', 1), ('granularity', 2)],
    'DatasetSummary': [
        ('id', 1),
        ('asset_symbol', 2),
        ('asset_type', 3),
        ('data_type', 4),
        ('source', 5),
        ('feed', 6),
        ('granularity', 7),
        ('start', 8),
        ('end', 9),
        ('update_type', 10),
        ('expiry_type', 11),
        ('expiry', 12),
        ('owner', 13),
        ('state', 14),
        ('first_bar', 15),
        ('last_bar', 16),
        ('bar_count', 17),
        ('freshness', 18),
        ('siblings', 19),
    ],
    'DatasetPage': [('items', 1), ('next_cursor', 2)],
    'SourceFacet': [('source', 1), ('count', 2)],
    'UpdateTypeFacet': [('update_type', 1), ('count', 2)],
    'StatusFacet': [('status', 1), ('count', 2)],
    'DatasetFacets': [('all', 1), ('needs_attention', 2), ('sources', 3), ('update_types', 4), ('statuses', 5)],
    'BarPage': [('bars', 1), ('next_cursor', 2)],
}

# The phase 2 appendix to phase 1 messages, exactly: the re-baseline's "next free numbers".
PHASE_TWO_APPENDED = {
    'DatasetSummary': [('last_run', 20, '.trader_joe.proto.ui.v1.IngestRun', FIELD.TYPE_MESSAGE)],
    'DatasetFacets': [('running', 6, '', FIELD.TYPE_INT64)],
}


@pytest.mark.parametrize('message', sorted(PHASE_ONE_NUMBERING))
def test_phase_one_numbers_and_names_are_unchanged_and_only_appended_to(message: str):
    """The phase 1 prefix is identical; anything after it is exactly the ruled phase 2 addition.

    Args:
        message: A phase 1 message of data.proto.
    """
    fields = list(messages(file_named(DATA))[message].field)
    prefix = PHASE_ONE_NUMBERING[message]

    assert [(field.name, field.number) for field in fields[: len(prefix)]] == prefix
    appended = [(field.name, field.number, field.type_name, field.type) for field in fields[len(prefix) :]]
    assert appended == PHASE_TWO_APPENDED.get(message, [])


# ---------------------------------------------------------------------------------------------------
# THE FIELD LISTS OF THE NEW MESSAGES, AS THE RE-BASELINE NAMES THEM

T, S, I64, I32, B, D = (
    '.google.protobuf.Timestamp',
    'TYPE_STRING',
    'TYPE_INT64',
    'TYPE_INT32',
    'TYPE_BOOL',
    'TYPE_DOUBLE',
)
M, UI = '.trader_joe.proto.market.v1.', '.trader_joe.proto.ui.v1.'


def _shape(field: descriptor_pb2.FieldDescriptorProto) -> tuple:
    kind = field.type_name if field.type in (FIELD.TYPE_MESSAGE, FIELD.TYPE_ENUM) else FIELD.Type.Name(field.type)
    return (field.name, field.number, kind, field.label == FIELD.LABEL_REPEATED)


def _f(name: str, number: int, kind: str, repeated: bool = False) -> tuple:
    return (name, number, kind, repeated)


NEW_MESSAGES = {
    (DATA, 'IngestRunError'): [_f('domain', 1, S), _f('reason', 2, S), _f('message', 3, S)],
    (DATA, 'IngestRun'): [
        _f('id', 1, S),
        _f('dataset_id', 2, S),
        _f('label', 3, S),
        _f('trigger', 4, f'{UI}RunTrigger'),
        _f('actor', 5, S),
        _f('start', 6, T),
        _f('end', 7, T),
        _f('state', 8, f'{UI}IngestRunState'),
        _f('bars_written', 9, I64),
        _f('error', 10, f'{UI}IngestRunError'),
        _f('started_at', 11, T),
        _f('finished_at', 12, T),
        _f('request_id', 13, S),
        _f('quota_key', 14, S),
    ],
    (DATA, 'IngestRunPage'): [_f('items', 1, f'{UI}IngestRun', True), _f('next_cursor', 2, S)],
    (DATA, 'RequestDatasetResult'): [
        _f('symbol', 1, S),
        _f('request_id', 2, S),
        _f('dataset_id', 3, S),
        _f('error', 4, f'{UI}RequestDatasetError'),
    ],
    (DATA, 'RequestEstimateResponse'): [
        _f('estimated_bars', 1, I64),
        _f('estimated_vendor_calls', 2, I64),
        _f('quota_key', 3, S),
        _f('percent_of_window', 4, D),
    ],
    # tj-grna9p.119 (decision tj-eozqhi): the Viewer's chart page index.
    (DATA, 'BarPageStart'): [_f('start', 1, T), _f('offset', 2, I64)],
    (DATA, 'BarPageIndex'): [
        _f('bar_count', 1, I64),
        _f('page_size', 2, I32),
        _f('overlap', 3, I32),
        _f('pages', 4, f'{UI}BarPageStart', True),
    ],
    (OPS, 'OpsHealth'): [
        _f('healthy', 1, I64),
        _f('late', 2, I64),
        _f('failed', 3, I64),
        _f('retired', 4, I64),
        _f('running', 5, I64),
        _f('queue_depth', 6, I64),
        _f('failed_runs_24h', 7, I64),
    ],
    (OPS, 'RunTimeline'): [_f('run_id', 1, S), _f('entries', 2, f'{UI}RunTimelineEntry', True)],
    (OPS, 'RunTimelineEntry'): [_f('at', 1, T), _f('kind', 2, S), _f('message', 3, S)],
    (OPS, 'RetryRequest'): [_f('request_id', 1, S)],
    (EVENTS, 'DatasetChanged'): [_f('id', 1, S), _f('change', 2, f'{UI}DatasetChange')],
    (EVENTS, 'IngestRunChanged'): [_f('run', 1, f'{UI}IngestRun')],
    (EVENTS, 'RequestChanged'): [_f('entry', 1, f'{UI}RequestLogEntry')],
    (EVENTS, 'ReaperPendingChanged'): [],
    (EVENTS, 'UiEvent'): [
        _f('server_time', 1, T),
        _f('dataset_changed', 2, f'{UI}DatasetChanged'),
        _f('ingest_run_changed', 3, f'{UI}IngestRunChanged'),
        _f('request_changed', 4, f'{UI}RequestChanged'),
        _f('reaper_pending_changed', 5, f'{UI}ReaperPendingChanged'),
    ],
    (STATS, 'CoverageStrip'): [
        _f('start', 1, T),
        _f('end', 2, T),
        _f('buckets', 3, f'{UI}CoverageBucket', True),
        _f('gaps', 4, f'{UI}CoverageGap', True),
        _f('gap_count', 5, I32),
        _f('as_of', 6, T),
    ],
    (STATS, 'CoverageBucket'): [
        _f('start', 1, T),
        _f('end', 2, T),
        _f('expected_sessions', 3, I32),
        _f('sessions_with_bars', 4, I32),
    ],
    (STATS, 'CoverageGap'): [_f('start', 1, T), _f('end', 2, T), _f('sessions', 3, I32)],
    (STATS, 'BarsPerDayEntry'): [_f('day', 1, S), _f('schedule', 2, I64), _f('request', 3, I64), _f('retry', 4, I64)],
    (STATS, 'RunDurationEntry'): [
        _f('day', 1, S),
        _f('p50', 2, '.google.protobuf.Duration'),
        _f('p95', 3, '.google.protobuf.Duration'),
        _f('runs', 4, I64),
    ],
    (STATS, 'RunOutcomesEntry'): [_f('day', 1, S), _f('succeeded', 2, I64), _f('empty', 3, I64), _f('failed', 4, I64)],
}


@pytest.mark.parametrize(('file', 'message'), sorted(NEW_MESSAGES), ids=[m for _, m in sorted(NEW_MESSAGES)])
def test_each_new_message_carries_exactly_the_ruled_fields(file: str, message: str):
    """Name, number, type and cardinality, as the full list: these are new, so an extra is a design change.

    Args:
        file: The .proto declaring it.
        message: The message.
    """
    assert [_shape(field) for field in messages(file_named(file))[message].field] == NEW_MESSAGES[(file, message)]


def test_the_ui_event_is_one_oneof_of_exactly_four_arms_beside_the_server_time():
    """tj-grna9p.70: DatasetChanged, IngestRunChanged, RequestChanged, ReaperPendingChanged. No SettingsChanged."""
    event = messages(file_named(EVENTS))['UiEvent']
    arms = [field.name for field in event.field if field.HasField('oneof_index')]
    assert [event.oneof_decl[0].name] == ['event']
    assert arms == ['dataset_changed', 'ingest_run_changed', 'request_changed', 'reaper_pending_changed']


def _enum_values(file: str, enum: str) -> list[str]:
    return [value.name for value in {e.name: e for e in file_named(file).enum_type}[enum].value]


def test_the_phase_two_enums_name_exactly_the_ruled_values():
    """Buf STANDARD: *_UNSPECIFIED zero and the enum-name prefix on every value."""
    assert _enum_values(DATA, 'RunTrigger') == [
        'RUN_TRIGGER_UNSPECIFIED',
        'RUN_TRIGGER_REQUEST',
        'RUN_TRIGGER_SCHEDULE',
        'RUN_TRIGGER_RETRY',
    ]
    assert _enum_values(DATA, 'IngestRunState') == [
        'INGEST_RUN_STATE_UNSPECIFIED',
        'INGEST_RUN_STATE_RUNNING',
        'INGEST_RUN_STATE_SUCCEEDED',
        'INGEST_RUN_STATE_EMPTY',
        'INGEST_RUN_STATE_FAILED',
    ]
    assert _enum_values(OPS, 'ActorKind') == [
        'ACTOR_KIND_UNSPECIFIED',
        'ACTOR_KIND_PERSON_OR_STRATEGY',
        'ACTOR_KIND_SCHEDULER',
    ]
    assert _enum_values(EVENTS, 'DatasetChange') == [
        'DATASET_CHANGE_UNSPECIFIED',
        'DATASET_CHANGE_CREATED',
        'DATASET_CHANGE_UPDATED',
        'DATASET_CHANGE_RETIRED',
        'DATASET_CHANGE_RESUMED',
        'DATASET_CHANGE_DELETED',
    ]


def test_request_state_keeps_admitted_and_refused_and_documents_them_unused():
    """The re-baseline: both defined, both documented as unused until the planner tj-lfd48x."""
    values = _enum_values(OPS, 'RequestState')
    assert values == [
        'REQUEST_STATE_UNSPECIFIED',
        'REQUEST_STATE_QUEUED',
        'REQUEST_STATE_ADMITTED',
        'REQUEST_STATE_IN_FLIGHT',
        'REQUEST_STATE_SUCCEEDED',
        'REQUEST_STATE_EMPTY',
        'REQUEST_STATE_FAILED',
        'REQUEST_STATE_REFUSED',
    ]
    enum_index = [enum.name for enum in _with_source_info(OPS).enum_type].index('RequestState')
    for name in ('REQUEST_STATE_ADMITTED', 'REQUEST_STATE_REFUSED'):
        text = _comments(OPS, [ENUM_TYPE_PATH, enum_index, ENUM_VALUE_PATH, values.index(name)])
        assert 'unused' in text.lower() and 'tj-lfd48x' in text, f'{name}: {text!r}'


# ---------------------------------------------------------------------------------------------------
# WHAT MUST NOT EXIST

FORBIDDEN_NAMES = ('attempts', 'next_attempt_at', 'shortfall', 'fits')


def test_no_settings_event_and_no_planner_field_exists_anywhere_in_ui_v1():
    """NO SettingsChanged (settings live in the browser); NO attempts, next_attempt_at, shortfall or fits."""
    all_messages = {name for file in UI_FILES for name in messages(file_named(file))}
    assert not [name for name in all_messages if 'settings' in name.lower()], sorted(all_messages)

    offenders = [
        f'{file}:{message.name}.{field.name}'
        for file in UI_FILES
        for message in file_named(file).message_type
        for field in message.field
        if field.name in FORBIDDEN_NAMES or field.name.endswith('_shortfall')
    ]
    assert not offenders, offenders


def test_no_ui_v1_file_names_credential_material():
    """The phase 1 sweep (DATA and SHELL only) extended to every ui/v1 file, nested messages included."""

    def walk(file):
        stack = list(file_named(file).message_type)
        while stack:
            message = stack.pop()
            stack.extend(message.nested_type)
            yield message

    named = [
        f'{message.name}.{field.name}'
        for file in UI_FILES
        for message in walk(file)
        for field in message.field
        if CREDENTIAL.search(field.name) or CREDENTIAL.search(message.name)
    ]
    assert not named, named


@pytest.mark.parametrize('file', [OPS, EVENTS, STATS])
def test_a_phase_two_file_imports_only_ui_v1_market_v1_and_well_known_types(file: str):
    """The re-baseline: market/v1 and google/protobuf only, never internal/*, and no googleapis (google.type)."""
    for dependency in file_named(file).dependency:
        assert dependency.startswith(('google/protobuf/', 'trader_joe/proto/market/v1/', f'{UI_DIR}/')), dependency
    assert file_named(file).package == 'trader_joe.proto.ui.v1'


# ---------------------------------------------------------------------------------------------------
# HALF-OPEN, ON EVERY RANGE


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
    found = [location for location in _with_source_info(file).source_code_info.location if list(location.path) == path]
    assert found, f'no source location at {path} in {file}'
    location = found[0]
    return ' '.join([*location.leading_detached_comments, location.leading_comments, location.trailing_comments])


def _ranges(file: str) -> list[tuple[str, int, int]]:
    # Every message with both a `start` and an `end` Timestamp: (name, message index, start field index).
    found = []
    for index, message in enumerate(_with_source_info(file).message_type):
        names = [field.name for field in message.field]
        if 'start' in names and 'end' in names:
            found.append((message.name, index, names.index('start')))
    return found


RANGES = [(file, name, index, start) for file in RANGED_FILES for name, index, start in _ranges(file)]


def test_the_range_sweep_finds_every_phase_two_range():
    """Guard the guard: the sweep below reaches each new message with a range, so 'none failed' means something."""
    found = {name for _, name, _, _ in RANGES}
    assert {
        'IngestRun',
        'RequestDatasetRequest',
        'RequestEstimateRequest',
        'RequestLogEntry',
        'CoverageStrip',
        'CoverageBucket',
        'CoverageGap',
    } <= found, sorted(found)


@pytest.mark.parametrize(('file', 'name', 'index', 'start'), RANGES, ids=[f'{n}' for _, n, _, _ in RANGES])
def test_every_range_is_documented_half_open_on_its_start_field(file: str, name: str, index: int, start: int):
    """Every [start, end) pair says so, on the field or on its message (tj-vhboky.1 addendum; tj-86g751).

    Args:
        file: The .proto declaring the message.
        name: The message.
        index: Its index in the file.
        start: The index of its start field.
    """
    text = _comments(file, [MESSAGE_TYPE_PATH, index, FIELD_PATH, start]) + _comments(file, [MESSAGE_TYPE_PATH, index])
    assert 'half-open' in text.lower() and HALF_OPEN in text, f'{name}: {text!r}'


@pytest.mark.parametrize('file', [OPS, STATS])
def test_each_new_file_declares_every_range_half_open_in_its_header(file: str):
    """The file header states the rule, as data.proto's does."""
    header = _comments(file, [12])
    assert 'half-open' in header.lower() and HALF_OPEN in header, header
